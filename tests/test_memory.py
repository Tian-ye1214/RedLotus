"""Offline persistence/scheduling invariants, not evidence of live memory production."""

import json

from redlotus.runtime.context import WorkspaceContext
from redlotus.tools.memory.models import MemoryRecord
from redlotus.tools.memory.observations import ObservationStore
from redlotus.tools.memory.ltm import LongTermMemory
from redlotus.agent_core.memory_service import MemoryService, MemoryJob


async def test_output_budget_failure_does_not_repeat_until_explicit_retry(
    tmp_path, monkeypatch
):
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    memory = MemoryService(workspace=WorkspaceContext.from_path(tmp_path))
    attempts = []

    async def produce(job):
        attempts.append(job.id)
        raise UnexpectedModelBehavior(
            "Model token limit (4096) exceeded before any response was generated."
        )

    monkeypatch.setattr(memory, "_produce", produce)
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


def test_window_boundary_overlap_flush_and_restart(tmp_path):
    workspace = WorkspaceContext.from_path(tmp_path)
    store = ObservationStore(workspace)
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
    assert ObservationStore(workspace).window(flush=True) is None
    event = store.begin("session", "last", "last", [])
    event.status = "cancelled"
    store.finish(event)
    assert store.window() is None
    assert store.window(flush=True).new_turn_ids == [event.id]


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
        "redlotus.tools.memory.store.missing_rag_api_keys", lambda: ("key",)
    )
    a = MemoryService(workspace=WorkspaceContext.from_path(tmp_path / "a"))
    b = MemoryService(workspace=WorkspaceContext.from_path(tmp_path / "b"))
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
    assert len(json.loads(await a.search_episodes("迁移失败"))["episodes"]) == 1
    assert json.loads(await b.search_episodes("迁移失败"))["episodes"] == []
    assert (await b.read_episode("one")).startswith("Error:")
    await a.close()
    await b.close()


async def test_non_owner_does_not_produce_or_offer_personal_memory(tmp_path):
    memory = MemoryService(
        workspace=WorkspaceContext.from_path(tmp_path), owner_memory_allowed=False
    )
    assert await memory.begin_turn("other", "turn", "hi") is None
    assert memory.worker_tools == [] and not memory.observations.turns.exists()
    assert (await memory.remember("remember")).startswith("Error:")
    await memory.close()


def test_legacy_user_document_is_pending_perception(tmp_path):
    (tmp_path / "USER.md").write_text("# USER\n\n项目的历史情况。", encoding="utf-8")
    core = LongTermMemory(tmp_path)
    assert "项目的历史情况。" in core.legacy_content()
    assert (tmp_path / "migration_backup" / "USER.md").is_file()


async def test_core_migration_reuses_production_after_partial_commit(
    tmp_path, monkeypatch
):
    from redlotus.tools.memory.models import MemoryDraft, PerceptionResult

    monkeypatch.setattr(
        "redlotus.tools.memory.store.missing_rag_api_keys", lambda: ("key",)
    )
    memory = MemoryService(workspace=WorkspaceContext.from_path(tmp_path / "project"))
    memory.long_term.path.parent.mkdir(parents=True, exist_ok=True)
    memory.long_term.path.write_text(
        "# MEMORY\n\n## 用户偏好\n\n默认中文。\n\n项目使用独立数据库。\n\n## 经验\n",
        encoding="utf-8",
    )
    calls = []

    class AuxiliaryProducer:
        async def produce(self, job_id, payload, references, **kwargs):
            calls.append(job_id)
            source = payload["new_turn_ids"]
            return PerceptionResult(
                request_authorized=True,
                reason="auxiliary persistence check",
                records=[
                    MemoryDraft(
                        scope="global",
                        kind="requested",
                        goal="项目资料",
                        content="项目使用独立数据库。",
                        core_old_text="项目使用独立数据库。",
                        source_turn_ids=source,
                    ),
                    MemoryDraft(
                        scope="global",
                        kind="requested",
                        goal="回复语言",
                        content="默认中文。",
                        core_old_text="默认中文。",
                        projection="profile",
                        source_turn_ids=source,
                    ),
                ],
            )

    memory.perception = AuxiliaryProducer()
    original = memory.long_term.apply_record
    interrupted = False

    def interrupt_once(record, *args, **kwargs):
        nonlocal interrupted
        if record.projection == "profile" and not interrupted:
            interrupted = True
            raise OSError("auxiliary interruption before core write")
        return original(record, *args, **kwargs)

    monkeypatch.setattr(memory.long_term, "apply_record", interrupt_once)
    await memory._migrate_core()
    assert not (
        memory.long_term.directory / "migration_backup/core_records_v3.json"
    ).exists()
    await memory._migrate_core()
    assert len(calls) == 1 and len(memory.store.all("global")) == 2
    assert memory.long_term.read().count("默认中文。") == 1
    assert "项目使用独立数据库。" not in memory.long_term.read()
    await memory.close()


async def test_clear_prevents_old_production_and_request_replay(tmp_path, monkeypatch):
    from redlotus.tools.memory.models import MemoryDraft, PerceptionResult

    monkeypatch.setattr(
        "redlotus.tools.memory.store.missing_rag_api_keys", lambda: ("key",)
    )
    memory = MemoryService(workspace=WorkspaceContext.from_path(tmp_path / "a"))
    old = await memory.begin_turn("s", "old", "记住旧称呼")
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
    fresh = await memory.begin_turn("s", "new", "记住新称呼")
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
    from redlotus.tools.memory.models import MemoryDraft, ObservedTurn

    memory = MemoryService(workspace=WorkspaceContext.from_path(tmp_path))
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
    record = memory._record(job, draft, 0)
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
    assert memory._record(job, draft, 0) is None
    job.created_at = "2026-09-13T13:30:00Z"
    assert memory._record(job, draft, 0) is not None
    event.created_at = "2026-09-13T11:00:00Z"
    assert memory._record(job, draft, 0) is None
    await memory.close()


async def test_same_pending_job_reuses_one_production_result(tmp_path, monkeypatch):
    import asyncio
    from redlotus.tools.memory.models import ObservedTurn, PerceptionResult

    memory = MemoryService(workspace=WorkspaceContext.from_path(tmp_path))
    event = ObservedTurn(
        id="event",
        project_id=memory.workspace.project_id,
        session_id="s",
        turn_id="one",
    )
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

    monkeypatch.setattr(memory, "_produce", produce)
    first = asyncio.create_task(memory._execute(job))
    await started.wait()
    copy = job.model_copy(deep=True)
    second = asyncio.create_task(memory._execute(copy))
    await asyncio.sleep(0.01)
    release.set()
    assert await first and await second
    assert calls == [job.id] and copy.done and copy.result is not None
    await memory.close()


async def test_restart_retries_paused_job_once_without_changing_configuration(
    tmp_path, monkeypatch
):
    from redlotus.infra.persist_utils import save_locked_json
    from redlotus.tools.memory.models import ObservedTurn, PerceptionResult

    workspace = WorkspaceContext.from_path(tmp_path)
    memory = MemoryService(workspace=workspace)
    memory._recovered = True
    memory._save_job(
        MemoryJob(
            id="pending",
            request="remember",
            events=[
                ObservedTurn(
                    id="event",
                    project_id=workspace.project_id,
                    session_id="s",
                    turn_id="one",
                )
            ],
        )
    )
    save_locked_json(
        memory.state_path,
        {"blocked_route": memory._route(), "error": "service unavailable"},
    )
    await memory.process_pending()
    assert memory._paused()
    await memory.close()
    restarted = MemoryService(workspace=workspace)
    calls = []

    async def produce(job):
        calls.append(job.id)
        job.result = PerceptionResult(records=[], reason="recovered")
        job.done = True
        restarted._save_job(job)

    monkeypatch.setattr(restarted, "_produce", produce)
    await restarted.process_pending()
    await restarted.process_pending()
    assert calls == ["pending"] and not restarted._paused()
    await restarted.close()


async def test_legacy_pending_requests_and_short_window_reuse_production(
    tmp_path, monkeypatch
):
    from redlotus.infra.persist_utils import save_locked_json
    from redlotus.tools.memory.models import PerceptionResult, MemoryDraft

    memory = MemoryService(workspace=WorkspaceContext.from_path(tmp_path))
    first = memory.observations.begin("s", "first", "请记住项目资料", [])
    first.status = "success"
    memory.observations.finish(first)
    window = memory.observations.window(flush=True)
    root = memory.observations.root
    request_id = "legacy-request"
    save_locked_json(
        root / "requests" / f"{request_id}.json",
        {
            "id": request_id,
            "event": first.model_dump(mode="json"),
            "request": "保存资料",
            "scope": "project",
        },
    )
    result = PerceptionResult(
        request_authorized=True,
        reason="已生产",
        records=[
            MemoryDraft(
                kind="requested",
                goal="旧版本已由模型提炼的资料",
                source_turn_ids=[first.id],
            )
        ],
    )
    save_locked_json(
        root / "production" / f"{request_id}.json",
        {"result": result.model_dump(), "sources": {}, "reference_ids": []},
    )
    save_locked_json(
        root / "production" / f"{window.id}.json",
        {
            "result": PerceptionResult(
                records=[], reason="无需重复自动记忆"
            ).model_dump(),
            "sources": {},
            "reference_ids": [],
        },
    )
    second = memory.observations.begin("s", "second", "新增未满一窗的事件", [])
    second.status = "success"
    memory.observations.finish(second)

    async def unexpected_production(job):
        raise AssertionError("Successful legacy production must not call the LLM again")

    async def no_index():
        pass

    monkeypatch.setattr(memory, "_produce", unexpected_production)
    monkeypatch.setattr(memory.store, "reconcile", no_index)
    await memory.process_pending()
    assert memory.observations.cursor() == 1
    assert len(memory.store.all("project")) == 1
    assert (root / "migration_backup/jobs-v2/requests/legacy-request.json").is_file()
    assert MemoryJob.model_validate_json(
        (memory.jobs_dir / f"{window.id}.json").read_text(encoding="utf-8")
    ).done
    await memory.close()
