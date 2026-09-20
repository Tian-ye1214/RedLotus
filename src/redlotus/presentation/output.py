"""Presentation output responsibilities."""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable, Protocol

from rich.align import Align
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

import redlotus.runtime.resources as _runtime_resources
from redlotus.documents.review import DiffKind, DiffLine, _gutter_width, _line_text
from redlotus.runtime.context import EventEmitter


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


_console = Console(highlight=False, legacy_windows=sys.platform == "win32")


_sink: OutputSink = LegacyOutputSink(_console)


def set_output_sink(sink: OutputSink | None) -> None:
    global _sink
    _sink = sink if sink is not None else LegacyOutputSink(_console)


def supports_model_stream() -> bool:
    return _sink.supports_model_stream


def emit_renderable(renderable: Any) -> None:
    _sink.emit(renderable)


def emit(kind, *args):
    """Send a presentation update through the currently selected output sink."""
    return _sink.update(kind, *args)


emit_rule = partial(emit, "rule")
set_context_usage = partial(emit, "set_context_usage")
clear_context_usage = partial(emit, "clear_context_usage")
begin_model_stream = partial(emit, "begin_model_stream")
append_model_stream_delta = partial(emit, "append_model_stream_delta")
end_model_stream = partial(emit, "end_model_stream")
clear_model_stream = partial(emit, "clear_model_stream")




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


def show_file_diff(lines, *, path: str, stats) -> None:
    """Render the tool's already calculated diff through the selected terminal sink."""
    emit_renderable(render_diff(lines, path=path, stats=stats))


def show_model_output(text: str, *, title: str = "模型", markdown: bool = True) -> None:
    """Rich 渲染模型输出；原文仅写入日志文件。纯文本汇总请设 markdown=False 以保留换行。"""
    body = (text or "").strip()
    if not body:
        return
    content: str | Markdown = Markdown(body) if markdown else body
    emit_renderable(Panel(content, title=title, border_style="cyan"))
    _runtime_resources.info_file_only("[模型]\n%s", body)


def finish_model_stream(text: str, *, title: str = "模型", markdown: bool = True) -> None:
    end_model_stream("已完成")
    show_model_output(text, title=title, markdown=markdown)


def _text_from_stream_event(event: Any) -> tuple[str, str]:
    event_kind = getattr(event, "event_kind", "")
    if event_kind == "part_start":
        part = getattr(event, "part", None)
        kind = getattr(part, "part_kind", None)
        if kind in {"text", "thinking"}:
            return kind, getattr(part, "content", "") or ""
    if event_kind == "part_delta":
        delta = getattr(event, "delta", None)
        kind = getattr(delta, "part_delta_kind", None)
        if kind in {"text", "thinking"}:
            return kind, getattr(delta, "content_delta", "") or ""
    return "", ""


class TextEventStreamHandler:
    def __init__(self, *, title: str, is_current: Callable[[], bool] | None = None) -> None:
        self.title = title
        self._started = False
        self._is_current = is_current or (lambda: True)

    async def __call__(self, _run_ctx: Any, event_stream: Any) -> None:
        response_started = False
        async for event in event_stream:
            kind, text = _text_from_stream_event(event)
            if not text or not self._is_current():
                continue
            if not self._started:
                begin_model_stream(f"{self.title} 正在回复")
                self._started = True
            if not response_started:
                _sink.update("begin_model_response")
                response_started = True
            append_model_stream_delta(text, kind)


def print_phase(title: str) -> None:
    emit_rule(f"[dim]{title}[/dim]")
    _runtime_resources.info_file_only(title)


def print_repl_welcome() -> None:
    print_panel(
        "输入 /help 查看斜杠命令；@文件路径 引用文本\n"
        "新任务 或 /clear 开启新对话 · /exit 或 quit 退出\n"
        "任务执行中按 Ctrl+C 中断",
        title="RedLotus CLI",
    )


Reader = Callable[[Path], tuple[list[Any], dict[str, Any]]]


def presentation_events():
    """Compose the terminal's explicit implementation of the core event port."""
    _runtime_resources.console_log_sink = render_log
    return EventEmitter({
        "supports_model_stream": supports_model_stream,
        "TextEventStreamHandler": TextEventStreamHandler,
        "clear_model_stream": clear_model_stream,
        "end_model_stream": end_model_stream,
        "finish_model_stream": finish_model_stream,
        "print_phase": print_phase,
        "print_warning": print_warning,
        "show_model_output": show_model_output,
        "show_file_diff": show_file_diff,
    })


def render_log(message):
    """Render the runtime logger through the currently selected terminal sink."""
    level = message.record["level"].name
    emit_renderable(Text(str(message).rstrip("\n"), style={
        "DEBUG": "dim cyan", "INFO": "green", "WARNING": "yellow",
        "ERROR": "bold red", "CRITICAL": "bold white on red",
    }.get(level, "")))
