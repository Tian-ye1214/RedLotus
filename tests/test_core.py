"""Persistence/context regressions; real API acceptance is recorded separately."""



import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
)
from pydantic_ai.usage import RequestUsage

from redlotus.core.tasks import TaskManager
from redlotus.documents.interaction import UserMessage
from redlotus.models.context import latest_usage_input_tokens
from redlotus.models.usage import summarize_messages
from redlotus.presentation.snapshots import WorkspaceSnapshot
from redlotus.prompts.message_text import pydantic_messages_to_text
from redlotus.runtime.context import TaskStatus
from redlotus.storage.session import SessionFile


def test_input_display_survives_reload_without_recounting(tmp_path):
    session = SessionFile.create(tmp_path, "test-project")
    for identity, text in (("task-a", "First task"), ("task-b", "Question"), ("reply", "Blue"), ("reply", "Blue")):
        session.record_input(identity, UserMessage(text))
    for identity in ("task-a", "task-b"):
        session.finish_turn(identity, {"status": "success"})
    row = SessionFile.scan_info(tmp_path)[0]
    snapshot = WorkspaceSnapshot(row.path, row.info, datetime.now(timezone.utc), "coordinator", "", "Test", 0)
    assert "用户输入次数：3" in snapshot.label
    assert SessionFile.load(session.path).completed_turns == 2
    assert len(SessionFile.scan_info(tmp_path)) == 1
    summarize_messages([], meta=session.info())  # New display fields must not break the usage panel.


@pytest.mark.parametrize("outcome,label", [("cancelled", "已取消"), ("failed", "失败")])
def test_session_picker_reports_last_actual_outcome(tmp_path, outcome, label):
    session = SessionFile.create(tmp_path, "test-project")
    session.finish_turn("first", {"status": "success"})
    session.finish_turn("last", {"status": outcome})
    row = SessionFile.scan_info(tmp_path)[0]
    snapshot = WorkspaceSnapshot(row.path, row.info, datetime.now(timezone.utc), "coordinator", "", "Test", 0)
    assert snapshot.status == label


def test_synthetic_receipt_does_not_hide_server_context_usage():
    actual = ModelResponse([TextPart("Answer")], usage=RequestUsage(input_tokens=950_000), model_name="test", provider_name="test")
    receipt = ModelResponse([TextPart("Cancelled")], metadata={"origin": "execution_status"})
    assert latest_usage_input_tokens([actual, receipt]) == 950_000
    unknown = ModelResponse([TextPart("No usage reported")], model_name="test", provider_name="test")
    assert latest_usage_input_tokens([actual, receipt, unknown]) is None


def test_compression_keeps_visible_reasoning_but_excludes_system_and_signature():
    messages = [ModelRequest([SystemPromptPart("FIXED-SYSTEM")]),
                ModelResponse([ThinkingPart("Visible reasoning", signature="OPAQUE-SIGNATURE"), TextPart("Answer")])]
    text = pydantic_messages_to_text(messages)
    assert "Visible reasoning" in text and "Answer" in text
    assert "FIXED-SYSTEM" not in text and "OPAQUE-SIGNATURE" not in text


def test_restore_does_not_replay_interrupted_task_or_lose_completed_result():
    manager = TaskManager()
    manager.restore([
        {"id": "A", "description": "Done", "status": "completed", "result": "Artifact saved", "artifacts": ["result.txt"]},
        {"id": "B", "description": "Interrupted side effect", "status": "running", "dependencies": ["A"]},
    ])
    assert manager.tasks["B"].status.value == "unverified"
    assert manager.tasks["A"].result == "Artifact saved"
    assert manager.tasks["A"].artifacts == ["result.txt"]
    assert not manager.get_all_ready_tasks()


def test_resume_requires_new_real_input_and_preserves_completed_dependencies():
    async def scenario():
        saved = []
        current_input = ["input-2", "The requested value is blue"]
        async def checkpoint():
            saved.append(manager.snapshot())
        manager = TaskManager(checkpoint=checkpoint, user_input=lambda: tuple(current_input))
        manager.restore([
            {"id": "A", "description": "Question", "status": "pending_confirmation", "blocked_input_id": "input-1"},
            {"id": "B", "description": "After A", "dependencies": ["A"]},
            {"id": "C", "description": "Finished", "status": "completed", "result": "Keep this"},
        ])
        await manager.resume_task("A")
        assert [task.id for task in manager.get_all_ready_tasks()] == ["A"]
        assert "blue" in manager.tasks["A"].user_input
        assert manager.tasks["C"].result == "Keep this"
        assert saved[-1][0]["status"] == "pending"
        manager.tasks["A"].status = TaskStatus.PENDING_CONFIRMATION
        manager.tasks["A"].blocked_input_id = "input-2"
        assert (await manager.resume_task("A")).startswith("Error:")
        assert (await manager.resume_task("C")).startswith("Error:")
    asyncio.run(scenario())


def test_cancelled_checkpoint_does_not_wait_for_another_user_input(tmp_path, monkeypatch):
    from redlotus.core.system import AgentSystem
    from redlotus.runtime.context import WorkspaceContext

    async def scenario():
        system = object.__new__(AgentSystem)
        from redlotus.runtime.context import EventEmitter
        system.events = EventEmitter()
        system.workspace = WorkspaceContext.from_path(tmp_path)
        system._session_file = None
        system._storage_retry = asyncio.Event()
        system._storage_paused = False
        def disk_failure():
            raise OSError("Disk unavailable")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(system._durable_write(disk_failure, cancelling=True), 1)
    asyncio.run(scenario())


def test_usage_deduplicates_the_same_response_across_retry_invocations(tmp_path):
    session = SessionFile.create(tmp_path, "test-project")
    response = ModelResponse([TextPart("A title")], usage=RequestUsage(input_tokens=37, output_tokens=3), model_name="test", provider_name="test", provider_response_id="response-1")
    for invocation in ("first", "retry"):
        session.record_usage([response], role="title", invocation=invocation)
    restored = SessionFile.load(session.path)
    rows = restored.usage_responses()
    assert len(rows) == 1
    assert rows[0]["role"] == "title"
    assert rows[0]["usage"]["input_tokens"] == 37


def test_auxiliary_response_is_saved_before_result_is_returned(tmp_path):
    from redlotus.core.system import AgentSystem
    from redlotus.models.gateway import RequestPolicy
    from redlotus.runtime.context import WorkspaceContext

    async def scenario():
        system = object.__new__(AgentSystem)
        from redlotus.runtime.context import EventEmitter
        system.events = EventEmitter()
        system.workspace = WorkspaceContext.from_path(tmp_path)
        system._session_file = SessionFile.create(tmp_path / "sessions", "test-project")
        system._storage_retry = asyncio.Event()
        system._cli_turn_id = None
        response = ModelResponse([TextPart("Title")], usage=RequestUsage(input_tokens=31), model_name="test", provider_name="test")
        policy = RequestPolicy("title", SimpleNamespace(name="test", protocol="test"), None)
        with system._usage_scope():
            await policy.after_model_request(None, request_context=None, response=response)
        assert SessionFile.load(system._session_file.path).usage_responses()[0]["role"] == "title"
    asyncio.run(scenario())


def test_prompt_snapshot_survives_restart_and_keeps_agents_separate(tmp_path):
    session = SessionFile.create(tmp_path, "test-project")
    assert session.prompt_snapshot("worker-a", lambda: "Original") == "Original"
    restored = SessionFile.load(session.path)
    assert restored.prompt_snapshot("worker-a", lambda: "Changed") == "Original"
    assert restored.prompt_snapshot("worker-b", lambda: "Independent") == "Independent"


def test_compression_save_failure_cannot_adopt_candidate(monkeypatch):
    from redlotus.models.gateway import RequestPolicy

    async def scenario():
        original, candidate = [ModelRequest([])], [ModelRequest([])]
        async def compact(*args, **kwargs):
            return candidate
        async def fail_save(messages):
            assert messages is candidate
            raise OSError("No disk space")
        monkeypatch.setattr("redlotus.models.context.compact_request_messages", compact)
        target = SimpleNamespace(context={"auto_compress_ratio": .9})
        policy = RequestPolicy("worker", target, None, persist_context=fail_save)
        request = SimpleNamespace(messages=original, model_request_parameters=SimpleNamespace(function_tools=[], output_tools=[]))
        with pytest.raises(OSError):
            await policy.before_model_request(None, request)
        assert request.messages is original
    asyncio.run(scenario())


def test_cancelled_worker_disk_failure_releases_thread_and_capacity(tmp_path, monkeypatch):
    from redlotus.core.agents import SubagentFactory
    from redlotus.core.system import AgentSystem
    from redlotus.runtime.context import SubagentSpec, WorkspaceContext, bind_to_loop

    async def scenario():
        system = object.__new__(AgentSystem)
        from redlotus.runtime.context import EventEmitter
        system.events = EventEmitter()
        system.workspace = WorkspaceContext.from_path(tmp_path)
        system._session_file = None
        system._storage_retry = asyncio.Event()
        factory = SubagentFactory(max_concurrent=1)
        owner = asyncio.get_running_loop()
        started = asyncio.Event()
        persist = bind_to_loop(system._durable_write, owner)
        def fail():
            raise OSError("Persistent disk failure")
        async def worker():
            owner.call_soon_threadsafe(started.set)
            try:
                await asyncio.Event().wait()
            finally:
                await persist(fail, cancelling=bool(asyncio.current_task().cancelling()))
        spec = SubagentSpec("isolated-session", "turn-1", system.workspace)
        task = asyncio.create_task(factory.run(spec, worker))
        await asyncio.wait_for(started.wait(), 5)
        handles = factory.handles
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        await asyncio.wait_for(factory.cancel_all(), 5)
        assert not factory.handles and not factory._slots
        assert all(not handle.thread.is_alive() for handle in handles)
        async def next_worker():
            return "capacity-reused"
        assert await asyncio.wait_for(factory.run(spec, next_worker), 5) == "capacity-reused"
        await factory.close()
    asyncio.run(scenario())


def test_dependency_runs_only_after_completed_state_is_durable(tmp_path):
    from redlotus.runtime.context import SubagentResult
    from redlotus.tools.worker_tools import WorkerOrchestrator

    async def scenario():
        session = SessionFile.create(tmp_path, "test-project")
        manager = TaskManager(checkpoint=lambda: asyncio.to_thread(session.update, metadata={"tasks": manager.snapshot()}))
        await manager.create_todo_list('[{"id":"A","description":"First"},{"id":"B","description":"Dependent","dependencies":["A"]},{"id":"C","description":"Already done"}]')
        manager.mark_task_complete("C", "Keep this")
        await manager.save()
        orchestrator = object.__new__(WorkerOrchestrator)
        orchestrator._task_manager = manager
        calls = []
        async def execute(*args, task_id, **kwargs):
            persisted = {row["id"]: row for row in SessionFile.load(session.path).metadata["tasks"]}
            assert persisted[task_id]["status"] == "running"
            if task_id == "B":
                assert persisted["A"]["status"] == "completed"
                assert persisted["A"]["artifacts"] == ["artifact-A"]
            calls.append(task_id)
            return SubagentResult(status="success", summary=task_id, artifacts=["artifact-" + task_id])
        orchestrator._execute = execute
        await orchestrator.execute_all_tasks_parallel("Test", turn_id="input-1")
        assert calls == ["A", "B"]
        assert manager.tasks["C"].result == "Keep this"
    asyncio.run(scenario())


def test_usage_report_keeps_roles_and_missing_usage_after_compaction(tmp_path):
    from redlotus.models.usage import summarize_usage_files
    from redlotus.presentation.reports import _format_usage_report

    session = SessionFile.create(tmp_path, "test-project")
    for index, role in enumerate(("coordinator", "worker", "compressor", "title")):
        response = ModelResponse([TextPart(role)], usage=RequestUsage(input_tokens=101 + index, output_tokens=7), model_name="test", provider_name="test", provider_response_id=role)
        session.record_usage([response], role=role, invocation=role)
    session.record_usage([ModelResponse([TextPart("Missing usage")], model_name="test", provider_name="test")], role="title", invocation="unknown")
    session.compact(keep_turn_ids=set())
    report = summarize_usage_files([session.path], price_resolver=lambda name: None)
    assert report.by_agent["compressor"].input_tokens == 103
    assert report.by_agent["title"].missing_usage_responses == 1
    assert report.totals.input_tokens == 410
    text = _format_usage_report(report)
    assert "Agent compressor:" in text and "Agent title:" in text


@pytest.mark.parametrize("with_known", [False, True])
def test_usage_cost_missing_usage_is_unknown_or_partial(with_known):
    from decimal import Decimal
    from redlotus.models.usage import ResolvedTokenPrice
    from redlotus.presentation.reports import _format_usage_report

    responses = [ModelResponse([TextPart("Unknown")], model_name="test", provider_name="test")]
    if with_known:
        responses.append(ModelResponse([TextPart("Known")], model_name="test", provider_name="test",
                                       usage=RequestUsage(input_tokens=100, output_tokens=10)))
    report = summarize_messages(responses, price_resolver=lambda name: ResolvedTokenPrice(name, Decimal("0.01"), Decimal("0.02"), "synthetic"))
    report.files = []
    text = _format_usage_report(report)
    assert "Estimated total: partly unavailable (known subtotal: $1.2)" in text if with_known else "Estimated total: unavailable" in text
    assert report.totals.missing_usage_responses == 1
    assert report.totals.responses == 1 + with_known
    assert report.totals.input_tokens == (100 if with_known else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_save", [False, True])
async def test_review_rejection_invalidates_inflight_compression_and_checkpoints(tmp_path, monkeypatch, fail_save):
    import threading
    from contextlib import nullcontext
    from pydantic_ai.messages import UserPromptPart
    from redlotus.core.session import AgentSession
    from redlotus.core.tasks import SessionController
    from redlotus.documents.review import PendingReviewStore
    from redlotus.models.context import ChatHistory
    from redlotus.runtime.context import EventEmitter, WorkspaceContext
    from redlotus.terminal.views import TerminalViews

    storage = SessionFile.create(tmp_path / "sessions", "test-project")
    history = ChatHistory()
    history.set_messages([ModelRequest([UserPromptPart("Original user request")])])
    storage.save_context(history.messages, turn_id=None)
    system = AgentSession()
    system.workspace = WorkspaceContext.from_path(tmp_path)
    system._session_file, system._session = storage, SessionController()
    system._manager_history = ChatHistory()
    system._compression_future = system._current_turn = None
    system._cancel_lock, system._storage_retry = asyncio.Lock(), asyncio.Event()
    system._storage_paused = False
    warnings = []
    system.events = EventEmitter({"print_warning": warnings.append})
    system.structured_task_status = lambda: ""
    system._usage_scope = nullcontext
    system.add_urgent = lambda notice: False
    started, release = asyncio.Event(), asyncio.Event()
    async def candidate(source, **kwargs):
        if kwargs["role"] == "manager":
            return None
        stale = ChatHistory()
        stale.set_messages([ModelRequest([UserPromptPart("STALE SUMMARY")])])
        started.set()
        await release.wait()
        return stale
    monkeypatch.setattr("redlotus.core.session.prepare_compression", candidate)
    path = tmp_path / "document.txt"
    path.write_text("before\n", encoding="utf-8")
    store = PendingReviewStore(threading.Lock())
    store.activate(lambda: None)
    store.write(path, path.name, lambda previous: "after\n")
    system._toolkit = SimpleNamespace(review_store=store)
    entry = store.get(str(path))
    view = SimpleNamespace(highlighted=0, replace_option_prompt_at_index=lambda *args: None)
    ui = SimpleNamespace(system=system, state=SimpleNamespace(history=history), _review_mode=True,
                         _review_items=[(entry, entry.hunks[0])], query_one=lambda *args: view,
                         _exit_review=lambda: None, refresh_status=lambda: None)
    compression = asyncio.create_task(system.compress_context(history))
    await started.wait()
    original_save = storage.save_context
    if fail_save:
        def unavailable(*args, **kwargs):
            if "rejected" in repr(args):
                raise OSError("Synthetic unavailable disk")
            return original_save(*args, **kwargs)
        monkeypatch.setattr(storage, "save_context", unavailable)
    TerminalViews._decide_current(ui, True)
    release.set()
    result = await compression
    assert path.read_text(encoding="utf-8") == "before\n"
    assert "丢弃" in result[0]
    assert "rejected" in repr(history.messages)
    if fail_save:
        for _ in range(100):
            if system.storage_paused:
                break
            await asyncio.sleep(0.01)
        assert system.storage_paused and warnings and "保存失败" in warnings[0]
        assert "rejected" not in repr(SessionFile.load(storage.path).model_messages())
        monkeypatch.setattr(storage, "save_context", original_save)
        await system.retry_saved_state()
    await system._session.queue.join()
    persisted = repr(SessionFile.load(storage.path).model_messages())
    assert "rejected" in persisted and "STALE SUMMARY" not in persisted
