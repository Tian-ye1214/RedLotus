"""Version/publication fault checks; live memory acceptance is separate."""

from types import SimpleNamespace

import pytest


async def test_memory_control_wait_uses_config_without_cancelling_production(tmp_path):
    import asyncio
    import json
    from concurrent.futures import Future

    from redlotus.core.system import AgentSystem
    from redlotus.memory.service import MemoryService

    (tmp_path / "config.json").write_text(json.dumps({
        "memory_perception": {"quiescence_wait_timeout_seconds": .01},
    }), encoding="utf-8")
    pending = Future()
    memory, system = object.__new__(MemoryService), object.__new__(AgentSystem)
    memory._background = SimpleNamespace(_future=pending)
    system._current_turn, system._memory = None, memory
    try:
        async with asyncio.timeout(.2):
            assert not await system.wait_for_memory_quiescent()
        assert not pending.cancelled()
    finally:
        pending.set_result(None)
        await asyncio.sleep(0)


async def test_non_owner_turns_count_without_reading_or_producing_personal_memory(tmp_path):
    import json

    from redlotus.memory.records import ObservationStore
    from redlotus.memory.service import MemoryService
    from redlotus.runtime.resources import WorkspaceContext
    from redlotus.sessions.storage import SessionFile

    (tmp_path / "config.json").write_text(json.dumps({
        "memory_perception": {"window_turns": 20, "overlap_turns": 3},
        "storage": {"project_dir": ".redlotus"},
    }), encoding="utf-8")
    workspace = WorkspaceContext.from_path(tmp_path)
    service = object.__new__(MemoryService)
    service.owner_memory_allowed, service.current = False, None
    service.session = SessionFile.create(tmp_path / "sessions", workspace.project_id, session_id="channel")
    service.observations = ObservationStore(workspace)
    service.observations.bind(service.session)
    # No long_term, store or factory exists: non-owner input must never use them.
    event = await service.begin_turn("channel", "input-id", "Public channel text")
    await service.finish_turn(event, status="success", user_inputs=["Public channel text"], evidence_paths=[])
    assert service.session.completed_turns == 1
    assert service.session.turn(event.id)["turn_id"] == "input-id"
    assert not service.session.pending_jobs()


@pytest.fixture
def publication(tmp_path, monkeypatch):
    from redlotus.memory import records
    from redlotus.memory import service as module
    from redlotus.memory.perception import MemoryJob
    from redlotus.sessions.storage import SessionFile

    (tmp_path / "config.json").write_text('{"storage":{"file_lock_timeout_seconds":0}}', encoding="utf-8")
    memory = records.LongTermMemory(tmp_path / "core-memory")
    original = memory.read()
    row = records.MemoryRecord(id="A", project_id="isolated", scope="global", kind="requested",
                               projection="profile", goal="Preference", content="Fixture tea", origin="explicit",
                               last_change_id="publication:0")
    event = records.ObservedTurn(id="event", project_id="isolated", session_id="fixture", turn_id="turn", origin="migration")
    draft = records.MemoryDraft(target_id="A", scope="global", kind="requested", projection="profile",
                                goal=row.goal, content=row.content, source_turn_ids=[event.id], search_id="search")
    job = MemoryJob(id="publication", request="Remember the fixture preference", scope="global", events=[event],
                    core_snapshot=original, result=records.PerceptionResult(records=[draft], reason="fixture", request_authorized=True),
                    searches=[dict(id="search", scope="global", revision="fixture", retrieval_error="")])
    formal, writes, model_calls = {}, [], []

    def save(rows):
        writes.append([record.id for record in rows])
        formal.update((record.id, record.model_copy(deep=True)) for record in rows)

    async def unexpected_production(*args):
        model_calls.append(True)
        raise AssertionError("A projection retry must not produce another model candidate")

    service = object.__new__(module.MemoryService)
    service.long_term, service.last_error, service.current = memory, "", None
    service.session = SessionFile.create(tmp_path / "sessions", "isolated", session_id="fixture")
    service.observations = SimpleNamespace(read=lambda identities: [event])
    service.store = SimpleNamespace(get=lambda identity: formal[identity], save=save, revision=lambda scope: "fixture",
                                    materialize=lambda *args: row)
    monkeypatch.setattr(service, "_route", lambda: "fixture")
    monkeypatch.setattr(module.logger, "error", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "produce_job", unexpected_production)
    return SimpleNamespace(service=service, job=job, row=row, formal=formal, writes=writes,
                           model_calls=model_calls, memory=memory, original=original, records=records)


async def test_database_failure_cannot_publish_core_memory(publication, monkeypatch):
    p = publication

    def failed_save(rows):
        raise OSError("isolated database failure")

    monkeypatch.setattr(p.service.store, "save", failed_save)
    with pytest.raises(OSError, match="database failure"):
        await p.service._apply(p.job)
    assert p.memory.read() == p.original and not p.formal
    assert not p.job.done


@pytest.mark.parametrize("newer", [False, True])
async def test_projection_retry_restores_stage_and_never_replays_model_or_database(publication, monkeypatch, newer):
    import os
    from pathlib import Path

    p = publication
    replace = os.replace

    def failed_projection(source, target):
        if Path(target) == p.memory.path:
            raise OSError("isolated projection failure")
        return replace(source, target)

    monkeypatch.setattr(os, "replace", failed_projection)
    assert not await p.service._produce_and_apply(p.job)
    assert p.formal["A"].version == 1 and p.memory.read() == p.original
    assert p.job.timings.get("records_committed_at") and not p.job.done
    restored = p.service._job(p.job)
    assert restored.timings["records_committed_at"] == p.job.timings["records_committed_at"]
    if newer:
        p.formal["A"] = p.row.model_copy(update={"version": 2, "state": "deleted", "last_change_id": "newer"})
    p.memory.path.write_text(p.original + "\nUser's later note\n", encoding="utf-8")
    monkeypatch.setattr(os, "replace", replace)
    assert await p.service._produce_and_apply(restored)
    assert not p.model_calls and p.writes == [["A"]]
    assert "User's later note" in p.memory.read()
    assert ("Fixture tea" in p.memory.read()) is not newer
    assert restored.records == ([] if newer else ["A"])


async def test_explicit_candidate_cannot_overwrite_later_manual_core_edit(publication):
    p = publication
    previous = p.row.model_copy(update={"content": "Original preference", "last_change_id": "before"})
    original = p.original.replace("## 用户画像\n", "## 用户画像\n\n<!-- memory:A -->\nOriginal preference\n<!-- /memory -->\n")
    p.row.version = 2
    p.job.bases = {"A": previous}
    p.job = p.job.model_copy(update={"core_snapshot": original})
    edited = original.replace("Original preference", "Manual preference")
    p.memory.path.write_text(edited, encoding="utf-8")
    with pytest.raises(ValueError, match="[Cc]ore memory changed"):
        await p.service._apply(p.job)
    assert not p.writes and p.memory.read() == edited


def test_injection_excludes_uncommitted_or_superseded_managed_blocks(publication):
    p = publication
    body = p.original + "\nHandwritten note\n<!-- memory:A version:2 -->\nUncommitted preference\n<!-- /memory -->\n"
    p.memory.path.write_text(body, encoding="utf-8")
    injection = p.memory.get_injection([p.row])
    assert "Uncommitted preference" not in injection and "Handwritten note" in injection
    assert p.memory.read() == body


async def test_candidate_cannot_replace_text_inside_another_managed_record(publication):
    p = publication
    original = p.original.replace("## 用户画像\n", "## 用户画像\n\n<!-- memory:B version:1 -->\nOther record\n<!-- /memory -->\n")
    p.job.core_snapshot = original
    p.job.result.records[0].core_old_text = "Other record"
    p.memory.path.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match="managed record"):
        await p.service._apply(p.job)
    assert not p.formal and p.memory.read() == original


async def test_projection_failure_receipt_reports_formal_commit(publication, monkeypatch):
    import asyncio
    import os
    from pathlib import Path

    p = publication
    replace = os.replace

    def failed_projection(source, target):
        if Path(target) == p.memory.path:
            raise OSError("isolated projection failure")
        return replace(source, target)

    p.service.current, p.service.owner_memory_allowed = p.job.events[0], True
    p.service._explicit = asyncio.Lock()
    p.service._input_source = lambda: ["Remember my tea preference"]
    monkeypatch.setattr(p.service, "_job", lambda job: p.job)
    monkeypatch.setattr(os, "replace", failed_projection)
    receipt = await p.service.remember("Remember my tea preference", scope="global")
    assert "正式记忆已保存" in receipt and "投影尚未完成" in receipt
    assert "未保存为记忆" not in receipt and p.formal["A"].version == 1


async def test_project_only_manual_memory_never_starts_global_production(publication):
    import json

    from pydantic_ai import Tool

    p = publication
    result = json.loads(await p.service.remember("Remember this only in the current project", scope="project"))
    assert result["status"] == "rejected" and not result["records"]
    assert not p.writes and not p.formal and not p.model_calls
    assert p.memory.read() == p.original and not p.service.session.pending_jobs()
    schema = Tool(p.service.remember).function_schema.json_schema
    assert {"request", "scope"} <= set(schema["required"])


@pytest.mark.parametrize("state", ["active", "deleted"])
def test_stale_candidate_cannot_replace_a_newer_record(tmp_path, monkeypatch, state):
    from redlotus.memory.perception import MemoryJob
    from redlotus.memory.records import MemoryDraft, MemoryRecord, ObservedTurn
    from redlotus.memory.store import MemoryStore
    from redlotus.runtime.resources import WorkspaceContext

    (tmp_path / "config.json").write_text(
        '{"storage":{"references_dir":"WorkDatabase/references"}}', encoding="utf-8",
    )
    workspace = WorkspaceContext.from_path(tmp_path)
    base = MemoryRecord(id="A", project_id=workspace.project_id, goal="Original", version=1, last_change_id="initial")
    current = base.model_copy(update={"version": 2, "goal": "Newer", "state": state, "last_change_id": "newer"})
    event = ObservedTurn(id="event", project_id=workspace.project_id, session_id="session", turn_id="turn", status="success")
    job = MemoryJob(id="stale", events=[event], bases={"A": base})
    draft = MemoryDraft(action="update", target_id="A", goal="Old candidate", source_turn_ids=[event.id])
    store = object.__new__(MemoryStore)
    store.workspace = workspace
    monkeypatch.setattr(store, "get", lambda identity: current)
    with pytest.raises(ValueError, match="changed"):
        store.materialize(job, draft, 0, lambda scope: "")
    assert current.version == 2 and current.state == state
    # A retry of the exact committed change is idempotent, even with its old base.
    current.last_change_id = "stale:0"
    assert store.materialize(job, draft, 0, lambda scope: "") is current


@pytest.mark.parametrize("indexed, stamp, expected", [
    (set(), "", "missing"),
    ({"A"}, "old-space", "stale"),
    ({"A"}, "bodynew-space", ""),
])
async def test_empty_vector_result_requires_a_complete_current_index(monkeypatch, indexed, stamp, expected):
    from redlotus.memory.records import MemoryRecord
    from redlotus.memory.store import MemoryStore

    async def retrieve(query):
        return []

    async def keys():
        return indexed

    async def refresh():
        return "model"

    index = SimpleNamespace(
        retrieve=retrieve, indexed_record_ids=keys, refresh_embedding_space=refresh,
        index_key="new-space", last_error="", config={"final_top_k": 5},
    )
    store = object.__new__(MemoryStore)
    store.indexes = {"project": index, "global": index}
    store.retrieval_error = ""
    record = MemoryRecord(id="A", scope="global", project_id="project", goal="Afternoon coffee")
    monkeypatch.setattr(store, "all", lambda scope: [record])
    monkeypatch.setattr(store, "_rows", lambda scope: [{"id": "A", "body_hash": "body", "indexed": stamp}])
    monkeypatch.setattr(store, "rag_unavailable_reason", lambda scope: "")
    assert await store.search("beverage", "global") == []
    assert expected in store.retrieval_error.lower() if expected else not store.retrieval_error


async def test_empty_authoritative_store_proves_absence_without_an_index(monkeypatch):
    from redlotus.memory.store import MemoryStore

    store = object.__new__(MemoryStore)
    monkeypatch.setattr(store, "all", lambda scope: [])
    monkeypatch.setattr(store, "rag_unavailable_reason", lambda scope: "embedding unavailable")
    assert await store.search("beverage", "global") == []
    assert not store.retrieval_error
