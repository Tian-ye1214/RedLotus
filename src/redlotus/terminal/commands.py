"""Terminal commands responsibilities."""

from __future__ import annotations

import inspect
from pathlib import Path

from redlotus.presentation.output import (
    console,
    print_error,
    print_markdown,
    print_markdown_panel,
    print_panel,
    print_success,
    print_warning,
)
from redlotus.presentation.panels import build_panel_snapshot, render_panel
from redlotus.presentation.reports import (
    _format_ltm_snapshot,
    _format_stm_snapshot,
    _out,
    _print_context_usage,
    _print_effort_usage,
    _print_lifecycle_status,
    _print_usage_report,
    print_agent_models,
    print_effort_settings,
)
from redlotus.prompts.prompt import get_skills_as_in_system_prompt
from redlotus.runtime.config import (
    get_agent_roles,
    get_env,
    role_supported_thinking_efforts,
    set_model_name,
    settings,
    update_config,
)
from redlotus.runtime.context import TRACE_STORE, conversations_root, current_workspace
from redlotus.runtime.files import config_file, config_sources
from redlotus.runtime.setup import configure_api
from redlotus.tools.registry import SkillsManager


def _strip_quotes(text: str) -> str:
    """去除首尾成对的引号（粘贴路径常带引号）。"""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text






def print_cli_help() -> None:
    from redlotus.terminal.console import COMMAND_HELP

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
                self.system.structured_task_status(), title="任务状态"
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
        _out(msg)
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
            answer = await self.system.ask_user(
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
                    _out(f"最近请求使用的配置模型: {target['name']}")
                _out(f"服务返回的模型标识: {latest.model_name}")
            _out(f"切换模型: /agent <{role_text}> <预设或模型名称>")
            presets = settings().get("model_presets", {})
            if presets:
                _out("可用预设: " + "、".join(presets))
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
