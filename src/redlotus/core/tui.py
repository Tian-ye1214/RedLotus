from __future__ import annotations

import asyncio
import threading
from enum import Enum
from typing import Any

from textual.binding import Binding
from rich.ansi import AnsiDecoder
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import (
    Button,
    Footer,
    Input,
    Label,
    OptionList,
    ProgressBar,
    RichLog,
    Sparkline,
    Static,
)
from textual.widgets.option_list import Option

from redlotus.core.console import (
    SnapshotAction,
    SnapshotSelection,
    input_completions,
    visible_conversation_entries,
)
from redlotus.core.presentation import (
    ContextUsageItem,
    OutputSink,
    set_output_sink,
    context_usage_renderable,
    model_stream_visible_text,
    render_review_hunk,
    user_text_panel,
    PanelSnapshotCache,
    build_panel_snapshot,
    render_panel,
)
from redlotus.core import config as logger
from redlotus.core.agents import current_short_agent_id
from redlotus.core.cli_commands import WorkspaceSnapshot

READY_LABEL = "就绪"
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
    BINDINGS = [
        *Input.BINDINGS,
        Binding("tab", "cursor_right", "Complete", show=False),
        # Traditional terminals encode Ctrl+Enter as LF (Textual's ctrl+j).
        Binding(
            "ctrl+enter,ctrl+j", "app.submit_urgent", "加急", key_display="Ctrl+Enter"
        ),
    ]


class SnapshotPickScreen(ModalScreen[SnapshotSelection]):
    BINDINGS = [Binding("escape", "cancel", "取消", show=False)]

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
    def __init__(self, app: "RedLotusTui", log: RichLog) -> None:
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


class RedLotusTui(App[None]):
    CSS = """
    Screen { layout: vertical; }
    #output { height: 1fr; border: round $accent; }
    #review-view { display: none; height: 1fr; border: round $warning; padding: 0 1; }
    #panel-view { display: none; height: 1fr; border: round $success; padding: 0 1; }
    .panel-chart-title { color: $text-muted; text-style: bold; margin-top: 1; }
    #panel-trend { height: 3; }
    .panel-bar-row { height: 1; width: 1fr; }
    .panel-bar-label { width: 12; }
    .panel-bar-row ProgressBar { width: 1fr; }
    #panel-task-progress { width: 1fr; }
    #panel-task-counts { height: 1; }
    #context-usage { display: none; height: 1; padding: 0 1; color: $text-muted; }
    #stream-preview { display: none; height: 12; max-height: 12; padding: 0 1; }
    #status { height: 1; padding: 0 1; background: $surface; color: $text-muted; }
    #session-context { height: 1; padding: 0 1; color: $text-muted; }
    #input-row { height: 3; }
    #input { width: 1fr; height: 3; border: round $primary; }
    #session-load { width: 16; height: 3; }
    #input.ask { border: thick $warning; }
    """

    BINDINGS = [
        ("ctrl+c", "stop_or_quit", "Stop"),
        ("ctrl+q", "stop_or_quit", "Quit"),
        Binding("shift+tab", "toggle_mode", "切换模式", priority=True),
        ("ctrl+r", "review", "审查更改"),
        Binding("y", "review_keep", "保留", show=False),
        Binding("n", "review_undo", "撤销", show=False),
        Binding("escape", "escape", "退出面板/审查", show=False),
    ]

    def __init__(self, system: Any, stop_event: asyncio.Event | None = None) -> None:
        super().__init__()
        self.system = system
        self.stop_event = stop_event
        self.state = system.new_cli_session_state()
        self._ask_future: asyncio.Future[str] | None = None
        self._ask_lock = asyncio.Lock()
        self._record_reply = True
        self._active_line_handlers = 0
        self._status_is_working = False
        self._working_frame = 0
        self._model_stream_title = ""
        self._model_stream_text = ""
        self._ui_thread_id = 0
        self._run_mode = TuiRunMode.REVIEW
        self._review_mode = False
        self._review_items: list = []  # [(entry, hunk), ...] 当前未决定的改动
        self._pending_count = 0
        self._panel_mode = False
        self._panel_include_all = False
        self._panel_cache = PanelSnapshotCache()
        self._panel_timer = None
        self._panel_refresh_task = None

    def compose(self) -> ComposeResult:
        with Vertical():
            yield RichLog(id="output", wrap=True, markup=False, highlight=False)
            yield OptionList(id="review-view")
            with VerticalScroll(id="panel-view"):
                yield Static("", id="panel-content")
                yield Label("Token 趋势（按会话 旧→新）", classes="panel-chart-title")
                yield Sparkline(id="panel-trend")
                yield Label(
                    "Token 占比（输入 / 非推理输出 / 推理输出）",
                    classes="panel-chart-title",
                )
                for label in ("Input", "Output", "Reasoning"):
                    with Horizontal(classes="panel-bar-row"):
                        yield Label(label, classes="panel-bar-label")
                        yield ProgressBar(
                            id="panel-comp-" + label.lower(), show_eta=False
                        )
                yield Label("任务进度", classes="panel-chart-title")
                yield ProgressBar(id="panel-task-progress", show_eta=False)
                yield Static("", id="panel-task-counts")
            yield Static("", id="context-usage")
            yield Static("", id="stream-preview")
            yield Static(READY_LABEL, id="status")
            yield Static(self._session_context_text(), id="session-context")
            with Horizontal(id="input-row"):
                yield AgentInput(
                    placeholder="📝 请输入您的任务:",
                    id="input",
                    suggester=AgentInputSuggester(case_sensitive=True, use_cache=False),
                )
                yield Button("会话 / 加载", id="session-load")
            yield Footer()

    async def on_mount(self) -> None:
        self._ui_thread_id = threading.get_ident()
        log = self.query_one("#output", RichLog)
        set_output_sink(TextualOutputSink(self, log))
        self.system.set_ask_user_handler(self._make_ask_user_bridge())
        if self._run_mode == TuiRunMode.REVIEW:
            self.system.review_store.activate(self._on_reviews_changed)
        self.set_interval(0.5, self.refresh_status)
        self.query_one("#input", AgentInput).focus()
        await self._prepare_cli_session()
        controller = self.system._cli_controller
        controller._active_session_state = self.state
        controller.set_snapshot_picker(self.pick_snapshot)
        controller.set_snapshot_loaded_callback(self._show_loaded_conversation)
        controller.config_prompt = self.ask_config
        if self.stop_event is not None:
            asyncio.create_task(self._watch_stop_event())
        self.call_after_refresh(self._schedule_workspace_enter)

    def _schedule_workspace_enter(self) -> None:
        self.run_worker(self._enter_workspace_after_mount, exclusive=True)

    async def _prepare_cli_session(self) -> None:
        missing = await self.system.prepare_cli_session()
        if missing:
            from redlotus.core.cli_commands import interactive_set_api

            await interactive_set_api(ask=self.ask_config)

    async def _enter_workspace_after_mount(self) -> None:
        controller = self.system._cli_controller
        self.query_one("#input", AgentInput).disabled = True
        self.query_one("#session-load", Button).disabled = True
        try:
            loaded = await controller.enter_current_workspace()
        finally:
            self.refresh_status()
        if loaded:
            self.state.is_first_input = False

    async def pick_snapshot(
        self,
        snapshots: list[WorkspaceSnapshot],
    ) -> SnapshotSelection:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[SnapshotSelection] = loop.create_future()
        generation = self.system._session.generation

        def _on_result(result: SnapshotSelection) -> None:
            if not future.done():
                future.set_result(
                    result
                    if generation == self.system._session.generation
                    else SnapshotSelection(SnapshotAction.CANCEL)
                )

        screen = SnapshotPickScreen(
            snapshots,
            project_name=str(self.system.workspace.root),
            current_session_id=self.system.session_key,
        )
        try:
            await self.push_screen(screen, callback=_on_result, wait_for_dismiss=False)
            return await future
        except asyncio.CancelledError:
            if self.screen is screen:
                screen.dismiss(SnapshotSelection(SnapshotAction.CANCEL))
            raise

    def _session_context_text(self) -> Text:
        workspace = self.system.workspace
        project = str(workspace.root)
        session = self.system.session_key or "新会话（首次输入后创建）"
        return Text(f"项目：{project} · 当前会话：{session}", style="dim")

    def _show_loaded_conversation(
        self,
        snapshot: WorkspaceSnapshot,
        messages,
    ) -> None:
        entries = visible_conversation_entries(messages or [])

        def render() -> None:
            log = self.query_one("#output", RichLog)
            log.write(
                Text(
                    f"已恢复会话：{snapshot.title} · {snapshot.completed_turns} 回合",
                    style="bold green",
                ),
                scroll_end=True,
            )
            if not entries:
                log.write(Text("该会话没有可显示的用户或助手文本。", style="dim"), scroll_end=True)
            for entry in entries:
                style = {} if entry.role == "用户" else {
                    "text_style": "white", "border_style": "cyan",
                }
                self._write_user_input(entry.text, title=entry.role, **style)
            self.query_one("#session-context", Static).update(self._session_context_text())

        self.call_ui(render)

    def _make_ask_user_bridge(self):
        async def ask_user_bridge(question: str) -> str:
            # Child toolkits already bridge calls onto the owner's event loop.
            who = current_short_agent_id()
            return await self.ask_user(f"[{who}] {question}" if who else question)

        return ask_user_bridge

    async def _watch_stop_event(self) -> None:
        assert self.stop_event is not None
        await self.stop_event.wait()
        self.exit()

    def call_ui(self, callback) -> None:
        """Dispatch output and review callbacks to the owning UI thread."""
        if threading.get_ident() == self._ui_thread_id:
            callback()
        else:
            try:
                self.call_from_thread(callback)
            except RuntimeError:
                pass

    def _on_reviews_changed(self) -> None:
        """Update pending review counts on the owning UI thread."""
        self.call_ui(self._update_pending)

    def _pending_hunks(self) -> list:
        return [
            (entry, hunk)
            for entry in self.system.review_store.entries()
            for hunk in entry.hunks
            if hunk.index not in entry.decisions
        ]

    def _update_pending(self) -> None:
        self._pending_count = len(self._pending_hunks())
        self.refresh_status()

    def action_review(self) -> None:
        """Ctrl+R：审查模式临时占用输出区，全量展示所有待审查改动；y/n 决定，完成后还原输出区。"""
        self._review_items = self._pending_hunks()
        if not self._review_items:
            return
        self._review_mode = True
        view = self.query_one("#review-view", OptionList)
        view.clear_options()
        view.add_options(
            Option(render_review_hunk(entry.name, hunk))
            for entry, hunk in self._review_items
        )
        view.border_title = "审查改动 · y 保留 · n 撤销 · ↑↓ 切换 · Esc 退出"
        self.query_one("#output", RichLog).display = False
        view.display = True
        view.highlighted = 0
        view.focus()
        self.refresh_status()

    def _decide_current(self, reject: bool) -> None:
        if not self._review_mode or not self._review_items:
            return
        view = self.query_one("#review-view", OptionList)
        index = view.highlighted
        if index is None:
            return
        entry, hunk = self._review_items[index]
        try:
            applied = self.system.review_store.decide(entry, hunk.index, reject)
        except (OSError, ValueError) as exc:
            from redlotus.core.presentation import print_warning

            print_warning(str(exc))
            return
        if not applied:
            self.action_review()
            return
        if reject:
            notice = f"用户在审查界面撤销了 {entry.name} 的 {hunk.location} 改动。保留撤销，不要重新写入被拒绝的改动，也不要为此创建探针文件。"
            if not self.system.add_urgent(notice):
                from pydantic_ai.messages import ModelRequest, UserPromptPart

                self.state.history.messages.append(
                    ModelRequest(
                        parts=[UserPromptPart(notice)],
                        metadata={"origin": "user_review"},
                    )
                )
        view.replace_option_prompt_at_index(
            index, render_review_hunk(entry.name, hunk, "undo" if reject else "keep")
        )
        was_last = index == len(self._review_items) - 1
        if was_last and all(hunk.index in entry.decisions for entry, hunk in self._review_items):
            self._exit_review()
            return
        view.highlighted = min(index + 1, len(self._review_items) - 1)
        self.refresh_status()

    def _exit_review(self) -> None:
        self.system.review_store.finish_decided()
        self._review_mode = False
        self._review_items = []
        view = self.query_one("#review-view", OptionList)
        view.clear_options()
        view.display = False
        self.query_one("#output", RichLog).display = True  # 还原输出区（内容原样保留）
        self._pending_count = len(self._pending_hunks())
        self.query_one("#input", AgentInput).focus()
        self.refresh_status()

    async def open_panel(self, *, include_all: bool = False) -> None:
        if self._review_mode:
            self._exit_review()
        self._panel_mode = True
        self._panel_include_all = include_all
        self.query_one("#output", RichLog).display = False
        panel_view = self.query_one("#panel-view", VerticalScroll)
        panel_view.border_title = "工作区总览 · 每 3 秒刷新 · Esc 退出"
        panel_view.display = True
        self._schedule_panel_refresh()
        self._ensure_panel_timer()
        self.query_one("#input", AgentInput).focus()
        self.refresh_status()

    async def _refresh_panel(self) -> None:
        if not self._panel_mode:
            return
        try:
            from redlotus.core.session import conversations_root

            snapshot = await build_panel_snapshot(
                log_root=conversations_root(),
                system=self.system,
                coordinator_history=self.state.history,
                manager_history=getattr(self.system, "_manager_history", None),
                include_all=self._panel_include_all,
                cache=self._panel_cache,
            )
            self.query_one("#panel-content", Static).update(render_panel(snapshot))
            self._update_panel_charts(snapshot)
        except Exception as e:
            logger.error(f"刷新工作区面板失败: {type(e).__name__}: {e}", exc_info=True)

    def _update_panel_charts(self, snapshot: Any) -> None:
        """就地更新面板内的原生图表控件（趋势 Sparkline、占比与任务 ProgressBar），避免重建。"""
        trend = self.query_one("#panel-trend", Sparkline)
        series = [float(v) for v in (snapshot.token_trend or [])]
        trend.data = series if any(series) else []
        trend.display = bool(trend.data)
        history = snapshot.history
        total = history.input_tokens + history.output_tokens
        reasoning = min(history.reasoning_tokens, history.output_tokens)
        for widget_id, value in (
            ("#panel-comp-input", history.input_tokens or 0),
            ("#panel-comp-output", history.output_tokens - reasoning),
            ("#panel-comp-reasoning", reasoning),
        ):
            self.query_one(widget_id, ProgressBar).update(
                total=total or 1, progress=value
            )
        tasks = snapshot.runtime.tasks
        task_total = tasks.total or 0
        self.query_one("#panel-task-progress", ProgressBar).update(
            total=task_total or 1, progress=tasks.completed or 0
        )
        self.query_one("#panel-task-counts", Static).update(Text.assemble(
            (f"✓ Completed {tasks.completed}/{task_total}", "green"), "   ",
            (f"⟳ Running {tasks.running}", "cyan"), "   ",
            (f"✗ Failed {tasks.failed}", "red"), "   ",
            (f"… Pending {tasks.pending}", "yellow"),
        ))

    def _schedule_panel_refresh(self) -> None:
        if not self._panel_mode:
            return
        task = self._panel_refresh_task
        if task is not None and not task.done():
            return
        self._panel_refresh_task = asyncio.create_task(self._refresh_panel())

    def _ensure_panel_timer(self) -> None:
        if self._panel_timer is not None:
            return
        self._panel_timer = self.set_interval(3.0, self._schedule_panel_refresh)

    def _stop_panel_timer(self) -> None:
        timer = self._panel_timer
        self._panel_timer = None
        task = self._panel_refresh_task
        self._panel_refresh_task = None
        if task is not None and not task.done():
            task.cancel()
        if timer is not None:
            timer.stop()

    def _exit_panel(self) -> None:
        if not self._panel_mode:
            return
        self._panel_mode = False
        self._stop_panel_timer()
        self.query_one("#panel-view", VerticalScroll).display = False
        self.query_one("#output", RichLog).display = True
        self.query_one("#input", AgentInput).focus()
        self.refresh_status()

    def action_review_keep(self) -> None:
        self._decide_current(False)

    def action_review_undo(self) -> None:
        self._decide_current(True)

    def action_escape(self) -> None:
        if self._cancel_pending_ask():
            return
        if self._panel_mode:
            self._exit_panel()
            return
        if self._review_mode:
            self._exit_review()

    def action_toggle_mode(self) -> None:
        """Shift+Tab：审查模式 -> 放行模式 -> 目标模式 -> 审查模式。"""
        if self._review_mode or self._panel_mode:
            return  # 浮层中不切换全局模式
        if self.system.has_current_goal_turn:
            return
        self._run_mode = self._run_mode.next()
        if self._run_mode == TuiRunMode.REVIEW:
            self.system.review_store.activate(self._on_reviews_changed)
        else:
            self.system.review_store.deactivate()  # 清空待审查；后续写入直接放行
        self._update_pending()

    def set_status(self, message: str) -> None:
        self._status_is_working = bool(message and message != READY_LABEL)
        self.query_one("#status", Static).update(self._status_text())

    def clear_status(self) -> None:
        self._status_is_working = False
        self.refresh_status()

    def set_context_usage(self, items: list[ContextUsageItem]) -> None:
        self._display_content("context-usage", context_usage_renderable(items), bool(items))

    def clear_context_usage(self) -> None:
        self._display_content("context-usage", "", False)

    def _display_content(self, widget_id, content, visible=True):
        """Update one auxiliary display and its visibility together."""
        widget = self.query_one("#" + widget_id, Static)
        widget.update(content)
        widget.display = visible

    def refresh_status(self) -> None:
        if not self.is_running:
            return
        self.query_one("#status", Static).update(self._status_text())
        controller = self.system._cli_controller
        input_box = self.query_one("#input", AgentInput)
        input_box.disabled = controller.is_transitioning
        self.query_one("#session-load", Button).disabled = (
            controller.is_transitioning or self._active_line_handlers > 0 or self.system.has_current_turn
        )
        self.query_one("#session-context", Static).update(self._session_context_text())
        if not input_box.disabled and controller.last_rejected_input and self._ask_future is None:
            if not input_box.value:
                input_box.value = controller.last_rejected_input
                controller.last_rejected_input = None

    def _is_working(self) -> bool:
        return bool(
            (self._ask_future is not None and not self._ask_future.done())
            or self._active_line_handlers > 0
            or self._status_is_working
            or self.system.has_current_turn
            or self.system._session.queue.pending
        )

    def _mode_chip(self) -> Text:
        label, color = {
            TuiRunMode.REVIEW: (" ⏵ 审查模式 ", "cyan"),
            TuiRunMode.PASS: (" ⏵⏵ 放行模式 ", "green"),
            TuiRunMode.GOAL: (" ◎ 目标模式 ", "yellow"),
        }[self._run_mode]
        return Text(label, style=f"bold black on {color}")

    def _status_text(self) -> Any:
        if self._panel_mode:
            return Text("Panel 总览    ·    每 3 秒刷新    ·    Esc 退出", style="bold")
        if self._review_mode:
            return Text(
                "审查改动中    ·    y 保留    ·    n 撤销    ·    ↑↓ 切换    ·    Esc 退出",
                style="bold",
            )
        text = Text.assemble(self._mode_chip(), "  ")
        if not self._is_working():
            self._working_frame = 0
            text.append(READY_LABEL, style="dim")
            if self._run_mode == TuiRunMode.REVIEW and self._pending_count > 0:
                text.append("       ")
                text.append(
                    f" ⚑ 待审查 {self._pending_count} 处 · 按 Ctrl+R 审查 ",
                    style="bold black on yellow",
                )
            return text
        suffix = WORKING_FRAMES[self._working_frame % len(WORKING_FRAMES)]
        self._working_frame += 1
        if self.system.has_current_goal_turn:
            iteration = self.system.current_goal_iteration
            label = f"目标循环第 {iteration} 轮" if iteration else "目标循环"
            text.append(f"{label}{suffix}")
        else:
            text.append(f"{WORKING_LABEL}{suffix}")
        return text

    def _write_user_input(self, value: str, *, title="用户", **style) -> None:
        """Echo an admitted user message or question reply with the same panel style."""
        self.query_one("#output", RichLog).write(
            user_text_panel(value, title, **style),
            scroll_end=True,
        )

    def _refresh_model_stream(self) -> None:
        self._display_content(
            "stream-preview",
            user_text_panel(
                model_stream_visible_text(self._model_stream_text),
                self._model_stream_title,
                text_style="white",
                border_style="cyan",
            )
        )

    def begin_model_stream(self, title: str) -> None:
        self._model_stream_title = title
        self._model_stream_text = ""
        self._refresh_model_stream()

    def append_model_stream_delta(self, text: str) -> None:
        if not text:
            return
        self._model_stream_text += text
        if not self._model_stream_title:
            self._model_stream_title = "模型正在回复"
        self._refresh_model_stream()

    def clear_model_stream(self) -> None:
        self._model_stream_title = ""
        self._model_stream_text = ""
        self._display_content("stream-preview", "", False)

    def _cancel_pending_ask(self) -> bool:
        fut = self._ask_future
        if fut is not None and not fut.done():
            fut.cancel()
            return True
        return False

    async def ask_user(self, question: str, *, record_reply=True, secret=False) -> str:
        """在 Textual 事件循环中弹出用户提问界面（内部方法）。"""
        generation = self.system._session.generation
        async with self._ask_lock:  # 多个并行提问按 FIFO 串行排队，互不丢弃
            if generation != self.system._session.generation:
                raise asyncio.CancelledError()
            self._record_reply = record_reply
            self._ask_future = asyncio.get_running_loop().create_future()
            inp = self.query_one("#input", AgentInput)
            inp.password = secret
            inp.add_class("ask")
            inp.suggester = None
            inp.placeholder = f"🤔 {question.strip()}"
            inp.value = ""
            inp.focus()
            self.refresh_status()
            try:
                answer = await self._ask_future
                if generation != self.system._session.generation:
                    raise asyncio.CancelledError()
                return answer
            finally:
                inp.password = False
                self._record_reply = True
                self._ask_future = None
                inp.remove_class("ask")
                inp.placeholder = "📝 请输入您的任务:"
                inp.suggester = AgentInputSuggester(
                    case_sensitive=True, use_cache=False
                )
                self.refresh_status()

    async def ask_config(self, question: str, *, secret=False):
        return await self.ask_user(question, record_reply=False, secret=secret)

    async def action_submit_urgent(self) -> None:
        """Submit Ctrl+Enter through the same input path with explicit priority."""
        inp = self.query_one("#input", AgentInput)
        await self.on_input_submitted(Input.Submitted(inp, inp.value), urgent=True)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "session-load" or self.system._cli_controller.is_transitioning or self._active_line_handlers:
            return
        if self._panel_mode:
            self._exit_panel()
        self._active_line_handlers += 1
        self.refresh_status()
        event.button.disabled = True
        asyncio.create_task(self._handle_line("/load"))

    async def on_input_submitted(self, event: Input.Submitted, *, urgent=False) -> None:
        if self.system._cli_controller.is_transitioning:
            return
        value = event.value.strip()
        event.input.value = ""
        if (
            self._ask_future is not None
            and not self._ask_future.done()
            and not value.startswith("/")
        ):
            if self._record_reply:
                self._write_user_input(value, title="用户回复")
                self.system._session.user_inputs.append(value)
            self._ask_future.set_result(value)
            return
        if not value:
            return
        parts = value.split()
        if parts and parts[0].lower() == "/panel":
            include_all = any(part.lower() == "--all" for part in parts[1:])
            await self.open_panel(include_all=include_all)
            return
        if self._panel_mode:
            self._exit_panel()
        if not value.startswith("/") and value.lower() not in self.system._cli_controller.EXIT_COMMANDS:
            inner = (
                urgent and self.system._session.active and self.system._session.accepting_urgent
            )
            queued = not inner and (
                self.system.has_current_turn or self._active_line_handlers > 0
            )
            title = "排队" if queued else "加急" if inner else "用户"
            self._write_user_input(
                value,
                title=title,
                text_style="dim" if queued else "bold white",
                border_style="grey50" if queued else "bright_blue",
            )
        self._active_line_handlers += 1
        self.refresh_status()
        asyncio.create_task(self._handle_line(value, urgent=urgent))

    async def _handle_line(self, value: str, *, urgent=False) -> None:
        try:
            action = await self.system.process_cli_line(
                value,
                self.state,
                wait_for_turn=False,
                goal_mode=self._run_mode == TuiRunMode.GOAL,
                urgent=urgent,
            )
            if action == "break":
                self.exit()
        finally:
            self._active_line_handlers = max(0, self._active_line_handlers - 1)
            self.refresh_status()

    async def action_stop_or_quit(self) -> None:
        ask_cancelled = self._cancel_pending_ask()
        if self.system.has_current_turn:
            msg = await self.system.stop_current_turn()
            from redlotus.core.presentation import print_warning

            print_warning(msg)
        elif not ask_cancelled:
            self.exit()


async def run_textual_tui(
    system: Any, *, stop_event: asyncio.Event | None = None
) -> None:
    app = RedLotusTui(system, stop_event=stop_event)
    try:
        await app.run_async()
    finally:
        app._stop_panel_timer()
        app._cancel_pending_ask()
        system._cli_controller.set_snapshot_loaded_callback(None)
        set_output_sink(None)
        system.set_ask_user_handler(None)
        try:
            system.review_store.deactivate()
        except Exception:
            pass
        await system.shutdown()
