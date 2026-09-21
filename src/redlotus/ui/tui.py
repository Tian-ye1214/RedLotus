from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Input,
    OptionList,
    RichLog,
    Static,
)
from textual.widgets.option_list import Option

from redlotus.runtime import logging as logger
from redlotus.runtime.config import settings
from redlotus.sessions.context import current_short_agent_id
from redlotus.ui.cli_commands import WorkspaceSnapshot
from redlotus.ui.presentation import (
    ContextUsageItem,
    PanelSnapshotCache,
    build_panel_snapshot,
    context_usage_renderable,
    model_stream_visible_text,
    render_review_hunk,
    set_output_sink,
    user_text_panel,
)
from redlotus.ui.widgets import (
    AgentInput,
    AgentInputSuggester,
    RunStatus,
    SnapshotAction,
    SnapshotPickScreen,
    SnapshotSelection,
    TextualOutputSink,
    TuiRunMode,
    UsagePanel,
    visible_conversation_entries,
)


class RedLotusTui(App[None]):
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
        self.state = controller.new_session_state()
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
            yield UsagePanel(id="panel-view")
            yield Static("", id="context-usage")
            with Collapsible(title="思考", collapsed=False, id="thinking-preview"):
                with VerticalScroll(id="thinking-scroll"):
                    yield Static("", id="thinking-content")
            yield Static("", id="stream-preview")
            yield RunStatus(id="status")
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
            self.system.toolkit.review_store.activate(self._on_reviews_changed)
        self.set_interval(settings()["ui"]["status_refresh_seconds"], self.refresh_status)
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
            from redlotus.ui.cli_commands import interactive_set_api

            await interactive_set_api(ask=self.ask_config)

    async def _enter_workspace_after_mount(self) -> None:
        controller = self.controller
        self.query_one("#input", AgentInput).disabled = True
        self.query_one("#session-load", Button).disabled = True
        try:
            if await controller.enter_current_workspace():
                self.state.is_first_input = False
        except Exception as exc:
            from redlotus.ui.presentation import print_warning

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
            for entry in self.system.toolkit.review_store.entries()
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
            applied = self.system.toolkit.review_store.decide(entry, hunk.index, reject)
        except (OSError, ValueError) as exc:
            from redlotus.ui.presentation import print_warning

            print_warning(str(exc))
            return
        if not applied:
            self.action_review()
            return
        if reject:
            notice = json.dumps(
                {"review": "rejected", "file": entry.name, "location": hunk.location},
                ensure_ascii=False,
            )
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
        self.system.toolkit.review_store.finish_decided()
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
        panel_view.display = True
        self._schedule_panel_refresh()
        self._ensure_panel_timer()
        self.query_one("#input", AgentInput).focus()
        self.refresh_status()

    async def _refresh_panel(self) -> None:
        if not self._panel_mode:
            return
        try:
            from redlotus.runtime.resources import conversations_root

            identity = (self.system.workspace, self.system.session_key)
            snapshot = await build_panel_snapshot(
                log_root=conversations_root(),
                system=self.system,
                coordinator_history=self.state.history,
                manager_history=getattr(self.system, "_manager_history", None),
                include_all=self._panel_include_all,
                cache=self._panel_cache,
            )
            if not self._panel_mode or identity != (self.system.workspace, self.system.session_key):
                return
            self.query_one(UsagePanel).update_snapshot(snapshot)
        except Exception as e:
            logger.error(f"刷新工作区面板失败: {type(e).__name__}: {e}", exc_info=True)




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
        interval = settings()["ui"]["panel_refresh_seconds"]
        self._panel_timer = self.set_interval(interval, self._schedule_panel_refresh)
        self.query_one("#panel-view").border_title = f"工作区总览 · 每 {interval:g} 秒刷新 · Esc 退出"

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
            self.system.toolkit.review_store.activate(self._on_reviews_changed)
        else:
            self.system.toolkit.review_store.deactivate()  # 清空待审查；后续写入直接放行
        self._update_pending()

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
        self.query_one(RunStatus).refresh()
        controller = self.controller
        input_box = self.query_one("#input", AgentInput)
        was_disabled = input_box.disabled
        asking = self._ask_future is not None and not self._ask_future.done()
        input_box.disabled = controller.is_transitioning or (
            self._startup_locked and not asking
        )
        if was_disabled and not input_box.disabled:
            input_box.focus()
        self.query_one("#session-load", Button).disabled = (
            self._startup_locked
            or controller.is_transitioning
            or self._active_line_handlers > 0
            or self.system.has_current_turn
        )
        self.query_one("#session-context", Static).update(self._session_context_text())
        if not input_box.disabled and self.system.last_rejected_input and self._ask_future is None:
            if not input_box.value:
                input_box.value = self.system.last_rejected_input
                self.system.last_rejected_input = None




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
                model_stream_visible_text(self._model_stream_text, self._stream_policy),
                self._model_stream_title,
                text_style="white",
                border_style="cyan",
            ),
            bool(self._model_stream_text),
        )

    def begin_model_stream(self, title: str) -> None:
        self.clear_model_stream()
        self._stream_session = (self.system.session_key, self.system._session.generation)
        self._stream_policy = settings()["ui"]
        self._model_stream_title = title
        self._refresh_model_stream()

    def begin_model_response(self) -> None:
        """Separate successive model requests within the same user turn."""
        self._model_response_count += 1
        self._model_stream_text = ""
        if self._model_stream_thinking:
            self._model_stream_thinking += f"\n\n── 第 {self._model_response_count} 次响应 ──\n"
        self._refresh_model_stream()

    def append_model_stream_delta(self, text: str, kind: str = "text") -> None:
        if not text or self._stream_session != (self.system.session_key, self.system._session.generation):
            return
        if kind == "thinking":
            self._model_stream_thinking += text
            preview = self.query_one("#thinking-preview", Collapsible)
            preview.display = True
            preview.collapsed = False
            preview.title = "思考 · 接收中"
            self.query_one("#thinking-content", Static).update(Text(self._model_stream_thinking))
            self.query_one("#thinking-scroll", VerticalScroll).scroll_end(animate=False)
            return
        self._model_stream_text += text
        if not self._model_stream_title:
            self._model_stream_title = "模型正在回复"
        self._refresh_model_stream()

    def end_model_stream(self, status: str) -> None:
        """Keep received thinking inspectable after the current reply ends."""
        if self._stream_session != (self.system.session_key, self.system._session.generation):
            return
        if status != "已完成" and self._model_stream_text:
            self.query_one("#output", RichLog).write(user_text_panel(
                self._model_stream_text, f"Coordinator · {status}", border_style="yellow",
            ))
        self._model_stream_text = ""
        self._display_content("stream-preview", "", False)
        preview = self.query_one("#thinking-preview", Collapsible)
        preview.title = f"思考 · {status} · 展开查看"
        preview.collapsed = True

    def clear_model_stream(self) -> None:
        self._model_stream_title = ""
        self._model_stream_text = ""
        self._model_stream_thinking = ""
        self._model_response_count = 0
        self._stream_session = None
        self._display_content("stream-preview", "", False)
        self.query_one("#thinking-preview", Collapsible).display = False
        self.query_one("#thinking-content", Static).update("")

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
            from redlotus.ui.presentation import print_warning

            print_warning(msg)
        elif not ask_cancelled:
            self.exit()


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
            system.toolkit.review_store.deactivate()
        except Exception:
            pass
        await system.shutdown()
