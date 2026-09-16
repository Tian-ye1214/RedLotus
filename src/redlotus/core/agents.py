"""Agent execution identity, lifecycle registry, bounded thread factory, and inner request loop."""

from __future__ import annotations

import hashlib
import os
import time
import asyncio
import uuid
import contextvars
import functools
import inspect
import threading
from contextlib import contextmanager, AbstractContextManager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from copy import deepcopy
from typing import Any, Literal
from collections import deque
from enum import Enum
from redlotus.core import config as logger
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import ModelRequestNode
from pydantic_ai.messages import (
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)


@dataclass(frozen=True)
class WorkspaceContext:
    """Immutable project identity carried into every run and child thread."""

    root: Path
    project_id: str

    @classmethod
    def from_path(cls, path: Path | str) -> WorkspaceContext:
        root = Path(path).expanduser().resolve()
        identity = os.path.normcase(str(root)).encode("utf-8")
        return cls(root, hashlib.sha256(identity).hexdigest()[:24])


_workspace_context: ContextVar[WorkspaceContext | None] = ContextVar(
    "workspace_context", default=None
)
_execution_role: ContextVar[str | None] = ContextVar("execution_role", default=None)


def current_execution_role() -> str | None:
    return _execution_role.get()


@contextmanager
def bind_context(variable, value, *, expose=False):
    """Set one execution context value and restore its parent on every exit path."""
    token = variable.set(value)
    try:
        yield value if expose else None
    finally:
        variable.reset(token)


def execution_role(role: str):
    """Bind tool permissions to the current Agent's role."""
    return bind_context(_execution_role, role)


def active_workspace() -> WorkspaceContext | None:
    return _workspace_context.get()


def workspace_context(workspace: WorkspaceContext):
    """Bind project identity for tools running in this execution context."""
    return bind_context(_workspace_context, workspace, expose=True)


_CURRENT_TURN_ID: ContextVar[str | None] = ContextVar("agent_turn_id", default=None)
_CURRENT_AGENT_ID: ContextVar[str | None] = ContextVar("agent_id", default=None)


@dataclass(frozen=True)
class AgentRunPolicy:
    max_concurrent_threads_per_session: int
    max_tool_output_chars: int
    max_command_timeout_seconds: int

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "AgentRunPolicy":
        return cls(**deepcopy(cfg["agent_run_policy"]))

    def clamp_command_timeout(self, timeout: int) -> int:
        return max(1, min(int(timeout), self.max_command_timeout_seconds))

    def truncate_text(self, text: str) -> str:
        if len(text) <= self.max_tool_output_chars:
            return text
        omitted = len(text) - self.max_tool_output_chars
        return (
            text[: self.max_tool_output_chars]
            + f"\n\n[tool output truncated: omitted {omitted} characters]"
        )


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
    from redlotus.core.config import settings

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
                from redlotus.core.config import close_all_clients

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
            from redlotus.core.config import settings

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


class SubagentResult(BaseModel):
    """Validated child output; absence of evidence must never imply success."""

    model_config = ConfigDict(str_strip_whitespace=True)

    status: Literal["success", "failed", "cancelled", "needs_input"] = Field(
        description="Outcome of the whole delegated goal. If a required step failed and remains unresolved, use failed; preserve successful substeps in summary.",
    )
    summary: str = Field(min_length=1)
    artifacts: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    needs_user_confirmation: bool = False

    @property
    def success(self) -> bool:
        return self.status == "success"


class AgentRunner:
    """The inner loop: finish a tool batch, assemble steering, request the model."""

    async def run(
        self,
        *,
        agent: Any,
        prompt: Any,
        message_history: list,
        usage_limits: Any,
        take_urgent: Callable[[], Awaitable[list]] | None = None,
        before_request: Callable[[Any, Any], Awaitable[None]] | None = None,
        on_node: Callable[[Any], Awaitable[None]] | None = None,
        on_complete: Callable[[], None] | None = None,
        event_stream_handler=None,
    ):
        from redlotus.core.gateway import InputLimitError

        original_history = list(message_history)
        response_received = False
        async with agent.iter(
            prompt, message_history=message_history, usage_limits=usage_limits
        ) as run:
            results = []
            prepared_request = None
            try:
                node = run.next_node
                while not agent.is_end_node(node):
                    if agent.is_model_request_node(node):
                        if take_urgent and node is not prepared_request:
                            node.request.parts.extend(
                                UserPromptPart(text) for text in await take_urgent()
                            )
                        prepared_request = None
                        if on_node:
                            # Audit the pending tool batch before a compressor changes the model view.
                            run.ctx.state.message_history.append(node.request)
                            try:
                                await on_node(run)
                            finally:
                                run.ctx.state.message_history.pop()
                        if before_request:
                            await before_request(run, node)

                    results = []
                    if agent.is_model_request_node(node) or agent.is_call_tools_node(
                        node
                    ):
                        async with node.stream(run.ctx) as stream:

                            async def events():
                                async for event in stream:
                                    if isinstance(event, FunctionToolResultEvent):
                                        results.append(event.part)
                                    yield event

                            if event_stream_handler:
                                await event_stream_handler(run.ctx, events())
                            else:
                                async for _ in events():
                                    pass
                    next_node = await run.next(node)
                    if agent.is_model_request_node(node):
                        response_received = True
                    if results and agent.is_model_request_node(next_node):
                        ids = {p.tool_call_id for p in results}
                        other = [
                            p
                            for p in next_node.request.parts
                            if getattr(p, "tool_call_id", None) not in ids
                        ]
                        next_node.request.parts[:] = [*results, *other]
                    if on_node:
                        await on_node(run)
                    # Steering arriving during a final model response still belongs to this turn.
                    if agent.is_end_node(next_node) and take_urgent:
                        urgent = await take_urgent()
                        if urgent:
                            next_node = ModelRequestNode(
                                ModelRequest(parts=[UserPromptPart(t) for t in urgent])
                            )
                            prepared_request = next_node
                    if agent.is_end_node(next_node) and on_complete:
                        on_complete()  # Later input is a new turn, even while trace writes are draining.
                    node = next_node
            except BaseException as exc:
                if on_complete:
                    on_complete()
                self._close_interrupted_calls(
                    run.ctx.state.message_history, results, exc
                )
                if on_node:
                    await on_node(run)
                if isinstance(exc, InputLimitError) and not response_received:
                    # The rejected input remains in the journal, outside the next request's view.
                    run.ctx.state.message_history[:] = original_history
                    if on_node:
                        await on_node(run)
                raise
        return run.result

    @staticmethod
    def _close_interrupted_calls(messages, results, error):
        pending = {}
        for message in messages:
            for part in message.parts:
                kind = getattr(part, "part_kind", "")
                if kind == "tool-call":
                    pending[part.tool_call_id] = part
                elif kind in ("tool-return", "retry-prompt"):
                    pending.pop(getattr(part, "tool_call_id", ""), None)
        completed = [part for part in results if part.tool_call_id in pending]
        for part in completed:
            pending.pop(part.tool_call_id, None)
        status = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
        completed.extend(
            ToolReturnPart(
                tool_name=part.tool_name,
                tool_call_id=key,
                content={
                    "status": status,
                    "error": "Execution interrupted; completion is unverified.",
                },
            )
            for key, part in pending.items()
        )
        if completed:
            messages.append(ModelRequest(parts=completed))
        messages.append(
            ModelResponse(
                parts=[
                    TextPart(
                        f"Execution {status}. Unfinished actions are unverified; await the next user instruction."
                    )
                ],
                metadata={"origin": "execution_status", "status": status},
            )
        )
