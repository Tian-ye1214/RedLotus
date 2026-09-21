from __future__ import annotations

import asyncio
import contextvars
import json
import mimetypes
import os
import re
import shutil
import signal
import sys
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import urlsplit

from redlotus.runtime import config as app_config
from redlotus.runtime import logging as logger
from redlotus.runtime.config import (
    ConfigError,
    _frozen,
    _model_selection_field,
    _validate_config,
    config_field,
    config_file,
    config_source_summary,
    config_value,
    get_env,
    get_model_and_params,
    load_config,
    missing_startup_fields,
    settings,
    update_config,
)
from redlotus.runtime.network import close_all_clients
from redlotus.sessions.control import UserMessage

if TYPE_CHECKING:
    from redlotus.core.system import AgentSystem
from redlotus.runtime.resources import WorkspaceContext, current_workspace
from redlotus.sessions.context import ChatHistory
from redlotus.sessions.control import InputAdmission, SessionController
from redlotus.tools import registry as tool_telemetry


@dataclass(frozen=True)
class QueuedTurn:
    user_message: UserMessage
    send_reply: Callable[..., Awaitable[Any]]
    loop: asyncio.AbstractEventLoop
    prepare: Callable[[], Awaitable[list]] | None = None
    admission: InputAdmission | None = None


@dataclass
class ChatSession:
    inputs: SessionController = field(default_factory=SessionController)
    history: ChatHistory = field(default_factory=ChatHistory)
    agent: AgentSystem | None = None
    question: asyncio.Future | None = None
    question_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    touched: float = field(default_factory=time.monotonic)


class BotBase:
    """所有平台机器人的公共基类。

    子类需实现：
      - platform_tag: str      —— 日志前缀，如 "WeChat" / "QQ"
      - session_prefix: str    —— 会话 ID 前缀，如 "wx_" / "qq_"
    """

    RESET_COMMANDS = frozenset({"新任务", "/新任务", "/reset"})
    END_TASK_COMMANDS = frozenset(
        {"结束任务", "/结束任务", "结束当前任务", "/结束当前任务"}
    )
    _MIME_MAP: dict[str, str] = {}

    # Legacy named fields still resolve through the three-layer configuration.
    _ENV_AGENT_TIMEOUT: str = ""
    _ENV_SEND_TIMEOUT: str = ""
    _ENV_SESSION_IDLE_TTL: str = ""

    def __init__(self):
        self._sessions: dict[str, ChatSession] = {}
        self._gc_task = None
        self._released = False
        self._policy = settings()["bot"]
        self._agent_ctx = contextvars.ContextVar(
            f"{type(self).__name__}_context", default=None
        )
        for source, target in (
            (self._ENV_AGENT_TIMEOUT, "agent_run_timeout_seconds"),
            (self._ENV_SEND_TIMEOUT, "send_reply_timeout_seconds"),
            (self._ENV_SESSION_IDLE_TTL, "session_idle_ttl_seconds"),
        ):
            if source and (value := get_env(source, warn=False)):
                self._policy[target] = float(value)

    platform_tag: str
    session_prefix: str

    def _session(self, session_id):
        if session_id not in self._sessions:
            state = ChatSession()
            state.ready.set()
            self._sessions[session_id] = state
        return self._sessions[session_id]

    def _is_owner_session(self, session_id):
        platform = self.platform_tag.lower()
        prefix = {"qq": "private_", "wechat": "wx_"}.get(platform)
        owners = settings().get("bot", {}).get("owner_channels", {}).get(platform, [])
        return bool(
            prefix
            and session_id.startswith(prefix)
            and session_id[len(prefix) :] in map(str, owners)
        )

    def _agent_for_session(self, session_id):
        from redlotus.core.system import AgentSystem
        from redlotus.ui import presentation

        state = self._session(session_id)
        if state.agent is None:
            state.agent = AgentSystem(
                presentation=presentation,
                owner_memory_allowed=self._is_owner_session(session_id),
                input_controller=state.inputs,
            )
            state.agent.set_ask_user_handler(self._ask_user)
            state.agent.toolkit.set_task_directory(f"{self.platform_tag}_{session_id[:20]}")
        return state.agent

    async def _close_session(self, state):
        if state.question and not state.question.done():
            state.question.cancel()
        state.inputs.reset(discard=True)
        await state.inputs.queue.cancel(discard=True)
        await state.inputs.queue.join()
        if state.agent:
            await state.agent.shutdown()

    async def _reset_session(self, session_id, *, preserve_queue=False):
        old = self._sessions.pop(session_id, None)
        state = ChatSession()
        self._sessions[session_id] = state
        if old:
            old.inputs.reset(discard=True)
            pending = [entry[2] for entry in old.inputs.queue.pending]
            old.inputs.queue.discard()
            if preserve_queue:
                for turn in pending:
                    self._submit_turn(session_id, state, turn)
        try:
            if old:
                await self._close_session(old)
        finally:
            state.ready.set()

    def _ensure_session_gc(self):
        if self._policy["session_idle_ttl_seconds"] > 0 and (
            self._gc_task is None or self._gc_task.done()
        ):
            self._gc_task = asyncio.create_task(self._session_gc_loop())

    async def _session_gc_loop(self):
        while True:
            await asyncio.sleep(self._policy["session_gc_interval_seconds"])
            for identity, state in list(self._sessions.items()):
                if (
                    not state.inputs.queue.current
                    and not state.inputs.queue.pending
                    and time.monotonic() - state.touched > self._policy["session_idle_ttl_seconds"]
                ):
                    self._sessions.pop(identity, None)
                    await self._close_session(state)
            logger.prune_old_logs()

    def _submit_turn(self, identity, state, turn):
        turn = replace(turn, admission=state.inputs.admit(
            WorkspaceContext.from_path(current_workspace()),
            input_id=turn.admission.id if turn.admission else None,
        ))
        logger.debug("[%s] input admitted id=%s sequence=%s", self.platform_tag, turn.admission.id, turn.admission.sequence)
        return state.inputs.queue.submit(
            lambda: self._consume_turn(identity, state, turn), data=turn
        )

    async def _consume_turn(self, identity, state, turn):
        await state.ready.wait()
        try:
            result = await asyncio.wait_for(
                self._run_turn(identity, state, turn), self._policy["agent_run_timeout_seconds"]
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.error("[%s] Agent 请求失败: %s", self.platform_tag, error)
            result = f"本轮执行失败（输入 {turn.admission.id}），未完成的操作不能视为成功：{error}"
        finally:
            state.touched = time.monotonic()
        if self._sessions.get(identity) is state:
            try:
                await self._safe_send(turn.send_reply, result)
            except Exception as exc:
                logger.error("[%s] 回复发送未确认，未自动重发: %s", self.platform_tag, exc)
                raise

    def _split_reply(self, text):
        chunks = []
        while len(text) > self._policy["reply_max_chars"]:
            window = text[: self._policy["reply_max_chars"]]
            cut = next(
                (
                    pos
                    for separator in ("\n\n", "\n", " ")
                    if (pos := window.rfind(separator)) > 0
                ),
                len(window),
            )
            chunks.append(text[:cut].rstrip())
            text = text[cut:].lstrip()
        return [*chunks, text] if text else chunks

    async def _safe_send(self, send_reply, text):
        """Send each chunk once; a missing acknowledgement is not failed delivery."""
        for chunk in self._split_reply(text):
            try:
                await asyncio.wait_for(send_reply(chunk), self._policy["send_reply_timeout_seconds"])
            except TimeoutError as exc:
                raise TimeoutError("发送确认超时，送达状态未知；未自动重发。") from exc

    def guess_download_mime(self, *, filename="", media_type_key=""):
        return mimetypes.guess_type(filename)[0] or self._MIME_MAP.get(
            media_type_key.lower(), "application/octet-stream"
        )

    def _notify(self, text):
        identity, state, turn = self._agent_ctx.get()

        async def send():
            if self._sessions.get(identity) is state:
                try:
                    await self._safe_send(turn.send_reply, text)
                except Exception as exc:
                    logger.error("[%s] 通知发送失败: %s", self.platform_tag, exc)

        turn.loop.call_soon_threadsafe(lambda: asyncio.create_task(send()))

    async def _run_turn(self, identity, state, turn):
        if turn.prepare:
            turn.user_message.attachments = await turn.prepare()
        if self._sessions.get(identity) is not state or not state.inputs.accepts(turn.admission):
            raise asyncio.CancelledError()
        if not (turn.user_message.text or turn.user_message.attachments or turn.user_message.references):
            return ""
        agent = self._agent_for_session(identity)
        token = self._agent_ctx.set((identity, state, turn))
        tool_telemetry.set_user_notify_callback(self._notify)
        try:
            with logger.session_log_context(identity):
                if agent.session_key is None:
                    from uuid import uuid4
                    await agent.bind_session(uuid4().hex)
                _, result = await agent.run_agent_system(
                    turn.user_message,
                    state.history,
                    turn_id=turn.admission.id,
                )
                return result
        finally:
            tool_telemetry.set_user_notify_callback(None)
            self._agent_ctx.reset(token)

    async def _ask_user(self, question, timeout=None):
        identity, state, turn = self._agent_ctx.get()
        async with state.question_lock:
            if self._sessions.get(identity) is not state:
                return None
            state.question = asyncio.get_running_loop().create_future()
            try:
                await self._safe_send(turn.send_reply, question)
                return await asyncio.wait_for(state.question, self._policy["question_timeout_seconds"] if timeout is None else timeout)
            except TimeoutError:
                return None
            finally:
                state.question = None

    async def dispatch_user_message(self, session_id, message, send_reply, *, prepare=None):
        user_text = message.text
        if not session_id or self._released:
            return
        state = self._session(session_id)
        state.touched = time.monotonic()
        if user_text == "/stop":
            if state.agent:
                await state.agent.stop_current_turn()
            else:
                state.inputs.reset()
                await state.inputs.queue.cancel()
            await self._safe_send(send_reply, "已停止当前任务，保留会话记录。")
            return
        if (
            user_text in self.RESET_COMMANDS
            or user_text == "/clear"
            or user_text in self.END_TASK_COMMANDS
        ):
            await self._reset_session(
                session_id, preserve_queue=user_text in self.END_TASK_COMMANDS
            )
            await self._safe_send(send_reply, "已结束当前任务并清空上下文。")
            return
        if state.question and not state.question.done() and not (prepare or message.attachments or message.references):
            state.question.set_result(user_text)
            state.agent._session.user_inputs.append(user_text)
            return
        if not user_text and not message.attachments and not message.references and prepare is None:
            return
        app_config.reload_config()
        if missing := app_config.missing_main_api_keys():
            await self._safe_send(send_reply, "缺少模型接口配置：" + ", ".join(missing))
            return
        if state.inputs.queue.maxsize and len(state.inputs.queue.pending) >= state.inputs.queue.maxsize:
            await self._safe_send(
                send_reply, f"待处理消息已达上限 {state.inputs.queue.maxsize}，请稍后再试。"
            )
            return
        self._submit_turn(
            session_id,
            state,
            QueuedTurn(message, send_reply, asyncio.get_running_loop(), prepare),
        )
        await self._safe_send(send_reply, "✓ 收到，正在处理…")
        self._ensure_session_gc()

    async def release_all_resources_async(self):
        if self._released:
            return
        self._released = True
        if self._gc_task:
            self._gc_task.cancel()
            await asyncio.gather(self._gc_task, return_exceptions=True)
        sessions, self._sessions = list(self._sessions.values()), {}
        await asyncio.gather(*(self._close_session(state) for state in sessions))
        await close_all_clients()

    def release_all_resources(self):
        asyncio.run(self.release_all_resources_async())

    def clean_text(self, raw):
        return re.sub(r"\s+", " ", (raw or "").strip())


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

    @staticmethod
    def assign(values, path, value):
        node = values
        for key in path[:-1]:
            if isinstance(node.get(key), str) and path[:1] == ("models",) and key == path[1]:
                node[key] = {"preset": node[key]}
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
        current = config_value(self.values, path)
        field = config_field(path)
        secret = "key" in path[-1].lower() or "token" in path[-1].lower() or any(
            gateway.get("api_key_env") == path[-1] for gateway in self.values.get("gateways", {}).values()
        )
        shown = ("已填写" if current else "空") if secret else str(current if current is not None else "空")
        hint = "回车保留"
        if _model_selection_field(path) and path[0] == "models":
            hint += "；输入 =角色名 复用模型名，保留本角色的连接与策略"
        while True:
            answer = await self.ask(f"{key}（当前 {shown}；{hint}；{field.get('description', '')}；Esc 取消）：", secret=secret)
            if answer is None or answer == "\x1b":
                return False
            text = answer.strip()
            if not text and current not in (None, ""):
                return True
            try:
                if not text and not field.get("allow_empty"):
                    raise ValueError("不能为空")
                if text.startswith("=") and _model_selection_field(path):
                    value = get_model_and_params(text[1:], cfg=self.values)[0]
                elif field.get("type") == "string" or "string" in field.get("type", []) and path != ("request_limit",):
                    value = text
                else:
                    try:
                        value = json.loads(text)
                    except json.JSONDecodeError:
                        value = text
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
        """Confirm once, then apply only this dialog's edits to the latest locked layer."""
        if not self.changes:
            return True
        answer = await self.ask(f"确认将 {len(self.changes)} 项修改写入 {config_file()}？y 确认 / 其他取消：")
        if answer is None or answer.strip().lower() not in {"y", "yes", "是"}:
            return False
        def apply(values):
            for path, value in self.changes.items():
                self.assign(values, path, value)
        update_config(apply, lock_timeout=self.values["storage"]["file_lock_timeout_seconds"])
        return True


def python_tool_startup_notice() -> str | None:
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

async def prepare_startup_configuration(*, required=(), ask=None, emit=print) -> bool:
    """Collect missing typed fields before constructing Agents; never silently fill policy values."""
    setup = ConfigurationSetup(ask, emit)
    interactive = ask is not None or configuration_prompt_available()
    emit(f"配置修改目标: {config_file()}\n读取来源: {config_source_summary()}")
    try:
        while missing := missing_startup_fields(setup.values, required):
            if not interactive:
                raise ConfigError(f"非交互启动缺少必填配置 {', '.join('.'.join(path) for path in missing)}；请编辑 {config_file()}")
            if not await setup.fill(missing[0]):
                return False
        paths = dict.fromkeys(path for role in setup.values["models"] for path in setup.connection_paths(role))
        for path in paths:
            if str(config_value(setup.values, path) or "").strip():
                continue
            if not interactive:
                raise ConfigError(f"非交互启动缺少必填配置 {'.'.join(path)}；请编辑 {config_file()}")
            if not await setup.fill(path):
                return False
        missing_rag = [path for path in rag_configuration_paths() if not str(config_value(setup.values, path) or "").strip()]
        if missing_rag and interactive:
            choice = await setup.ask("现在配置 RAG 向量检索吗？y 配置 / n 稍后（回车稍后；Esc 取消）：")
            if choice is None or choice == "\x1b":
                return False
            if choice.strip().lower() in {"y", "yes", "是"}:
                for path in missing_rag:
                    if not await setup.fill(path):
                        return False
        elif missing_rag:
            emit("RAG 尚未配置；可先聊天，使用 /api embedding 补齐向量检索配置。")
        if not await setup.commit():
            return False
        if notice := python_tool_startup_notice():
            emit(notice)
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

class ExitDeadline(threading.Timer):
    """Bound process exit even when a native call ignores task cancellation."""

    def __init__(self, seconds: float):
        super().__init__(seconds, os._exit, args=(0,))
        self.daemon = True

    close = threading.Timer.cancel

def install_stop_handlers(stop_event: asyncio.Event) -> None:
    """Map process signals to the interactive runner's stop event."""
    loop = asyncio.get_running_loop()

    def request_stop(*_args: object) -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except (NotImplementedError, ValueError):
            signal.signal(sig, request_stop)

async def run_cli(system=None):
    """Run the interactive RedLotus CLI/TUI."""
    from redlotus.core.system import AgentSystem
    from redlotus.ui import presentation
    from redlotus.ui.console import AgentCliController

    if system is None:
        load_config()
        system = AgentSystem(presentation=presentation)
    stop_event = asyncio.Event()
    install_stop_handlers(stop_event)
    try:
        await AgentCliController(system).run_interactive(stop_event=stop_event)
    finally:
        await system.shutdown()
        await close_all_clients()
    return system

def main(channel=None) -> None:
    """Configure the selected transport before constructing its runtime."""
    try:
        load_config()
        if not asyncio.run(prepare_startup_configuration(required=("bot",) if channel else ())):
            return
        if channel:
            channel().run()
            return
        from redlotus.core.system import AgentSystem
        from redlotus.ui import presentation

        deadline = ExitDeadline(settings()["lifecycle"]["shutdown_grace_seconds"])
        try:
            system = AgentSystem(presentation=presentation, exit_deadline=deadline)
            asyncio.run(run_cli(system))
        finally:
            deadline.close()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
