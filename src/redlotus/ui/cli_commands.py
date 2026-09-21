"""交互式 CLI 斜杠命令解析（/help、/agent、/api、/load）。"""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from redlotus.api.base import configure_api
from redlotus.core.agents import AgentInvocationState
from redlotus.core.history import (
    USAGE_CATEGORY_LABELS,
    UsageReport,
    context_usage_breakdown,
    model_message_files_for_path,
    session_model_message_files,
    summarize_usage_files,
)
from redlotus.prompts.prompt import get_skills_as_in_system_prompt
from redlotus.runtime.config import (
    config_file,
    config_sources,
    get_agent_roles,
    get_env,
    get_model_and_params,
    role_supported_thinking_efforts,
    set_model_name,
    settings,
    update_config,
)
from redlotus.runtime.network import (
    ModelTarget,
    _lookup_openrouter_meta,
    lookup_model_context,
    lookup_model_max_output_tokens,
)
from redlotus.runtime.resources import conversations_root, current_workspace
from redlotus.sessions.context import TRACE_STORE, ChatHistory
from redlotus.sessions.storage import SessionFile
from redlotus.tools.registry import SkillsManager
from redlotus.ui.presentation import (
    build_panel_snapshot,
    console,
    print_error,
    print_markdown,
    print_markdown_panel,
    print_panel,
    print_success,
    print_warning,
    render_panel,
)


def _strip_quotes(text: str) -> str:
    """去除首尾成对的引号（粘贴路径常带引号）。"""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


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


def print_cli_help() -> None:
    from redlotus.ui.widgets import COMMAND_HELP

    rows = [
        f"| `{command}` | {description.replace('<', '&lt;').replace('>', '&gt;')} |"
        for command, description in COMMAND_HELP.items()
    ]
    print_markdown(
        "\n".join(
            [
                "## 斜杠命令",
                "",
                "| 命令 | 说明 |",
                "|---|---|",
                *rows,
                "",
                '用 @路径、@"含空格路径" 或 @{路径} 引用文件，支持 Tab 补全；最多 20 个。'
                "图片、视频按原生多模态协议传递，具体支持取决于当前网关。",
            ]
        )
    )


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


def _print_effort_usage(roles: tuple[str, ...]) -> None:
    role_text = "|".join(roles)
    console.print(f"设置思考: /effort <{role_text}> off")
    for role in roles:
        values = "|".join(role_supported_thinking_efforts(role))
        if values:
            console.print(f"设置思考: /effort {role} <{values}>")


async def interactive_set_api(*, embedding=False, ask=None):
    """One API configuration dialog shared by terminal adapters; cancel never writes."""
    if await configure_api(embedding=embedding, ask=ask, emit=print_warning):
        print_success(f"已保存配置: {config_file()}")


def print_loaded_skills(skills_manager: SkillsManager) -> None:
    """输出与模型系统提示中 Skills 区块相同的内容。"""
    block = get_skills_as_in_system_prompt(skills_manager)
    print_markdown_panel(
        block if block.strip() else "(无：layout 与 summary 均为空)",
        title="当前 Skills",
    )


def print_config_summary() -> None:
    base = (get_env("BASE_URL", warn=False) or "").strip()
    key_set = bool((get_env("API_KEY", warn=False) or "").strip())
    lines = [
        "读取顺序（仅存在的文件参与）:",
        *(f"  {index}. {path} {'[存在]' if path.is_file() else '[不存在]'}"
          for index, path in enumerate(config_sources(), 1)),
        f"修改目标: {config_file()}",
        f"BASE_URL: {base or '(空)'}",
        f"API_KEY: {'已填写' if key_set else '(空)'}",
        f"工作目录: {current_workspace()}",
    ]
    print_panel("\n".join(lines), title="配置摘要")


def _k_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _format_usd(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"${text or '0'}"


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
    for kind, buckets in (("类别", report.by_category), ("身份", report.by_agent)):
        for name, usage in sorted(buckets.items()):
            lines.append(f"{kind} {USAGE_CATEGORY_LABELS.get(name, name) if kind == '类别' else name}: input={usage.input_tokens}, output={usage.output_tokens}, "
                         f"cache={usage.cache_hit_tokens}/{usage.cache_miss_tokens}, missing usage={usage.missing_usage_responses}")
    costs = [summary.price for summary in report.by_model.values() if summary.price]
    missing = sum(
        summary.price_unavailable_responses for summary in report.by_model.values()
    )
    lines.append(
        "Estimated total: "
        + (
            "partly unavailable"
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


async def _print_usage_report(raw: str, system: Any) -> None:
    tail = raw.strip()[6:].strip()
    if tail:
        tail = _strip_quotes(tail)
        target = Path(tail).expanduser()
        if not target.is_absolute():
            target = (current_workspace() / target).resolve()
        files = model_message_files_for_path(target)
        if not files:
            print_error(f"未找到 model_messages 日志: {target}")
            return
    else:
        session_key = system.session_key
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


class SlashCommands:
    """Commands use the same session controller and histories as normal input."""

    def __init__(self, controller, state, raw):
        self.controller, self.state, self.system = controller, state, controller.system
        self.raw, self.parts = raw, raw.strip().split(maxsplit=2)

    async def run(self):
        handlers = {
            "/help": print_cli_help,
            "/config": print_config_summary,
            "/pwd": lambda: print_success(str(self.system.workspace.root)),
            "/skills": lambda: print_loaded_skills(self.system._skills_manager),
            "/tasks": lambda: print_markdown_panel(
                self.system._task_manager.structured_status(), title="任务状态"
            ),
            "/context": lambda: _print_context_usage(
                self.system, self.state.history, self.system._manager_history
            ),
            "/usage": lambda: _print_usage_report(self.raw, self.system),
            "/status": lambda: _print_lifecycle_status(self.system),
            "/panel": self.panel,
            "/cd": self.cd,
            "/load": self.load,
            "/trace": self.trace,
            "/stop": self.stop,
            "/cancel": self.cancel,
            "/ltm": self.memory,
            "/stm": self.memory,
            "/agent": self.agent,
            "/effort": self.effort,
            "/api": self.api,
            "/compress": self.compress,
        }
        handler = handlers.get(self.parts[0].lower())
        if handler is None:
            print_warning(f"未知命令 {self.parts[0]}，输入 /help 查看可用命令")
            return None
        value = handler()
        return await value if inspect.isawaitable(value) else value

    async def panel(self):
        include_all = any(part.strip().lower() == "--all" for part in self.parts[1:])
        snapshot = await build_panel_snapshot(
            log_root=conversations_root(),
            system=self.system,
            coordinator_history=self.state.history,
            manager_history=self.system._manager_history,
            include_all=include_all,
        )
        console.print(render_panel(snapshot))
        return None

    async def trace(self):
        if len(self.parts) < 2:
            print_error("用法: /trace <turn_id>")
            return None
        print_markdown(TRACE_STORE.format_turn(self.parts[1].strip()))
        return None

    async def stop(self):
        msg = await self.system.stop_current_turn()
        console.print(msg)
        return None

    async def cancel(self):
        if len(self.parts) < 2:
            print_error("用法: /cancel <invocation_id> 或 /cancel agent <agent_id>")
            return None
        if self.parts[1].lower() == "agent":
            if len(self.parts) < 3:
                print_error("用法: /cancel agent <agent_id>")
                return None
            aid = self.parts[2].strip()
            n = await self.system.registry.cancel_agent(aid)
            self.system.record_control_result("cancel_agent", aid, "cancellation_requested" if n else "not_running", accepted=bool(n))
            if n:
                print_success(f"已请求取消 agent_id={aid!r} 的当前 invocation。")
            else:
                print_warning(f"未找到 agent_id={aid!r} 的活跃 invocation。")
            return None
        iid = self.parts[1].strip()
        n_match = await self.system.registry.count_active_invocation_prefix_matches(iid)
        if n_match > 1:
            self.system.record_control_result("cancel", iid, "ambiguous", accepted=False)
            print_warning(f"前缀 {iid!r} 匹配到多个活跃 invocation，请使用更长的 id。")
            return None
        resolved = await self.system.registry.resolve_active_invocation_id(iid)
        ok = await self.system.registry.cancel(iid)
        if ok:
            self.system.record_control_result("cancel", resolved or iid, "cancellation_requested", accepted=True)
            print_success(f"已请求取消 invocation_id={(resolved or iid)!r}。")
            return None
        sk = getattr(self.system, "session_key", None)
        if sk:
            recent = await self.system.registry.find_recent_invocation_by_prefix(
                sk, iid
            )
            if recent is not None:
                self.system.record_control_result("cancel", recent.invocation_id, recent.state.value, accepted=False)
                print_warning(
                    f"invocation {iid!r} 已在近期历史中结束"
                    f"（state={recent.state.value}），无法取消。"
                )
                return None
        self.system.record_control_result("cancel", iid, "not_found", accepted=False)
        print_warning(f"未找到活跃 invocation_id={iid!r}（支持 UUID 前缀匹配）。")
        return None

    async def memory(self):
        global_scope = self.parts[0].lower() == "/ltm"
        memory = self.system._memory
        label = "LTM" if global_scope else "STM"
        snapshot = (
            memory.reader.long_term_snapshot if global_scope else memory.short_term_snapshot
        )
        render = _format_ltm_snapshot if global_scope else _format_stm_snapshot
        action = self.parts[1].lower() if len(self.parts) > 1 else "show"
        if action == "retry":
            await memory.process_pending(recover=True)
        elif action == "show":
            print_markdown_panel(render(await snapshot()), title=label)
        elif action == "clear":
            if not await self.system.wait_for_memory_quiescent():
                print_error("Memory is still busy; clear was cancelled.")
                return None
            detail = (
                "清空全局长期记录和 MEMORY.md；保留项目情景与原始轨迹。"
                if global_scope
                else "清空当前项目情景；保留全局记忆、原始轨迹和迁移备份。"
            )
            print_markdown_panel(
                render(await snapshot()) + "\n\n" + detail, title=label
            )
            answer = await self.system.toolkit.ask_user(
                f"Type CLEAR {label} to clear {label}. This cannot be undone."
            )
            if answer.strip() == "CLEAR " + label:
                await (
                    memory.clear_long_term()
                    if global_scope
                    else memory.clear_short_term()
                )
                print_success(label + " cleared.")
        else:
            print_error(f"Usage: /{label} show | clear | retry")
        return None

    async def agent(self):
        roles = get_agent_roles()
        role_text = "|".join(roles)
        if len(self.parts) == 1:
            print_agent_models()
            latest = next(
                (
                    m
                    for m in reversed(self.state.history.messages)
                    if getattr(m, "model_name", None)
                ),
                None,
            )
            if latest:
                target = (latest.metadata or {}).get("model_target", {})
                if target:
                    console.print(f"最近请求使用的配置模型: {target['name']}")
                console.print(f"服务返回的模型标识: {latest.model_name}")
            console.print(f"切换模型: /agent <{role_text}> <预设或模型名称>")
            presets = settings().get("model_presets", {})
            if presets:
                console.print("可用预设: " + "、".join(presets))
            return None
        if len(self.parts) < 3:
            print_error(f"用法: /agent <{role_text}> <模型名称>")
            return None
        role = self.parts[1].lower()
        if role not in roles:
            print_error(f"未知角色: {role}（可选: {role_text}）")
            return None
        model_name = self.parts[2].strip()
        try:
            set_model_name(role, model_name)
            when = (
                "当前请求和工具批次结束后，下一次模型请求生效"
                if role == "coordinator"
                else "新启动的任务生效，已启动任务保持原配置"
            )
            print_success(f"已选择 [{role}] {model_name}；{when}。")
        except ValueError as e:
            print_error(str(e))
        return None

    async def effort(self):
        if len(self.parts) == 1:
            print_effort_settings()
            _print_effort_usage(get_agent_roles())
            return
        if len(self.parts) != 3 or self.parts[1].lower() not in get_agent_roles():
            _print_effort_usage(get_agent_roles())
            return
        role, value = (part.lower() for part in self.parts[1:])
        supported = role_supported_thinking_efforts(role)
        if value != "off" and value not in supported:
            print_error(f"{role} 模型不支持 {value}（可选: off|{'|'.join(supported)}）")
            return

        def change(config):
            selected = config["models"][role]
            if isinstance(selected, str):
                selected = config["models"][role] = {"preset": selected}
            selected["thinking"] = "disabled" if value == "off" else "enabled"
            if value != "off":
                selected["reasoning_effort"] = value

        update_config(change)
        print_success(f"已设置 [{role}] 思考为 {value}（已写入 config.json）")

    async def api(self):
        if len(self.parts) > 2 or (
            len(self.parts) == 2 and self.parts[1].lower() != "embedding"
        ):
            print_error("用法: /api [embedding]")
            return None
        await interactive_set_api(
            embedding=len(self.parts) == 2,
            ask=self.controller.config_prompt,
        )
        return None

    async def cd(self):
        if len(self.parts) < 2:
            print_error("用法：/cd <path>")
            return
        target = Path(_strip_quotes(self.raw.split(maxsplit=1)[1])).expanduser()
        if not target.is_absolute():
            target = self.system.workspace.root / target
        if not target.is_dir():
            print_error(f"目录不存在: {target}")
            return
        await self.controller.reset_session(self.state.history, workspace=target)
        print_success(f"已切换工作目录: {target}")
        return not await self.controller.enter_current_workspace(state=self.state)

    async def load(self):
        loaded = await self.controller.enter_current_workspace(
            state=self.state, force_picker=True
        )
        return None if loaded is None else not loaded

    async def compress(self):
        lines = await self.system.compress_context(self.state.history)
        print_panel("\n".join(lines), title="上下文压缩")


MODEL_MESSAGES_GLOB = "*/model_messages.json"


@dataclass(frozen=True)
class WorkspaceSnapshot:
    path: Path
    meta: dict
    saved_at: datetime
    agent: str
    date: str
    topic: str
    message_count: int
    error: str = ""

    @property
    def is_loadable(self) -> bool:
        return not self.error

    @property
    def title(self) -> str:
        return self.topic.strip() if isinstance(self.topic, str) and self.topic.strip() else "未命名会话"

    @property
    def session_id(self) -> str:
        return str(self.meta.get("session_id") or self.path.parent.name)

    @property
    def completed_turns(self) -> int:
        value = self.meta.get("completed_turns", 0)
        return value if isinstance(value, int) and value >= 0 else 0

    @property
    def status(self) -> str:
        if not self.is_loadable:
            return "损坏"
        cached = {
            "active": "进行中",
            "interrupted": "上次已中断",
            "completed": "已完成",
            "new": "未开始",
        }.get(self.meta.get("status"))
        if cached:
            return cached
        if self.meta.get("active_turn"):
            return "进行中"
        if self.meta.get("interrupted_turn"):
            return "上次已中断"
        return "已完成" if self.completed_turns else "未开始"

    @property
    def local_activity_time(self) -> str:
        return self.saved_at.astimezone().strftime("%Y-%m-%d %H:%M")

    @property
    def error_summary(self) -> str:
        return " ".join(self.error.split())[:120] or "会话文件不可读取"

    @property
    def label(self):
        if not self.is_loadable:
            return (
                f"标题：无法加载 · 本地活动：{self.local_activity_time} · "
                f"状态：损坏 · 原因：{self.error_summary}"
            )
        return (
            f"标题：{self.title} · 本地活动：{self.local_activity_time} · "
            f"{self.completed_turns} 回合 · 状态：{self.status} · 会话：{self.session_id}"
        )


def list_workspace_snapshots(*, root=None, include_unloadable=False):
    snapshots = []
    for entry in SessionFile.scan_info(root or conversations_root()):
        path, meta = entry.path, entry.info
        if not entry.error:
            snapshots.append(WorkspaceSnapshot(
                path, meta, datetime.fromisoformat(meta["saved_at"]), "coordinator",
                meta["saved_at"][:10], meta["title"], 0
            ))
            continue
        else:
            from redlotus.ui.presentation import print_warning
            print_warning(f"会话无法加载: {path}: {entry.error}")
            if include_unloadable:
                try:
                    saved_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                except OSError:
                    saved_at = datetime.fromtimestamp(0, timezone.utc)
                snapshots.append(WorkspaceSnapshot(
                    path, {}, saved_at, "coordinator", "", path.parent.name, 0, entry.error
                ))
    return sorted(snapshots, key=lambda row: (row.saved_at, str(row.path)), reverse=True)
