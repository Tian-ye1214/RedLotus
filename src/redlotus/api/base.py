from __future__ import annotations

import asyncio
import contextvars
import mimetypes
import os
import re
import signal
import sys
import threading
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import urlsplit

from redlotus.runtime import config as app_config
from redlotus.runtime import logging as logger
from redlotus.runtime.config import (
    ConfigError,
    _model_selection_field,
    config_file,
    config_source_summary,
    config_value,
    get_model_and_params,
    settings,
    update_config,
)
from redlotus.runtime.network import close_all_clients

if TYPE_CHECKING:
    from redlotus.core.system import AgentSystem
from redlotus.runtime.resources import WorkspaceContext, current_workspace
from redlotus.sessions.context import ChatHistory
from redlotus.sessions.control import InputAdmission, SessionController, UserMessage
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

    def __init__(self):
        self._sessions: dict[str, ChatSession] = {}
        self._released = False
        self._agent_ctx = contextvars.ContextVar(
            f"{type(self).__name__}_context", default=None
        )

    platform_tag: str
    session_prefix: str

    def _session(self, session_id):
        if session_id not in self._sessions:
            state = ChatSession()
            state.ready.set()
            self._sessions[session_id] = state
        return self._sessions[session_id]

    def _agent_for_session(self, session_id):
        from redlotus.core.system import AgentSystem
        from redlotus.ui import presentation

        state = self._session(session_id)
        if state.agent is None:
            state.agent = AgentSystem(
                presentation=presentation,
                owner_memory_allowed=False,
                input_controller=state.inputs,
            )
            logger.activate_log_dir(logger.prepare_log_dir(state.agent.workspace))
            logger.prune_old_logs()
            state.agent.set_ask_user_handler(self._ask_user)
            state.agent.toolkit.set_task_directory(f"{self.platform_tag}_{session_id}")
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
            result = await self._run_turn(identity, state, turn)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.error("[%s] Agent 请求失败: %s", self.platform_tag, error)
            result = f"本轮执行失败（输入 {turn.admission.id}），未完成的操作不能视为成功：{error}"
        if self._sessions.get(identity) is state:
            try:
                await turn.send_reply(result)
            except Exception as exc:
                logger.error("[%s] 回复发送未确认，未自动重发: %s", self.platform_tag, exc)
                raise

    def guess_download_mime(self, *, filename="", media_type_key=""):
        return mimetypes.guess_type(filename)[0] or self._MIME_MAP.get(
            media_type_key.lower(), "application/octet-stream"
        )

    def _notify(self, text):
        identity, state, turn = self._agent_ctx.get()

        async def send():
            if self._sessions.get(identity) is state:
                try:
                    await turn.send_reply(text)
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
                await turn.send_reply(question)
                return await (state.question if timeout is None else asyncio.wait_for(state.question, timeout))
            except TimeoutError:
                return None
            finally:
                state.question = None

    async def dispatch_user_message(self, session_id, message, send_reply, *, prepare=None):
        user_text = message.text
        if not session_id or self._released:
            return
        state = self._session(session_id)
        if user_text == "/stop":
            if state.agent:
                await state.agent.stop_current_turn()
            else:
                state.inputs.reset()
                await state.inputs.queue.cancel()
            await send_reply("已停止当前任务，保留会话记录。")
            return
        if (
            user_text in self.RESET_COMMANDS
            or user_text == "/clear"
            or user_text in self.END_TASK_COMMANDS
        ):
            await self._reset_session(
                session_id, preserve_queue=user_text in self.END_TASK_COMMANDS
            )
            await send_reply("已结束当前任务并清空上下文。")
            return
        if state.question and not state.question.done() and not (prepare or message.attachments or message.references):
            state.question.set_result(user_text)
            return
        if not user_text and not message.attachments and not message.references and prepare is None:
            return
        if missing := app_config.missing_main_api_keys():
            await send_reply("缺少模型接口配置：" + ", ".join(missing))
            return
        self._submit_turn(
            session_id,
            state,
            QueuedTurn(message, send_reply, asyncio.get_running_loop(), prepare),
        )
        await send_reply("✓ 收到，正在处理…")

    async def release_all_resources_async(self):
        if self._released:
            return
        self._released = True
        sessions, self._sessions = list(self._sessions.values()), {}
        await asyncio.gather(*(self._close_session(state) for state in sessions))
        await close_all_clients()

    def clean_text(self, raw):
        return re.sub(r"\s+", " ", (raw or "").strip())


async def ask_configuration(question: str, *, secret=False):
    """Read a startup answer with the same hidden-key contract as the TUI dialog."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import get_app_session
    from prompt_toolkit.input.typeahead import store_typeahead
    from prompt_toolkit.key_binding import KeyBindings

    bindings = KeyBindings()

    @bindings.add("escape", eager=True)
    def cancel(event):
        event.app.exit(result=None)

    # The TERM=dumb shortcut bypasses password masking; keep the session renderer.
    session = PromptSession(
        key_bindings=bindings, output=get_app_session().output
    )
    answer = await session.prompt_async(question, is_password=secret)
    # A pending Esc must survive the previous prompt's cancelled input-flush task.
    store_typeahead(session.input, session.input.flush_keys())
    return answer

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
            node = node.setdefault(key, {})
        node[path[-1]] = deepcopy(value)

    def connection_paths(self):
        return [("BASE_URL",), ("API_KEY",)]

    async def fill(self, path):
        """Edit only model parameters and connection fields; never collect runtime policies."""
        key = ".".join(path)
        model = _model_selection_field(path)
        connection = path in rag_configuration_paths() or path in self.connection_paths()
        if not model and not connection:
            raise ConfigError(f"配置向导仅支持模型与连接字段；请在 {config_file()} 编辑 {key}")
        current = config_value(self.values, path)
        secret = connection and ("key" in path[-1].lower() or "token" in path[-1].lower())
        can_keep = isinstance(current, str) and bool(current.strip())
        shown = ("已填写" if current else "空") if secret else str(current if current is not None else "空")
        hint = "回车保留" if can_keep else "必须填写"
        if not can_keep:
            hint += "；作用：" + ("选择调用的模型" if model else "连接服务的地址或认证凭据") + "；类型：字符串；可选值：无枚举限制"
        if model and path[0] == "models":
            hint += "；输入 =角色名 复用模型名，保留本角色的连接与策略"
        while True:
            answer = await self.ask(f"{key}（当前 {shown}；{hint}；Esc 取消）：", secret=secret)
            if answer is None or answer == "\x1b":
                return False
            text = answer.strip()
            if not text and can_keep:
                return True
            if not text:
                self.emit(f"{key} 不能为空。")
                continue
            try:
                value = get_model_and_params(text[1:], cfg=self.values)[0] if text.startswith("=") and model else text
                if connection and ("url" in path[-1].lower() or path[-1] == "SILICONFLOW_BASE"):
                    parsed = urlsplit(value)
                    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                        self.emit(f"{key} 需要完整的 http/https 服务地址。")
                        continue
                candidate = deepcopy(self.values)
                self.assign(candidate, path, value)
                if path[0] == "models":
                    get_model_and_params(path[1], cfg=candidate)
            except (ValueError, KeyError, TypeError):
                self.emit(f"{key} 无效：请检查模型参数或引用的角色名称。")
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
        update_config(apply)
        return True


def rag_configuration_paths():
    """Connection and model fields exposed by the existing RAG configuration dialog."""
    return [("SILICONFLOW_BASE",), ("SILICONFLOW_KEY",), ("RAG_models", "embedding"), ("RAG_models", "reranker")]

async def prepare_startup_configuration(*, ask=None, emit=print) -> bool:
    """Collect model names and credentials without inventing runtime configuration."""
    setup = ConfigurationSetup(ask, emit)
    interactive = ask is not None or configuration_prompt_available()
    emit(f"配置修改目标: {config_file()}\n读取来源: {config_source_summary()}")
    try:
        models = config_value(setup.values, ("models",), purpose="已有 Agent 角色与模型选择", kind=dict)
        if not isinstance(models, dict) or not models:
            raise ConfigError(f"请在 {config_file()} 中提供 models 配置对象")
        for role in models:
            try:
                get_model_and_params(role, cfg=setup.values)
            except ConfigError as exc:
                if not exc.missing or not interactive:
                    raise
                if not await setup.fill(exc.path):
                    return False
        for path in setup.connection_paths():
            try:
                config_value(setup.values, path, purpose="连接模型服务的地址或认证凭据", kind=str)
                continue
            except ConfigError as exc:
                if not exc.missing or not interactive:
                    raise
            if not await setup.fill(path):
                return False
        return await setup.commit()
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
        system = AgentSystem(presentation=presentation)
    stop_event = asyncio.Event()
    install_stop_handlers(stop_event)
    try:
        await AgentCliController(system).run_interactive(stop_event=stop_event)
    finally:
        seconds = config_value(settings(), ('lifecycle', 'shutdown_grace_seconds'), kind=(int, float))
        deadline = ExitDeadline(seconds) if seconds is not None else None
        if deadline is not None:
            deadline.start()
        try:
            await system.shutdown()
            await close_all_clients()
        finally:
            if deadline is not None:
                deadline.close()
    return system

def main(channel=None) -> None:
    """Configure the selected transport before constructing its runtime."""
    try:
        if not asyncio.run(prepare_startup_configuration()):
            return
        if channel:
            channel().run()
            return
        asyncio.run(run_cli())
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
