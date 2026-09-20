
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from pydantic_ai import BinaryContent

import redlotus.api.base as _api_base
import redlotus.api.qq_media_helpers as _api_qq_media_helpers
import redlotus.runtime.config as configuration
import redlotus.runtime.resources as resources
from redlotus.api.base import BotBase
from redlotus.api.QQ import QQBot
from redlotus.api.WeChat import WeChatAgentBot
from redlotus.documents.interaction import UserMessage


class FakeAgent:
    instances = []
    on_run = None

    def __init__(self, *, owner_memory_allowed, session_controller):
        self.owner_memory_allowed = owner_memory_allowed
        self._session = session_controller
        self.session_key = None
        self.ask = None
        self.runs = []
        self.stopped = False
        self.shutdown_called = False
        type(self).instances.append(self)

    def set_ask_user_handler(self, handler):
        self.ask = handler

    def set_task_directory(self, _):
        pass

    async def bind_session(self, identity):
        self.session_key = identity

    async def run_agent_system(self, message, history, *, turn_id, **_):
        self.supplied_history = list(history.messages)
        async with self._session.turn(message.text, turn_id=turn_id):
            self.runs.append((message, turn_id))
            if type(self).on_run:
                return history, await type(self).on_run(self, message)
            return history, f"done:{message.text}"

    async def stop_current_turn(self):
        self.stopped = True
        self._session.reset()
        await self._session.queue.cancel()

    async def shutdown(self):
        self.shutdown_called = True


class DemoBot(BotBase):
    platform_tag = "QQ"
    session_prefix = "private_"
    SESSION_IDLE_TTL_S = 0


@pytest.fixture
def channel_runtime(monkeypatch):
    FakeAgent.instances = []
    FakeAgent.on_run = None
    monkeypatch.setattr(_api_base, "AgentSystem", FakeAgent)
    monkeypatch.setattr(configuration, "reload_config", lambda: None)
    monkeypatch.setattr(configuration, "missing_main_api_keys", lambda: [])
    monkeypatch.setattr(_api_base, "close_all_clients", lambda: asyncio.sleep(0))
    monkeypatch.setattr(resources, "session_log_context", lambda _: nullcontext())
    monkeypatch.setattr(resources, "error", lambda *args, **kwargs: None)


def reply_collector():
    replies = []

    async def send(text):
        replies.append(text)

    return replies, send


async def prepared(text, attachments=None):
    return UserMessage(text, attachments or [], original_text=text)


@pytest.mark.asyncio
async def test_slow_attachment_keeps_arrival_fifo_and_one_input_id(channel_runtime):
    bot = DemoBot()
    replies, send = reply_collector()
    release = asyncio.Event()

    async def slow():
        await release.wait()
        return await prepared("slow", [BinaryContent(data=b"x", media_type="image/png")])

    first = await bot.dispatch_event("private_1", "slow", slow, send, retry_context="first")
    second = await bot.dispatch_event(
        "private_1", "fast", lambda: prepared("fast"), send, retry_context="second"
    )
    await asyncio.sleep(0)
    assert not FakeAgent.instances

    release.set()
    await bot._sessions["private_1"].controller.queue.join()

    agent = FakeAgent.instances[0]
    assert [message.text for message, _ in agent.runs] == ["slow", "fast"]
    assert [identity for _, identity in agent.runs] == [first.id, second.id]
    assert agent._session is bot._sessions["private_1"].controller


@pytest.mark.asyncio
async def test_clear_invalidates_late_attachment_and_does_not_execute(channel_runtime):
    bot = DemoBot()
    replies, send = reply_collector()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow():
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    await bot.dispatch_event("private_1", "old", slow, send, retry_context="old-event")
    await started.wait()
    await bot.dispatch_event(
        "private_1", "/clear", lambda: prepared("/clear"), send, retry_context=None
    )
    await asyncio.wait_for(cancelled.wait(), 1)

    assert not FakeAgent.instances
    assert bot._sessions["private_1"].last_rejected is None


@pytest.mark.asyncio
async def test_stop_cancels_current_but_keeps_queued_ordinary_turn(channel_runtime):
    bot = DemoBot()
    replies, send = reply_collector()
    started = asyncio.Event()

    async def run(agent, message):
        if message.text == "first":
            started.set()
            await asyncio.Future()
        return f"done:{message.text}"

    FakeAgent.on_run = run
    first = await bot.dispatch_event(
        "private_1", "first", lambda: prepared("first"), send, retry_context=1
    )
    await started.wait()
    second = await bot.dispatch_event(
        "private_1", "second", lambda: prepared("second"), send, retry_context=2
    )
    await bot.dispatch_event("private_1", "/stop", lambda: prepared("/stop"), send, retry_context=None)
    await bot._sessions["private_1"].controller.queue.join()

    assert FakeAgent.instances[0].stopped
    assert [(m.text, identity) for m, identity in FakeAgent.instances[0].runs] == [
        ("first", first.id),
        ("second", second.id),
    ]


@pytest.mark.asyncio
async def test_stop_cancels_urgent_answer_preparation(channel_runtime):
    bot = DemoBot()
    replies, send = reply_collector()
    question_sent = asyncio.Event()
    preparation_started = asyncio.Event()
    preparation_cancelled = asyncio.Event()

    async def run(agent, _message):
        pending = asyncio.create_task(agent.ask("question?"))
        while bot._sessions["private_1"].question is None:
            await asyncio.sleep(0)
        question_sent.set()
        await pending
        return "done"

    async def slow_answer():
        preparation_started.set()
        try:
            await asyncio.Future()
        finally:
            preparation_cancelled.set()

    FakeAgent.on_run = run
    await bot.dispatch_event("private_1", "ask", lambda: prepared("ask"), send, retry_context=1)
    await question_sent.wait()
    await bot.dispatch_event("private_1", "answer", slow_answer, send, retry_context=2)
    await preparation_started.wait()
    await bot.dispatch_event("private_1", "/stop", lambda: prepared("/stop"), send)

    await asyncio.wait_for(preparation_cancelled.wait(), 1)


@pytest.mark.asyncio
async def test_end_task_rehomes_pending_turn_in_order(channel_runtime):
    bot = DemoBot()
    replies, send = reply_collector()
    started = asyncio.Event()

    async def run(_agent, message):
        if message.text == "first":
            started.set()
            await asyncio.Future()
        return f"done:{message.text}"

    FakeAgent.on_run = run
    await bot.dispatch_event("private_1", "first", lambda: prepared("first"), send, retry_context=1)
    await started.wait()
    second = await bot.dispatch_event(
        "private_1", "second", lambda: prepared("second"), send, retry_context=2
    )
    await bot.dispatch_event(
        "private_1", "结束任务", lambda: prepared("结束任务"), send, retry_context=None
    )
    await bot._sessions["private_1"].controller.queue.join()

    assert FakeAgent.instances[-1].runs[0][0].text == "second"
    assert FakeAgent.instances[-1].runs[0][1] == second.id


@pytest.mark.asyncio
async def test_question_only_consumes_input_admitted_for_that_question(channel_runtime):
    bot = DemoBot()
    replies, send = reply_collector()
    release_queued = asyncio.Event()
    release_ask = asyncio.Event()
    question_sent = asyncio.Event()
    answer_received = asyncio.Event()
    answer_value = None

    async def delayed_ordinary():
        await release_queued.wait()
        return await prepared("queued-before-question")

    async def delayed_ask():
        await release_ask.wait()
        return await prepared("ask")

    async def run(agent, message):
        nonlocal answer_value
        if message.text == "ask":
            pending = asyncio.create_task(agent.ask("question?"))
            while bot._sessions["private_1"].question is None:
                await asyncio.sleep(0)
            question_sent.set()
            answer_value = await pending
            answer_received.set()
        return "done"

    FakeAgent.on_run = run
    await bot.dispatch_event("private_1", "ask", delayed_ask, send, retry_context=1)
    queued = await bot.dispatch_event(
        "private_1", "queued-before-question", delayed_ordinary, send, retry_context=2
    )
    release_ask.set()
    await question_sent.wait()
    answer = await bot.dispatch_event(
        "private_1", "answer", lambda: prepared("answer"), send, retry_context=3
    )
    await answer_received.wait()

    assert str(answer_value) == "answer"
    assert answer_value.input_id == answer.id
    release_queued.set()
    await bot._sessions["private_1"].controller.queue.join()
    assert FakeAgent.instances[0].runs[1][1] == queued.id


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "caption"])
async def test_question_rejects_media_answer_and_keeps_question_pending(
    channel_runtime, text
):
    bot = DemoBot()
    replies, send = reply_collector()
    question_sent = asyncio.Event()
    answer_received = asyncio.Event()
    answer_value = None
    original_event = object()

    async def run(agent, _message):
        nonlocal answer_value
        pending = asyncio.create_task(agent.ask("question?"))
        while bot._sessions["private_1"].question is None:
            await asyncio.sleep(0)
        question_sent.set()
        answer_value = await pending
        answer_received.set()
        return "done"

    async def media_answer():
        return UserMessage(
            text,
            [BinaryContent(
                data=b"image", media_type="image/png", identifier="photo.png"
            )],
            original_text=text,
        )

    FakeAgent.on_run = run
    await bot.dispatch_event("private_1", "ask", lambda: prepared("ask"), send)
    await question_sent.wait()
    await bot.dispatch_event(
        "private_1", text, media_answer, send, retry_context=original_event
    )
    for _ in range(100):
        if bot._sessions["private_1"].last_rejected is original_event:
            break
        await asyncio.sleep(0)

    assert bot._sessions["private_1"].last_rejected is original_event
    assert bot._sessions["private_1"].question is not None
    assert not answer_received.is_set()
    assert "photo.png" in replies[-1]
    assert "question replies do not support attachments" in replies[-1]

    await bot.dispatch_event(
        "private_1", "text answer", lambda: prepared("text answer"), send
    )
    await answer_received.wait()
    assert str(answer_value) == "text answer"


@pytest.mark.asyncio
async def test_attachment_failure_reports_identity_and_keeps_retry_context(channel_runtime):
    bot = DemoBot()
    replies, send = reply_collector()
    original_event = object()

    async def broken():
        raise _api_base.AttachmentError("report.pdf", "network timeout")

    await bot.dispatch_event(
        "private_1", "summarize", broken, send, retry_context=original_event
    )
    await bot._sessions["private_1"].controller.queue.join()

    assert not FakeAgent.instances
    assert "report.pdf" in replies[-1]
    assert "network timeout" in replies[-1]
    assert bot._sessions["private_1"].last_rejected is original_event


@pytest.mark.asyncio
async def test_qq_handler_admits_before_downloading(channel_runtime, monkeypatch):
    bot = QQBot.__new__(QQBot)
    BotBase.__init__(bot)
    bot._bot_client = SimpleNamespace(api=object())
    monkeypatch.setattr(bot, "_is_at_me", lambda event: True)
    monkeypatch.setattr(bot, "_session_id", lambda event: "private_qq")

    async def extract(_event):
        assert bot._sessions["private_qq"].controller.generation == (0, 0)
        return []

    monkeypatch.setattr(bot, "_extract_attachments", extract)
    async def reply(**_):
        pass

    event = SimpleNamespace(raw_message="hello", reply=reply)

    await bot._handle_message(event)
    await bot._sessions["private_qq"].controller.queue.join()
    assert FakeAgent.instances[0].runs[0][0].text == "hello"


@pytest.mark.asyncio
async def test_wechat_handler_admits_before_downloading(channel_runtime):
    bot = WeChatAgentBot()

    class FakeWechat:
        async def download(self, _msg):
            assert bot._sessions["wx_user"].controller.generation == (0, 0)
            return None

        async def reply(self, _msg, _text):
            pass

    msg = SimpleNamespace(user_id="user", text="hello", type="text", id="m1")
    await bot._handle_message(FakeWechat(), msg)
    await bot._sessions["wx_user"].controller.queue.join()
    assert FakeAgent.instances[0].runs[0][0].text == "hello"


@pytest.mark.asyncio
async def test_revoked_owner_permission_rebuilds_agent_for_next_turn(
    channel_runtime, monkeypatch
):
    bot = DemoBot()
    replies, send = reply_collector()
    allowed = True
    monkeypatch.setattr(bot, "_is_owner_session", lambda _: allowed)

    await bot.dispatch_event("private_1", "one", lambda: prepared("one"), send, retry_context=1)
    await bot._sessions["private_1"].controller.queue.join()
    allowed = False
    await bot.dispatch_event("private_1", "two", lambda: prepared("two"), send, retry_context=2)
    await bot._sessions["private_1"].controller.queue.join()

    assert [agent.owner_memory_allowed for agent in FakeAgent.instances] == [True, False]
    await bot.release_all_resources_async()
    assert FakeAgent.instances[0].shutdown_called


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", [True, False])
async def test_permission_boundary_discards_old_prompts_and_tool_results_in_fifo(channel_runtime, monkeypatch, initial):
    from pydantic_ai.messages import ModelRequest, ToolReturnPart
    from redlotus.prompts.prompt import session_prompt_from_history

    bot = DemoBot()
    _, send = reply_collector()
    allowed = initial
    monkeypatch.setattr(bot, "_is_owner_session", lambda _: allowed)
    await bot.dispatch_user_message("private_1", UserMessage("before"), send)
    state = bot._sessions["private_1"]
    await state.controller.queue.join()
    state.history.set_messages([ModelRequest(
        [ToolReturnPart("search_memory", "PRIVATE-TOOL-SENTINEL", tool_call_id="old")],
        instructions="PRIVATE-PROMPT-SENTINEL",
    )])
    state.history.compress_summary_state = "PRIVATE-SUMMARY-SENTINEL"
    old_session = state.agent.session_key
    allowed = not initial
    release = asyncio.Event()
    async def slow():
        await release.wait()
        return UserMessage("after-one")
    first = await bot.dispatch_event("private_1", "after-one", slow, send)
    second = await bot.dispatch_user_message("private_1", UserMessage("after-two"), send)
    release.set()
    state = bot._sessions["private_1"]
    await state.controller.queue.join()
    agent = state.agent
    assert agent.session_key != old_session
    assert "PRIVATE-" not in repr(agent.supplied_history)
    assert "PRIVATE-" not in (session_prompt_from_history(agent.supplied_history) or "")
    assert state.history.compress_summary_state is None
    assert [identity for _, identity in agent.runs] == [first.id, second.id]
    await bot.release_all_resources_async()


@pytest.mark.asyncio
async def test_owner_revocation_during_question_cancels_old_agent_and_keeps_fifo(channel_runtime, monkeypatch):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    bot = DemoBot()
    _, send = reply_collector()
    allowed = True
    monkeypatch.setattr(bot, "_is_owner_session", lambda _: allowed)
    asking = asyncio.Event()
    resumed = []
    async def run(agent, message):
        if message.text == "ask":
            state = bot._sessions["private_1"]
            state.history.set_messages([ModelRequest([UserPromptPart("PRIVATE-SENTINEL")], instructions="PRIVATE-SENTINEL")])
            pending = asyncio.create_task(agent.ask("question?"))
            while state.question is None:
                await asyncio.sleep(0)
            asking.set()
            resumed.append(await pending)
        return "done"
    FakeAgent.on_run = run
    release_ask = asyncio.Event()
    async def prepare_ask():
        await release_ask.wait()
        return UserMessage("ask")
    await bot.dispatch_event("private_1", "ask", prepare_ask, send)
    queued = await bot.dispatch_user_message("private_1", UserMessage("queued"), send)
    release_ask.set()
    await asking.wait()
    allowed = False
    answer = await bot.dispatch_user_message("private_1", UserMessage("answer-after-revoke"), send)
    following = await bot.dispatch_user_message("private_1", UserMessage("following"), send)
    await bot._sessions["private_1"].controller.queue.join()
    assert not resumed
    agent = FakeAgent.instances[-1]
    assert not agent.owner_memory_allowed
    assert "PRIVATE-SENTINEL" not in repr(agent.supplied_history)
    assert [identity for _, identity in agent.runs] == [queued.id, answer.id, following.id]
    await bot.release_all_resources_async()


@pytest.mark.asyncio
async def test_qq_media_batch_fails_instead_of_returning_partial(monkeypatch):
    event = SimpleNamespace(
        message=[
            {"type": "file", "data": {"file_id": "one", "file": "one.pdf"}},
            {"type": "file", "data": {"file_id": "two", "file": "two.pdf"}},
        ],
        raw_message="",
    )

    async def download(_api, _event, file_id, filename, _allow):
        if file_id == "two":
            raise _api_base.AttachmentError(filename, "download failed")
        return BinaryContent(data=b"ok", media_type="application/pdf", identifier=filename)

    monkeypatch.setattr(_api_qq_media_helpers, "file_id_to_binary", download)
    with pytest.raises(_api_base.AttachmentError, match="two.pdf.*download failed"):
        await _api_qq_media_helpers.extract_media(object(), event, frozenset({".pdf"}))


def test_qq_invalid_base64_attachment_is_rejected():
    with pytest.raises(_api_base.AttachmentError, match="base64 image"):
        _api_qq_media_helpers.binary_b64("base64://***")
