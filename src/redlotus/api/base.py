import re
import time
import asyncio
import contextvars
import mimetypes
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

from redlotus.core import config as app_config, config as logger
from redlotus.core.config import get_env, settings, close_all_clients
from redlotus.tools.interaction import UserMessage
from redlotus.core.system import AgentSystem
from redlotus.core.history import ChatHistory
from redlotus.core.session import TurnQueue
from redlotus.tools import registry as tool_telemetry


@dataclass(frozen=True)
class QueuedTurn:
    user_message: UserMessage
    send_reply: Callable[..., Awaitable[Any]]
    loop: asyncio.AbstractEventLoop


@dataclass
class ChatSession:
    queue: TurnQueue
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

    AGENT_RUN_TIMEOUT_S = 900.0
    SEND_REPLY_TIMEOUT_S = 120.0
    REPLY_MAX_CHARS = 4500  # 单条回复字符上限，超长按段落/换行/空格切分后分条发送
    SESSION_IDLE_TTL_S = 3600.0  # 空闲超过此时长的会话将被后台回收；<=0 关闭回收
    SESSION_GC_INTERVAL_S = 300.0  # 空闲会话清扫间隔
    RESET_COMMANDS = frozenset({"新任务", "/新任务", "/reset"})
    END_TASK_COMMANDS = frozenset(
        {"结束任务", "/结束任务", "结束当前任务", "/结束当前任务"}
    )
    _MIME_MAP: dict[str, str] = {}

    # 子类可覆盖（从环境变量读取超时）
    _ENV_AGENT_TIMEOUT: str = ""
    _ENV_SEND_TIMEOUT: str = ""
    _ENV_SESSION_IDLE_TTL: str = ""

    def __init__(self):
        self._sessions: dict[str, ChatSession] = {}
        self._gc_task = None
        self._released = False
        self._agent_ctx = contextvars.ContextVar(
            f"{type(self).__name__}_context", default=None
        )
        for source, target in (
            (self._ENV_AGENT_TIMEOUT, "AGENT_RUN_TIMEOUT_S"),
            (self._ENV_SEND_TIMEOUT, "SEND_REPLY_TIMEOUT_S"),
            (self._ENV_SESSION_IDLE_TTL, "SESSION_IDLE_TTL_S"),
        ):
            if source and (value := get_env(source, warn=False)):
                setattr(self, target, float(value))

    platform_tag: str
    session_prefix: str

    def _new_session(self):
        return ChatSession(
            TurnQueue(maxsize=int(settings()["bot"]["session_queue_maxsize"]))
        )

    def _session(self, session_id):
        if session_id not in self._sessions:
            state = self._new_session()
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
        state = self._session(session_id)
        if state.agent is None:
            state.agent = AgentSystem(
                owner_memory_allowed=self._is_owner_session(session_id)
            )
            state.agent.set_ask_user_handler(self._ask_user)
            state.agent.set_task_directory(f"{self.platform_tag}_{session_id[:20]}")
        return state.agent

    async def _close_session(self, state):
        if state.question and not state.question.done():
            state.question.cancel()
        await state.queue.cancel(discard=True)
        await state.queue.join()
        if state.agent:
            await state.agent.shutdown()

    async def _reset_session(self, session_id, *, preserve_queue=False):
        old = self._sessions.pop(session_id, None)
        state = self._new_session()
        if old:
            pending = [entry[2] for entry in old.queue.pending]
            old.queue.discard()
            if preserve_queue:
                for turn in pending:
                    self._submit_turn(session_id, state, turn)
        self._sessions[session_id] = state
        try:
            if old:
                await self._close_session(old)
        finally:
            state.ready.set()

    def _ensure_session_gc(self):
        if self.SESSION_IDLE_TTL_S > 0 and (
            self._gc_task is None or self._gc_task.done()
        ):
            self._gc_task = asyncio.create_task(self._session_gc_loop())

    async def _session_gc_loop(self):
        while True:
            await asyncio.sleep(self.SESSION_GC_INTERVAL_S)
            for identity, state in list(self._sessions.items()):
                if (
                    not state.queue.current
                    and not state.queue.pending
                    and time.monotonic() - state.touched > self.SESSION_IDLE_TTL_S
                ):
                    self._sessions.pop(identity, None)
                    await self._close_session(state)
            logger.prune_old_logs()

    def _submit_turn(self, identity, state, turn):
        return state.queue.submit(
            lambda: self._consume_turn(identity, state, turn), data=turn
        )

    async def _consume_turn(self, identity, state, turn):
        await state.ready.wait()
        try:
            result = await asyncio.wait_for(
                self._run_turn(identity, state, turn), self.AGENT_RUN_TIMEOUT_S
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[%s] Agent 请求失败: %s", self.platform_tag, exc)
            result = f"本轮执行失败，未完成的操作不能视为成功：{exc}"
        finally:
            state.touched = time.monotonic()
        if self._sessions.get(identity) is state:
            await self._safe_send(turn.send_reply, result)

    def _split_reply(self, text):
        chunks = []
        while len(text) > self.REPLY_MAX_CHARS:
            window = text[: self.REPLY_MAX_CHARS]
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
        for chunk in self._split_reply(text):
            try:
                await asyncio.wait_for(send_reply(chunk), self.SEND_REPLY_TIMEOUT_S)
            except TimeoutError:
                await asyncio.wait_for(send_reply(chunk), self.SEND_REPLY_TIMEOUT_S)

    def guess_download_mime(self, *, filename="", media_type_key=""):
        return mimetypes.guess_type(filename)[0] or self._MIME_MAP.get(
            media_type_key.lower(), "application/octet-stream"
        )

    def _notify(self, text):
        identity, state, turn = self._agent_ctx.get()

        def send():
            if self._sessions.get(identity) is state:
                asyncio.create_task(self._safe_send(turn.send_reply, text))

        turn.loop.call_soon_threadsafe(send)

    async def _run_turn(self, identity, state, turn):
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
                    conversation_log_hint=identity,
                    conversation_log_extra={
                        "session_id": identity,
                        "platform": self.platform_tag,
                    },
                )
                return result
        finally:
            tool_telemetry.set_user_notify_callback(None)
            self._agent_ctx.reset(token)

    async def _ask_user(self, question, timeout=120):
        identity, state, turn = self._agent_ctx.get()
        async with state.question_lock:
            if self._sessions.get(identity) is not state:
                return None
            state.question = asyncio.get_running_loop().create_future()
            try:
                await self._safe_send(turn.send_reply, question)
                return await asyncio.wait_for(state.question, timeout)
            except TimeoutError:
                return None
            finally:
                state.question = None

    async def dispatch_user_message(self, session_id, message, send_reply):
        user_text = message.text
        if not session_id or self._released:
            return
        state = self._session(session_id)
        state.touched = time.monotonic()
        if user_text == "/stop":
            if state.agent:
                await state.agent.stop_current_turn()
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
        if user_text == "/urgent" or user_text.startswith("/urgent "):
            user_text = user_text[len("/urgent") :].strip()
            if not user_text:
                await self._safe_send(send_reply, "用法：/urgent <内容>")
                return
            message = replace(message, text=user_text, original_text=user_text)
            if state.agent and await state.agent.add_urgent_message(message):
                await self._safe_send(
                    send_reply, "已加入当前回合，将与工具结果一起处理。"
                )
                return
        if state.question and not state.question.done():
            state.question.set_result(user_text)
            state.agent._session.user_inputs.append(user_text)
            return
        if not user_text and not message.attachments and not message.references:
            return
        app_config.reload_config()
        if missing := app_config.missing_main_api_keys():
            await self._safe_send(send_reply, "缺少模型接口配置：" + ", ".join(missing))
            return
        if state.queue.maxsize and len(state.queue.pending) >= state.queue.maxsize:
            await self._safe_send(
                send_reply, f"待处理消息已达上限 {state.queue.maxsize}，请稍后再试。"
            )
            return
        self._submit_turn(
            session_id,
            state,
            QueuedTurn(message, send_reply, asyncio.get_running_loop()),
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
