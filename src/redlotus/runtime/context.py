"""Runtime context responsibilities."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import os
import time
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

import redlotus.runtime.files as _runtime_files


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


_workspace = None


def current_workspace():
    active = active_workspace()
    return active.root if active else _workspace or Path.cwd().resolve()


def set_workspace(path):
    global _workspace
    _workspace = Path(path).expanduser().resolve()
    return _workspace


def conversations_root():
    return _runtime_files.session_data_dir(WorkspaceContext.from_path(current_workspace()))


_CURRENT_TURN_ID: ContextVar[str | None] = ContextVar("agent_turn_id", default=None)


_CURRENT_AGENT_ID: ContextVar[str | None] = ContextVar("agent_id", default=None)


@dataclass(frozen=True)
class AgentRunPolicy:
    max_concurrent_threads_per_session: int
    max_command_timeout_seconds: int

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "AgentRunPolicy":
        values = deepcopy(cfg["agent_run_policy"])
        values.pop("max_tool_output_chars", None)
        return cls(**values)

    def clamp_command_timeout(self, timeout: int) -> int:
        return max(1, min(int(timeout), self.max_command_timeout_seconds))


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


@dataclass(frozen=True)
class InputAdmission:
    id: str
    sequence: int
    generation: int
    workspace: WorkspaceContext
    turn_id: str | None
    urgent: bool


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


Outcome = Literal["success", "failed", "cancelled", "needs_input", "unverified"]


class SubagentResult(BaseModel):
    """Validated child output; absence of evidence must never imply success."""

    model_config = ConfigDict(str_strip_whitespace=True)

    status: Outcome
    summary: str = Field(min_length=1)
    artifacts: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    needs_user_confirmation: bool = False

    @property
    def success(self) -> bool:
        return self.status == "success"


class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PENDING_CONFIRMATION = "pending_confirmation"
    CANCELLED = "cancelled"
    UNVERIFIED = "unverified"


class EventEmitter:
    """Dispatch optional presentation events without importing an interface implementation."""

    def __init__(self, handlers=None):
        self.handlers = handlers or {}

    def emit(self, kind, *args, **kwargs):
        handler = self.handlers.get(kind)
        if handler is not None:
            return handler(*args, **kwargs)
