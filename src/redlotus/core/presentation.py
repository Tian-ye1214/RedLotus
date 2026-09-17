"""Terminal presentation: output adapters, streams, file diffs, and usage/status panels."""

from __future__ import annotations

import sys
import difflib
import math
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, Callable
from rich.console import Console, Group
from rich.text import Text
from enum import StrEnum
from rich.panel import Panel
from redlotus.tools.interaction import UserMessage
from redlotus.core import config as logger
from rich.markdown import Markdown
from rich.align import Align
from pathlib import Path
from rich.table import Table
from redlotus.core.session import conversations_root
from redlotus.core.history import (
    MODEL_MESSAGES_GLOB,
    UsageTotals,
    ContentTokenStats,
    read_usage_messages,
    latest_usage_input_tokens,
    summarize_messages,
)


@dataclass(frozen=True)
class ContextUsageItem:
    role_label: str
    used_tokens: int
    max_tokens: int
    percent: float


class OutputSink(Protocol):
    supports_model_stream: bool

    def emit(self, renderable: Any) -> None: ...
    def update(self, action: str, *args) -> None: ...


class LegacyOutputSink:
    supports_model_stream = False

    def __init__(self, console: Console) -> None:
        self.console = console

    def emit(self, renderable: Any) -> None:
        if isinstance(renderable, str) and "\x1b[" in renderable:
            renderable = Text.from_ansi(renderable)
        self.console.print(renderable)

    def update(self, action: str, *args) -> None:
        if action == "rule":
            self.console.rule(*args)
        elif action == "set_status":
            self.console.print(Text(args[0], style="dim"))


_console = Console(highlight=False, legacy_windows=sys.platform == "win32")
_sink: OutputSink = LegacyOutputSink(_console)


def set_output_sink(sink: OutputSink | None) -> None:
    global _sink
    _sink = sink if sink is not None else LegacyOutputSink(_console)


def supports_model_stream() -> bool:
    return _sink.supports_model_stream


def emit_renderable(renderable: Any) -> None:
    _sink.emit(renderable)


def emit_rule(title: str) -> None:
    _sink.update("rule", title)


def set_status(message: str) -> None:
    _sink.update("set_status", message)


def clear_status() -> None:
    _sink.update("clear_status")


def set_context_usage(items: list[ContextUsageItem]) -> None:
    _sink.update("set_context_usage", items)


def clear_context_usage() -> None:
    _sink.update("clear_context_usage")


def begin_model_stream(title: str) -> None:
    _sink.update("begin_model_stream", title)


def append_model_stream_delta(text: str) -> None:
    _sink.update("append_model_stream_delta", text)


def clear_model_stream() -> None:
    _sink.update("clear_model_stream")


@contextmanager
def status_message(message: str):
    set_status(message)
    try:
        yield
    finally:
        clear_status()


class DiffKind(StrEnum):
    ADD = "add"
    DEL = "del"
    MOD = "mod"
    CTX = "ctx"
    GAP = "gap"


@dataclass(frozen=True)
class DiffStyle:
    max_lines: int = 300
    colors: dict[DiffKind, str] = field(
        default_factory=lambda: {
            DiffKind.ADD: "green",
            DiffKind.DEL: "red",
            DiffKind.MOD: "blue",
            DiffKind.CTX: "dim",
            DiffKind.GAP: "dim italic",
        }
    )
    signs: dict[DiffKind, str] = field(
        default_factory=lambda: {
            DiffKind.ADD: "+",
            DiffKind.DEL: "-",
            DiffKind.MOD: "~",
            DiffKind.CTX: " ",
            DiffKind.GAP: " ",
        }
    )


DEFAULT_STYLE = DiffStyle()


@dataclass(frozen=True)
class DiffLine:
    kind: DiffKind
    old_no: int | None
    new_no: int | None
    text: str


def compute_line_diff(old: str, new: str, *, context: int = 3) -> list[DiffLine]:
    """按行对比 old→new，返回带类别和行号的 DiffLine 列表；长未改段折叠为 gap。"""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    ops = sm.get_opcodes()
    out: list[DiffLine] = []
    for idx, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            out += _equal_block(old_lines, i1, i2, j1, context=context, first=idx == 0, last=idx == len(ops) - 1)
        elif tag == "insert":
            out += [DiffLine(DiffKind.ADD, None, j1 + k + 1, line) for k, line in enumerate(new_lines[j1:j2])]
        elif tag == "delete":
            out += [DiffLine(DiffKind.DEL, i1 + k + 1, None, line) for k, line in enumerate(old_lines[i1:i2])]
        elif tag == "replace":
            out += [DiffLine(DiffKind.MOD, i1 + k + 1, None, line) for k, line in enumerate(old_lines[i1:i2])]
            out += [DiffLine(DiffKind.MOD, None, j1 + k + 1, line) for k, line in enumerate(new_lines[j1:j2])]
    return out


def _equal_block(old_lines, i1, i2, j1, *, context, first, last) -> list[DiffLine]:
    n = i2 - i1

    def ctx(off: int) -> DiffLine:
        return DiffLine(DiffKind.CTX, i1 + off + 1, j1 + off + 1, old_lines[i1 + off])

    top = 0 if first else context
    bot = 0 if last else context
    if top + bot >= n:
        return [ctx(k) for k in range(n)]
    hidden = n - top - bot
    return (
        [ctx(k) for k in range(top)]
        + [DiffLine(DiffKind.GAP, None, None, f"⋯ {hidden} 行未改动 ⋯")]
        + [ctx(k) for k in range(n - bot, n)]
    )


def diff_stats(lines: list[DiffLine]) -> tuple[int, int, int]:
    """统计 (新增, 删除, 改动) 行数；改动按新侧计。"""
    add = sum(1 for ln in lines if ln.kind is DiffKind.ADD)
    dele = sum(1 for ln in lines if ln.kind is DiffKind.DEL)
    mod = sum(1 for ln in lines if ln.kind is DiffKind.MOD and ln.new_no is not None)
    return add, dele, mod


def _gutter_width(lines: list[DiffLine]) -> int:
    nums = [n for ln in lines for n in (ln.old_no, ln.new_no) if n is not None]
    return max((len(str(n)) for n in nums), default=1)


def _line_text(ln: DiffLine, width: int, signs: dict[DiffKind, str]) -> str:
    sign = signs[ln.kind]
    if ln.kind is DiffKind.GAP:
        return f"{sign} {'':>{width}}  {ln.text}"
    no = ln.new_no if ln.new_no is not None else ln.old_no
    return f"{sign} {no:>{width}}  {ln.text}"


def render_diff(
    lines: list[DiffLine], *, path: str, stats: tuple[int, int, int], style: DiffStyle = DEFAULT_STYLE
) -> Panel:
    """渲染成带行号、彩色、可折叠、超长截断的 rich Panel。"""
    add, dele, mod = stats
    width = _gutter_width(lines)
    body = Text()
    for idx, ln in enumerate(lines[: style.max_lines]):
        if idx:
            body.append("\n")
        body.append(_line_text(ln, width, style.signs), style=style.colors[ln.kind])
    if len(lines) > style.max_lines:
        body.append(f"\n… 还有 {len(lines) - style.max_lines} 行（已截断）", style=style.colors[DiffKind.GAP])
    title = Text.assemble(
        (f"{path}  ", "bold"),
        (f"+{add} ", style.colors[DiffKind.ADD]),
        (f"-{dele} ", style.colors[DiffKind.DEL]),
        (f"~{mod}", style.colors[DiffKind.MOD]),
    )
    return Panel(body, title=title, title_align="left", border_style="cyan")


def format_diff_text(
    lines: list[DiffLine], *, path: str, stats: tuple[int, int, int], style: DiffStyle = DEFAULT_STYLE
) -> str:
    """无样式纯文本版（写日志 / bot 用）。"""
    add, dele, mod = stats
    width = _gutter_width(lines)
    return "\n".join([f"{path}  +{add} -{dele} ~{mod}", *(_line_text(ln, width, style.signs) for ln in lines)])


def print_startup_logo() -> None:
    stdout_encoding = (getattr(sys.stdout, "encoding", None) or "utf-8").lower()
    unicode_safe = "utf" in stdout_encoding

    def color_text(r: int, g: int, b: int, text: str) -> str:
        return f"\033[38;2;{r};{g};{b}m{text}\033[0m"

    logo_lines = [
        "██████╗ ███████╗██████╗ ██╗      ██████╗ ████████╗██╗   ██╗███████╗",
        "██╔══██╗██╔════╝██╔══██╗██║     ██╔═══██╗╚══██╔══╝██║   ██║██╔════╝",
        "██████╔╝█████╗  ██║  ██║██║     ██║   ██║   ██║   ██║   ██║███████╗",
        "██╔══██╗██╔══╝  ██║  ██║██║     ██║   ██║   ██║   ██║   ██║╚════██║",
        "██║  ██║███████╗██████╔╝███████╗╚██████╔╝   ██║   ╚██████╔╝███████║",
        "╚═╝  ╚═╝╚══════╝╚═════╝ ╚══════╝ ╚═════╝    ╚═╝    ╚═════╝ ╚══════╝",
    ]
    shadow_char = "░" if unicode_safe else "."
    shadow_color = (70, 40, 60)
    start_color = (255, 80, 60)
    end_color = (140, 50, 130)

    max_width = max(len(line) for line in logo_lines)
    foreground = {
        (row, column): character
        for row, line in enumerate(logo_lines)
        for column, character in enumerate(line) if character != " "
    }
    canvas = {
        (row + 1, column + 2): (shadow_char, shadow_color)
        for row, column in foreground
    }
    for (row, column), character in foreground.items():
        ratio = column / (max_width - 1)
        color = tuple(int(start + (end - start) * ratio) for start, end in zip(start_color, end_color))
        canvas[row, column] = character, color

    emit_renderable("")
    for row in range(len(logo_lines) + 1):
        cells = (canvas.get((row, column), (" ", None)) for column in range(max_width + 2))
        emit_renderable("".join(color_text(*color, char) if color else char for char, color in cells))

    if unicode_safe:
        tagline = "❦ ────  红莲极意  ·  RedLotus Agent  ──── ❦"
    else:
        tagline = "<>----  RedLotus Agent  ----<>"
    pad = max(0, (max_width + 2 - len(tagline)) // 2)
    emit_renderable(" " * pad + color_text(255, 165, 90, tagline))
    emit_renderable("")


def format_user_log_text(message: UserMessage) -> str:
    """供任务 .log 落盘：用户可见文本 + 附件说明。"""
    t = (message.text or "").strip()
    n = len(message.attachments or [])
    if n and t:
        return f"{t}\n（含 {n} 个多媒体附件）"
    if n:
        return f"（仅 {n} 个多媒体附件，无文本）"
    return t or "（空文本）"


if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            try:
                _s.reconfigure(encoding="utf-8")
            except Exception:
                pass

class OutputConsoleProxy:
    def print(self, *objects: Any, **_: Any) -> None:
        for obj in objects:
            emit_renderable(obj)


console = OutputConsoleProxy()
STREAM_PREVIEW_MAX_LINES = 10
STREAM_PREVIEW_MAX_CHARS = 6000


def render_review_hunk(name, hunk, status=None) -> Text:
    """Render every changed line and the review decision without truncation."""
    body = Text.assemble((f"{name}  {hunk.location}\n", "bold"))
    for sign, lines, color in (("-", hunk.old_lines, "red"), ("+", hunk.new_lines, "green")):
        for line in lines:
            body.append(f"  {sign} {line.rstrip()}\n", style=color)
    label, style = {
        "keep": ("  → ✓ 已保留", "bold green"),
        "undo": ("  → ✗ 已撤销", "bold red"),
    }.get(status, ("  …待决定（y 保留 / n 撤销）", "dim"))
    body.append(label, style=style)
    return body


def context_usage_renderable(items) -> Align:
    """Render the context bar shared by terminal status displays."""
    parts = []
    for item in items:
        filled = math.ceil(8 * max(0.0, min(100.0, float(item.percent))) / 100.0)
        bar = "[" + "=" * filled + "." * (8 - filled) + "]"
        parts.append(f"{item.role_label} {bar} {item.percent:.0f}%")
    return Align.right(Text("  ".join(parts), style="dim"))


def user_text_panel(content, title, *, text_style="bold white", border_style="bright_blue") -> Panel:
    """Frame user text and streaming output with the same terminal panel layout."""
    return Panel(
        Text(content or " ", style=text_style), title=title, title_align="left",
        border_style=border_style, padding=(0, 1), expand=False,
    )


def model_stream_visible_text(text) -> str:
    """Bound only the transient preview; the full response remains in the log."""
    body = text or ""
    if len(body) > STREAM_PREVIEW_MAX_CHARS:
        body = body[-STREAM_PREVIEW_MAX_CHARS:].lstrip("\n")
    lines = body.splitlines()
    return "\n".join(lines[-STREAM_PREVIEW_MAX_LINES:]) if len(lines) > STREAM_PREVIEW_MAX_LINES else body


def print_error(message: str) -> None:
    emit_renderable(Text(f"Error: {message}", style="bold red"))


def print_warning(message: str) -> None:
    emit_renderable(Text(f"Warning: {message}", style="yellow"))


def print_success(message: str) -> None:
    emit_renderable(Text(message, style="green"))


def print_markdown(text: str) -> None:
    body = (text or "").strip()
    if not body:
        return
    emit_renderable(Markdown(body))


def print_panel(content: str, *, title: str = "") -> None:
    emit_renderable(Panel(content, title=title or None))


def print_markdown_panel(text: str, *, title: str = "") -> None:
    body = (text or "").strip()
    if not body:
        return
    emit_renderable(Panel(Markdown(body), title=title or None, border_style="cyan"))


def show_file_diff(old: str, new: str, *, path: str) -> tuple[int, int, int]:
    """在 CLI 打印 old→new 的彩色 diff（增绿/删红/改蓝）；无变化则不输出。返回 (增, 删, 改)。"""
    lines = compute_line_diff(old, new)
    stats = diff_stats(lines)
    if stats == (0, 0, 0):
        return stats
    emit_renderable(render_diff(lines, path=path, stats=stats))
    logger.info_file_only("[diff] %s\n%s", path, format_diff_text(lines, path=path, stats=stats))
    return stats


def show_model_output(text: str, *, title: str = "模型", markdown: bool = True) -> None:
    """Rich 渲染模型输出；原文仅写入日志文件。纯文本汇总请设 markdown=False 以保留换行。"""
    body = (text or "").strip()
    if not body:
        return
    content: str | Markdown = Markdown(body) if markdown else body
    emit_renderable(Panel(content, title=title, border_style="cyan"))
    logger.info_file_only("[模型]\n%s", body)


def finish_model_stream(text: str, *, title: str = "模型", markdown: bool = True) -> None:
    clear_model_stream()
    show_model_output(text, title=title, markdown=markdown)


def _text_from_stream_event(event: Any) -> str:
    event_kind = getattr(event, "event_kind", "")
    if event_kind == "part_start":
        part = getattr(event, "part", None)
        if getattr(part, "part_kind", None) == "text":
            return getattr(part, "content", "") or ""
    if event_kind == "part_delta":
        delta = getattr(event, "delta", None)
        if getattr(delta, "part_delta_kind", None) == "text":
            return getattr(delta, "content_delta", "") or ""
    return ""


class TextEventStreamHandler:
    def __init__(self, *, title: str) -> None:
        self.title = title
        self._started = False

    async def __call__(self, _run_ctx: Any, event_stream: Any) -> None:
        try:
            async for event in event_stream:
                text = _text_from_stream_event(event)
                if not text:
                    continue
                if not self._started:
                    begin_model_stream(f"{self.title} 正在回复")
                    self._started = True
                append_model_stream_delta(text)
        except BaseException:
            if self._started:
                clear_model_stream()
                self._started = False
            raise


def print_phase(title: str) -> None:
    emit_rule(f"[dim]{title}[/dim]")
    logger.info_file_only(title)


def print_repl_welcome() -> None:
    print_panel(
        "输入 /help 查看斜杠命令；@文件路径 引用文本\n"
        "新任务 或 /clear 开启新对话 · /exit 或 quit 退出\n"
        "任务执行中按 Ctrl+C 中断",
        title="RedLotus CLI",
    )


Reader = Callable[[Path], tuple[list[Any], dict[str, Any]]]
RECENT_SESSION_LIMIT = 20


@dataclass
class PanelSessionSummary(UsageTotals):
    date: str = ""
    topic: str = ""
    agents: set[str] = field(default_factory=set)
    file_count: int = 0
    saved_at: str = ""


@dataclass
class PanelHistoryStats(UsageTotals):
    file_count: int = 0
    conversation_count: int = 0
    by_agent: dict[str, UsageTotals] = field(default_factory=dict)
    by_model: dict[str, UsageTotals] = field(default_factory=dict)
    skipped_count: int = 0
    skipped_files: list[str] = field(default_factory=list)
    content: ContentTokenStats = field(default_factory=ContentTokenStats)


@dataclass
class TaskPanelStats:
    total: int = 0
    completed: int = 0
    running: int = 0
    failed: int = 0
    pending: int = 0


@dataclass
class RuntimePanelStats:
    session_key: str = "-"
    active_invocations: int = 0
    active_invocations_error: bool = False
    running_agents: int = 0
    queued_agents: int = 0
    context_input_tokens: dict[str, int] = field(default_factory=dict)
    tasks: TaskPanelStats = field(default_factory=TaskPanelStats)


@dataclass
class PanelSnapshot:
    runtime: RuntimePanelStats
    history: PanelHistoryStats
    visible_sessions: list[PanelSessionSummary]
    include_all: bool = False
    # 可见会话按时间从旧到新排列的每会话总 token 数（供 Sparkline 趋势图使用）
    token_trend: list[int] = field(default_factory=list)

    @property
    def content(self):
        return self.history.content


class PanelSnapshotCache:
    def __init__(self, *, reader: Reader | None = None):
        self._reader = reader or read_usage_messages
        self._cache = {}

    def load(self, path: Path):
        try:
            stat = path.stat()
            signature = (
                stat.st_mtime_ns,
                stat.st_size,
            )
            cached = self._cache.get(path)
            if cached and cached[0] == signature:
                return cached[1]
            messages, meta = self._reader(path)
            result = summarize_messages(
                messages, meta=meta, path=path, price_resolver=lambda model: None
            )
            self._cache[path] = (signature, result)
            return result
        except Exception as exc:
            return f"{path}: {type(exc).__name__}: {exc}"


async def build_panel_snapshot(
    *,
    log_root: Path | None = None,
    system: Any = None,
    coordinator_history: Any = None,
    manager_history: Any = None,
    include_all: bool = False,
    cache: PanelSnapshotCache | None = None,
) -> PanelSnapshot:
    root = Path(log_root or conversations_root())
    history, sessions = await asyncio.to_thread(
        _collect_history, root, cache or PanelSnapshotCache()
    )
    visible_sessions = sessions if include_all else sessions[:RECENT_SESSION_LIMIT]
    runtime = await _collect_runtime(system, coordinator_history, manager_history)
    token_trend = [_session_total_tokens(s) for s in reversed(visible_sessions)]
    return PanelSnapshot(
        runtime=runtime,
        history=history,
        visible_sessions=visible_sessions,
        include_all=include_all,
        token_trend=token_trend,
    )


def render_panel(snapshot: PanelSnapshot) -> Panel:
    history = snapshot.history
    runtime = snapshot.runtime
    parts = [
        _render_kpis(snapshot),
        _render_runtime(runtime),
        _render_distribution(history),
        _render_sessions(snapshot.visible_sessions, include_all=snapshot.include_all),
    ]
    if history.skipped_count:
        parts.append(_render_skipped(history))
    return Panel(Group(*parts), title="RedLotus Panel", border_style="cyan")


def _collect_history(
    log_root: Path,
    cache: PanelSnapshotCache,
) -> tuple[PanelHistoryStats, list[PanelSessionSummary]]:
    history = PanelHistoryStats()
    sessions: dict[str, PanelSessionSummary] = {}
    files = sorted(log_root.rglob(MODEL_MESSAGES_GLOB), key=str)
    for path in files:
        summary = cache.load(path)
        if isinstance(summary, str):
            history.skipped_count += 1
            if len(history.skipped_files) < 5:
                history.skipped_files.append(summary)
            continue
        date, topic = summary.meta["date"], summary.meta["topic"]
        history.file_count += 1
        history.add_totals(summary.totals)
        history.content.add(summary.content)
        for role, totals in summary.by_agent.items():
            history.by_agent.setdefault(role, UsageTotals()).add_totals(totals)
        for model, usage in summary.by_model.items():
            history.by_model.setdefault(model, UsageTotals()).add_totals(usage.totals)
        session = sessions.setdefault(
            summary.meta["session_id"], PanelSessionSummary(date=date, topic=topic)
        )
        session.add_totals(summary.totals)
        session.file_count += 1
        session.agents.update(summary.by_agent)
        session.saved_at = max(
            session.saved_at, str(summary.meta.get("saved_at") or "")
        )

    ordered_sessions = sorted(
        sessions.values(), key=lambda s: (s.saved_at, s.date, s.topic), reverse=True
    )
    history.conversation_count = len(ordered_sessions)
    return history, ordered_sessions


async def _collect_runtime(
    system: Any,
    coordinator_history: Any,
    manager_history: Any,
) -> RuntimePanelStats:
    runtime = RuntimePanelStats()
    runtime.session_key = str(getattr(system, "session_key", "") or "-")
    runtime.context_input_tokens = {
        "Coordinator": latest_usage_input_tokens(
            getattr(coordinator_history, "messages", []) or []
        )
        or 0,
        "Manager": latest_usage_input_tokens(
            getattr(manager_history, "messages", []) or []
        )
        or 0,
    }
    runtime.tasks = _collect_task_stats(getattr(system, "_task_manager", None))
    if system is not None:
        try:
            runtime.running_agents, runtime.queued_agents = system._factory.activity(system.session_key)
            runtime.running_agents += int(system._session.active)
            runtime.active_invocations = runtime.running_agents
        except Exception:
            runtime.active_invocations_error = True
    return runtime


def _collect_task_stats(task_manager: Any) -> TaskPanelStats:
    tasks = getattr(task_manager, "tasks", {}) or {}
    stats = TaskPanelStats(total=len(tasks))
    for task in tasks.values():
        value = getattr(
            getattr(task, "status", None), "value", getattr(task, "status", "")
        )
        if value == "completed":
            stats.completed += 1
        elif value == "running":
            stats.running += 1
        elif value == "failed":
            stats.failed += 1
        else:
            stats.pending += 1
    return stats


def _render_kpis(snapshot: PanelSnapshot) -> Table:
    history = snapshot.history
    runtime = snapshot.runtime
    table = Table.grid(expand=True)
    for _ in range(4):
        table.add_column(justify="left")
    table.add_row(
        f"历史对话 {history.conversation_count}",
        f"model_messages {history.file_count}",
        f"responses {history.responses}",
        f"Agent Running {'暂不可用' if runtime.active_invocations_error else runtime.running_agents}",
    )
    table.add_row(
        f"session {runtime.session_key}",
        f"missing usage {history.missing_usage_responses}",
        f"skipped {history.skipped_count}",
        f"tasks {runtime.tasks.completed}/{runtime.tasks.total}",
    )
    return table


def _new_table(
    title: str, columns: tuple[str, ...], *, right_columns: tuple[str, ...] = ()
) -> Table:
    table = Table(title=title, expand=True)
    for column in columns:
        table.add_column(column, justify="right" if column in right_columns else "left")
    return table


def _render_runtime(runtime: RuntimePanelStats) -> Table:
    table = _new_table("当前运行态", ("项目", "值"), right_columns=("值",))
    for label, tokens in runtime.context_input_tokens.items():
        table.add_row(f"{label} context input", _fmt_int(tokens))
    return table


def _render_distribution(history: PanelHistoryStats) -> Table:
    table = _new_table(
        "分布",
        ("类型", "名称", "Responses", "Tokens", "占比"),
        right_columns=("Responses", "Tokens"),
    )
    rows: list[tuple[str, str, UsageTotals]] = []
    rows.extend(("Agent", name, bucket) for name, bucket in history.by_agent.items())
    rows.extend(("Model", name, bucket) for name, bucket in history.by_model.items())
    rows.sort(
        key=lambda row: (
            _session_total_tokens(row[2]),
            row[2].responses,
            row[1],
        ),
        reverse=True,
    )
    if not rows:
        table.add_row("-", "暂无分布数据", "0", "0", "")
        return table
    top = rows[:12]
    max_tokens = max(
        (_session_total_tokens(b) for _, _, b in top),
        default=0,
    )
    for kind, name, bucket in top:
        tokens = _session_total_tokens(bucket)
        bar = Text(
            _block_bar(tokens, max_tokens),
            style="cyan" if kind == "Agent" else "magenta",
        )
        table.add_row(kind, name, str(bucket.responses), _fmt_int(tokens), bar)
    return table


def _session_total_tokens(session: PanelSessionSummary) -> int:
    return session.input_tokens + session.output_tokens


def _render_sessions(
    sessions: list[PanelSessionSummary], *, include_all: bool
) -> Table:
    title = (
        "历史对话（全部）"
        if include_all
        else f"历史对话（最近 {RECENT_SESSION_LIMIT}）"
    )
    table = _new_table(
        title,
        ("时间", "Topic", "Agents", "Responses", "Tokens"),
        right_columns=("Responses", "Tokens"),
    )
    if not sessions:
        table.add_row("-", "暂无历史对话", "-", "0", "0")
        return table
    for session in sessions:
        agents = ",".join(sorted(session.agents))
        tokens = _session_total_tokens(session)
        table.add_row(
            session.saved_at or session.date,
            session.topic,
            agents,
            str(session.responses),
            _fmt_int(tokens),
        )
    return table


def _render_skipped(history: PanelHistoryStats) -> Text:
    text = Text()
    text.append(
        f"Skipped corrupt model_messages: {history.skipped_count}\n", style="yellow"
    )
    for item in history.skipped_files:
        text.append(f"- {item}\n", style="dim yellow")
    return text


_BLOCK_EIGHTHS = " ▏▎▍▌▋▊▉█"


def _block_bar(value: int, max_value: int, *, width: int = 12) -> str:
    if max_value <= 0 or value <= 0:
        return ""
    eighths = round(width * 8 * min(value, max_value) / max_value)
    full, rem = divmod(eighths, 8)
    bar = "█" * full
    if rem:
        bar += _BLOCK_EIGHTHS[rem]
    return bar or _BLOCK_EIGHTHS[1]


def _fmt_int(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)
