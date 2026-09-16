"""CLI control operations must not lose durable session state at interruption boundaries."""

import asyncio
import errno
import json

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
    ModelMessagesTypeAdapter,
)

from redlotus.core import system as system_module
from redlotus.core.history import ChatHistory
from redlotus.core.session import SessionFile
from redlotus.tools.interaction import UserMessage
from test_system import configured_system


async def _until(predicate, *, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def _history(question, answer):
    history = ChatHistory()
    history.set_messages(
        [
            ModelRequest(parts=[UserPromptPart(question)]),
            ModelResponse(parts=[TextPart(answer)]),
        ]
    )
    return history


def _candidate(summary):
    history = ChatHistory()
    history.set_messages([ModelRequest(parts=[UserPromptPart(summary)])])
    return history


async def test_manual_compression_commits_before_replacing_history_and_blocks_next_job(
    tmp_path, monkeypatch
):
    """Changing history before its transaction commits would lose the pre-compression recovery state."""
    system = configured_system(tmp_path, monkeypatch)
    coordinator = _history("原始目标", "原始结果")
    manager = _history("规划", "规划结果")
    system._manager_history = manager
    await system.bind_session("manual-compression")
    source = list(coordinator.messages)
    candidate = _candidate("压缩后的可恢复摘要")
    entered, release = asyncio.Event(), asyncio.Event()
    queue_started, saved_views = [], []
    original_save = system._session_file.save_context

    async def prepare(history, **_kwargs):
        if history is coordinator:
            entered.set()
            await release.wait()
            return candidate
        return None

    def save_context(messages, **kwargs):
        saved_views.append(list(coordinator.messages))
        return original_save(messages, **kwargs)

    async def queued_after_compression():
        queue_started.append("next")

    monkeypatch.setattr(
        system_module, "prepare_compression", prepare, raising=False
    )
    monkeypatch.setattr(system._session_file, "save_context", save_context)
    try:
        compression = asyncio.create_task(system.compress_context(coordinator))
        await asyncio.wait_for(entered.wait(), 3)
        following = system._session.queue.submit(queued_after_compression)
        await asyncio.sleep(0.03)
        assert queue_started == []
        assert coordinator.messages == source

        release.set()
        assert isinstance(await asyncio.wait_for(compression, 3), list)
        await asyncio.wait_for(following, 3)

        assert queue_started == ["next"]
        assert saved_views and saved_views[0] == source
        assert coordinator.messages == candidate.messages
        restored = SessionFile.load(system._session_file.path)
        assert restored.model_messages() == candidate.messages
    finally:
        release.set()
        await system.shutdown()


async def test_cancelled_manual_compression_cannot_apply_a_late_candidate(
    tmp_path, monkeypatch
):
    """A compression worker that returns after cancellation must not overwrite the active session."""
    system = configured_system(tmp_path, monkeypatch)
    coordinator = _history("仍在执行的任务", "尚未压缩")
    system._manager_history = ChatHistory()
    await system.bind_session("cancel-compression")
    source = list(coordinator.messages)
    candidate = _candidate("不应写回的迟到摘要")
    entered, release, saw_cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def delayed_prepare(history, **_kwargs):
        if history is not coordinator:
            return None
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            saw_cancel.set()
            await release.wait()
        return candidate

    monkeypatch.setattr(
        system_module, "prepare_compression", delayed_prepare, raising=False
    )
    try:
        work = asyncio.create_task(system.compress_context(coordinator))
        await asyncio.wait_for(entered.wait(), 3)
        cancelling = asyncio.create_task(system.cancel_compression())
        await asyncio.wait_for(saw_cancel.wait(), 3)
        release.set()
        await asyncio.wait_for(cancelling, 3)
        await asyncio.wait_for(asyncio.gather(work, return_exceptions=True), 3)

        assert not system.is_compressing
        assert coordinator.messages == source
        assert "不应写回的迟到摘要" not in system._session_file.path.read_text(
            encoding="utf-8"
        )
    finally:
        release.set()
        await system.shutdown()


async def test_storage_pause_retries_the_same_write_only_after_an_explicit_signal(
    tmp_path, monkeypatch
):
    """Immediately re-running a full-disk write can spin the queue and repeat side effects."""
    system = configured_system(tmp_path, monkeypatch)
    await system.bind_session("paused-write")
    target = tmp_path / "written-after-retry.txt"
    attempts = []

    def write_once():
        attempts.append("attempt")
        if len(attempts) == 1:
            raise OSError(errno.ENOSPC, "disk full")
        target.write_text("durable", encoding="utf-8")
        return target

    try:
        pending = asyncio.create_task(system._durable_write(write_once))
        await _until(lambda: system.storage_paused)
        await asyncio.sleep(0.03)
        assert attempts == ["attempt"]
        assert not target.exists()

        await system.retry_saved_state()
        assert await asyncio.wait_for(pending, 3) == target
        assert attempts == ["attempt", "attempt"]
        assert target.read_text(encoding="utf-8") == "durable"
        assert not system.storage_paused
    finally:
        await system.shutdown()


async def test_stop_cancels_a_paused_durable_queue_job_without_retrying_it(
    tmp_path, monkeypatch
):
    """Stopping a turn must release a full-disk wait instead of leaving the CLI permanently busy."""
    system = configured_system(tmp_path, monkeypatch)
    await system.bind_session("stop-paused-write")
    attempts = []

    def always_full():
        attempts.append("attempt")
        raise OSError(errno.ENOSPC, "disk full")

    try:
        queued = system._session.queue.submit(
            lambda: system._durable_write(always_full)
        )
        await _until(lambda: system.storage_paused)
        assert attempts == ["attempt"]

        await asyncio.wait_for(system.stop_current_turn(), 3)
        await _until(queued.cancelled)
        await asyncio.sleep(0.03)
        assert attempts == ["attempt"]
    finally:
        await system.shutdown()


async def test_stop_releases_function_model_checkpoint_pause_without_replaying_agent(
    tmp_path, monkeypatch
):
    """Cancelling a disk-paused real AgentRunner turn must release FIFO work without a second model run."""
    system = configured_system(tmp_path, monkeypatch)
    await system.bind_session("function-model-pause")
    history = ChatHistory()
    model_calls, following = [], []

    async def model(messages, _info):
        model_calls.append(messages[-1].parts[0].content[0])
        yield "模型已完成，正在保存"

    async def create(*_args, **_kwargs):
        return Agent(FunctionModel(stream_function=model))

    original_checkpoint = system._session_file.save_context

    def full_disk_checkpoint(*args, **kwargs):
        # AgentRunner checkpoints the admitted input before invoking the model.
        # Let that durable pre-model record through, then fail the response checkpoint.
        if model_calls:
            raise OSError(errno.ENOSPC, "disk full")
        return original_checkpoint(*args, **kwargs)

    async def run_turn():
        task = system._start_user_turn(
            UserMessage(text="只运行一次"), history, turn_id="function-model-turn"
        )
        assert task is not None
        await task

    async def next_normal_task():
        following.append("next")

    monkeypatch.setattr("redlotus.core.system.create_coordinator_agent", create)
    monkeypatch.setattr(system._session_file, "save_context", full_disk_checkpoint)
    try:
        active = system._session.queue.submit(run_turn)
        queued = system._session.queue.submit(next_normal_task)
        await _until(lambda: system.storage_paused and model_calls == ["只运行一次"])
        assert following == []

        await asyncio.wait_for(system.stop_current_turn(), 3)
        await asyncio.wait_for(active, 3)
        await asyncio.wait_for(queued, 3)

        assert model_calls == ["只运行一次"]
        assert following == ["next"]
    finally:
        await system.shutdown()


async def test_manual_compression_commits_both_histories_or_applies_neither(
    tmp_path, monkeypatch
):
    """Separate coordinator and manager commits could recover an impossible half-compressed session."""
    system = configured_system(tmp_path, monkeypatch)
    coordinator = _history("协调器原文", "协调器原结果")
    manager = _history("管理器原文", "管理器原结果")
    system._manager_history = manager
    await system.bind_session("dual-compression")
    coordinator_source, manager_source = list(coordinator.messages), list(manager.messages)
    coordinator_candidate = _candidate("协调器压缩摘要")
    manager_candidate = _candidate("管理器压缩摘要")
    candidates = {
        id(coordinator): coordinator_candidate,
        id(manager): manager_candidate,
    }
    writes = []
    original_save = system._session_file.save_context

    async def prepare(history, **_kwargs):
        return candidates[id(history)]

    def save_context(messages, **kwargs):
        writes.append((list(coordinator.messages), list(manager.messages), kwargs))
        return original_save(messages, **kwargs)

    monkeypatch.setattr(system_module, "prepare_compression", prepare, raising=False)
    monkeypatch.setattr(system._session_file, "save_context", save_context)
    try:
        before = len(json.loads(system._session_file.path.read_text(encoding="utf-8"))["updates"])
        await system.compress_context(coordinator)

        assert len(writes) == 1
        saved_coordinator, saved_manager, kwargs = writes[0]
        assert saved_coordinator == coordinator_source
        assert saved_manager == manager_source
        restored_manager = ModelMessagesTypeAdapter.validate_python(
            kwargs["metadata"]["manager_context"]
        )
        assert restored_manager == manager_candidate.messages
        after = len(json.loads(system._session_file.path.read_text(encoding="utf-8"))["updates"])
        assert after == before + 1
        assert coordinator.messages == coordinator_candidate.messages
        assert manager.messages == manager_candidate.messages
    finally:
        await system.shutdown()

    failing = configured_system(tmp_path / "failing", monkeypatch)
    failed_coordinator = _history("失败协调器原文", "失败协调器原结果")
    failed_manager = _history("失败管理器原文", "失败管理器原结果")
    failing._manager_history = failed_manager
    await failing.bind_session("dual-compression-failure")
    failed_coordinator_source = list(failed_coordinator.messages)
    failed_manager_source = list(failed_manager.messages)
    failed_candidates = {
        id(failed_coordinator): _candidate("不应应用的协调器摘要"),
        id(failed_manager): _candidate("不应应用的管理器摘要"),
    }

    async def failed_prepare(history, **_kwargs):
        return failed_candidates[id(history)]

    def disk_full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(system_module, "prepare_compression", failed_prepare, raising=False)
    monkeypatch.setattr(failing._session_file, "save_context", disk_full)
    try:
        work = asyncio.create_task(failing.compress_context(failed_coordinator))
        await _until(lambda: failing.storage_paused)
        await asyncio.wait_for(failing.stop_current_turn(), 3)
        await asyncio.wait_for(work, 3)
        assert failed_coordinator.messages == failed_coordinator_source
        assert failed_manager.messages == failed_manager_source
    finally:
        await failing.shutdown()


async def test_load_repairs_unknown_tool_result_once_and_marks_turn_interrupted(
    tmp_path, monkeypatch
):
    """Reloading an interrupted tool call must close its protocol pair without replaying the tool."""
    system = configured_system(tmp_path, monkeypatch)
    store = SessionFile.create(
        tmp_path / "sessions", system.workspace.project_id, session_id="resume"
    )
    messages = [
        ModelRequest(parts=[UserPromptPart("生成一次产物")]),
        ModelResponse(
            parts=[ToolCallPart("write_file", {"path": "result.txt"}, tool_call_id="call-1")]
        ),
    ]
    store.save_context(messages, turn_id="unfinished")
    store.update(metadata={"active_turn": {"id": "unfinished", "status": "running"}})
    meta = store.info()
    try:
        repaired = await system.bind_loaded_snapshot("coordinator", store.path, meta)
        unknown = [
            part
            for message in repaired
            for part in message.parts
            if isinstance(part, ToolReturnPart) and part.tool_call_id == "call-1"
        ]
        assert len(unknown) == 1
        assert unknown[0].content["status"] == "unknown"
        assert unknown[0].metadata["execution_outcome"] == "unknown"
        assert unknown[0].outcome == "failed"

        saved = SessionFile.load(store.path)
        assert saved.completed_turns == 0
        assert saved.turn("unfinished")["status"] == "interrupted"
        assert saved.metadata.get("active_turn") is None
        assert len(
            [
                part
                for message in saved.model_messages()
                for part in message.parts
                if isinstance(part, ToolReturnPart) and part.tool_call_id == "call-1"
            ]
        ) == 1

        await system.end_session_agents("resume")
        reloaded = await system.bind_loaded_snapshot(
            "coordinator", store.path, saved.info()
        )
        assert len(
            [
                part
                for message in reloaded
                for part in message.parts
                if isinstance(part, ToolReturnPart) and part.tool_call_id == "call-1"
            ]
        ) == 1
    finally:
        await system.shutdown()


async def test_load_repair_keeps_the_tool_receipt_in_the_observation_turn(
    tmp_path, monkeypatch
):
    """Observation IDs identify memory rows, while message evidence belongs to the raw turn ID."""
    system = configured_system(tmp_path, monkeypatch)
    store = SessionFile.create(
        tmp_path / "sessions", system.workspace.project_id, session_id="resume-observed"
    )
    raw_turn_id = "cli-turn"
    observations = system._memory.observations
    observations.bind(store)
    event = observations.begin("resume-observed", raw_turn_id, "生成一次产物", [])
    assert event.id != raw_turn_id
    messages = [
        ModelRequest(parts=[UserPromptPart("生成一次产物")]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    "write_file", {"path": "result.txt"}, tool_call_id="call-observed"
                )
            ]
        ),
    ]
    store.save_context(messages, turn_id=raw_turn_id)
    try:
        await system.bind_loaded_snapshot("coordinator", store.path, store.info())
        saved = SessionFile.load(store.path)
        assert any(
            isinstance(part, ToolReturnPart) and part.tool_call_id == "call-observed"
            for message in saved.read_turn(raw_turn_id)
            for part in message.parts
        )
    finally:
        await system.shutdown()


def test_corrupted_middle_transaction_never_discards_a_following_commit_on_the_same_line(
    tmp_path,
):
    """Recovery may discard only a terminal partial transaction, regardless of whitespace damage."""
    store = SessionFile.create(tmp_path, "project")
    for title in ("first", "second", "third"):
        store.update(metadata={"title": title})
    damaged = (
        store.path.read_bytes()
        .replace(b'"title":"second"', b'"title":broken')
        .replace(b'},\n{"metadata":{"title":"third"', b'}, {"metadata":{"title":"third"')
    )
    store.path.write_bytes(damaged)

    with pytest.raises(ValueError, match="事务|transaction|损坏"):
        SessionFile.load(store.path)
    assert store.path.read_bytes() == damaged


def test_terminal_corruption_ignores_commit_shaped_text_and_nested_metadata(tmp_path):
    """A literal or nested object resembling a journal row is not a later transaction."""
    store = SessionFile.create(tmp_path, "project")
    store.update(metadata={"title": "first"})
    embedded = {"metadata": {"title": "embedded"}}
    embedded["commit"] = store._commit_tag(embedded, 99)
    store.update(
        metadata={
            "title": "last",
            "literal": json.dumps(embedded, ensure_ascii=False),
            "nested": embedded,
        }
    )
    damaged = store.path.read_bytes().replace(b'"title":"last"', b'"title":broken')
    store.path.write_bytes(damaged)

    with pytest.raises(ValueError, match="事务|损坏"):
        SessionFile.load(store.path)
    assert store.path.read_bytes() == damaged


def test_incomplete_tail_with_nested_commit_values_recovers_only_prior_commit(tmp_path):
    store = SessionFile.create(tmp_path, "project")
    store.update(metadata={"title": "first"})
    embedded = {"metadata": {"title": "embedded"}}
    embedded["commit"] = store._commit_tag(embedded, 99)
    store.update(metadata={"literal": json.dumps(embedded), "nested": [embedded], "unfinished": "ending"})
    raw = store.path.read_bytes()
    store.path.write_bytes(raw[:raw.index(b'"ending"') + 4])
    restored = SessionFile.load(store.path)
    assert restored.metadata["title"] == "first"
    assert restored.recovered_partial_write


def test_multiple_broken_middle_transactions_do_not_hide_a_later_valid_commit(tmp_path):
    """A broken next row does not make an even later committed row disposable."""
    store = SessionFile.create(tmp_path, "project")
    for title in ("first", "second", "third", "fourth"):
        store.update(metadata={"title": title})
    damaged = (
        store.path.read_bytes()
        .replace(b'"title":"second"', b'"title":broken')
        .replace(b'"title":"third"', b'"title":broken')
    )
    store.path.write_bytes(damaged)

    with pytest.raises(ValueError, match="事务|transaction|损坏"):
        SessionFile.load(store.path)
    assert store.path.read_bytes() == damaged


def test_complete_later_commit_before_a_torn_journal_trailer_is_not_discarded(tmp_path):
    """A missing array trailer does not make a following valid transaction an incomplete batch."""
    store = SessionFile.create(tmp_path, "project")
    for title in ("first", "second", "third"):
        store.update(metadata={"title": title})
    raw = store.path.read_bytes()
    assert raw.endswith(b"\n]}")
    damaged = raw.replace(b'"title":"second"', b'"title":broken')[:-3]
    store.path.write_bytes(damaged)

    with pytest.raises(ValueError, match="事务|transaction|损坏"):
        SessionFile.load(store.path)
    assert store.path.read_bytes() == damaged
