"""Terminal tui responsibilities."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Input,
    Label,
    OptionList,
    ProgressBar,
    RichLog,
    Static,
)

from redlotus.presentation.output import set_output_sink
from redlotus.presentation.panels import PanelSnapshotCache
from redlotus.presentation.snapshots import WorkspaceSnapshot
from redlotus.presentation.widgets import (
    PREPARING_LABEL,
    AgentInput,
    AgentInputSuggester,
    SnapshotPickScreen,
    TextualOutputSink,
    TuiRunMode,
)
from redlotus.runtime.context import current_short_agent_id
from redlotus.terminal.console import (
    SnapshotAction,
    SnapshotSelection,
    visible_conversation_entries,
)
from redlotus.terminal.views import TerminalViews


async def run_textual_tui(
    controller: Any, *, stop_event: asyncio.Event | None = None
) -> None:
    system = controller.system
    app = RedLotusTui(controller, stop_event=stop_event)
    try:
        await app.run_async()
    finally:
        app._stop_panel_timer()
        app._cancel_pending_ask()
        controller.set_snapshot_loaded_callback(None)
        set_output_sink(None)
        system.set_ask_user_handler(None)
        try:
            system.review_store.deactivate()
        except Exception:
            pass
        await system.shutdown()


class RedLotusTui(TerminalViews):
    CSS = """
    Screen { layout: vertical; }
    #output { height: 1fr; border: round $accent; }
    #review-view { display: none; height: 1fr; border: round $warning; padding: 0 1; }
    #panel-view { display: none; height: 1fr; border: round $success; padding: 0 1; }
    .panel-chart-title { color: $text-muted; text-style: bold; margin-top: 1; }
    .panel-bar-row { height: auto; min-height: 1; width: 1fr; }
    .panel-bar-label { width: 22; }
    .panel-bar-row ProgressBar { width: 1fr; }
    .panel-token-value { width: 38; height: auto; padding-left: 1; }
    #panel-task-progress { width: 1fr; }
    #panel-task-counts { height: 1; }
    #context-usage { display: none; height: 1; padding: 0 1; color: $text-muted; }
    #stream-preview { display: none; height: 12; max-height: 12; padding: 0 1; }
    #thinking-preview { display: none; height: auto; padding: 0 1; }
    #thinking-scroll { height: 10; }
    #thinking-content { height: auto; color: $text-muted; }
    #status { height: 1; padding: 0 1; background: $surface; color: $text-muted; }
    #session-context { height: auto; padding: 0 1; color: $text-muted; }
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

    def __init__(self, controller: Any, stop_event: asyncio.Event | None = None) -> None:
        super().__init__()
        self.controller = controller
        self.system = controller.system
        self.stop_event = stop_event
        self.state = self.controller.new_session_state()
        self._ask_future: asyncio.Future[str] | None = None
        self._ask_lock = asyncio.Lock()
        self._record_reply = True
        self._active_line_handlers = 0
        self._working_frame = 0
        self._model_stream_title = ""
        self._model_stream_text = ""
        self._model_stream_thinking = ""
        self._model_response_count = 0
        self._stream_session = None
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
        # Mount starts asynchronous preparation before the workspace admission
        # gate exists. Keep the composer closed across that gap.
        self._startup_locked = True

    def compose(self) -> ComposeResult:
        with Vertical():
            yield RichLog(id="output", wrap=True, markup=False, highlight=False)
            yield OptionList(id="review-view")
            with VerticalScroll(id="panel-view"):
                yield Static("", id="panel-content")
                yield Label(
                    "新增内容 Token 占比（当前项目全部会话）",
                    classes="panel-chart-title",
                )
                for key, label in (("input", "用户输入（估算）"), ("output", "模型输出（非推理）"), ("reasoning", "推理输出")):
                    with Horizontal(classes="panel-bar-row"):
                        yield Label(label, classes="panel-bar-label")
                        yield ProgressBar(
                            id="panel-comp-" + key, show_eta=False, show_percentage=False
                        )
                        yield Static("", id="panel-value-" + key, classes="panel-token-value")
                yield Static("", id="panel-content-note")
                yield Label("API 实际用量（包含历史重发）", classes="panel-chart-title")
                yield Static("", id="panel-api-usage")
                yield Label("当前会话 Agent", classes="panel-chart-title")
                yield Static("", id="panel-agent-counts")
                yield Label("计划任务进度", classes="panel-chart-title")
                yield ProgressBar(id="panel-task-progress", show_eta=False)
                yield Static("", id="panel-task-counts")
            yield Static("", id="context-usage")
            with Collapsible(title="思考", collapsed=False, id="thinking-preview"):
                with VerticalScroll(id="thinking-scroll"):
                    yield Static("", id="thinking-content")
            yield Static("", id="stream-preview")
            yield Static(PREPARING_LABEL, id="status")
            yield Static(self._session_context_text(), id="session-context")
            with Horizontal(id="input-row"):
                yield AgentInput(
                    placeholder="📝 请输入您的任务:",
                    id="input",
                    suggester=AgentInputSuggester(case_sensitive=True, use_cache=False),
                    disabled=True,
                )
                yield Button("会话 / 加载", id="session-load", disabled=True)
            yield Footer()

    async def on_mount(self) -> None:
        self._ui_thread_id = threading.get_ident()
        log = self.query_one("#output", RichLog)
        set_output_sink(TextualOutputSink(self, log))
        self.system.set_ask_user_handler(self._make_ask_user_bridge())
        if self._run_mode == TuiRunMode.REVIEW:
            self.system.review_store.activate(self._on_reviews_changed)
        self.set_interval(0.5, self.refresh_status)
        await self._prepare_cli_session()
        controller = self.controller
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
        missing = await self.controller.prepare_session()
        if missing:
            from redlotus.terminal.commands import interactive_set_api

            await interactive_set_api(ask=self.ask_config)

    async def _enter_workspace_after_mount(self) -> None:
        controller = self.controller
        self.query_one("#input", AgentInput).disabled = True
        self.query_one("#session-load", Button).disabled = True
        try:
            if await controller.enter_current_workspace():
                self.state.is_first_input = False
        except Exception as exc:
            from redlotus.presentation.output import print_warning

            print_warning(f"启动时加载会话失败: {exc}")
        finally:
            self._startup_locked = False
            if self.is_running and self.query("#input"):
                self.refresh_status()
                self.query_one("#input", AgentInput).focus()

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
            self.clear_model_stream()
            log = self.query_one("#output", RichLog)
            log.write(
                Text(
                    f"已恢复会话：{snapshot.title} · {snapshot.input_summary}",
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
            if self._panel_mode:
                self._exit_panel()
            if self._review_mode:
                self._exit_review()
            self._write_user_input(
                question.strip(),
                title="需要回复",
                text_style="white",
                border_style="yellow",
            )
            inp = self.query_one("#input", AgentInput)
            inp.password = secret
            inp.add_class("ask")
            inp.suggester = None
            inp.placeholder = "🤔 请回复"
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
        if inp.disabled or self.controller.is_transitioning:
            return
        await inp.action_submit(urgent=True)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "session-load" or self.controller.is_transitioning or self._active_line_handlers:
            return
        if self._panel_mode:
            self._exit_panel()
        self._active_line_handlers += 1
        self.refresh_status()
        event.button.disabled = True
        asyncio.create_task(self._handle_line("/load"))

    async def on_input_submitted(self, event: Input.Submitted, *, urgent=False) -> None:
        value = event.value.strip()
        if self.controller.is_transitioning:
            return
        urgent = urgent or isinstance(event, AgentInput.Submitted) and event.urgent
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
        if not value.startswith("/") and value.lower() not in self.controller.EXIT_COMMANDS:
            inner = (
                urgent and self.system._session.active and self.system._session.accepting_urgent
            )
            queued = not inner and (
                self.system.has_current_turn or self._active_line_handlers > 0
            )
            title = "排队" if queued else "用户"
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
            action = await self.controller.process_line(
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
            from redlotus.presentation.output import print_warning

            print_warning(msg)
        elif not ask_cancelled:
            self.exit()
