"""Offline persistence/scheduling invariants, not evidence of live memory production."""

import json

from redlotus.core.agents import WorkspaceContext
from redlotus.memory.records import MemoryRecord
from memory_helpers import bound_observations
from redlotus.memory.records import LongTermMemory
from memory_helpers import new_memory
from redlotus.memory.service import MemoryJob


async def test_output_budget_failure_does_not_repeat_until_explicit_retry(
    tmp_path, monkeypatch
):
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    memory = new_memory(workspace=WorkspaceContext.from_path(tmp_path))
    attempts = []

    async def produce(job):
        attempts.append(job.id)
        raise UnexpectedModelBehavior(
            "Model token limit (4096) exceeded before any response was generated."
        )

    monkeypatch.setattr("redlotus.memory.service.produce_job", lambda service, job: produce(job))
    event = memory.observations.begin("session", "turn", "store a verified result", [])
    event.status = "success"
    memory.observations.finish(event)
    job = memory._job(MemoryJob(id="budget-failure", events=[event]))
    assert not await memory._execute(job)
    assert not await memory._execute(job)
    assert attempts == ["budget-failure"]
    assert not await memory._execute(job, retry=True)
    assert attempts == ["budget-failure", "budget-failure"]
    await memory.close()


def test_window_boundary_overlap_and_restart_without_partial_flush(tmp_path):
    workspace = WorkspaceContext.from_path(tmp_path)
    store = bound_observations(workspace)
    ids = []
    for index in range(19):
        event = store.begin("session", str(index), str(index), [])
        event.status = "success"
        store.finish(event)
        ids.append(event.id)
    assert store.window() is None
    event = store.begin("session", "19", "19", [])
    event.status = "success"
    store.finish(event)
    ids.append(event.id)
    first = store.window()
    assert first.new_turn_ids == ids and not first.overlap_turn_ids
    store.commit(first)
    for index in range(20, 40):
        event = store.begin("session", str(index), str(index), [])
        event.status = "success"
        store.finish(event)
        ids.append(event.id)
    second = store.window()
    assert second.overlap_turn_ids == ids[17:20] and second.new_turn_ids == ids[20:40]
    store.commit(second)
    assert bound_observations(workspace).window() is None
    event = store.begin("session", "last", "last", [])
    event.status = "cancelled"
    store.finish(event)
    assert store.window() is None
    assert bound_observations(workspace).window() is None


def test_core_has_no_character_cap_and_preserves_manual_preamble(tmp_path):
    core = LongTermMemory(tmp_path)
    body = core.read().replace("# MEMORY", "# MEMORY\n\n手动说明。")
    core.path.write_text(body, encoding="utf-8")
    record = MemoryRecord(
        id="a",
        project_id="project",
        scope="global",
        kind="requested",
        origin="explicit",
        projection="profile",
        goal="画像",
        content="长画像资料" * 2000,
        last_change_id="one",
    )
    core.apply_record(record)
    assert len(core.read()) > 8000 and "手动说明。" in core.read()
    updated = record.model_copy(
        update=dict(content="默认中文", version=2, last_change_id="two")
    )
    core.apply_record(updated, record)
    assert "长画像资料" not in core.read() and core.read().count("默认中文") == 1
    core.apply_record(updated, updated)
    assert core.read().count("默认中文") == 1


async def test_project_text_search_and_repository_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "redlotus.memory.store.missing_rag_api_keys", lambda: ("key",)
    )
    a = new_memory(workspace=WorkspaceContext.from_path(tmp_path / "a"))
    b = new_memory(workspace=WorkspaceContext.from_path(tmp_path / "b"))
    record = MemoryRecord(
        id="one",
        project_id=a.workspace.project_id,
        goal="数据库迁移",
        content="失败后回滚事务",
        last_change_id="one",
    )
    a.store.save([record])
    a.store.save([record])
    assert len(a.store.all("project")) == 1
    assert len(json.loads(await a.reader.search_episodes("迁移失败"))["episodes"]) == 1
    assert json.loads(await b.reader.search_episodes("迁移失败"))["episodes"] == []
    assert (await b.reader.read_episode("one")).startswith("Error:")
    await a.close()
    await b.close()


async def test_non_owner_does_not_produce_or_offer_personal_memory(tmp_path):
    memory = new_memory(
        workspace=WorkspaceContext.from_path(tmp_path), owner_memory_allowed=False
    )
    assert await memory.begin_turn("other", "turn", "hi") is None
    assert memory.worker_tools == [] and memory.session.completed_turns == 0
    assert memory.session.pending_turns(0) == [] and memory.session.pending_jobs() == []
    assert (await memory.remember("remember")).startswith("Error:")
    await memory.close()


def test_legacy_user_document_is_pending_perception(tmp_path):
    (tmp_path / "USER.md").write_text("# USER\n\n项目的历史情况。", encoding="utf-8")
    core = LongTermMemory(tmp_path)
    assert "项目的历史情况。" in core.legacy_content()
    assert (tmp_path / "migration_backup" / "USER.md").is_file()


async def test_existing_core_document_is_preserved_without_startup_production(tmp_path):
    memory = new_memory(workspace=WorkspaceContext.from_path(tmp_path))
    body = "# MEMORY\n\n## 用户偏好\n\n默认中文。\n\n## 项目简况\n\n项目使用独立数据库。\n"
    memory.long_term.path.parent.mkdir(parents=True, exist_ok=True)
    memory.long_term.path.write_text(body, encoding="utf-8")
    await memory.process_pending()
    assert memory.long_term.read() == body
    assert memory.session.pending_jobs() == []
    assert not list(memory.workspace.root.rglob("*.jsonl"))
    await memory.close()


async def test_clear_prevents_old_production_and_request_replay(tmp_path, monkeypatch):
    from redlotus.memory.records import MemoryDraft
    from redlotus.memory.records import PerceptionResult

    monkeypatch.setattr(
        "redlotus.memory.store.missing_rag_api_keys", lambda: ("key",)
    )
    memory = new_memory(workspace=WorkspaceContext.from_path(tmp_path / "a"))
    old = await memory.begin_turn("session", "old", "记住旧称呼")
    draft = MemoryDraft(
        scope="global",
        kind="requested",
        projection="profile",
        goal="称呼",
        content="旧称呼",
        source_turn_ids=[old.id],
    )
    produced = PerceptionResult(
        records=[draft], reason="auxiliary persistence check", request_authorized=True
    )
    assert await memory._execute(
        MemoryJob(id="before-clear", events=[old], request="记住", result=produced)
    )
    await memory.clear_long_term()
    assert (
        await memory._apply(
            MemoryJob(id="before-clear", events=[old], request="记住", result=produced)
        )
        is None
    )
    assert (
        await memory._apply(
            MemoryJob(
                id="pending-request", events=[old], request="记住", result=produced
            )
        )
        is None
    )
    assert memory.store.all("global") == [] and "旧称呼" not in memory.long_term.read()
    await memory.finish_turn(
        old, status="success", user_inputs=old.user_inputs, evidence_paths=[]
    )
    fresh = await memory.begin_turn("session", "new", "记住新称呼")
    produced.records[0] = draft.model_copy(
        update=dict(source_turn_ids=[fresh.id], content="新称呼")
    )
    assert await memory._execute(
        MemoryJob(id="after-clear", events=[fresh], request="记住", result=produced)
    )
    assert [record.content for record in memory.store.all("global")] == ["新称呼"]
    assert "新称呼" in memory.long_term.read()
    await memory.close()


async def test_explicit_correction_order_uses_user_event_time_not_commit_time(tmp_path):
    from redlotus.memory.records import MemoryDraft
    from redlotus.memory.records import ObservedTurn

    memory = new_memory(workspace=WorkspaceContext.from_path(tmp_path))
    existing = MemoryRecord(
        id="preference",
        project_id=memory.workspace.project_id,
        scope="global",
        kind="requested",
        origin="explicit",
        goal="preference",
        source_updated_at="2026-09-13T12:00:00Z",
        updated_at="2026-09-13T14:00:00Z",
    )
    memory.store.save([existing])
    event = ObservedTurn(
        id="new",
        project_id=memory.workspace.project_id,
        session_id="s",
        turn_id="new",
        created_at="2026-09-13T13:00:00Z",
    )
    job = MemoryJob(
        id="correction",
        events=[event],
        request="correct preference",
        created_at="2026-09-13T13:05:00Z",
        bases={existing.id: existing},
    )
    draft = MemoryDraft(
        action="update",
        target_id=existing.id,
        scope="global",
        goal="new preference",
        source_turn_ids=[event.id],
    )
    record = memory.store.materialize(job, draft, 0, memory._cleared_at)
    assert record and record.source_updated_at == event.created_at
    memory.store.save(
        [
            existing.model_copy(
                update={
                    "source_updated_at": event.created_at,
                    "request_created_at": "2026-09-13T13:20:00Z",
                    "version": 2,
                    "last_change_id": "newer-request",
                }
            )
        ]
    )
    assert memory.store.materialize(job, draft, 0, memory._cleared_at) is None
    job.created_at = "2026-09-13T13:30:00Z"
    assert memory.store.materialize(job, draft, 0, memory._cleared_at) is not None
    event.created_at = "2026-09-13T11:00:00Z"
    assert memory.store.materialize(job, draft, 0, memory._cleared_at) is None
    await memory.close()


async def test_same_pending_job_reuses_one_production_result(tmp_path, monkeypatch):
    import asyncio
    from redlotus.memory.records import ObservedTurn
    from redlotus.memory.records import PerceptionResult

    memory = new_memory(workspace=WorkspaceContext.from_path(tmp_path))
    event = ObservedTurn(
        id="event",
        project_id=memory.workspace.project_id,
        session_id="s",
        turn_id="one",
    )
    memory.observations.save(event)
    job = MemoryJob(id="same-job", events=[event], request="remember")
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def produce(current):
        calls.append(current.id)
        started.set()
        await release.wait()
        current.result = PerceptionResult(records=[], reason="not authorized")
        current.done = True
        memory._save_job(current)

    monkeypatch.setattr("redlotus.memory.service.produce_job", lambda service, job: produce(job))
    first = asyncio.create_task(memory._execute(job))
    await started.wait()
    copy = job.model_copy(deep=True)
    second = asyncio.create_task(memory._execute(copy))
    await asyncio.sleep(0.01)
    release.set()
    assert await first and await second
    assert calls == [job.id] and copy.done and copy.result is not None
    await memory.close()


async def test_restart_preserves_paused_job_until_explicit_retry(tmp_path, monkeypatch):
    from redlotus.memory.records import PerceptionResult
    workspace = WorkspaceContext.from_path(tmp_path)
    memory = new_memory(workspace=workspace)
    event = memory.observations.begin("session", "one", "remember", [])
    memory.observations.finish(event)
    memory._save_job(MemoryJob(id="pending", request="remember", events=[event]))
    memory._processor({"blocked_route": memory._route(), "error": "service unavailable"})
    await memory.close()
    restarted = new_memory(workspace=workspace)
    calls = []

    async def produce(service, job):
        calls.append(job.id)
        job.result = PerceptionResult(records=[], reason="recovered")
        job.done = True
        service._save_job(job)

    monkeypatch.setattr("redlotus.memory.service.produce_job", produce)
    await restarted.process_pending()
    assert not calls and restarted._paused()
    await restarted.process_pending(recover=True)
    await restarted.process_pending()
    assert calls == ["pending"] and not restarted._paused()
    await restarted.close()


async def test_index_retry_reuses_saved_current_session_production(tmp_path, monkeypatch):
    from redlotus.memory.records import PerceptionResult
    memory = new_memory(workspace=WorkspaceContext.from_path(tmp_path))
    event = memory.observations.begin("session", "one", "A completed task", [])
    memory.observations.finish(event)
    memory._save_job(MemoryJob(id="saved-result", events=[event], done=True,
        result=PerceptionResult(records=[], reason="Already produced")))

    async def unexpected_production(*args):
        raise AssertionError("Index retry must not produce again")

    monkeypatch.setattr("redlotus.memory.service.produce_job", unexpected_production)
    await memory.process_pending()
    await memory.process_pending()
    assert memory.session.job("saved-result")["indexed"]
    assert memory.session.pending_jobs() == []
    assert memory.observations.cursor() == 0
    await memory.close()
