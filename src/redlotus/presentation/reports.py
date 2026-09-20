"""Terminal commands responsibilities."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from redlotus.core.agents import AgentInvocationState
from redlotus.models.context import (
    ChatHistory,
    _lookup_openrouter_meta,
    context_usage_breakdown,
    lookup_model_context,
    lookup_model_max_output_tokens,
)
from redlotus.models.providers import ModelTarget
from redlotus.models.usage import (
    UsageReport,
    model_message_files_for_path,
    session_model_message_files,
    summarize_usage_files,
)
from redlotus.presentation.output import (
    console,
    print_error,
    print_panel,
)
from redlotus.runtime.config import (
    get_agent_roles,
    get_model_and_params,
    role_supported_thinking_efforts,
)
from redlotus.runtime.context import conversations_root, current_workspace


def _out(text: str = "") -> None:
    console.print(text)


def _format_model_tokens(value: Any) -> str:
    if not isinstance(value, int) or value <= 0:
        return "-"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return str(value)


def _openrouter_agent_meta_lines(model_name: str) -> list[str]:
    meta = _lookup_openrouter_meta(model_name)
    if not meta:
        return []
    context = _format_model_tokens(lookup_model_context(model_name))
    output = _format_model_tokens(lookup_model_max_output_tokens(model_name))
    lines = [f"OpenRouter: context {context}  max_output {output}"]
    inputs = (meta.get("architecture") or {}).get("input_modalities")
    if inputs:
        lines.append("modalities: " + ", ".join(inputs))
    prices = meta.get("pricing") or {}
    if prices:
        lines.append(
            f"price: prompt={prices.get('prompt', '-')} completion={prices.get('completion', '-')}"
        )
    return lines


def print_agent_models() -> None:
    labels = {
        "manager": "Manager（任务规划）",
        "worker": "Worker（子任务执行）",
        "coordinator": "Coordinator（入口协调）",
    }
    roles = get_agent_roles()
    lines = ["当前模型配置（config.json）", ""]
    for role in roles:
        name, p = get_model_and_params(role)
        lines.append(f"• {labels.get(role, role)}")
        lines.append(f"  模型名: {name}")
        target = ModelTarget.for_role(role)
        lines.append(f"  SDK 路由: {target.protocol}")
        th_s = f"  thinking: {p.get('thinking', 'default')}"
        lines.append(
            f"  temperature: {p.get('temperature', 'default')}  max_tokens: {p.get('max_tokens', 'default')}{th_s}"
        )
        for meta_line in _openrouter_agent_meta_lines(name):
            lines.append(f"  {meta_line}")
        lines.append("")
    print_panel("\n".join(lines), title="Agent 模型")


def print_effort_settings() -> None:
    lines = ["当前思考配置（config.json）", ""]
    for role in get_agent_roles():
        _, p = get_model_and_params(role)
        effort = p.get("reasoning_effort")
        if str(p.get("thinking")).strip().lower() == "enabled" and effort:
            state = f"on · {effort}"
        else:
            state = "off"
        lines.append(f"• {role}: {state}")
    print_panel("\n".join(lines), title="思考配置")


def _effort_values_text(role: str) -> str:
    return "|".join(role_supported_thinking_efforts(role))


def _print_effort_usage(roles: tuple[str, ...]) -> None:
    role_text = "|".join(roles)
    _out(f"设置思考: /effort <{role_text}> off")
    for role in roles:
        values = _effort_values_text(role)
        if values:
            _out(f"设置思考: /effort {role} <{values}>")


def _k_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _format_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _format_usd(value: Decimal) -> str:
    return f"${_format_decimal(value)}"


def _format_usage_report(report: UsageReport) -> str:
    totals = report.totals
    lines = [
        f"Responses: {totals.responses}; missing usage: {totals.missing_usage_responses}",
        f"Tokens: input={totals.input_tokens}, output={totals.output_tokens}, reasoning={totals.reasoning_tokens}",
        f"Input cache: hit={totals.cache_hit_tokens}, miss={totals.cache_miss_tokens}, unreported={totals.input_tokens - totals.cache_hit_tokens - totals.cache_miss_tokens}",
        f"Billable: prompt={totals.prompt_billable_tokens}, completion={totals.completion_billable_tokens}",
    ]
    reported = totals.cache_hit_tokens + totals.cache_miss_tokens
    if reported:
        lines.append(
            f"Reported cache hit rate: {totals.cache_hit_tokens / reported:.2%}"
        )
    for role, usage in sorted(report.by_agent.items()):
        lines.append(f"Agent {role}: responses={usage.responses}, input={usage.input_tokens}, output={usage.output_tokens}, missing usage={usage.missing_usage_responses}, cache hit={usage.cache_hit_tokens}")
    costs = [summary.price for summary in report.by_model.values() if summary.price]
    missing = totals.missing_usage_responses + sum(
        summary.price_unavailable_responses for summary in report.by_model.values()
    )
    lines.append(
        "Estimated total: "
        + (
            (f"partly unavailable (known subtotal: {_format_usd(sum((cost.total_usd for cost in costs), Decimal(0)))})"
             if costs else "unavailable")
            if missing
            else _format_usd(sum((cost.total_usd for cost in costs), Decimal(0)))
        )
    )
    for name, summary in sorted(report.by_model.items()):
        usage, price = summary.totals, summary.price
        lines.append(
            f"{name}: responses={usage.responses}, raw={usage.input_tokens}/{usage.output_tokens}, billable={usage.prompt_billable_tokens}/{usage.completion_billable_tokens}"
        )
        lines.append(
            f"  cost: input={_format_usd(price.input_usd)}, output={_format_usd(price.output_usd)}, total={_format_usd(price.total_usd)} ({price.source})"
            if price
            else "  cost: unavailable"
        )
        if summary.price_unavailable_responses:
            lines.append(
                f"  missing price: {summary.price_unavailable_responses} responses"
            )
    lines.extend(
        [f"Files: {len(report.files)}", *(str(item.path) for item in report.files[:10])]
    )
    return "\n".join(lines)


def _conversation_session_key(system: Any) -> str | None:
    return system.session_key


async def _print_usage_report(raw: str, system: Any) -> None:
    tail = raw.strip()[6:].strip()
    if tail:
        if len(tail) >= 2 and tail[0] == tail[-1] and tail[0] in {"'", '"'}:
            tail = tail[1:-1]
        target = Path(tail).expanduser()
        if not target.is_absolute():
            target = (current_workspace() / target).resolve()
        files = model_message_files_for_path(target)
        if not files:
            print_error(f"未找到 model_messages 日志: {target}")
            return
    else:
        session_key = _conversation_session_key(system)
        if not session_key:
            print_error("当前会话尚未绑定日志目录；请使用 /usage <path>")
            return
        files = session_model_message_files(conversations_root(), session_key)
        if not files:
            print_error(f"当前会话没有 model_messages 日志: {session_key}")
            return

    try:
        report = await asyncio.to_thread(summarize_usage_files, files)
    except Exception as e:
        print_error(f"统计 usage 失败: {e}")
        return
    print_panel(_format_usage_report(report), title="Usage")


async def _print_context_usage(
    system: Any,
    coordinator_history: ChatHistory | None,
    manager_history: ChatHistory | None,
) -> None:
    roles = [
        ("coordinator", "Coordinator", coordinator_history),
        ("manager", "Manager", manager_history),
    ]
    lines: list[str] = []
    for role, label, history in roles:
        messages = list(history.messages) if history is not None else []
        bd = await asyncio.to_thread(
            context_usage_breakdown,
            role,
            messages,
        )
        lines.append(
            f"{label}: {bd['percent']:.0f}%  "
            f"({_k_tokens(bd['total'])}/{_k_tokens(bd['max'])} tok)"
        )
        if bd.get("has_usage"):
            lines.append(f"   latest real input_tokens {_k_tokens(bd['input'])}")
        else:
            lines.append("   no real usage yet; estimates are disabled")
        lines.append(f"   自动压缩阈值 {_k_tokens(bd['threshold'])} tok")
        lines.append("")
    print_panel("\n".join(lines).rstrip(), title="上下文用量")


async def _print_lifecycle_status(system: Any) -> None:
    session = system.session_key
    keys = {session, f"memory:{system.workspace.project_id}"}
    agents = [
        row
        for row in await system.registry.list_agents()
        if not session or row.session_key in keys
    ]
    active = [
        row
        for row in await system.registry.list_active_invocations()
        if not session or row.session_key in keys
    ]
    recent = await system.registry.list_recent_invocations(session) if session else []
    lines = [
        f"Session: {session or '(not started)'}",
        *(f"[{row.state.value}] {row.agent_id}" for row in agents),
    ]
    lines.append(
        f"Active: {len(active)}; recent completed: {sum(row.state == AgentInvocationState.COMPLETED for row in recent)}; failed/cancelled: {sum(row.state in (AgentInvocationState.FAILED, AgentInvocationState.CANCELLED) for row in recent)}"
    )
    for row in [*active, *recent[-5:]]:
        elapsed = (row.finished_at or time.monotonic()) - row.started_at
        lines.append(
            f"[{row.state.value}] {row.role} inv={row.invocation_id[:8]} parent={(row.parent_invocation_id or '-')[:8]} turn={row.turn_id} {elapsed:.1f}s"
        )
    print_panel("\n".join(lines), title="Agent 生命周期")


def _format_ltm_snapshot(snapshot: dict) -> str:
    core = snapshot.get("memory", {})
    records = snapshot.get("global_records", {})
    return f"## 长期记忆\n\n核心文档：{core.get('path', '')}\n字符数：{core.get('chars', 0)}（无固定上限）\n全局 LanceDB 记录：{records.get('row_count', 0)}\n索引：{records.get('index_error') or 'ready'}\n召回：{records.get('retrieval_error') or 'ready'}\n\n{core.get('body') or '(empty)'}"


def _format_stm_snapshot(snapshot: dict) -> str:
    fields = {
        "db_path": "LanceDB",
        "table_name": "记忆表",
        "row_count": "项目记录数",
        "observed_turns": "当前会话已结束回合",
        "consumed_turns": "当前会话已处理回合",
        "pending_turns": "当前会话待处理回合",
        "window_turns": "窗口大小",
        "overlap_turns": "重叠回合",
    }
    lines = [
        "## 项目情景记忆",
        *(f"- {label}: {snapshot.get(key, '')}" for key, label in fields.items()),
    ]
    lines.extend(
        [
            f"- 感知：{snapshot.get('perception_error') or 'ready'}",
            f"- 索引：{snapshot.get('index_error') or 'ready'}",
            f"- 召回：{snapshot.get('retrieval_error') or 'ready'}",
        ]
    )
    return "\n".join(lines)
