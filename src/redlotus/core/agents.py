"""Agent execution identity, lifecycle registry, workspace state, and turn admission."""

from __future__ import annotations

import time
import asyncio
import uuid
import contextvars
import functools
import inspect
import threading
from contextlib import AbstractContextManager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal
from collections import deque
from enum import Enum
from redlotus.runtime import logging as logger
from redlotus.runtime.resources import WorkspaceContext, bind_context, workspace_context
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from pydantic import BaseModel, ConfigDict, Field


_execution_role: ContextVar[str | None] = ContextVar("execution_role", default=None)


def current_execution_role() -> str | None:
    return _execution_role.get()


def execution_role(role: str):
    """Bind tool permissions to the current Agent's role."""
    return bind_context(_execution_role, role)


_CURRENT_TURN_ID: ContextVar[str | None] = ContextVar("agent_turn_id", default=None)
_CURRENT_AGENT_ID: ContextVar[str | None] = ContextVar("agent_id", default=None)


def current_turn_id() -> str | None:
    return _CURRENT_TURN_ID.get()


def current_agent_id() -> str | None:
    return _CURRENT_AGENT_ID.get()


def short_agent_id(agent_id: str | None) -> str:
    if not agent_id:
        return ""
    parts = agent_id.split(":")
    if len(parts) >= 2:
        return ":".join(parts[1:])
    return agent_id


def current_short_agent_id() -> str:
    return short_agent_id(current_agent_id())


def turn_context(turn_id: str | None) -> AbstractContextManager[None]:
    """Attach tool and model trace events to their owning user turn."""
    return bind_context(_CURRENT_TURN_ID, turn_id)


def agent_context(agent_id: str | None) -> AbstractContextManager[None]:
    """Attach tool and model trace events to their owning Agent."""
    return bind_context(_CURRENT_AGENT_ID, agent_id)


class TurnTraceStore:
    def __init__(self, max_turns: int = 200) -> None:
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._max_turns = max_turns

    def record(self, turn_id: str | None, kind: str, **fields: Any) -> None:
        key = turn_id or "unbound"
        event = {
            "at": time.time(),
            "kind": kind,
            **fields,
        }
        self._events.setdefault(key, []).append(event)
        while len(self._events) > self._max_turns:
            oldest = next(iter(self._events))
            if oldest == key:
                break
            del self._events[oldest]

    def events_for_turn(self, turn_id: str) -> list[dict[str, Any]]:
        return list(self._events.get(turn_id, []))

    def format_turn(self, turn_id: str) -> str:
        events = self.events_for_turn(turn_id)
        if not events:
            return f"Trace for {turn_id}: no events recorded."
        lines = [f"Trace for {turn_id} ({len(events)} event(s))"]
        for i, event in enumerate(events, 1):
            kind = event.get("kind", "event")
            if kind == "tool_call":
                status = "ok" if event.get("success") else "failed"
                agent_detail = (
                    f"agent_id={event.get('agent_id')} "
                    if event.get("agent_id")
                    else ""
                )
                detail = (
                    f"{agent_detail}tool={event.get('tool_name')} status={status} "
                    f"elapsed_ms={event.get('elapsed_ms', 0)} "
                    f"output_chars={event.get('output_chars', 0)}"
                )
                if event.get("error"):
                    detail += f" error={event.get('error')}"
            else:
                detail = " ".join(
                    f"{k}={v}" for k, v in event.items() if k not in {"at", "kind"}
                )
            lines.append(f"{i}. {kind}: {detail}")
        return "\n".join(lines)


TRACE_STORE = TurnTraceStore()


_invocation_stack: ContextVar[tuple[str, ...]] = ContextVar(
    "lifecycle_invocation_stack", default=()
)


class AgentInstanceState(Enum):
    IDLE = "idle"
    RUNNING = "running"


class AgentInvocationState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def make_agent_id(session_key: str, role: str, suffix: str | None = None) -> str:
    if suffix:
        return f"{session_key}:{role}:{suffix}"
    return f"{session_key}:{role}"


def _invocation_history_limit() -> int:
    from redlotus.runtime.config import settings

    lc = settings().get("lifecycle")
    if not isinstance(lc, dict):
        raise KeyError("config.json 缺少 lifecycle 配置块")
    n = lc.get("invocation_history_per_session")
    if not isinstance(n, int) or n < 1:
        raise ValueError("lifecycle.invocation_history_per_session 须为正整数")
    return n


@dataclass
class AgentInstance:
    agent_id: str
    role: str
    session_key: str
    state: AgentInstanceState
    current_invocation_id: str | None = None


@dataclass
class AgentInvocation:
    invocation_id: str
    agent_id: str
    role: str
    session_key: str
    parent_invocation_id: str | None
    turn_id: str | None
    state: AgentInvocationState
    started_at: float
    finished_at: float | None
    task_ref: asyncio.Task[Any] | None


@dataclass
class SessionLifecycleView:
    session_key: str
    agents: list[AgentInstance]
    active_invocations: list[AgentInvocation]
    recent_invocations: list[AgentInvocation]


@dataclass(frozen=True)
class InputAdmission:
    id: str
    sequence: int
    generation: int
    workspace: WorkspaceContext
    turn_id: str | None
    urgent: bool


class TurnQueue:
    """FIFO work admission; cancelling one turn never kills the queue consumer."""

    def __init__(self, maxsize=0):
        self.pending = deque()
        self.maxsize = maxsize
        self.current = None
        self.worker = None

    def submit(self, work, *, data=None):
        if self.maxsize and len(self.pending) >= self.maxsize:
            raise asyncio.QueueFull
        result = asyncio.get_running_loop().create_future()
        result.add_done_callback(
            lambda done: None if done.cancelled() else done.exception()
        )
        self.pending.append((work, result, data))
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._consume())
        return result

    async def _consume(self):
        try:
            while self.pending:
                work, result, _ = self.pending.popleft()
                if result.cancelled():
                    continue
                self.current = asyncio.create_task(work())
                try:
                    value = await self.current
                    if not result.done():
                        result.set_result(value)
                except asyncio.CancelledError:
                    result.cancel()
                    if asyncio.current_task().cancelling():
                        raise
                except Exception as exc:
                    if not result.done():
                        result.set_exception(exc)
                finally:
                    self.current = None
        finally:
            self.worker = None

    def discard(self):
        while self.pending:
            self.pending.popleft()[1].cancel()

    async def join(self):
        while self.worker and not self.worker.done():
            await asyncio.shield(self.worker)

    async def cancel(self, *, discard=False):
        if discard:
            self.discard()
        current = self.current
        if current and not current.done():
            current.cancel()
            await asyncio.gather(current, return_exceptions=True)


class AgentRegistry:
    """Lifecycle state owned by the application loop; child threads use their owner bridge."""

    def __init__(self):
        self._agents = {}
        self._invocations = {}
        self._history = {}

    @staticmethod
    def _prefix_matches(keys, prefix):
        return (
            [prefix]
            if prefix in keys
            else [key for key in keys if key.startswith(prefix.strip())]
        )

    async def ensure_agent(self, session_key, role, suffix=None):
        identity = make_agent_id(session_key, role, suffix)
        self._agents.setdefault(
            identity,
            AgentInstance(identity, role, session_key, AgentInstanceState.IDLE),
        )
        return identity

    async def list_agents(self, session_key=None):
        return [
            row
            for row in self._agents.values()
            if session_key is None or row.session_key == session_key
        ]

    async def list_active_invocations(self, session_key=None):
        return [
            row
            for row in self._invocations.values()
            if session_key is None or row.session_key == session_key
        ]

    async def list_recent_invocations(self, session_key):
        return list(self._history.get(session_key, ()))

    async def remove_session(self, session_key):
        self._agents = {
            key: row
            for key, row in self._agents.items()
            if row.session_key != session_key
        }
        self._history.pop(session_key, None)

    async def resolve_active_invocation_id(self, prefix):
        matches = self._prefix_matches(self._invocations, prefix)
        return matches[0] if len(matches) == 1 else None

    async def count_active_invocation_prefix_matches(self, prefix):
        return len(self._prefix_matches(self._invocations, prefix))

    async def find_recent_invocation_by_prefix(self, session_key, prefix):
        rows = {row.invocation_id: row for row in self._history.get(session_key, ())}
        matches = self._prefix_matches(rows, prefix)
        return rows[matches[0]] if len(matches) == 1 else None

    async def cancel(self, invocation_id):
        identity = await self.resolve_active_invocation_id(invocation_id)
        if identity is None:
            return False
        task = self._invocations[identity].task_ref
        if task and not task.done():
            task.cancel()
        return True

    async def _cancel_where(self, predicate):
        ids = [
            row.invocation_id for row in self._invocations.values() if predicate(row)
        ]
        for identity in ids:
            await self.cancel(identity)
        return len(ids)

    async def cancel_agent(self, agent_id):
        return await self._cancel_where(lambda row: row.agent_id == agent_id)

    async def cancel_turn(self, turn_id):
        return await self._cancel_where(lambda row: row.turn_id == turn_id)

    async def cancel_session(self, session_key):
        return await self._cancel_where(lambda row: row.session_key == session_key)

    async def cancel_all(self):
        return await self._cancel_where(lambda row: True)

    async def run(self, factory, *, agent_id, turn_id=None):
        agent = self._agents[agent_id]
        stack = _invocation_stack.get()
        invocation = AgentInvocation(
            uuid.uuid4().hex,
            agent_id,
            agent.role,
            agent.session_key,
            stack[-1] if stack else None,
            turn_id,
            AgentInvocationState.RUNNING,
            time.monotonic(),
            None,
            asyncio.current_task(),
        )
        self._invocations[invocation.invocation_id] = invocation
        agent.state, agent.current_invocation_id = (
            AgentInstanceState.RUNNING,
            invocation.invocation_id,
        )
        token = _invocation_stack.set((*stack, invocation.invocation_id))
        fields = dict(
            invocation_id=invocation.invocation_id,
            agent_id=agent_id,
            role=agent.role,
            parent=invocation.parent_invocation_id,
        )
        TRACE_STORE.record(turn_id, "invocation_start", **fields)
        error = ""
        try:
            with turn_context(turn_id), agent_context(agent_id):
                result = await factory()
            invocation.state = AgentInvocationState.COMPLETED
            return result
        except asyncio.CancelledError:
            invocation.state = AgentInvocationState.CANCELLED
            raise
        except BaseException as exc:
            invocation.state, error = AgentInvocationState.FAILED, str(exc)
            raise
        finally:
            invocation.finished_at = time.monotonic()
            invocation.task_ref = None
            _invocation_stack.reset(token)
            self._invocations.pop(invocation.invocation_id, None)
            self._history.setdefault(
                agent.session_key, deque(maxlen=_invocation_history_limit())
            ).append(invocation)
            if agent.current_invocation_id == invocation.invocation_id:
                agent.state, agent.current_invocation_id = AgentInstanceState.IDLE, None
            if agent.role == "worker":
                self._agents.pop(agent_id, None)
            TRACE_STORE.record(
                turn_id, "invocation_" + invocation.state.value, **fields, error=error
            )
            logger.debug(
                "[lifecycle] role=%s state=%s invocation=%s",
                agent.role,
                invocation.state.value,
                invocation.invocation_id,
            )


def bind_to_loop(function, loop):
    """Expose an owner-loop service to child tools without sharing loop-bound resources."""
    @functools.wraps(function)
    async def call(*args, **kwargs):
        async def invoke():
            result = function(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result

        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(invoke(), loop))

    return call


@dataclass(frozen=True)
class SubagentSpec:
    session_id: str
    turn_id: str | None
    workspace: WorkspaceContext
    role: str = "worker"


class SubagentHandle:
    """One Agent, one execution thread, one loop, and an idempotent cancel path."""

    def __init__(
        self,
        spec: SubagentSpec,
        execute: Callable[[], Awaitable[Any]],
    ) -> None:
        self.id = uuid.uuid4().hex
        self.spec = spec
        self._execute = execute
        self._future: Future = Future()
        self._cancelled = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._context = contextvars.copy_context()
        self.admission: asyncio.Task | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(
            target=lambda: self._context.run(self._run),
            name=f"subagent-{self.id[:8]}",
            daemon=True,
        )
        try:
            self.thread.start()
        except RuntimeError as exc:
            self._future.set_exception(exc)
            raise

    def _run(self) -> None:
        async def execute():
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            if self._cancelled.is_set():
                raise asyncio.CancelledError()
            try:
                with (
                    workspace_context(self.spec.workspace),
                    execution_role(self.spec.role),
                ):
                    return await self._execute()
            finally:
                from redlotus.runtime.network import close_all_clients

                await close_all_clients()

        try:
            result = asyncio.run(execute())
        except asyncio.CancelledError:
            self._future.cancel()
        except BaseException as exc:
            self._future.set_exception(exc)
        else:
            self._future.set_result(result)
        finally:
            self._task = None
            self._loop = None

    def cancel(self) -> None:
        if self._cancelled.is_set():
            return
        self._cancelled.set()
        if self.thread is None and self.admission is not None:
            self.admission.cancel()
        loop, task = self._loop, self._task
        if loop is not None and task is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The thread finished between the check and scheduling.

    async def result(self):
        future = asyncio.wrap_future(self._future)
        future.add_done_callback(
            lambda done: None if done.cancelled() else done.exception()
        )
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            self.cancel()
            raise

    async def close(self) -> None:
        if not self._future.done():
            self.cancel()
        # Yield to the UI and other threads while cooperative cleanup finishes.
        while self.thread is not None and self.thread.is_alive():
            await asyncio.sleep(0.01)
        if self._future.done() and not self._future.cancelled():
            self._future.exception()  # Retrieve errors even when its caller was cancelled.


class SubagentFactory:
    """Own the concurrency limit and all live child handles on the caller's loop."""

    def __init__(self, max_concurrent: int | None = None) -> None:
        if max_concurrent is None:
            from redlotus.runtime.config import settings

            max_concurrent = settings()["agent_run_policy"]["max_concurrent_threads_per_session"]
        if max_concurrent < 1:
            raise ValueError("Session thread limit must be positive")
        self._limit = max_concurrent
        self._slots: dict[str, asyncio.Semaphore] = {}
        self._handles: dict[str, SubagentHandle] = {}
        self._closed = False
        self._dispatches: dict[str, asyncio.Task] = {}

    def start_background(
        self, spec: SubagentSpec, execute: Callable[[], Awaitable[Any]]
    ):
        """Queue on the owner loop; only admitted jobs allocate an Agent thread."""
        if self._closed:
            raise asyncio.CancelledError("Agent factory is closed")

        handle = SubagentHandle(spec, execute)
        self._handles[handle.id] = handle
        task = asyncio.create_task(self._dispatch(handle))
        handle.admission = self._dispatches[handle.id] = task
        task.add_done_callback(lambda done: self._finished(handle, done))
        return handle

    async def _dispatch(self, handle):
        slots = self._slots.setdefault(handle.spec.session_id, asyncio.Semaphore(self._limit))
        async with slots:
            if self._closed or handle._cancelled.is_set():
                raise asyncio.CancelledError()
            try:
                handle.start()
                await handle.result()
            finally:
                await handle.close()

    def _finished(self, handle, task):
        self._dispatches.pop(handle.id, None)
        self._handles.pop(handle.id, None)
        if task.cancelled():
            handle._future.cancel()
        elif (error := task.exception()) is not None and not handle._future.done():
            handle._future.set_exception(error)
        if not any(item.spec.session_id == handle.spec.session_id for item in self.handles):
            self._slots.pop(handle.spec.session_id, None)

    @property
    def handles(self) -> tuple[SubagentHandle, ...]:
        return tuple(self._handles.values())

    def activity(self, session_id):
        """Read foreground child activity without counting admission waiters as running."""
        running = queued = 0
        for handle in self.handles:
            if handle.spec.session_id != session_id or handle.spec.role not in {"worker", "manager"}:
                continue
            if handle._future.done():
                continue
            if handle.thread is not None:
                running += int(handle.thread.is_alive())
            elif not handle._cancelled.is_set():
                queued += 1
        return running, queued

    async def run(self, spec: SubagentSpec, execute: Callable[[], Awaitable[Any]]):
        handle = self.start_background(spec, execute)
        try:
            return await handle.result()
        finally:
            await asyncio.shield(asyncio.gather(handle.admission, return_exceptions=True))

    async def _cancel(self, handles):
        for handle in handles:
            handle.cancel()
        await asyncio.gather(*(handle.admission for handle in handles), return_exceptions=True)

    async def cancel_all(self) -> None:
        await self._cancel(self.handles)

    async def cancel_turn(self, turn_id: str) -> None:
        handles = [handle for handle in self.handles if handle.spec.turn_id == turn_id]
        await self._cancel(handles)

    async def cancel_session(self, session_id: str) -> None:
        handles = [
            handle for handle in self.handles if handle.spec.session_id == session_id
        ]
        await self._cancel(handles)

    async def close(self) -> None:
        await self._cancel(self.stop())

    def stop(self) -> tuple[SubagentHandle, ...]:
        """Close admission and cancel every owned task before awaiting any cleanup."""
        self._closed = True
        handles = self.handles
        for handle in handles:
            handle.cancel()
        return handles


Outcome = Literal["success", "failed", "cancelled", "needs_input", "unverified"]


class SubagentResult(BaseModel):
    """Outcome of this child's assigned task only, not of the entire parent goal.

    Absence of evidence must never imply success.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    status: Outcome
    summary: str = Field(min_length=1)
    artifacts: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    needs_user_confirmation: bool = False

    @property
    def success(self) -> bool:
        return self.status == "success"


from redlotus.core.gateway import AgentRunner
