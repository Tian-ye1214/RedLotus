import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from pydantic_ai import BinaryContent

from redlotus.api.base import BotBase
from redlotus.api.QQ import QQBot
from redlotus.api.WeChat import WeChatAgentBot
from redlotus.api import media as qq
from redlotus.runtime.resources import WorkspaceContext
from redlotus.sessions.control import UserMessage
from redlotus.tools.references import ReferenceStore


@pytest.mark.asyncio
async def test_shared_start_and_final_reply(phone):
    bot, state, send, replies, calls = phone
    await bot.dispatch_user_message("wx_owner", UserMessage("hello\n  code"), send)
    await state.queue.join()
    assert [message.text for message in calls] == ["hello\n  code"]
    assert replies[-1] == "final answer"



@pytest.mark.asyncio
async def test_question_attachment_bypasses_queue_and_prepares(phone):
    bot, state, send, replies, calls = phone
    state.question = asyncio.get_running_loop().create_future()
    message = UserMessage("", attachments=[BinaryContent(b"answer evidence", media_type="text/plain", identifier="answer.txt")])
    await bot.dispatch_user_message("wx_owner", message, send)
    assert state.question.done()
    answer = state.question.result()
    assert isinstance(answer, UserMessage)
    assert "answer evidence" in answer.references[0].parts[0].text
    assert calls == []
    assert not state.queue.pending



@pytest.mark.asyncio
async def test_failed_question_attachment_keeps_question_waiting(phone):
    bot, state, send, replies, calls = phone
    state.question = asyncio.get_running_loop().create_future()
    async def prepare():
        raise ValueError("broken.txt download failed")
    await bot.dispatch_user_message("wx_owner", UserMessage("answer"), send, prepare=prepare)
    assert not state.question.done()
    assert "broken.txt" in replies[-1]
    assert calls == []
    state.question.cancel()



@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/clear", "/stop"])
async def test_late_question_attachment_cannot_resolve_after_control(phone, command):
    bot, state, send, replies, calls = phone
    state.question = asyncio.get_running_loop().create_future()
    old_question = state.question
    started, finish = asyncio.Event(), asyncio.Event()
    async def prepare():
        started.set()
        await finish.wait()
        return [BinaryContent(b"late", media_type="text/plain", identifier="late.txt")]
    delivery = asyncio.create_task(bot.dispatch_user_message("wx_owner", UserMessage(""), send, prepare=prepare))
    await started.wait()
    await bot.dispatch_user_message("wx_owner", UserMessage(command), send)
    replacement = bot._session("wx_owner")
    replacement.question = asyncio.get_running_loop().create_future()
    finish.set()
    await delivery
    assert not replacement.question.done()
    assert old_question.cancelled() or not old_question.done()
    replacement.question.cancel()



@pytest.mark.asyncio
async def test_failed_download_rejects_entire_turn(phone):
    bot, state, send, replies, calls = phone
    async def prepare():
        raise ValueError("second.txt download failed")
    await bot.dispatch_user_message("wx_owner", UserMessage("hello"), send, prepare=prepare)
    await state.queue.join()
    assert calls == []
    assert "second.txt" in replies[-1]



@pytest.mark.asyncio
async def test_send_failure_does_not_rerun_completed_turn(phone):
    bot, state, send, replies, calls = phone
    async def fail_final(text):
        if text == "final answer":
            raise OSError("transport closed")
    await bot.dispatch_user_message("wx_owner", UserMessage("first"), fail_final)
    await state.queue.join()
    await bot.dispatch_user_message("wx_owner", UserMessage("second"), send)
    await state.queue.join()
    assert [message.text for message in calls] == ["first", "second"]
    assert replies[-1] == "final answer"



@pytest.mark.asyncio
async def test_real_question_flow_unblocks_before_next_queued_turn(phone):
    bot, state, send, replies, calls = phone
    seen = []
    def start(message, history, *, turn_id):
        async def run():
            seen.append(message.text)
            if message.text == "question":
                answer = await bot._ask_user("send evidence")
                assert answer.references[0].snapshot.read_bytes() == b"evidence"
            return "finished " + message.text
        return asyncio.create_task(run())
    state.agent._start_user_turn = start
    await bot.dispatch_user_message("wx_owner", UserMessage("question"), send)
    await bot.dispatch_user_message("wx_owner", UserMessage("queued"), send)
    for _ in range(100):
        if state.question is not None:
            break
        await asyncio.sleep(0)
    assert state.question is not None
    await bot.dispatch_user_message("wx_owner", UserMessage("", attachments=[
        BinaryContent(b"evidence", media_type="text/plain", identifier="evidence.txt")]), send)
    await asyncio.wait_for(state.queue.join(), 2)
    assert seen == ["question", "queued"]
    assert replies[-2:] == ["finished question", "finished queued"]



@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, "synthetic model failed"])
async def test_cancelled_turn_sends_no_answer_and_failure_reports_error(phone, error):
    bot, state, send, replies, calls = phone
    def start(*args, **kwargs):
        async def run():
            if error:
                raise ValueError(error)
            raise asyncio.CancelledError()
        return asyncio.create_task(run())
    state.agent._start_user_turn = start
    await bot.dispatch_user_message("wx_owner", UserMessage("run"), send)
    await state.queue.join()
    if error:
        assert error in replies[-1]
    else:
        assert len(replies) == 1



@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/stop", "/clear"])
async def test_controls_during_preparation_cancel_without_running_model(phone, command):
    bot, state, send, replies, calls = phone
    preparing = asyncio.Event()
    async def prepare():
        preparing.set()
        await asyncio.Event().wait()
    await bot.dispatch_user_message("wx_owner", UserMessage("first"), send, prepare=prepare)
    await preparing.wait()
    await bot.dispatch_user_message("wx_owner", UserMessage("second"), send)
    await bot.dispatch_user_message("wx_owner", UserMessage(command), send)
    await state.queue.join()
    assert [message.text for message in calls] == (["second"] if command == "/stop" else [])



@pytest.mark.asyncio
async def test_question_text_and_reference_answers(phone):
    bot, state, send, replies, calls = phone
    state.question = asyncio.get_running_loop().create_future()
    await bot.dispatch_user_message("wx_owner", UserMessage("  exact\nanswer"), send)
    assert state.question.result().text == "  exact\nanswer"
    message = UserMessage("", attachments=[BinaryContent(b"reference", media_type="text/plain", identifier="ref.txt")])
    await state.agent.toolkit._references.prepare_message(message)
    state.question = asyncio.get_running_loop().create_future()
    await bot.dispatch_user_message("wx_owner", message, send)
    assert state.question.result().references[0].snapshot.read_bytes() == b"reference"



@pytest.mark.asyncio
async def test_question_parse_failure_names_item_and_keeps_waiting(phone):
    bot, state, send, replies, calls = phone
    state.question = asyncio.get_running_loop().create_future()
    message = UserMessage("", attachments=[
        BinaryContent(b"good", media_type="text/plain", identifier="good.txt"),
        BinaryContent(b"not a picture", media_type="image/png", identifier="broken.png")])
    await bot.dispatch_user_message("wx_owner", message, send)
    assert not state.question.done()
    assert "broken.png" in replies[-1]
    assert calls == []
    state.question.cancel()



@pytest.mark.asyncio
async def test_stopped_preparation_cannot_revive_turn_if_downloader_finishes(phone):
    bot, state, send, replies, calls = phone
    preparing = asyncio.Event()
    async def prepare():
        preparing.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return [BinaryContent(b"late", media_type="text/plain", identifier="late.txt")]
    await bot.dispatch_user_message("wx_owner", UserMessage("run"), send, prepare=prepare)
    await preparing.wait()
    await bot.dispatch_user_message("wx_owner", UserMessage("/stop"), send)
    await state.queue.join()
    assert calls == []



@pytest.mark.asyncio
@pytest.mark.parametrize("command", [" /stop\n", "\t/clear "])
async def test_padded_control_commands_do_not_answer_pending_question(phone, command):
    bot, state, send, replies, calls = phone
    state.question = asyncio.get_running_loop().create_future()
    question = state.question
    await bot.dispatch_user_message("wx_owner", UserMessage(command), send)
    assert question.cancelled()
    assert calls == []
    assert len(replies) == 1
    assert "已停止" in replies[0] if command.strip() == "/stop" else "清空" in replies[0]



@pytest.mark.asyncio
async def test_late_notification_after_stop_cannot_reply(phone):
    import contextvars
    bot, state, send, replies, calls = phone
    contexts = []
    def start(message, history, *, turn_id):
        async def run():
            contexts.append(contextvars.copy_context())
            return "final answer"
        return asyncio.create_task(run())
    state.agent._start_user_turn = start
    await bot.dispatch_user_message("wx_owner", UserMessage("run"), send)
    await state.queue.join()
    await bot.dispatch_user_message("wx_owner", UserMessage("/stop"), send)
    before = list(replies)
    contexts[0].run(bot._notify, "stale notice")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert replies == before



@pytest.mark.asyncio
@pytest.mark.parametrize("command,expected", [("/clear", []), ("结束任务", ["queued"])])
async def test_reset_of_paused_queue_discards_or_transfers_pending_once(phone, command, expected):
    bot, old, send, replies, calls = phone
    old.queue.ready.clear()
    await bot.dispatch_user_message("wx_owner", UserMessage("queued"), send)
    original_agent = old.agent
    original_factory = bot._agent_for_session
    def agent_for(identity):
        state = bot._session(identity)
        state.agent = original_agent
        return original_agent
    bot._agent_for_session = agent_for
    await bot.dispatch_user_message("wx_owner", UserMessage(command), send)
    state = bot._session("wx_owner")
    await state.queue.join()
    assert state is not old
    assert [message.text for message in calls] == expected
    assert old.queue.ready.is_set()
    assert not old.queue.pending



@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["bot", "cli"])
async def test_transport_pause_serializes_and_resumes_original_input_with_queued_attachment(phone, channel, tmp_path, monkeypatch):
    import json
    bot, state, send, replies, calls = phone
    agent, saved, attempts = state.agent, [], []
    class Storage:
        def update(self, **value):
            saved.append(json.loads(json.dumps(value)))
        def retry_pending(self):
            pass
        def read_turn(self, turn_id):
            return []
    async def cancel_children(turn_id):
        pass
    async def durable(operation):
        operation()
    agent._session_file = Storage()
    agent._memory = SimpleNamespace(current=None)
    agent._orchestrator = SimpleNamespace(factory=SimpleNamespace(cancel_turn=cancel_children))
    agent.presentation = SimpleNamespace(update_output=lambda *args: None)
    agent._durable_write = durable
    agent.workspace = agent.toolkit._references.workspace
    agent.has_current_turn = True
    agent._session = state
    agent._handle_turn_error = lambda error: None
    agent.record_control_result = lambda *args, **kwargs: None
    from redlotus.ui.console import AgentCliController
    cli = AgentCliController(agent)
    state.is_first_input = False
    async def publish(history):
        pass
    monkeypatch.setattr(cli, "_publish_context_usage", publish)
    first = tmp_path / "original.txt"
    first.write_bytes(b"saved attachment")
    original_text = "original" if channel == "bot" else '@"' + str(first) + '"'
    def start(message, history, *, turn_id):
        async def run():
            attempts.append((turn_id, message))
            agent._current_turn = dict(task=asyncio.current_task(), message=message, turn_id=turn_id,
                                       mode="single", goal_iteration=0)
            try:
                if message.resume is None and message.text == original_text:
                    await state.pause(agent, reason="transport_error")
                    raise ConnectionError("synthetic disconnect")
                return "resumed final" if message.resume else "queued final"
            finally:
                agent._current_turn = None
        return asyncio.create_task(run())
    agent._start_user_turn = start
    if channel == "bot":
        await bot.dispatch_user_message("wx_owner", UserMessage(original_text, attachments=[
            BinaryContent(b"saved attachment", media_type="text/plain", identifier="original.txt")]), send)
    else:
        await cli.process_line(original_text, state, wait_for_turn=False)
    try:
        await asyncio.wait_for(state.queue.join(), 10)
        assert isinstance(state.paused["request"], dict)
        original_id = state.paused["request"]["id"]
        assert state.paused["request"]["reference_ids"]
        assert saved[-1]["metadata"]["paused_turn"]["turn_id"] == original_id
        queued = tmp_path / "queued.txt"
        queued.write_bytes(b"queued attachment")
        if channel == "bot":
            await bot.dispatch_user_message("wx_owner", UserMessage("queued", attachments=[
                BinaryContent(b"queued attachment", media_type="text/plain", identifier="queued.txt")]), send)
            await asyncio.gather(*tuple(state._preparations))
        else:
            await cli.process_line('@"' + str(queued) + '"', state, wait_for_turn=False)
        queued_row = saved[-1]["metadata"]["paused_turn"]["queued"][0]
        assert queued_row["reference_ids"]
        if channel == "bot":
            await bot.dispatch_user_message("wx_owner", UserMessage("/resume"), send)
        else:
            assert await cli.resume_current_turn(state)
        await asyncio.wait_for(state.queue.join(), 10)
        assert [identity for identity, _ in attempts] == [original_id, original_id, queued_row["id"]]
        assert attempts[1][1].references[0].snapshot.read_bytes() == b"saved attachment"
        assert attempts[2][1].references[0].snapshot.read_bytes() == b"queued attachment"
        if channel == "bot":
            assert replies[-2:] == ["resumed final", "queued final"]
        assert state.paused is None
    finally:
        state.queue.discard()
        await state.queue.cancel(discard=True)



@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/stop", "/clear"])
async def test_control_during_resume_reference_loading_prevents_revival(phone, monkeypatch, command):
    from redlotus.sessions import control
    bot, state, send, replies, calls = phone
    agent, entered, released = state.agent, asyncio.Event(), asyncio.Event()
    state.paused = dict(request=dict(id="original", text="request", goal_mode=False), turn_id="original",
                        reference_ids=[], supplements=[], queued=[], user_inputs=["request"])
    agent._session_file = object()
    agent.workspace = agent.toolkit._references.workspace
    agent.presentation = SimpleNamespace(update_output=lambda *args: None)
    async def load(*args, **kwargs):
        entered.set()
        await released.wait()
        return []
    async def execute(*args):
        calls.append("revived")
    monkeypatch.setattr(control, "load_file_refs", load)
    restoration = asyncio.create_task(state.resume(agent, execute, lambda: None))
    await entered.wait()
    await bot.dispatch_user_message("wx_owner", UserMessage(command), send)
    released.set()
    assert await restoration is False
    assert calls == []
    assert not state.queue.pending
