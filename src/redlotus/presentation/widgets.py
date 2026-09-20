"""Presentation widgets responsibilities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from rich.ansi import AnsiDecoder
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from redlotus.presentation.output import OutputSink
from redlotus.presentation.snapshots import WorkspaceSnapshot
from redlotus.terminal.console import (
    SnapshotAction,
    SnapshotSelection,
    input_completions,
)

READY_LABEL = "就绪"


PREPARING_LABEL = "正在准备会话…"


WORKING_LABEL = "工作中"


WORKING_FRAMES = ("", ".", "..", "...")


class TuiRunMode(str, Enum):
    REVIEW = "review"
    PASS = "pass"
    GOAL = "goal"

    def next(self) -> "TuiRunMode":
        return list(TuiRunMode)[(list(TuiRunMode).index(self) + 1) % len(TuiRunMode)]


class AgentInputSuggester(Suggester):
    async def get_suggestion(self, value: str) -> str | None:
        for completion in input_completions(value):
            candidate = value[: len(value) + completion.start_position] + completion.text
            if candidate != value:
                return candidate
        return None


class AgentInput(Input):
    @dataclass
    class Submitted(Input.Submitted, namespace="input"):
        urgent: bool = False

    BINDINGS = [
        *Input.BINDINGS,
        Binding("tab", "cursor_right", "Complete", show=False),
        Binding("ctrl+enter", "app.submit_urgent", "发送", key_display="Ctrl+Enter"),
    ]

    async def on_key(self, event: events.Key) -> None:
        """Capture submission in the same queue that applies typed characters."""
        if event.key in {"enter", "ctrl+enter"}:
            event.stop()
            event.prevent_default()
            await self.action_submit(urgent=event.key == "ctrl+enter")

    async def action_submit(self, *, urgent=False) -> None:
        """Consume this draft before another key can submit or replace it."""
        if self.disabled:
            return
        if urgent:
            self.post_message(self.Submitted(self, self.value, urgent=True))
        else:
            await super().action_submit()
        self.value = ""


class SnapshotPickScreen(ModalScreen[SnapshotSelection]):
    BINDINGS = [
        Binding("escape", "cancel", "取消", show=False),
        Binding("ctrl+c", "cancel", "取消", show=False),
    ]

    DEFAULT_CSS = """
    SnapshotPickScreen {
        align: center middle;
    }
    #snapshot-dialog {
        width: 90%;
        max-width: 120;
        height: auto;
        max-height: 80%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #snapshot-title {
        text-style: bold;
        margin-bottom: 1;
    }
    .snapshot-hint {
        color: $text-muted;
        margin-bottom: 1;
    }
    #snapshot-list {
        height: auto;
        max-height: 24;
        min-height: 5;
    }
    """

    def __init__(
        self,
        snapshots: list[WorkspaceSnapshot],
        *,
        project_name: str = "",
        current_session_id: str | None = None,
    ) -> None:
        super().__init__()
        self._snapshots = snapshots
        self._project_name = project_name
        self._current_session_id = current_session_id

    def compose(self) -> ComposeResult:
        with Vertical(id="snapshot-dialog"):
            context = "新建会话或恢复原会话"
            if self._project_name:
                session = self._current_session_id or "新会话"
                context = f"项目：{self._project_name} · 当前会话：{session}\n{context}"
            yield Static(context, id="snapshot-title")
            yield Static("↑↓ 选择 · Enter 确认 · Esc 取消", classes="snapshot-hint")
            yield OptionList(
                Option("新建会话", id="new"),
                *[
                    Option(
                        snapshot.label,
                        id=str(index),
                        disabled=not snapshot.is_loadable,
                    )
                    for index, snapshot in enumerate(self._snapshots)
                ],
                Option("取消", id="cancel"),
                id="snapshot-list",
            )

    def on_mount(self) -> None:
        self.query_one("#snapshot-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id
        if option_id == "new":
            self.dismiss(SnapshotSelection(SnapshotAction.NEW))
        elif option_id is None or option_id == "cancel":
            self.dismiss(SnapshotSelection(SnapshotAction.CANCEL))
        else:
            self.dismiss(
                SnapshotSelection(SnapshotAction.RESTORE, self._snapshots[int(option_id)])
            )

    def action_cancel(self) -> None:
        self.dismiss(SnapshotSelection(SnapshotAction.CANCEL))


class TextualOutputSink(OutputSink):
    def __init__(self, app, log: RichLog) -> None:
        self._app = app
        self._log = log
        self._ansi_decoder = AnsiDecoder()

    supports_model_stream = True

    def emit(self, renderable: Any) -> None:
        parts = (
            list(self._ansi_decoder.decode(renderable))
            if isinstance(renderable, str) and "\x1b[" in renderable
            else [renderable]
        )

        def write():
            for part in parts:
                self._log.write(part, scroll_end=True)

        self._app.call_ui(write)

    def update(self, action: str, *args) -> None:
        if action == "rule":
            self.emit(Text(args[0], style="dim"))
        else:
            self._app.call_ui(lambda: getattr(self._app, action)(*args))
