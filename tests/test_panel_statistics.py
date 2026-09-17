"""Panel counts distinguish new content, billable requests, and live Agents."""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.usage import RequestUsage
from textual.app import App
from textual.widgets import ProgressBar, Sparkline, Static

from redlotus.core.agents import AgentRegistry, SubagentFactory, SubagentSpec, WorkspaceContext
from redlotus.core.history import ChatHistory
from redlotus.core.presentation import build_panel_snapshot, _collect_runtime
from redlotus.core.session import SessionController, SessionFile
from redlotus.core.tui import RedLotusTui
from redlotus.tools.interaction import TaskManager, UserMessage
from redlotus.tools.references import ReferenceFile, ReferencePart


class ChartApp(App):
    """Mount the actual panel widgets without launching a model or the main CLI."""

    _update_token_trend = RedLotusTui._update_token_trend
    _update_content_chart = RedLotusTui._update_content_chart
    _update_api_usage = RedLotusTui._update_api_usage
    _update_agent_counts = RedLotusTui._update_agent_counts

    def compose(self):
        yield Sparkline(id="panel-trend")
        yield Static(id="panel-trend-note")
        yield Static(id="panel-content-note")
        yield Static(id="panel-api-usage")
        yield Static(id="panel-agent-counts")
        for category in ("input", "output", "reasoning"):
            yield ProgressBar(id="panel-comp-" + category)
            yield Static(id="panel-value-" + category)
        yield ProgressBar(id="panel-task-progress")
        yield Static(id="panel-task-counts")


async def snapshot(root, values=()):
    for i, value in enumerate(values):
        session = SessionFile.create(root, "project", session_id=str(i))
        response = ModelResponse(parts=[TextPart("answer")], usage=RequestUsage(input_tokens=value))
        session.save_context([response], turn_id=str(i))
    return await build_panel_snapshot(log_root=root)


@pytest.mark.parametrize("values, visible, note", [
    ([], False, "暂无"), ([7292], False, "7,292"),
    ([0, 0], False, "无变化"), ([35, 35], False, "无变化"),
    ([10, 35], True, ""),
])
async def test_trend_requires_multiple_distinct_values(tmp_path, values, visible, note):
    data = await snapshot(tmp_path, values)
    app = ChartApp()
    async with app.run_test():
        RedLotusTui._update_panel_charts(app, data)
        assert app.query_one("#panel-trend").display is visible
        if note:
            assert note in str(app.query_one("#panel-trend-note").render())
        assert not app.query_one("#panel-task-progress").display
        assert "暂无计划任务" in str(app.query_one("#panel-task-counts").render())


async def test_running_counts_main_and_started_children_not_waiters(tmp_path):
    factory = SubagentFactory(max_concurrent=1)
    controller = SessionController()
    system = SimpleNamespace(session_key="session", _session=controller, _factory=factory,
                             registry=AgentRegistry(), _task_manager=TaskManager())
    workspace = WorkspaceContext.from_path(tmp_path)
    release, started = threading.Event(), threading.Event()

    async def child():
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)

    try:
        async with controller.turn("work"):
            first = factory.start_background(SubagentSpec("session", "turn", workspace), child)
            factory.start_background(SubagentSpec("session", "turn", workspace), child)
            async with asyncio.timeout(3):
                while not started.is_set():
                    await asyncio.sleep(0.01)
            runtime = await _collect_runtime(system, ChatHistory(), ChatHistory())
            assert runtime.running_agents == 2
            assert runtime.queued_agents == 1
            assert runtime.tasks.total == 0
            release.set()
            await first.result()
            await asyncio.gather(*factory._dispatches.values())
        runtime = await _collect_runtime(system, ChatHistory(), ChatHistory())
        assert runtime.running_agents == runtime.queued_agents == 0
    finally:
        release.set()
        await factory.close()


def reference(tmp_path, digest="a"):
    return ReferenceFile(id=digest, project_id="project", name="test.txt", source="test.txt",
                         media_type="text/plain", byte_size=6, sha256=digest,
                         snapshot=tmp_path / "test.txt", parts=[ReferencePart.from_text("中文")])


async def test_new_input_is_idempotent_and_survives_compaction(tmp_path):
    session = SessionFile.create(tmp_path, "project")
    prompt = UserMessage("你好", references=[reference(tmp_path)])
    session.record_input("first", prompt)
    response = ModelResponse(parts=[TextPart("a")], usage=RequestUsage(
        input_tokens=1000, output_tokens=30, details={"reasoning_tokens": 20}))
    session.save_context([response], turn_id="first")
    session.record_input("first", prompt)
    session.save_context([response], turn_id="first")
    session.record_input("urgent", prompt)
    session.record_input("changed-file", UserMessage("", references=[reference(tmp_path, "b")]))
    session.save_context([], turn_id="first")
    session.compact(keep_turn_ids=set())
    restored = SessionFile.load(session.path)
    restored.record_input("urgent", prompt)
    data = await build_panel_snapshot(log_root=tmp_path)
    assert data.content.input_tokens == 8
    assert data.content.output_tokens == 30
    assert data.content.reasoning_tokens == 20
    assert data.history.input_tokens == 1000
    assert data.history.responses == 1
    assert data.content.incomplete_sessions == 0
    assert len(list(session.path.parent.glob("*.json"))) == 1


async def test_old_usage_and_unknown_reasoning_are_not_presented_as_complete(tmp_path):
    await snapshot(tmp_path, [100])
    data = await build_panel_snapshot(log_root=tmp_path)
    assert data.content.incomplete_sessions == 1
    assert data.content.missing_reasoning_responses == 1
    app = ChartApp()
    async with app.run_test():
        RedLotusTui._update_panel_charts(app, data)
        assert not app.query_one("#panel-comp-input").display
        assert "不完整" in str(app.query_one("#panel-content-note").render())
        assert "未知" in str(app.query_one("#panel-value-reasoning").render())
        assert "100" in str(app.query_one("#panel-api-usage").render())


async def test_input_values_and_unmetered_images_are_explicit(tmp_path):
    from pydantic_ai import BinaryContent

    session = SessionFile.create(tmp_path, "project")
    message = UserMessage("中文", attachments=[BinaryContent(data=b"image", media_type="image/png")])
    session.record_input("one", message)
    session.record_input("one", message)
    data = await build_panel_snapshot(log_root=tmp_path)
    assert data.content.input_tokens == 2
    assert data.content.unmetered_attachments == 1
    app = ChartApp()
    async with app.run_test():
        RedLotusTui._update_panel_charts(app, data)
        assert "2" in str(app.query_one("#panel-value-input").render())
        assert "未计量附件 1" in str(app.query_one("#panel-content-note").render())


async def test_new_input_only_enters_accounting_when_consumed(tmp_path, monkeypatch):
    from test_system import configured_system

    system = configured_system(tmp_path, monkeypatch)
    try:
        async with system._outer_turn(UserMessage("开始"), "normal"):
            await system.add_urgent_message(UserMessage("补充"))
            assert system._session_file.input_usage()["input_tokens"] == 2
            await system._take_inner_inputs()
            assert system._session_file.input_usage()["input_tokens"] == 4
            await system._take_inner_inputs()
            assert system._session_file.input_usage()["input_tokens"] == 4
            await system.add_urgent_message(UserMessage("不消费"))
        assert system._session_file.input_usage()["input_tokens"] == 4
    finally:
        await system.shutdown()


async def test_background_and_other_sessions_do_not_inflate_agent_count(tmp_path):
    factory = SubagentFactory()
    workspace = WorkspaceContext.from_path(tmp_path)
    system = SimpleNamespace(session_key="current", _session=SessionController(),
                             _factory=factory, _task_manager=TaskManager())

    async def waiting():
        await asyncio.sleep(20)

    try:
        factory.start_background(SubagentSpec("other", None, workspace), waiting)
        factory.start_background(SubagentSpec("current", None, workspace, role="perception"), waiting)
        runtime = await _collect_runtime(system, None, None)
        assert runtime.running_agents == runtime.queued_agents == 0
    finally:
        await factory.close()


async def test_unreadable_activity_is_unknown_not_idle():
    class BrokenFactory:
        def activity(self, _):
            raise RuntimeError("cannot collect status")

    runtime = await _collect_runtime(SimpleNamespace(session_key="s", _factory=BrokenFactory()), None, None)
    assert runtime.active_invocations_error


async def test_user_answer_counts_once_when_returned_to_agent(tmp_path, monkeypatch):
    from test_system import configured_system

    system = configured_system(tmp_path, monkeypatch)

    async def answer(_):
        return "同意"

    system.set_ask_user_handler(answer)
    try:
        async with system._outer_turn(UserMessage("开始"), "normal"):
            assert await system.ask_user("可以吗") == "同意"
            assert system._session_file.input_usage()["input_tokens"] == 4
    finally:
        await system.shutdown()
