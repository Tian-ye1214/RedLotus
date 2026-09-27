"""Regression contracts for completed logical user turns and durable evidence."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from redlotus.runtime.resources import WorkspaceContext
from redlotus.sessions.storage import SessionFile


def session(tmp_path):
    workspace = WorkspaceContext.from_path(tmp_path)
    return SessionFile.create(tmp_path / "sessions", workspace.project_id, workspace=workspace)


def event(store, identity="event-1", turn_id="turn-1", **fields):
    return dict(id=identity, session_id=store.session_id, project_id=store.project_id,
                turn_id=turn_id, status="success", user_inputs=["request"],
                reference_ids=[], created_at="2026-01-01T00:00:00+00:00", **fields)


def reply(text="A complete answer"):
    return [ModelRequest(parts=[UserPromptPart("request")]), ModelResponse(parts=[TextPart(text)])]


def test_recovery_and_interruption_share_pending_tool_evidence():
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart
    from redlotus.core.gateway import AgentRunner
    from redlotus.sessions.context import messages_safe_for_new_prompt, repair_interrupted_tool_calls

    previous = reply()
    call = ToolCallPart("read_file", {"name": "a.txt"}, "pending")
    history = [*previous, ModelRequest(parts=[UserPromptPart("next")]), ModelResponse(parts=[call])]
    assert messages_safe_for_new_prompt(history) == previous
    repaired = repair_interrupted_tool_calls(history)
    assert repaired[-1].parts[0].content["status"] == "unknown"
    assert repaired[-1].parts[0].metadata["execution_outcome"] == "unknown"
    assert repair_interrupted_tool_calls(repaired) == repaired
    result = ToolReturnPart("read_file", "preserved output", "pending")
    AgentRunner._close_interrupted_calls(history, [result], RuntimeError("lost stream"))
    assert history[-2].parts == [result]
    assert history[-1].metadata["origin"] == "execution_status"


def test_goal_summary_uses_shared_formatter_without_repeating_user_requirements():
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart
    from redlotus.core.tasks import summarize_last_coordinator_turn

    messages = [*reply("earlier answer"), ModelRequest(parts=[UserPromptPart("new requirements")]),
                ModelResponse(parts=[ToolCallPart("inspect", {}, "call")]),
                ModelRequest(parts=[ToolReturnPart("inspect", "line 1\n  line 2", "call")]),
                ModelResponse(parts=[TextPart("next step\n  code")])]
    text = summarize_last_coordinator_turn(messages)
    assert "line 1\n  line 2" in text and "next step\n  code" in text
    assert "earlier answer" not in text and "new requirements" not in text and "TOOL_CALL" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["yes", "no"])
async def test_command_confirmation_accepts_rich_question_text(tmp_path, isolated_config, monkeypatch, answer):
    from pydantic_ai import ToolReturn
    from redlotus.tools.base_tools import BasicToolkit
    from redlotus.tools.registry import SkillsManager

    isolated_config["storage"]["runtime_dir"] = "runtime"
    workspace = WorkspaceContext.from_path(tmp_path)
    toolkit = BasicToolkit(SkillsManager(workspace=workspace), workspace=workspace, show_diff=Mock())
    toolkit.set_ask_user_handler(AsyncMock(return_value=ToolReturn(return_value=answer, content=["evidence"])))
    monkeypatch.setattr(toolkit, "_is_command_safe", lambda command: (True, ""))
    monkeypatch.setattr(toolkit, "_command_needs_confirm", lambda command: "recursive delete")
    execute = AsyncMock(return_value=Mock(to_text=Mock(return_value="executed")))
    monkeypatch.setattr("redlotus.tools.base_tools.run_subprocess", execute)
    result = await toolkit.run_command("dummy command")
    assert result == "executed" if answer == "yes" else "已取消执行" in result
    assert execute.await_count == (answer == "yes")


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [True, False])
async def test_registered_memory_tool_covers_project_episode_search_and_read(owner):
    from redlotus.memory.records import MemoryRecord
    from redlotus.memory.store import MemoryReader

    episode = MemoryRecord(id="episode", project_id="project", goal="finished task")
    store = Mock(retrieval_error="", search=AsyncMock(return_value=[episode]))
    store.get.return_value = episode
    reader = MemoryReader(store, Mock(), Mock(), owner)
    results = [await reader.search_memory("finished", scope="project"),
               await reader.search_memory(scope="project", id="episode")]
    if owner:
        assert json.loads(results[0])["memories"][0]["id"] == "episode"
        assert json.loads(results[1])["goal"] == "finished task"
        assert "selected scope" in await reader.search_memory(scope="global", id="episode")
    else:
        assert results == ["Error: Personal memory unavailable."] * 2
        store.search.assert_not_called()
        store.get.assert_not_called()


@pytest.mark.parametrize("status", ["cancelled", "failed", "needs_input", "running", "success"])
def test_unmarked_attempt_never_counts(tmp_path, status):
    store = session(tmp_path)
    details = event(store)
    details["status"] = status
    store.finish_turn(details["id"], details)
    assert store.completed_turns == 0
    assert store.turn(details["id"])["status"] == status
    assert store.pending_turns(0) == []


def test_final_reply_and_completion_are_one_durable_commit(tmp_path):
    store = session(tmp_path)
    details = event(store)
    store.save_context(reply("The requested operation failed; here is the full result."),
                       turn_id=details["turn_id"], completed_turn=details)
    loaded = SessionFile.load(store.path, workspace=store.workspace)
    assert loaded.completed_turns == 1
    assert loaded.turn(details["id"])["completion_number"] == 1
    assert loaded.metadata["turn_count_version"] == 2
    assert loaded.model_messages()[-1].parts[0].content.startswith("The requested operation failed")
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert "messages" in raw["updates"][-1] and "turns" in raw["updates"][-1]


def test_repeated_completion_and_late_cleanup_error_do_not_recount(tmp_path):
    store = session(tmp_path)
    details = event(store)
    messages = reply()
    for _ in range(2):
        store.save_context(messages, turn_id="turn-1", completed_turn=details)
        store = SessionFile.load(store.path, workspace=store.workspace)
    store.finish_turn(details["id"], dict(details, status="failed", error="cleanup failed"))
    assert store.completed_turns == 1
    assert store.turn(details["id"])["final_response_completed"] is True
    assert store.turn(details["id"])["error"] == "cleanup failed"


def test_cancel_then_resume_preserves_audit_order_and_counts_once(tmp_path):
    store = session(tmp_path)
    details = event(store)
    store.finish_turn(details["id"], dict(details, status="cancelled", error="paused"))
    store.save_context(reply(), turn_id="turn-1", completed_turn=details)
    row = store.turn(details["id"])
    assert store.completed_turns == 1
    assert row["number"] == 1
    assert row["completion_number"] == 1
    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert any(update.get("turns", {}).get(details["id"], {}).get("status") == "cancelled"
               for update in saved["updates"])


def test_memory_window_counts_completed_replies_only(tmp_path):
    from redlotus.memory.records import ObservationStore

    store = session(tmp_path)
    for index in range(4):
        details = event(store, f"event-{index}", f"turn-{index}")
        if index in (0, 2):
            store.finish_turn(details["id"], dict(details, status="cancelled"))
        else:
            store.save_context(reply(str(index)), turn_id=details["turn_id"], completed_turn=details)
    observations = ObservationStore(store.workspace, window_turns=2, overlap_turns=0)
    observations.bind(store)
    window = observations.window()
    assert window.new_turn_ids == ["event-1", "event-3"]
    assert window.end_position == 2


@pytest.fixture
def make_system(tmp_path, isolated_config, monkeypatch):
    from pydantic_ai import Agent
    from pydantic_ai.models.function import FunctionModel
    from redlotus.core.system import AgentSystem

    def create(stream, *, ask=False):
        system = AgentSystem(presentation=Mock(), workspace=WorkspaceContext.from_path(tmp_path),
                             owner_memory_allowed=False)
        system.presentation.supports_model_stream.return_value = False
        system._context_prewarmed = True
        monkeypatch.setattr("redlotus.core.system.create_coordinator_agent",
                            AsyncMock(return_value=Agent(FunctionModel(stream_function=stream),
                                                         tools=[system.toolkit.ask_user] if ask else [])))
        return system
    return create


@pytest.mark.asyncio
@pytest.mark.parametrize("goal", [False, True])
async def test_shared_entry_returns_final_text_and_counts_goal_once(make_system, goal):
    from redlotus.sessions.context import ChatHistory, UserMessage

    outputs = iter(["working<!-- REDLOTUS_GOAL: CONTINUE -->", "complete<!-- REDLOTUS_GOAL: DONE -->"]
                   if goal else ["complete"])
    calls = 0

    async def stream(messages, info):
        nonlocal calls
        calls += 1
        yield next(outputs)

    system = make_system(stream)
    try:
        output = await system._start_user_turn(UserMessage("request"), ChatHistory(), goal_mode=goal, turn_id="original")
        assert output == "complete"
        assert system._session_file.completed_turns == 1
        assert calls == (2 if goal else 1)
        assert len(system._session_file.pending_turns(0)) == 1
    finally:
        await system.shutdown()


@pytest.mark.asyncio
async def test_multiple_pause_resume_keeps_one_logical_turn(make_system):
    from redlotus.sessions.context import ChatHistory, UserMessage
    from redlotus.ui.console import AgentCliController

    entered, proceed = asyncio.Event(), asyncio.Event()

    async def stream(messages, info):
        entered.set()
        await proceed.wait()
        yield "complete"

    system = make_system(stream)
    controller = AgentCliController(system)
    state = controller.new_session_state()
    state.is_first_input = False
    try:
        system._start_user_turn(UserMessage("request"), state.history, turn_id="original")
        for _ in range(3):
            await asyncio.wait_for(entered.wait(), 5)
            assert await controller.pause_current_turn()
            assert system._session_file.completed_turns == 0
            assert system._session.paused["turn_id"] == "original"
            entered.clear()
            assert await controller.resume_current_turn(state)
        proceed.set()
        await asyncio.wait_for(system._session.queue.join(), 5)
        assert system._session_file.completed_turns == 1
        assert {row["turn_id"] for row in system._session_file.pending_turns(0)} == {"original"}
        assert len(system._session_file._turns) == 1
    finally:
        await system.shutdown()


@pytest.mark.asyncio
async def test_pause_before_coroutine_entry_preserves_input_and_resume_identity(make_system):
    from redlotus.sessions.context import UserMessage
    from redlotus.ui.console import AgentCliController

    async def stream(messages, info):
        yield "complete"

    system = make_system(stream)
    controller = AgentCliController(system)
    try:
        system._start_user_turn(UserMessage("before entry"), system._session.history, turn_id="original")
        assert await controller.pause_current_turn()
        assert system._session.paused["user_inputs"] == ["before entry"]
        assert system._session.paused["submitted"] is False
        assert await controller.resume_current_turn(system._session)
        await system._session.queue.join()
        assert system._session_file.completed_turns == 1
        assert system._session_file.pending_turns(0)[0]["turn_id"] == "original"
    finally:
        await system.shutdown()


@pytest.mark.asyncio
async def test_interrupted_model_response_retains_error_without_count(make_system):
    from redlotus.sessions.context import ChatHistory, UserMessage

    async def stream(messages, info):
        yield "partial"
        raise RuntimeError("synthetic model failure")

    system = make_system(stream)
    try:
        with pytest.raises(RuntimeError, match="synthetic model failure"):
            await system._start_user_turn(UserMessage("request"), ChatHistory())
        assert system._session_file.completed_turns == 0
        assert str(system.last_turn_error) == "synthetic model failure"
        assert next(iter(system._session_file._turns.values()))["status"] == "failed"
    finally:
        await system.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["message", "cli"])
async def test_attachment_answer_is_native_tool_content_and_not_an_extra_turn(make_system, source):
    from pydantic_ai import BinaryContent, ToolReturn
    from redlotus.sessions.context import UserMessage

    async def unused_stream(messages, info):
        yield "unused"

    system = make_system(unused_stream)
    answer = UserMessage("line 1\n  line 2", attachments=[
        BinaryContent(b"original attachment", media_type="text/plain", identifier="answer.txt")])
    if source == "cli":
        (system.workspace.root / "answer.txt").write_text("original attachment", encoding="utf-8")
        answer = "line 1\n  line 2 @answer.txt"
    expected_text = answer if isinstance(answer, str) else answer.text
    system.set_ask_user_handler(AsyncMock(return_value=answer))
    try:
        async with system._outer_turn(UserMessage("request"), "original"):
            result = await system.toolkit.ask_user("Please send the file")
            assert isinstance(result, ToolReturn)
            assert result.return_value == expected_text
            assert any("original attachment" in str(part) for part in result.content)
            assert len(system._memory.current.reference_ids) == 1
            assert system._session.user_inputs == ["request", expected_text]
        assert system._session_file.completed_turns == 0
    finally:
        await system.shutdown()


@pytest.mark.asyncio
async def test_channel_send_failure_preserves_completed_result(make_system):
    from redlotus.api.base import BotBase
    from redlotus.sessions.context import UserMessage

    calls = 0
    async def stream(messages, info):
        nonlocal calls
        calls += 1
        yield "saved final answer"

    system = make_system(stream)
    class FakeBot(BotBase):
        def adapt_message(self, *args):
            raise NotImplementedError
    bot = FakeBot()
    bot.platform_tag = "QQ"
    state = bot._session("private_owner")
    system._session = state
    state.agent = system
    async def fail_send(text):
        raise OSError("synthetic transport failure")
    try:
        future = bot._submit_turn("private_owner", state, UserMessage("request"), fail_send)
        with pytest.raises(OSError, match="transport failure"):
            await future
        loaded = SessionFile.load(system._session_file.path, workspace=system.workspace)
        assert loaded.completed_turns == 1
        assert loaded.model_messages()[-1].parts[0].content == "saved final answer"
        assert calls == 1
    finally:
        await bot.release_all_resources_async()


@pytest.mark.asyncio
async def test_channel_image_answer_reaches_model_during_same_turn(make_system):
    import io
    from PIL import Image
    from pydantic_ai import BinaryContent
    from pydantic_ai.models.function import DeltaToolCall
    from redlotus.api.base import BotBase
    from redlotus.sessions.context import UserMessage

    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(buffer, format="PNG")
    png = buffer.getvalue()
    question = asyncio.Event()
    requests = []
    async def stream(messages, info):
        requests.append(list(messages))
        if len(requests) == 1:
            yield {0: DeltaToolCall(name="ask_user", json_args='{"question":"send image"}')}
        else:
            yield "received image"

    system = make_system(stream, ask=True)
    class FakeBot(BotBase):
        def adapt_message(self, *args):
            raise NotImplementedError
    bot = FakeBot()
    bot.platform_tag = "QQ"
    state = bot._session("private_owner")
    system._session = state
    system.set_ask_user_handler(bot._ask_user)
    state.agent = system
    replies = []
    async def send(text):
        replies.append(text)
        if text == "send image":
            question.set()
    try:
        future = bot._submit_turn("private_owner", state, UserMessage("request"), send)
        await asyncio.wait_for(question.wait(), 5)
        await bot.dispatch_user_message("private_owner", UserMessage("", attachments=[
            BinaryContent(png, media_type="image/png", identifier="answer.png")]), send)
        await asyncio.wait_for(future, 5)
        media = [item for message in requests[-1] for part in message.parts
                 for item in (part.content if isinstance(getattr(part, "content", None), list) else [])
                 if isinstance(item, BinaryContent)]
        assert [item.data for item in media] == [png]
        assert replies[-1] == "received image"
        assert system._session_file.completed_turns == 1
        assert len(system._session_file._turns) == 1
        assert not state.queue.pending
    finally:
        await bot.release_all_resources_async()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [False, True])
async def test_actual_coordinator_permissions_and_prompt_without_runtime(tmp_path, isolated_config, monkeypatch, owner):
    from pydantic_ai.models.function import FunctionModel
    from redlotus.core.system import AgentSystem
    from redlotus.sessions.context import ChatHistory, UserMessage

    definitions = []
    async def stream(messages, info):
        definitions.extend(tool.name for tool in info.function_tools)
        yield "complete"
    monkeypatch.setattr("redlotus.core.gateway.create_model", lambda *args: FunctionModel(stream_function=stream))
    presentation = Mock()
    presentation.supports_model_stream.return_value = False
    system = AgentSystem(presentation=presentation, workspace=WorkspaceContext.from_path(tmp_path), owner_memory_allowed=owner)
    system._context_prewarmed = True
    # A synthetic already-loaded snapshot avoids unrelated database/network work.
    system._memory._injection_snapshot = "synthetic memory"
    system._memory.observations.window_turns = 20
    system._memory.observations.overlap_turns = 3
    try:
        await system.bind_session("synthetic-session")
        system._memory._injection_snapshot = "synthetic memory"
        output = await system._start_user_turn(UserMessage("hello"), ChatHistory())
        assert output == "complete", repr(system.last_turn_error)
        if owner:
            assert {"run_command", "execute_task_with_worker", "execute_task_with_manager", "remember", "search_memory"} <= set(definitions)
        else:
            assert definitions == []
        assert system._session_file.completed_turns == 1
    finally:
        await system.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("goal", [False, True])
async def test_reply_completion_survives_later_worker_cleanup_error(make_system, monkeypatch, goal):
    from redlotus.sessions.context import ChatHistory, UserMessage

    async def stream(messages, info):
        yield "complete<!-- REDLOTUS_GOAL: DONE -->" if goal else "complete"
    system = make_system(stream)
    try:
        await system.bind_session("cleanup-session")
        worker = Mock()
        worker.compact.side_effect = RuntimeError("synthetic cleanup failure")
        monkeypatch.setattr(system._session_file, "role_file", lambda role, **kwargs: worker)
        with pytest.raises(RuntimeError, match="synthetic cleanup failure"):
            await system._start_user_turn(UserMessage("request"), ChatHistory(), goal_mode=goal)
        assert system._session_file.completed_turns == 1
        assert str(system.last_turn_error) == "synthetic cleanup failure"
    finally:
        await system.shutdown()
