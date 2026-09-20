"""Presentation panels responsibilities."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from redlotus.models.context import MODEL_MESSAGES_GLOB, latest_usage_input_tokens
from redlotus.models.usage import (
    ContentTokenStats,
    UsageTotals,
    read_usage_messages,
    summarize_messages,
)
from redlotus.presentation.output import Reader
from redlotus.runtime.context import conversations_root

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
                tuple((child.name, child.stat().st_mtime_ns, child.stat().st_size)
                      for child in sorted(path.parent.glob("model_messages.*.json"))),
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
    return PanelSnapshot(
        runtime=runtime,
        history=history,
        visible_sessions=visible_sessions,
        include_all=include_all,
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
    """Compare labeled session totals without implying continuous time or quota progress."""
    scope = "全部" if include_all else f"最近 {RECENT_SESSION_LIMIT} 个"
    table = Table(title=f"会话 API 用量 · {scope} · 最近活动优先", expand=True, show_lines=True)
    table.add_column("时间 / 会话", min_width=12, ratio=1, overflow="fold")
    table.add_column("相对用量", width=12, overflow="crop", no_wrap=True)
    table.add_column("API 总 Token", justify="right", min_width=13, no_wrap=True)
    if not sessions:
        table.add_row("暂无会话用量", "", "—")
        return table
    maximum = max((_session_total_tokens(s) for s in sessions if not s.missing_usage_responses), default=0)
    table.caption = "API 输入＋输出，包含历史重发；条长按列表中完整用量的最大值比较。"
    for session in sessions:
        tokens = _session_total_tokens(session)
        when = (datetime.fromisoformat(session.saved_at).astimezone().strftime("%m-%d %H:%M")
                if session.saved_at else session.date or "—")
        label = Text(when + "\n", style="dim")
        label.append(session.topic or "未命名会话", style="bold")
        label.append(f"\n{', '.join(sorted(session.agents)) or '—'} · {session.responses:,} 次响应", style="dim")
        value = f"{tokens:,}"
        if session.missing_usage_responses:
            value = f"{tokens:,}\n已报告部分" if tokens else "用量未知"
        table.add_row(
            label,
            Text(_block_bar(tokens, maximum) if not session.missing_usage_responses else "", style="cyan"),
            Text(value, style="yellow" if session.missing_usage_responses else "bold"),
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
