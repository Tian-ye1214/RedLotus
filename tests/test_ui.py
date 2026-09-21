"""Auxiliary UI contracts; product/API acceptance is recorded separately."""

from types import SimpleNamespace

def test_output_actions_follow_the_selected_sink(monkeypatch):
    from redlotus.core import presentation as ui

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
    from redlotus.core.presentation import LegacyOutputSink

    output = StringIO()
    sink = LegacyOutputSink(Console(file=output, width=40, color_system=None))
    sink.emit("\x1b[31m正文\x1b[0m")
    sink.update("rule", "任务")
    assert "正文" in output.getvalue() and "任务" in output.getvalue()
    assert "\x1b[" not in output.getvalue()


async def test_release_ui_has_no_keyboard_diagnostics(tmp_path, monkeypatch):
    from redlotus.core.tui import RedLotusTui

    system = SimpleNamespace(
        new_cli_session_state=lambda: None,
        workspace=SimpleNamespace(root=tmp_path),
        session_key=None,
    )
    # This check mounts the real layout without starting a model session.
    monkeypatch.setattr(RedLotusTui, "on_mount", lambda self: None)
    async with RedLotusTui(system).run_test() as pilot:
        assert not pilot.app.query("#keyboard-test")
        assert pilot.app.query("#input") and pilot.app.query("#session-load")


async def test_composer_consumes_each_enter_once_and_preserves_urgency():
    from textual.app import App, ComposeResult
    from redlotus.core.tui import AgentInput

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
