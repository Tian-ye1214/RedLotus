"""Shared input completion, terminal controls, conversation picker and usage widgets."""
from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from itertools import islice
from pathlib import Path
from typing import Any, Literal

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from rich.ansi import AnsiDecoder
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import Input, Label, OptionList, ProgressBar, RichLog, Static
from textual.widgets.option_list import Option

from redlotus.runtime.config import (
    get_agent_roles,
    role_supported_thinking_efforts,
    settings,
    supported_thinking_efforts,
)
from redlotus.runtime.resources import current_workspace, user_data_dir
from redlotus.sessions.control import iter_reference_spans, quote_reference_path
from redlotus.ui.cli_commands import WorkspaceSnapshot
from redlotus.ui.presentation import (
    OutputSink,
    print_error,
    print_panel,
    print_success,
    print_warning,
    render_panel,
)

COMMAND_HELP = {
    "/help": "显示本帮助",
    "/exit": "退出程序（也接受 quit、exit、退出）",
    "/quit": "退出程序",
    "/clear": "清空上下文并开启新对话（也接受“新任务”，旧快照保留）",
    "/status": "查看 Agent 生命周期与调用状态",
    "/config": "查看配置摘要",
    "/context": "查看上下文 token 用量分解与压缩阈值",
    "/usage": "查看用量与计费统计，可指定日志路径",
    "/panel": "查看工作区运行和历史总览；--all 显示全部会话",
    "/LTM": "show / clear / retry：查看、清空或重试全局长期记忆",
    "/STM": "show / clear / retry：查看、清空或重试当前项目情景记忆",
    "/pwd": "查看当前项目目录",
    "/cd": "/cd <path>：切换项目并加载该项目对话",
    "/skills": "查看已加载 Skills",
    "/agent": "/agent <role> <预设或模型名>：下一次请求切换模型，保留会话",
    "/effort": "/effort <role> off 或支持的级别：查看或设置思考",
    "/api": "查看配置对话；/api embedding 配置 embedding/rerank 接口",
    "/compress": "压缩 Manager / Coordinator 上下文",
    "/cancel": "/cancel <invocation_id> 或 /cancel agent <agent_id>：取消调用",
    "/stop": "停止当前任务，保留会话",
    "/load": "选择并加载当前项目对话快照",
    "/trace": "/trace <turn_id>：查看追踪记录",
    "/tasks": "查看任务状态与依赖",
}
COMMANDS = tuple(COMMAND_HELP)

CompletionKind = Literal[
    "command", "agent_role", "effort_value", "literal_choice", "file_path"
]

_SUBCOMMAND_CHOICES: dict[str, tuple[str, ...]] = {
    "/ltm": ("show", "clear", "retry"),
    "/stm": ("show", "clear", "retry"),
    "/cancel": ("agent",),
    "/api": ("embedding",),
}


@dataclass(frozen=True)
class InputCompletion:
    """Describes what to complete for a given input prefix."""

    kind: CompletionKind
    prefix: str
    at_mode: bool = False
    choices: tuple[str, ...] = ()
    role: str = ""


def completion_for_input(text: str) -> InputCompletion | None:
    """Return completion context for *text*, or None if no completion applies."""
    if text.startswith("/") and " " not in text:
        return InputCompletion(kind="command", prefix=text)

    if text.startswith("/agent "):
        prefix = text[len("/agent ") :]
        if " " not in prefix:
            return InputCompletion(kind="agent_role", prefix=prefix)
        role, selected = prefix.split(" ", 1)
        if " " not in selected:
            return InputCompletion(
                kind="literal_choice",
                prefix=selected,
                choices=tuple(settings().get("model_presets", {})),
                role=role,
            )
        return None

    if text.startswith("/effort "):
        rest = text[len("/effort ") :]
        if " " not in rest:
            return InputCompletion(kind="agent_role", prefix=rest)
        role, prefix = rest.split(" ", 1)
        if " " not in prefix.strip():
            return InputCompletion(
                kind="effort_value", prefix=prefix, role=role.lower()
            )
        return None

    if text.startswith("/cd "):
        prefix = text.split(" ", 1)[1] if " " in text else ""
        return InputCompletion(kind="file_path", prefix=prefix)

    for cmd, choices in _SUBCOMMAND_CHOICES.items():
        if text.lower().startswith(cmd + " "):
            prefix = text[len(cmd) + 1 :]
            if " " not in prefix:
                return InputCompletion(
                    kind="literal_choice", prefix=prefix, choices=choices
                )
            return None

    references = list(iter_reference_spans(text, root=current_workspace()))
    if references:
        reference = references[-1]
        if reference.end == len(text) and not (reference.opener and reference.closed):
            return InputCompletion(
                kind="file_path", prefix=text[reference.start + 1 :], at_mode=True
            )

    return None


class AgentCompleter(Completer):
    """根据光标前上下文补全命令、角色名或文件路径。"""

    def get_completions(self, document, complete_event):
        yield from input_completions(document.text_before_cursor)


def input_completions(text):
    if context := completion_for_input(text):
        if context.kind == "file_path":
            yield from _iter_file_completions(context.prefix, at_mode=context.at_mode)
            return
        choices = {
            "command": COMMANDS,
            "agent_role": get_agent_roles(),
            "literal_choice": context.choices,
        }
        if context.kind == "effort_value":
            values = (
                ("off", *role_supported_thinking_efforts(context.role))
                if context.role in get_agent_roles()
                else ("off", *supported_thinking_efforts(None))
            )
        else:
            values = choices[context.kind]
        for value in values:
            if value.lower().startswith(context.prefix.lower()):
                yield Completion(
                    value, start_position=-len(context.prefix), display_meta=context.kind
                )


def _resolve_parent(fragment: str) -> tuple[Path, str]:
    path = Path(fragment.replace("\\", "/")).expanduser()
    path = path if path.is_absolute() else current_workspace() / path
    return (
        (path, "")
        if fragment.endswith(("/", "\\")) or not fragment
        else (path.parent, path.name)
    )


def _iter_file_completions(fragment: str, *, at_mode: bool):
    opener = fragment[:1] if fragment[:1] in ('"', "'", "{") else ""
    parent, prefix = _resolve_parent(fragment[1:] if opener else fragment)
    if not parent.exists():
        return

    try:
        children = sorted(
            parent.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
        )
    except (PermissionError, OSError):
        return

    match = prefix.casefold() if os.name == "nt" else prefix
    matching = (child for child in children if (child.name.casefold() if os.name == "nt" else child.name).startswith(match))
    for child in islice(matching, settings()["ui"]["file_completion_limit"]):
        try:
            candidate = child.relative_to(current_workspace()).as_posix()
        except ValueError:
            candidate = child.as_posix()
        if child.is_dir():
            candidate += "/"
        candidate = quote_reference_path(
            candidate, opener=opener, directory=child.is_dir()
        )
        display = ("@" if at_mode else "") + candidate
        yield Completion(
            candidate,
            start_position=-len(fragment),
            display=display,
            display_meta="dir" if child.is_dir() else "file",
        )


def _history_path() -> Path:
    (base := user_data_dir()).mkdir(parents=True, exist_ok=True)
    return base / "history"


def create_prompt_session() -> PromptSession:
    kb = KeyBindings()

    @kb.add("c-c", eager=True)
    def _interrupt(event) -> None:
        if event.app.current_buffer.text:
            # 有内容：仅清空当前行，不退出
            return event.app.current_buffer.reset()
        # Use a regular exception so the input task cannot abort the event loop.
        event.app.exit(exception=InterruptedError())

    return PromptSession(
        history=FileHistory(str(_history_path())),
        completer=AgentCompleter(),
        complete_while_typing=False,
        key_bindings=kb,
        interrupt_exception=InterruptedError,
    )


class InteractiveRepl:
    """TTY 交互循环；非 TTY 回退到标准 input。"""

    def __init__(
        self,
        *,
        prompt: str = "\n📝 请输入您的任务: ",
        on_interrupt_during_handler: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.prompt = prompt
        self._session: PromptSession | None = None
        self._interrupt_hits = 0
        self._last_interrupt_at = 0.0
        self._on_interrupt_during_handler = on_interrupt_during_handler

    def _on_keyboard_interrupt(self) -> bool:
        """处理空行 Ctrl+C。返回 True 表示应退出 REPL。"""
        now = time.monotonic()
        self._interrupt_hits = (
            1 if now - self._last_interrupt_at > settings()["ui"]["interrupt_repeat_seconds"]
            else self._interrupt_hits + 1
        )
        self._last_interrupt_at = now
        if self._interrupt_hits < 2:
            print_warning("再次按 Ctrl+C 退出，或输入 /exit、quit。")
        return self._interrupt_hits >= 2

    async def read_line(self, *, stop_event: asyncio.Event | None = None) -> str | None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            if self._session is None:
                self._session = create_prompt_session()
            try:
                with patch_stdout(raw=True):
                    read_coro = self._session.prompt_async(self.prompt)
                    if stop_event is None:
                        return (await read_coro).strip()
                    read_task = asyncio.create_task(read_coro)
                    stop_task = asyncio.create_task(stop_event.wait())
                    done, pending = await asyncio.wait(
                        {read_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                        try:
                            await t
                        except asyncio.CancelledError:
                            pass
                    return None if stop_task in done else read_task.result().strip()
            except (EOFError, asyncio.CancelledError):
                return None
        try:
            return (await asyncio.to_thread(input, self.prompt)).strip()
        except EOFError:
            return None

    async def run(
        self,
        handler: Callable[[str], Awaitable[str]],
        *,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """
        handler 返回 "continue" | "break"。
        空行 Ctrl+C：连按两次退出；有内容时 Ctrl+C 仅清空输入行。
        """
        while stop_event is None or not stop_event.is_set():
            try:
                line = await self.read_line(stop_event=stop_event)
                if line is None:
                    break
                self._interrupt_hits = 0
                action = await handler(line)
                if action == "break":
                    break
            except (KeyboardInterrupt, InterruptedError):
                if self._on_interrupt_during_handler is not None:
                    try:
                        await self._on_interrupt_during_handler()
                        continue
                    except KeyboardInterrupt:
                        pass
                if self._on_keyboard_interrupt():
                    print_success("再见！")
                    break
            except asyncio.CancelledError:
                break


ReadLineFn = Callable[[], Awaitable[str | None]]


class SnapshotAction(str, Enum):
    NEW = "new"
    RESTORE = "restore"
    CANCEL = "cancel"


@dataclass(frozen=True)
class SnapshotSelection:
    action: SnapshotAction
    snapshot: WorkspaceSnapshot | None = None


@dataclass(frozen=True)
class VisibleConversationEntry:
    role: Literal["用户", "助手"]
    text: str


def visible_conversation_entries(messages) -> list[VisibleConversationEntry]:
    """Keep only human-readable turns when replaying a restored conversation."""
    entries = []
    for message in messages:
        if isinstance(message, ModelRequest):
            parts = []
            for part in message.parts:
                if not isinstance(part, UserPromptPart):
                    continue
                content = part.content
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, (list, tuple)) and content:
                    # UserMessage.to_prompt() keeps the original request first;
                    # later entries are references, media, or runtime metadata.
                    if isinstance(content[0], str):
                        parts.append(content[0])
            role = "用户"
        elif isinstance(message, ModelResponse):
            parts = [part.content for part in message.parts if isinstance(part, TextPart)]
            role = "助手"
        else:
            continue
        text = "\n".join(part for part in parts if part.strip())
        if text:
            entries.append(VisibleConversationEntry(role, text))
    return entries


def format_snapshot_choices(snapshots: list[WorkspaceSnapshot]) -> str:
    return "\n".join(
        [
            "选择新建会话或恢复（新 → 旧）：",
            "",
            "  0. 新建会话",
            *(f"  {index}. {snapshot.label}" for index, snapshot in enumerate(snapshots, 1)),
            "  c. 取消",
            "",
            "输入序号恢复，输入 0 新建，留空或 c 取消。",
        ]
    )


async def legacy_pick_snapshot(
    snapshots: list[WorkspaceSnapshot],
    read_line: ReadLineFn,
) -> SnapshotSelection:
    print_panel(format_snapshot_choices(snapshots), title="加载对话")
    while True:
        try:
            raw = await read_line()
        except (KeyboardInterrupt, InterruptedError):
            raw = None
        if raw is None:
            return SnapshotSelection(SnapshotAction.CANCEL)
        text = raw.strip()
        if not text or text.lower() in ("c", "cancel"):
            return SnapshotSelection(SnapshotAction.CANCEL)
        if text == "0":
            return SnapshotSelection(SnapshotAction.NEW)
        if not text.isdigit():
            print_error("请输入有效序号。")
            continue
        index = int(text)
        if index < 1 or index > len(snapshots):
            print_error(f"序号超出范围（1-{len(snapshots)}）。")
            continue
        snapshot = snapshots[index - 1]
        if not snapshot.is_loadable:
            print_error("该会话条目无法加载，请选择其他会话或新建会话。")
            continue
        return SnapshotSelection(SnapshotAction.RESTORE, snapshot)




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
    def __init__(self, app: Any, log: RichLog) -> None:
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




class UsagePanel(VerticalScroll):
    """Persistent usage widgets; refresh updates values without rebuilding layout."""

    def compose(self) -> ComposeResult:
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

    def update_snapshot(self, snapshot: Any) -> None:
        """Render independent content, API, Agent and plan counters without rebuilding widgets."""
        self.query_one("#panel-content", Static).update(render_panel(snapshot))
        self._update_content_chart(snapshot)
        self._update_api_usage(snapshot.history)
        self._update_agent_counts(snapshot.runtime)


    def _update_content_chart(self, snapshot) -> None:
        """Show once-counted content and mark measurements with incomplete coverage."""
        content = snapshot.content
        complete = content.complete and not snapshot.history.skipped_count
        total = content.input_tokens + content.output_tokens
        for key, value in (
            ("input", content.input_tokens),
            ("output", content.output_tokens - content.reasoning_tokens),
            ("reasoning", content.reasoning_tokens),
        ):
            bar = self.query_one("#panel-comp-" + key, ProgressBar)
            bar.display = complete and total > 0
            bar.update(total=total or 1, progress=value)
            text = f"{value:,} tokens"
            if key == "input":
                text += "（估算）"
            if key != "input" and content.missing_reasoning_responses:
                text = (f"未知（总输出 {content.output_tokens:,} tokens）" if key == "output"
                        else f"已报告 {content.reasoning_tokens:,} tokens；其余未知")
            elif complete:
                text += f"  {value / total * 100 if total else 0:.1f}%"
            self.query_one("#panel-value-" + key, Static).update(text)
        notes = ["用户输入及引用文本只计一次；不含系统提示词、旧回复和工具结果。"]
        if not complete:
            notes.append("统计不完整，暂不展示完整占比。")
        if content.incomplete_sessions:
            notes.append(f"{content.incomplete_sessions} 个旧会话输入统计不完整；输入仅为已统计部分。")
        if content.unmetered_attachments:
            notes.append(f"未计量附件 {content.unmetered_attachments} 个。")
        if content.missing_reasoning_responses:
            notes.append(f"{content.missing_reasoning_responses} 次响应推理明细未知。")
        if content.missing_usage_responses:
            notes.append(f"{content.missing_usage_responses} 次响应未报告用量。")
        self.query_one("#panel-content-note", Static).update("\n".join(notes))


    def _update_api_usage(self, history) -> None:
        """Keep provider request accounting separate from unique input estimates."""
        self.query_one("#panel-api-usage", Static).update(
            f"输入 {history.input_tokens:,} tokens · 输出 {history.output_tokens:,} tokens（含推理）\n"
            f"输入缓存：命中 {history.cache_hit_tokens:,} · 未命中 {history.cache_miss_tokens:,} · "
            f"未报告 {max(0, history.input_tokens - history.cache_hit_tokens - history.cache_miss_tokens):,} tokens"
        )


    def _update_agent_counts(self, runtime) -> None:
        """Display live Agents separately from the optional planning checklist."""
        self.query_one("#panel-agent-counts", Static).update(
            "暂不可用" if runtime.active_invocations_error else
            Text.assemble((f"Running {runtime.running_agents}", "cyan"), "   ",
                          (f"Queued {runtime.queued_agents}", "yellow"))
        )
        tasks = runtime.tasks
        task_total = tasks.total or 0
        self.query_one("#panel-task-progress", ProgressBar).display = task_total > 0
        self.query_one("#panel-task-progress", ProgressBar).update(
            total=task_total or 1, progress=tasks.completed or 0
        )
        self.query_one("#panel-task-counts", Static).update(Text.assemble(
            (f"✓ Completed {tasks.completed}/{task_total}", "green"), "   ",
            (f"⟳ Running {tasks.running}", "cyan"), "   ",
            (f"✗ Failed {tasks.failed}", "red"), "   ",
            (f"… Pending {tasks.pending}", "yellow"),
        ) if task_total else "暂无计划任务")


READY_LABEL = "就绪"
PREPARING_LABEL = "正在准备会话…"
WORKING_LABEL = "工作中"
WORKING_FRAMES = ("", ".", "..", "...")


class RunStatus(Static):
    """Render the same interaction state on each native Textual refresh."""

    def _is_working(self) -> bool:
        return bool(
            (self.app._ask_future is not None and not self.app._ask_future.done())
            or self.app._active_line_handlers > 0
            or self.app.system.has_current_turn
            or self.app.system._session.queue.pending
        )


    def _mode_chip(self) -> Text:
        label, color = {
            TuiRunMode.REVIEW: (" ⏵ 审查模式 ", "cyan"),
            TuiRunMode.PASS: (" ⏵⏵ 放行模式 ", "green"),
            TuiRunMode.GOAL: (" ◎ 目标模式 ", "yellow"),
        }[self.app._run_mode]
        return Text(label, style=f"bold black on {color}")


    def render(self) -> Any:
        if self.app._panel_mode:
            return Text(str(self.app.query_one("#panel-view").border_title), style="bold")
        if self.app._review_mode:
            return Text(
                "审查改动中    ·    y 保留    ·    n 撤销    ·    ↑↓ 切换    ·    Esc 退出",
                style="bold",
            )
        text = Text.assemble(self._mode_chip(), "  ")
        if self.app._startup_locked and not (
            self.app._ask_future is not None and not self.app._ask_future.done()
        ):
            text.append(PREPARING_LABEL, style="dim")
            return text
        if not self._is_working():
            self.app._working_frame = 0
            text.append(READY_LABEL, style="dim")
            if self.app._run_mode == TuiRunMode.REVIEW and self.app._pending_count > 0:
                text.append("       ")
                text.append(
                    f" ⚑ 待审查 {self.app._pending_count} 处 · 按 Ctrl+R 审查 ",
                    style="bold black on yellow",
                )
            return text
        suffix = WORKING_FRAMES[self.app._working_frame % len(WORKING_FRAMES)]
        self.app._working_frame += 1
        if self.app.system.has_current_goal_turn:
            iteration = self.app.system.current_goal_iteration
            label = f"目标循环第 {iteration} 轮" if iteration else "目标循环"
            text.append(f"{label}{suffix}")
        else:
            text.append(f"{WORKING_LABEL}{suffix}")
        return text
