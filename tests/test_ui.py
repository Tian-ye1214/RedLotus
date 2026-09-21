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
    monkeypatch.setattr(base, "settings", lambda: {"bot": {
        "agent_run_timeout_seconds": 10, "send_reply_timeout_seconds": 1, "reply_max_chars": 4500,
        "session_idle_ttl_seconds": 0, "session_gc_interval_seconds": 10, "question_timeout_seconds": .01,
    }})
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


async def test_channel_wait_uses_configured_deadline(channel_probe):
    import asyncio

    from redlotus.api.base import QueuedTurn
    from redlotus.sessions.control import UserMessage

    p = channel_probe
    state = p.bot._session("wx_fixture")

    async def reply(text):
        p.replies.append(text)

    token = p.bot._agent_ctx.set(("wx_fixture", state, QueuedTurn(UserMessage(text="question"), reply, asyncio.get_running_loop())))
    try:
        assert await asyncio.wait_for(p.bot._ask_user("Choose format"), .3) is None
        assert p.replies == ["Choose format"] and state.question is None
    finally:
        p.bot._agent_ctx.reset(token)
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


@pytest.mark.parametrize("initial", [{}, {"BASE_URL": "https://example.invalid/v1", "API_KEY": "fixture-only"}])
async def test_first_use_enters_wizard_and_cancellation_writes_nothing(tmp_path, initial):
    import json

    from redlotus.api.base import prepare_startup_configuration

    path = tmp_path / "config.json"
    path.write_text(json.dumps(initial), encoding="utf-8")
    before, questions = path.read_bytes(), []

    async def cancel(question, **kwargs):
        questions.append(question)
        return None

    assert not await prepare_startup_configuration(ask=cancel, emit=lambda text: None)
    assert questions and not any("max_context_windows" in q for q in questions)
    assert path.read_bytes() == before and not (tmp_path / "global/config.json").exists()


@pytest.fixture
def startup_values():
    """Independently authored non-sensitive answers, never a copy of owner configuration."""
    return {
        "models": {
            "coordinator": {"name": "openai:fixture", "auto_compress_ratio": .8,
                            "compress_head_turns": 1, "compress_tail_turns": 2},
            "manager": {"name": "openai:fixture", "auto_compress_ratio": .7,
                        "compress_head_turns": 0, "compress_tail_turns": 2},
            "worker": {"name": "openai:fixture", "auto_compress_ratio": .9,
                       "compress_head_turns": 1, "compress_tail_turns": 1},
            "compressor": {"name": "openai:fixture"}, "title": {"name": "openai:fixture"},
        },
        "BASE_URL": "https://example.invalid/v1", "API_KEY": "not-a-real-key",
        "MODEL_HTTP_TIMEOUT": 30, "request_limit": None,
        "agent_run_policy": {"max_concurrent_threads_per_session": 2, "max_command_timeout_seconds": 15, "max_task_retries": 2},
        "lifecycle": {"invocation_history_per_session": 10, "shutdown_grace_seconds": 2},
        "storage": {"project_dir": "data", "sessions_dir": "data/sessions", "project_logs_dir": "data/logs",
                    "references_dir": "data/references", "runtime_dir": "data/runtime", "state_dir": "",
                    "cleanup": {"enabled": False, "execution_cache": False, "session_retention_days": 7, "log_retention_days": 7, "session_log_max_bytes": 4096}},
        "input_limits": {"defaults": {"max_files": 2, "max_file_bytes": 1024}},
        "memory_perception": {"model_role": "worker", "window_turns": 20, "overlap_turns": 3},
        "short_term_memory": {"db_path": "memory", "table_name": "fixture", "turn_token_limit": 200,
                              "turn_chunk_overlap_tokens": 20, "vector_search_limit": 5, "final_top_k": 2,
                              "min_similarity": .4, "use_rerank": False,
                              "index": {"metric": "cosine", "min_rows": 10, "rebuild_every_n_adds": 10,
                                        "rows_per_partition": 4, "dimensions_per_sub_vector": 4}},
        "long_term_memory": {"table_name": "fixture_profile"},
        "model_metadata": {"url": "https://openrouter.ai/api/v1/models", "timeout": 10,
                           "supported_thinking_efforts": ["low", "high"]},
        "rag_service": {"http2": False, "timeout": 10, "embedding_batch_size": 2, "index_batch_size": 2},
    }


@pytest.mark.parametrize("confirm", ["y", "n"])
async def test_first_use_collects_typed_fields_and_confirms_once(tmp_path, startup_values, confirm):
    import json

    from redlotus.api.base import prepare_startup_configuration
    from redlotus.runtime.config import config_value, missing_startup_fields, settings

    questions, notices, invalid = [], [], set()

    async def answer(question, **kwargs):
        questions.append(question)
        if question.startswith("确认将"):
            return confirm
        if question.startswith("现在配置 RAG"):
            return "n"
        path = tuple(question.split("（", 1)[0].split("."))
        if path == ("MODEL_HTTP_TIMEOUT",) and path not in invalid:
            invalid.add(path)
            return "true"  # bool is not a valid numeric timeout.
        if path[:2] == ("models", "title"):
            return "=coordinator"
        value = config_value(startup_values, path)
        return value if isinstance(value, str) else json.dumps(value)

    assert await prepare_startup_configuration(ask=answer, emit=notices.append) == (confirm == "y")
    assert sum(q.startswith("确认将") for q in questions) == 1
    assert not any("max_context_windows" in q for q in questions)
    assert any("MODEL_HTTP_TIMEOUT 无效" in notice for notice in notices)
    if confirm == "y":
        assert not missing_startup_fields(settings())
        assert settings()["models"]["title"] == startup_values["models"]["title"]
        assert settings()["storage"]["state_dir"] == ""
    else:
        assert not (tmp_path / "global/config.json").exists() and not settings()


async def test_model_reuse_preserves_role_policy_and_layered_writes(tmp_path, startup_values):
    import json

    from redlotus.api.base import ConfigurationSetup, prepare_startup_configuration
    from redlotus.runtime.config import settings, get_model_and_params

    global_file = tmp_path / "global/config.json"
    global_file.parent.mkdir()
    startup_values["models"]["reviewer"] = "review"
    startup_values["model_presets"] = {"review": {"name": "openai:review"}}
    global_file.write_text(json.dumps(startup_values), encoding="utf-8")
    local_file = tmp_path / "config.json"
    local_file.write_text("{}", encoding="utf-8")
    (tmp_path / ".env").write_text("MODEL_HTTP_TIMEOUT=45\n", encoding="utf-8")
    before = global_file.read_bytes()
    responses = iter(["=coordinator", "y"])

    async def answer(question, **kwargs):
        return next(responses)

    setup = ConfigurationSetup(answer)
    assert await setup.fill(("models", "manager", "name")) and await setup.commit()
    assert global_file.read_bytes() == before and settings()["MODEL_HTTP_TIMEOUT"] == 45
    assert settings()["models"]["manager"]["auto_compress_ratio"] == .7
    assert json.loads(local_file.read_text()) == {}, "Unchanged inherited model must not be copied to local config"
    assert get_model_and_params("reviewer")[0] == "openai:review"

    async def skip_optional(question, **kwargs):
        assert question.startswith("现在配置 RAG")
        return "n"

    assert await prepare_startup_configuration(ask=skip_optional, emit=lambda text: None)


@pytest.mark.parametrize("channel_name", ["QQ", "WeChat"])
def test_channel_startup_collects_policy_before_constructing_bot(tmp_path, startup_values, monkeypatch, channel_name):
    import json
    from importlib import import_module

    from redlotus.api import base

    (tmp_path / "config.json").write_text(json.dumps(startup_values), encoding="utf-8")
    questions, started = [], []

    async def answer(question, **kwargs):
        questions.append(question)
        if question.startswith("现在配置 RAG"):
            return "n"
        if question.startswith("确认将"):
            return "y"
        return "10"

    monkeypatch.setattr(base, "ask_configuration", answer)
    monkeypatch.setattr(base, "configuration_prompt_available", lambda: True)
    channel = import_module("redlotus.api." + channel_name)
    bot_type = channel.QQBot if channel_name == "QQ" else channel.WeChatAgentBot
    monkeypatch.setattr(bot_type, "__init__", lambda self: base.BotBase.__init__(self))
    monkeypatch.setattr(bot_type, "run", lambda self: started.append(self._policy))
    channel.main(bot_type)
    assert len(started) == 1 and len(started[0]) == 6
    assert sum(question.startswith("bot.") for question in questions) == 6
    assert not any("max_context_windows" in question for question in questions)


@pytest.mark.parametrize("limit", ["12", " +12 ", "1_2"])
@pytest.mark.parametrize("empty", [None, " "])
def test_legacy_numeric_limit_and_empty_model_override(tmp_path, limit, empty):
    import json

    from redlotus.runtime.config import get_agent_usage_limits, get_model_and_params

    global_file = tmp_path / "global/config.json"
    global_file.parent.mkdir()
    global_file.write_text('{"models":{"worker":{"name":"openai:fixture"}}}')
    (tmp_path / "config.json").write_text(json.dumps({
        "request_limit": limit, "models": {"worker": {"name": empty}},
    }))
    assert get_agent_usage_limits().request_limit == 12
    assert get_model_and_params("worker")[0] == "openai:fixture"
