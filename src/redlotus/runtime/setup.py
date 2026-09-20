"""Runtime setup responsibilities."""

from __future__ import annotations

import asyncio
import shutil
import sys
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

from redlotus.runtime.config import (
    ConfigError,
    _model_roles,
    _model_selection_field,
    _selected_model_name_path,
    _validate_config,
    get_model_and_params,
    initialize_user_configuration,
    settings,
    update_config,
    validate_runtime_configuration,
)
from redlotus.runtime.files import _frozen, config_file, config_source_summary


async def ask_configuration(question: str, *, secret=False):
    """Read a startup answer with the same hidden-key contract as the TUI dialog."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings

    bindings = KeyBindings()

    @bindings.add("escape", eager=True)
    def cancel(event):
        event.app.exit(result=None)

    return await PromptSession(key_bindings=bindings).prompt_async(question, is_password=secret)


def configuration_prompt_available() -> bool:
    """Return whether the default first-use dialog can safely open a terminal prompt."""
    return bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stdout, "isatty", lambda: False)()
    )


class ConfigurationSetup:
    """Collect explicit configuration edits and commit them as a single atomic change."""

    def __init__(self, ask=None, emit=print):
        self.values = settings()
        self.changes = {}
        self.ask, self.emit = ask or ask_configuration, emit

    def value(self, path):
        node = self.values
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        return node

    @staticmethod
    def assign(values, path, value):
        node = values
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = deepcopy(value)

    def connection_paths(self, role="coordinator"):
        """Use the selected role's credential reference, not an unrelated global API key."""
        _, parameters = get_model_and_params(role, cfg=self.values)
        gateway = parameters.get("gateway")
        if not gateway:
            return [("BASE_URL",), ("API_KEY",)]
        selected = self.values["gateways"][gateway]
        # Edit an explicit gateway key when present; otherwise retain its named reference.
        key = (selected["api_key_env"],) if selected.get("api_key_env") and not selected.get("api_key") else ("gateways", gateway, "api_key")
        return [("gateways", gateway, "base_url"), key]

    async def fill(self, path):
        """Validate one field, retaining previous input until the whole dialog succeeds."""
        key = ".".join(path)
        current = self.value(path)
        secret = "key" in path[-1].lower() or "token" in path[-1].lower()
        shown = ("已填写" if current else "空") if secret else str(current if current is not None else "空")
        hint = "回车保留"
        if _model_selection_field(path) and path[0] == "models":
            hint += "；输入 =角色名 可明确复用其模型"
        while True:
            answer = await self.ask(f"{key}（当前 {shown}；{hint}；Esc 取消）：", secret=secret)
            if answer is None or answer == "\x1b":
                return False
            text = answer.strip()
            if not text and current not in (None, ""):
                return True
            try:
                if not text:
                    raise ValueError("不能为空")
                value = get_model_and_params(text[1:], cfg=self.values)[0] if text.startswith("=") and path[0] == "models" else text
                if "url" in path[-1].lower() or path[-1] == "SILICONFLOW_BASE":
                    parsed = urlsplit(value)
                    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                        raise ValueError("请输入完整的 http/https 服务地址")
                candidate = deepcopy(self.values)
                self.assign(candidate, path, value)
                _validate_config(candidate, config_file())
            except (ValueError, KeyError, TypeError):
                self.emit(f"{key} 无效：请按字段类型填写；模型复用需指定已配置的角色。")
                continue
            self.values = candidate
            self.changes[path] = value
            return True

    async def commit(self):
        """Only changed fields are applied to the latest locked configuration."""
        if self.changes:
            answer = await self.ask(f"确认仅将本次填写的字段写入 {config_file()}？y 确认 / 其他取消：")
            if answer is None or answer.strip().lower() not in {"y", "yes", "是"}:
                self.emit("配置已取消，未保存本次填写内容。")
                return False
            def apply(values):
                for path, value in self.changes.items():
                    self.assign(values, path, value)
            update_config(apply)
        return True


def python_tool_startup_notice(cfg: dict[str, Any]) -> str | None:
    """Explain an optional frozen-build Python requirement without blocking chat."""
    if not _frozen():
        return None
    if any(shutil.which(name) for name in ("python", "python3", "py")):
        return None
    return (
        "未发现外部 Python：纯聊天仍可使用；Python/pip 工具暂不可用。"
        "请将现有 Python 加入 PATH 后重启。"
    )


def rag_configuration_paths():
    """Connection and model fields exposed by the existing RAG configuration dialog."""
    return [("SILICONFLOW_BASE",), ("SILICONFLOW_KEY",), ("RAG_models", "embedding"), ("RAG_models", "reranker")]


async def prepare_startup_configuration(*, ask=None, emit=print) -> bool:
    """Complete first-use configuration before constructing Agents or starting clients."""
    initialize_user_configuration()
    setup = ConfigurationSetup(ask, emit)
    setup.values.setdefault("models", {}).setdefault("coordinator", {})
    schema = setup.values
    try:
        missing_model_names = []
        roles = _model_roles(schema, setup.values)
        roles.sort(key=lambda role: role != "coordinator")
        for role in roles:
            try:
                get_model_and_params(role, cfg=setup.values)
            except ConfigError as exc:
                path = _selected_model_name_path(role, setup.values, schema)
                missing_role = str(exc).startswith(
                    f"缺少配置 models.{role}；"
                ) or str(exc).startswith("缺少配置 models；")
                if ".".join(path) not in str(exc) and not missing_role:
                    raise
                missing_model_names.append(path)
        interactive = ask is not None or configuration_prompt_available()
        if not interactive:
            missing = list(missing_model_names)
            if not missing:
                paths = dict.fromkeys(path for role in roles for path in setup.connection_paths(role))
                missing.extend(path for path in paths if not str(setup.value(path) or "").strip())
            if missing:
                fields = "、".join(".".join(path) for path in missing)
                raise ConfigError(f"非交互启动缺少必填配置 {fields}；请编辑 {config_file()} 后重试")
            missing_rag = [path for path in rag_configuration_paths() if not str(setup.value(path) or "").strip()]
            if missing_rag:
                emit("RAG 尚未配置；可先聊天，使用 /api embedding 补齐向量检索配置。")
            if notice := python_tool_startup_notice(setup.values):
                emit(notice)
            validate_runtime_configuration(setup.values)
            return True
        emit(f"配置修改目标: {config_file()}\n读取来源: {config_source_summary()}")
        for path in dict.fromkeys(missing_model_names):
            if not await setup.fill(path):
                return False
        paths = dict.fromkeys(path for role in roles for path in setup.connection_paths(role))
        for path in paths:
            if not str(setup.value(path) or "").strip() and not await setup.fill(path):
                return False
        missing_rag = [path for path in rag_configuration_paths() if not str(setup.value(path) or "").strip()]
        if missing_rag:
            choice = await setup.ask("现在配置 RAG 向量检索吗？y 配置 / n 稍后（回车稍后；Esc 取消）：")
            if choice is None or choice == "\x1b":
                return False
            if choice.strip().lower() in {"y", "yes", "是"}:
                for path in missing_rag:
                    if not await setup.fill(path):
                        return False
        if not await setup.commit():
            return False
        if notice := python_tool_startup_notice(setup.values):
            emit(notice)
        validate_runtime_configuration(setup.values)
        return True
    except (KeyboardInterrupt, EOFError, asyncio.CancelledError):
        emit("配置已取消，未保存本次填写内容。")
        return False


async def configure_api(*, embedding=False, ask=None, emit=print) -> bool:
    """Edit service fields atomically for both plain CLI and TUI callers."""
    setup = ConfigurationSetup(ask, emit)
    paths = rag_configuration_paths() if embedding else setup.connection_paths()
    try:
        for path in paths:
            if not await setup.fill(path):
                return False
        return await setup.commit()
    except (KeyboardInterrupt, EOFError, asyncio.CancelledError):
        return False
