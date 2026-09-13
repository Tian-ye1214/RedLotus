from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterator


_CURRENT_TURN_ID: ContextVar[str | None] = ContextVar("agent_turn_id", default=None)
_CURRENT_AGENT_ID: ContextVar[str | None] = ContextVar("agent_id", default=None)


@dataclass(frozen=True)
class AgentRunPolicy:
    max_worker_concurrent: int
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


@contextmanager
def turn_context(turn_id: str | None) -> Iterator[None]:
    token = _CURRENT_TURN_ID.set(turn_id)
    try:
        yield
    finally:
        _CURRENT_TURN_ID.reset(token)


@contextmanager
def agent_context(agent_id: str | None) -> Iterator[None]:
    token = _CURRENT_AGENT_ID.set(agent_id)
    try:
        yield
    finally:
        _CURRENT_AGENT_ID.reset(token)


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
