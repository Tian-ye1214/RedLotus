"""Shared slash-command and @file-path completion logic for CLI and TUI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from redlotus.config.app_config import (
    settings,
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
    "/urgent": "/urgent <内容>：加入当前回合，工具继续执行，下一次模型请求统一处理",
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

    at_index = text.rfind("@")
    if at_index != -1:
        if (
            at_index
            and text[at_index - 1].isascii()
            and (text[at_index - 1].isalnum() or text[at_index - 1] in "._%+-")
        ):
            return None
        fragment = text[at_index + 1 :]
        if fragment[:1] in ('"', "'", "{"):
            closing = "}" if fragment[0] == "{" else fragment[0]
            if closing in fragment[1:]:
                return None
            return InputCompletion(kind="file_path", prefix=fragment, at_mode=True)
        if any(char.isspace() for char in fragment):
            return None
        return InputCompletion(kind="file_path", prefix=fragment, at_mode=True)

    return None
