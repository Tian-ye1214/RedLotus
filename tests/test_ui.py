"""Auxiliary UI contracts; product/API acceptance is recorded separately."""

from types import SimpleNamespace

import pytest


def test_output_actions_follow_the_selected_sink(monkeypatch):
    from redlotus.ui import presentation as ui

    received = []
    first = SimpleNamespace(update=lambda *event: received.append(("first", event)))
    second = SimpleNamespace(update=lambda *event: received.append(("second", event)))
    monkeypatch.setattr(ui, "_sink", first)
    dispatch = getattr(ui, "update_output", None)
    assert callable(dispatch), "UI actions must share one dynamic output dispatcher"
    events = [
        ("rule", "任务"),
        ("set_context_usage", [ui.ContextUsageItem("主 Agent", 10, 100, 10)]),
        ("clear_context_usage",),
        ("begin_model_stream", "正在回复"),
        ("append_model_stream_delta", "正文", "text"),
        ("append_model_stream_delta", "思考", "thinking"),
        ("end_model_stream", "已完成"),
        ("clear_model_stream",),
    ]
    for event in events:
        dispatch(*event)
    ui.set_output_sink(second)
    dispatch("clear_model_stream")
    assert received == [("first", event) for event in events] + [
        ("second", ("clear_model_stream",))
    ]


def test_legacy_output_keeps_ansi_and_rule_rendering():
    from io import StringIO

    from rich.console import Console

    from redlotus.ui.presentation import LegacyOutputSink

    output = StringIO()
    sink = LegacyOutputSink(Console(file=output, width=40, color_system=None))
    sink.emit("\x1b[31m正文\x1b[0m")
    sink.update("rule", "任务")
    assert "正文" in output.getvalue() and "任务" in output.getvalue()
    assert "\x1b[" not in output.getvalue()


def test_textual_sink_preserves_ansi_and_dispatches_on_ui_thread():
    from redlotus.ui.widgets import TextualOutputSink

    rendered, updates = [], []
    app = SimpleNamespace(
        call_ui=lambda operation: operation(),
        clear_model_stream=lambda: updates.append("cleared"),
    )
    sink = TextualOutputSink(app, SimpleNamespace(write=lambda text, **kw: rendered.append(text.plain)))
    sink.emit("\x1b[31m正文\x1b[0m")
    sink.update("rule", "任务")
    sink.update("clear_model_stream")
    assert rendered == ["正文", "任务"] and updates == ["cleared"]


async def test_release_ui_has_no_keyboard_diagnostics(tmp_path, monkeypatch):
    from redlotus.ui.tui import RedLotusTui

    system = SimpleNamespace(
        workspace=SimpleNamespace(root=tmp_path),
        session_key=None,
    )
    # This check mounts the real layout without starting a model session.
    monkeypatch.setattr(RedLotusTui, "on_mount", lambda self: None)
    controller = SimpleNamespace(system=system, new_session_state=lambda: None)
    async with RedLotusTui(controller).run_test() as pilot:
        assert not pilot.app.query("#keyboard-test")
        assert pilot.app.query("#input") and pilot.app.query("#session-load")


async def test_composer_consumes_each_enter_once_and_preserves_urgency():
    from textual.app import App, ComposeResult

    from redlotus.ui.tui import AgentInput

    received = []

    class InputProbe(App):
        def compose(self) -> ComposeResult:
            yield AgentInput(id="draft")

        def on_input_submitted(self, message):
            received.append((message.value, message.urgent))

    async with InputProbe().run_test() as pilot:
        await pilot.press("a", "enter", "b", "ctrl+enter")
        assert received == [("a", False), ("b", True)]
        assert pilot.app.query_one(AgentInput).value == ""


@pytest.fixture
def channel_probe(tmp_path, monkeypatch):
    from contextlib import nullcontext

    from redlotus.api import base
    from redlotus.api.WeChat import WeChatAgentBot
    from redlotus.core import system
    from redlotus.runtime.resources import WorkspaceContext
    from redlotus.sessions.control import SessionController

    calls, replies = [], []

    class Agent:
        def __init__(self, *, presentation, owner_memory_allowed=True, input_controller=None):
            self._session = input_controller or SessionController()
            self.workspace = WorkspaceContext.from_path(tmp_path)
            self.session_key = None
            self.toolkit = SimpleNamespace(set_task_directory=lambda title: None)

        def set_ask_user_handler(self, handler):
            self.handler = handler

        async def bind_session(self, identity):
            self.session_key = identity

        async def run_agent_system(self, message, history, **kwargs):
            calls.append((message.text, len(message.attachments), kwargs.get("turn_id")))
            return history, "done:" + message.text

        async def shutdown(self):
            self._session.reset(discard=True)

        async def stop_current_turn(self):
            self._session.reset()
            await self._session.queue.cancel()

    monkeypatch.setattr(system, "AgentSystem", Agent)
    monkeypatch.setattr(base.app_config, "reload_config", lambda: None)
    monkeypatch.setattr(base.app_config, "missing_main_api_keys", lambda: [])
    monkeypatch.setattr(base, "settings", lambda: {})
    monkeypatch.setattr(base, "get_env", lambda *args, **kwargs: None)
    monkeypatch.setattr(base.logger, "session_log_context", lambda *args: nullcontext())
    monkeypatch.setattr(base.logger, "error", lambda *args: None)
    monkeypatch.setattr(base.logger, "debug", lambda *args: None)
    monkeypatch.setattr(base.logger, "warning", lambda *args: None)
    bot = WeChatAgentBot()
    monkeypatch.setattr(bot, "_ensure_session_gc", lambda: None)

    async def reply(message, text):
        replies.append(text)

    return SimpleNamespace(bot=bot, calls=calls, replies=replies, reply=reply)


def channel_message(text, *names):
    from dataclasses import field, make_dataclass

    # The public IncomingMessage media fields from the locked SDK version.
    message = make_dataclass("Message", [("user_id", str), ("text", str)] +
                            [(kind, list, field(default_factory=list)) for kind in ("images", "files", "videos", "voices")])
    return message("fixture", text, files=[SimpleNamespace(file_name=name) for name in names])


@pytest.mark.parametrize("command, expected", [(None, ["first", "second"]),
                                               ("/stop", ["second"]), ("/clear", ["fresh"]),
                                               ("结束任务", ["second", "fresh"])])
async def test_slow_channel_attachment_respects_queue_stop_and_clear(channel_probe, command, expected):
    import asyncio

    p = channel_probe
    started, release = asyncio.Event(), asyncio.Event()

    async def download(message):
        if message.files:
            started.set()
            await release.wait()
            return SimpleNamespace(data=b"fixture", type="file", file_name="first.txt")

    sdk = SimpleNamespace(download=download, reply=p.reply)
    slow = asyncio.create_task(p.bot._handle_message(sdk, channel_message("first", "first.txt")))
    await asyncio.wait_for(started.wait(), 1)
    await p.bot._handle_message(sdk, channel_message("second"))
    for _ in range(10):
        await asyncio.sleep(0)
    assert not p.calls, "Later text overtook an admitted attachment"
    if command:
        await p.bot._handle_message(sdk, channel_message(command))
        if command != "/stop":
            await p.bot._handle_message(sdk, channel_message("fresh"))
    release.set()
    await slow
    state = p.bot._sessions["wx_fixture"]
    await asyncio.wait_for(state.inputs.queue.join(), 1)
    assert [row[0] for row in p.calls] == expected
    assert len({row[2] for row in p.calls}) == len(p.calls) and all(row[2] for row in p.calls)
    assert state.agent._session is state.inputs
    await p.bot.release_all_resources_async()


@pytest.mark.parametrize("names, missing", [(('missing.txt',), False),
                                          (('first.txt', 'missing.txt'), False), (('missing.txt',), True)])
async def test_channel_attachment_failure_never_executes_partial_request(channel_probe, names, missing):
    import asyncio

    p = channel_probe

    async def download(message):
        name = message.files[0].file_name
        if name == "missing.txt":
            if missing:
                return None
            raise OSError("download unavailable")
        return SimpleNamespace(data=b"fixture", type="file", file_name=name)

    sdk = SimpleNamespace(download=download, reply=p.reply)
    await p.bot._handle_message(sdk, channel_message("", *names))
    state = p.bot._sessions["wx_fixture"]
    await asyncio.wait_for(state.inputs.queue.join(), 1)
    assert not p.calls
    assert any("missing.txt" in reply and "输入" in reply for reply in p.replies)
    await p.bot.release_all_resources_async()


async def test_qq_admits_before_downloading(channel_probe, monkeypatch):
    import asyncio

    from pydantic_ai import BinaryContent

    from redlotus.api import base
    from redlotus.api.QQ import QQBot

    p = channel_probe
    bot = object.__new__(QQBot)
    base.BotBase.__init__(bot)
    started, release = asyncio.Event(), asyncio.Event()

    async def attachments(event):
        if event.raw_message == "first":
            started.set()
            await release.wait()
            return [BinaryContent(data=b"fixture", media_type="text/plain", identifier="first.txt")]
        return []

    async def reply(event, text):
        p.replies.append(text)

    monkeypatch.setattr(bot, "_extract_attachments", attachments)
    monkeypatch.setattr(bot, "_reply_event", reply)
    monkeypatch.setattr(bot, "_is_at_me", lambda event: True)
    monkeypatch.setattr(bot, "_session_id", lambda event: "private_fixture")
    monkeypatch.setattr(bot, "_ensure_session_gc", lambda: None)
    slow = asyncio.create_task(bot._handle_message(SimpleNamespace(
        raw_message="first", message=[{"type": "file", "data": {"file": "first.txt", "file_id": "fixture"}}],
    )))
    await asyncio.wait_for(started.wait(), 1)
    await bot._handle_message(SimpleNamespace(raw_message="second"))
    for _ in range(10):
        await asyncio.sleep(0)
    assert not p.calls, "QQ text overtook the first attachment"
    release.set()
    await slow
    await asyncio.wait_for(bot._sessions["private_fixture"].inputs.queue.join(), 1)
    assert [(text, count) for text, count, _ in p.calls] == [("first", 1), ("second", 0)]
    state = bot._sessions["private_fixture"]
    state.question = asyncio.get_running_loop().create_future()
    await bot._handle_message(SimpleNamespace(raw_message="answer", message=[]))
    assert state.question.done() and state.question.result() == "answer"
    assert state.inputs.user_inputs == ["answer"]
    await bot.release_all_resources_async()


def test_qq_second_download_failure_is_not_a_partial_image_request(monkeypatch):
    import httpx

    from redlotus.api import qq_media_helpers as media

    original = httpx.Client
    client = lambda **kwargs: original(transport=httpx.MockTransport(
        lambda request: httpx.Response(404 if request.url.path.endswith("missing.png") else 200,
                                      content=b"fixture", headers={"content-type": "image/png"})
    ), **kwargs)
    monkeypatch.setattr(media.httpx, "Client", client)
    monkeypatch.setattr(media, "_resolve_public_addr", lambda host: "203.0.113.10")
    monkeypatch.setattr(media.ModelInputPolicy, "for_role", lambda: SimpleNamespace(check=lambda sizes: None))
    event = SimpleNamespace(message=[{"type": "image", "data": {"url": "https://example.com/" + name}}
                                    for name in ("first.png", "missing.png")], raw_message="")
    with pytest.raises(ValueError, match="image\\[2\\]"):
        media.extract_image_video(event)
