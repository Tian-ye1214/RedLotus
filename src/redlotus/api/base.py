"""Api base responsibilities."""

from __future__ import annotations

import asyncio
import contextvars
import mimetypes
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

import redlotus.runtime.config as _runtime_config
import redlotus.runtime.resources as _runtime_resources
import redlotus.tools.registry as _tools_registry
from redlotus.core.system import AgentSystem
from redlotus.core.tasks import SessionController
from redlotus.documents.interaction import UserMessage
from redlotus.models.context import ChatHistory
from redlotus.runtime.config import get_env, settings
from redlotus.runtime.context import InputAdmission, WorkspaceContext, current_workspace
from redlotus.runtime.resources import close_all_clients


class AttachmentError(ValueError):
    """A complete incoming event could not be prepared for the model."""

    def __init__(self, identity: str, reason: str):
        self.identity = identity or "attachment"
        self.reason = reason or "unknown failure"
        super().__init__(f"{self.identity}: {self.reason}")


class InputAnswer(str):
    def __new__(cls, value: str, input_id: str):
        answer = super().__new__(cls, value)
        answer.input_id = input_id
        return answer


@dataclass(frozen=True)
class QueuedTurn:
    admission: InputAdmission
    prepare: Callable[[], Awaitable[UserMessage]]
    send_reply: Callable[..., Awaitable[Any]]
    loop: asyncio.AbstractEventLoop
    retry_context: Any = None


@dataclass(frozen=True)
class PendingQuestion:
    future: asyncio.Future


@dataclass
class ChatSession:
    controller: SessionController = field(default_factory=SessionController)
    history: ChatHistory = field(default_factory=ChatHistory)
    agent: AgentSystem | None = None
    retired_agents: list[AgentSystem] = field(default_factory=list)
    owner_memory_allowed: bool | None = None
    question: PendingQuestion | None = None
    question_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    preparations: set[asyncio.Task] = field(default_factory=set)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    touched: float = field(default_factory=time.monotonic)
    last_rejected: Any = None


class BotBase:
    AGENT_RUN_TIMEOUT_S = 900.0
    SEND_REPLY_TIMEOUT_S = 120.0
    REPLY_MAX_CHARS = 4500
    SESSION_IDLE_TTL_S = 3600.0
    SESSION_GC_INTERVAL_S = 300.0
    RESET_COMMANDS = frozenset({"新任务", "/新任务", "/reset"})
    END_TASK_COMMANDS = frozenset(
        {"结束任务", "/结束任务", "结束当前任务", "/结束当前任务"}
    )
    _MIME_MAP: dict[str, str] = {}
    _ENV_AGENT_TIMEOUT = ""
    _ENV_SEND_TIMEOUT = ""
    _ENV_SESSION_IDLE_TTL = ""

    platform_tag: str
    session_prefix: str

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

    def _new_session(self):
        return ChatSession()

    def _session(self, session_id):
        if session_id not in self._sessions:
            state = self._new_session()
            state.ready.set()
            self._sessions[session_id] = state
        return self._sessions[session_id]

    def _workspace(self):
        return WorkspaceContext.from_path(current_workspace())

    def _is_owner_session(self, session_id):
        platform = self.platform_tag.lower()
        prefix = {"qq": "private_", "wechat": "wx_"}.get(platform)
        owners = settings().get("bot", {}).get("owner_channels", {}).get(platform, [])
        return bool(
            prefix
            and session_id.startswith(prefix)
            and session_id[len(prefix) :] in map(str, owners)
        )

    async def _agent_for_session(self, session_id, state):
        allowed = self._is_owner_session(session_id)
        if state.agent is not None and state.owner_memory_allowed != allowed:
            state.retired_agents.append(state.agent)
            state.agent = None
            state.history.reset()
            state.controller.reset()
            state.controller.user_inputs.clear()
            state.last_rejected = None
        if state.agent is None:
            state.agent = AgentSystem(
                owner_memory_allowed=allowed,
                session_controller=state.controller,
            )
            state.owner_memory_allowed = allowed
            state.agent.set_ask_user_handler(self._ask_user)
            state.agent.set_task_directory(f"{self.platform_tag}_{session_id[:20]}")
        return state.agent

    async def _close_session(self, state):
        if state.question and not state.question.future.done():
            state.question.future.cancel()
        await self._cancel_preparations(state)
        state.controller.reset(discard=True)
        await state.controller.queue.cancel(discard=True)
        await state.controller.queue.join()
        for agent in [*state.retired_agents, state.agent]:
            if agent is not None:
                await agent.shutdown()

    async def _cancel_preparations(self, state):
        for task in tuple(state.preparations):
            task.cancel()
        if state.preparations:
            await asyncio.gather(*state.preparations, return_exceptions=True)

    def _submit_turn(self, identity, state, turn):
        return state.controller.queue.submit(
            lambda: self._consume_turn(identity, state, turn), data=turn
        )

    async def _reset_session(self, session_id, *, preserve_queue=False):
        old = self._sessions.pop(session_id, None)
        state = self._new_session()
        if old and preserve_queue:
            pending = [entry[2] for entry in old.controller.queue.pending]
            for turn in pending:
                admission = state.controller.admit(
                    self._workspace(), input_id=turn.admission.id
                )
                self._submit_turn(session_id, state, replace(turn, admission=admission))
        self._sessions[session_id] = state
        try:
            if old:
                await self._close_session(old)
        finally:
            state.ready.set()

    async def _reject_preparation(self, identity, state, turn, exc):
        if self._sessions.get(identity) is not state:
            return
        state.last_rejected = turn.retry_context
        error = (
            exc
            if isinstance(exc, AttachmentError)
            else AttachmentError("attachment", str(exc))
        )
        await self._safe_send(
            turn.send_reply,
            f"附件准备失败（{error.identity}）：{error.reason}。请重试原消息。",
        )

    async def _consume_turn(self, identity, state, turn):
        await state.ready.wait()
        if not state.controller.accepts(turn.admission):
            return
        try:
            message = await turn.prepare()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._reject_preparation(identity, state, turn, exc)
            return
        if (
            self._sessions.get(identity) is not state
            or not state.controller.accepts(turn.admission)
            or not (message.text or message.attachments or message.references)
        ):
            return
        _runtime_config.reload_config()
        if missing := _runtime_config.missing_main_api_keys():
            await self._safe_send(
                turn.send_reply, "缺少模型接口配置：" + ", ".join(missing)
            )
            return
        try:
            result = await asyncio.wait_for(
                self._run_turn(identity, state, turn, message),
                self.AGENT_RUN_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _runtime_resources.error("[%s] Agent 请求失败: %s", self.platform_tag, exc)
            result = f"本轮执行失败，未完成的操作不能视为成功：{exc}"
        finally:
            state.touched = time.monotonic()
        if self._sessions.get(identity) is state:
            await self._safe_send(turn.send_reply, result)

    async def dispatch_event(
        self, session_id, user_text, prepare, send_reply, *, retry_context=None
    ):
        if not session_id or self._released:
            return None
        state = self._session(session_id)
        state.touched = time.monotonic()
        if user_text == "/stop":
            await self._cancel_preparations(state)
            if state.agent:
                await state.agent.stop_current_turn()
            else:
                state.controller.reset()
                await state.controller.queue.cancel()
            await self._safe_send(send_reply, "已停止当前任务，保留会话记录。")
            return None
        if (
            user_text in self.RESET_COMMANDS
            or user_text == "/clear"
            or user_text in self.END_TASK_COMMANDS
        ):
            await self._reset_session(
                session_id, preserve_queue=user_text in self.END_TASK_COMMANDS
            )
            await self._safe_send(send_reply, "已结束当前任务并清空上下文。")
            return None

        if state.agent is not None and state.owner_memory_allowed != self._is_owner_session(session_id):
            await self._reset_session(session_id, preserve_queue=True)
            state = self._session(session_id)
        question = state.question
        admission = state.controller.admit(
            self._workspace(), urgent=bool(question and not question.future.done())
        )
        turn = QueuedTurn(
            admission,
            prepare,
            send_reply,
            asyncio.get_running_loop(),
            retry_context,
        )
        if question and admission.urgent:
            task = asyncio.create_task(
                self._deliver_answer(session_id, state, question, turn)
            )
            state.preparations.add(task)
            task.add_done_callback(state.preparations.discard)
            return admission
        queue = state.controller.queue
        if queue.maxsize and len(queue.pending) >= queue.maxsize:
            await self._safe_send(
                send_reply, f"待处理消息已达上限 {queue.maxsize}，请稍后再试。"
            )
            return None
        self._submit_turn(session_id, state, turn)
        await self._safe_send(send_reply, "✓ 收到，正在处理…")
        self._ensure_session_gc()
        return admission

    async def dispatch_user_message(self, session_id, message, send_reply):
        return await self.dispatch_event(
            session_id,
            message.text,
            lambda: asyncio.sleep(0, result=message),
            send_reply,
            retry_context=message,
        )

    async def _deliver_answer(self, identity, state, question, turn):
        try:
            message = await turn.prepare()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._reject_preparation(identity, state, turn, exc)
            return
        if message.attachments:
            attachment = message.attachments[0]
            attachment_identity = next(
                (
                    str(value)
                    for name in ("identifier", "name", "url")
                    if (value := getattr(attachment, name, None))
                ),
                type(attachment).__name__,
            )
            await self._reject_preparation(
                identity,
                state,
                turn,
                AttachmentError(
                    attachment_identity,
                    "question replies do not support attachments",
                ),
            )
            return
        text = message.text.strip()
        if (
            not text
            or self._sessions.get(identity) is not state
            or state.question is not question
            or question.future.done()
            or not state.controller.accepts(turn.admission)
            or state.owner_memory_allowed != self._is_owner_session(identity)
        ):
            return
        answer = InputAnswer(text, turn.admission.id)
        state.controller.user_inputs.append(text)
        question.future.set_result(answer)

    def _ensure_session_gc(self):
        if self.SESSION_IDLE_TTL_S > 0 and (
            self._gc_task is None or self._gc_task.done()
        ):
            self._gc_task = asyncio.create_task(self._session_gc_loop())

    async def _session_gc_loop(self):
        while True:
            await asyncio.sleep(self.SESSION_GC_INTERVAL_S)
            for identity, state in list(self._sessions.items()):
                queue = state.controller.queue
                if (
                    not queue.current
                    and not queue.pending
                    and time.monotonic() - state.touched > self.SESSION_IDLE_TTL_S
                ):
                    self._sessions.pop(identity, None)
                    await self._close_session(state)
            _runtime_resources.prune_old_logs()

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
                    _runtime_resources.error("[%s] 通知发送失败: %s", self.platform_tag, exc)

        turn.loop.call_soon_threadsafe(lambda: asyncio.create_task(send()))

    async def _run_turn(self, identity, state, turn, message):
        agent = await self._agent_for_session(identity, state)
        token = self._agent_ctx.set((identity, state, turn))
        _tools_registry.set_user_notify_callback(self._notify)
        try:
            with _runtime_resources.session_log_context(identity):
                if agent.session_key is None:
                    from uuid import uuid4

                    await agent.bind_session(uuid4().hex)
                _, result = await agent.run_agent_system(
                    message,
                    state.history,
                    turn_id=turn.admission.id,
                    conversation_log_hint=identity,
                    conversation_log_extra={
                        "session_id": identity,
                        "platform": self.platform_tag,
                    },
                )
                return result
        finally:
            _tools_registry.set_user_notify_callback(None)
            self._agent_ctx.reset(token)

    async def _ask_user(self, question, timeout=120):
        identity, state, turn = self._agent_ctx.get()
        async with state.question_lock:
            if self._sessions.get(identity) is not state:
                return None
            pending = PendingQuestion(asyncio.get_running_loop().create_future())
            state.question = pending
            try:
                await self._safe_send(turn.send_reply, question)
                return await asyncio.wait_for(pending.future, timeout)
            except TimeoutError:
                return None
            finally:
                if state.question is pending:
                    state.question = None

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
