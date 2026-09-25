"""Conversation history, execution identity and recoverable outcome contracts."""
from __future__ import annotations

import json
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable, Literal, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai.messages import ModelRequest, ToolReturnPart

from redlotus.runtime.resources import WorkspaceContext, bind_context
from redlotus.prompts.prompt import with_runtime_context

if TYPE_CHECKING:
    from redlotus.tools.references import ReferenceFile


@dataclass
class UserMessage:
    """User prompt text plus optional pydantic-AI multimodal content."""

    text: str
    attachments: list = field(default_factory=list)
    original_text: str | None = None
    references: list[ReferenceFile] = field(default_factory=list)
    resume: dict | None = None

    def to_prompt(self):
        """Pass original requirements and explicitly labelled reference data together."""
        if self.resume is not None:
            from pydantic_ai.messages import TextContent
            return [*([] if self.resume.get('submitted', True) else with_runtime_context([self.text])),
                    TextContent(json.dumps({'command': 'resume', 'turn_id': self.resume['turn_id']}, ensure_ascii=False),
                                metadata={'origin': 'runtime_control'}),
                    *(item['text'] for item in self.resume['supplements']),
                    *(part for ref in self.references if not self.resume.get('submitted', True) or any(ref.id in row.get('reference_ids', []) for row in self.resume['supplements']) for part in ref.to_prompt())]
        parts = [self.text]
        for reference in self.references:
            parts.extend(reference.to_prompt())
        parts.extend(self.attachments)
        return with_runtime_context(parts)



def _part_kind(part) -> str:
    return str(getattr(part, "part_kind", "") or "")


def _tool_key(part) -> str:
    return str(
        getattr(part, "tool_call_id", None) or getattr(part, "tool_name", "") or ""
    )


def _has_user_prompt(message) -> bool:
    return any(
        _part_kind(part) == "user-prompt"
        for part in getattr(message, "parts", ()) or ()
    )


def messages_safe_for_new_prompt(messages: list) -> list:
    pending: dict[str, int] = {}
    for index, message in enumerate(messages):
        for part in getattr(message, "parts", ()) or ():
            kind = _part_kind(part)
            key = _tool_key(part)
            if kind in ("tool-return", "retry-prompt"):
                pending.pop(key, None)
            elif kind == "tool-call":
                pending[key] = index
    if not pending:
        return list(messages)

    cut = min(pending.values())
    for index in range(cut, -1, -1):
        if _has_user_prompt(messages[index]):
            cut = index
            break
    return list(messages[:cut])


def repair_interrupted_tool_calls(messages: list) -> list:
    """Close only persisted tool calls that have no recorded result."""
    pending: dict[str, Any] = {}
    for message in messages:
        for part in getattr(message, "parts", ()) or ():
            kind, key = _part_kind(part), _tool_key(part)
            if kind == "tool-call" and key:
                pending[key] = part
            elif kind in ("tool-return", "retry-prompt") and key:
                pending.pop(key, None)
    if not pending:
        return list(messages)
    metadata = {"origin": "runtime_control", "execution_outcome": "unknown"}
    returns = [
        ToolReturnPart(
            str(getattr(part, "tool_name", "")),
            {
                "status": "unknown",
                "result_recorded": False,
                "replayed": False,
            },
            tool_call_id=key,
            outcome="failed",
            metadata={**metadata, "tool_call_id": key},
        )
        for key, part in pending.items()
    ]
    return [*messages, ModelRequest(parts=returns, metadata=metadata)]


def _context_summary_metadata(message):
    """Return checkpoint metadata from either current or persisted SDK message shape."""
    candidates = [getattr(message, "metadata", None) or {}]
    for part in getattr(message, "parts", ()):
        if isinstance(getattr(part, "content", None), list):
            candidates.extend(getattr(item, "metadata", None) or {} for item in part.content)
    return next(
        (metadata for metadata in candidates if metadata.get("origin") == "context_summary"),
        None,
    )


class ChatHistory:
    __slots__ = (
        "_messages",
        "_compress_summary_state",
        "_revision",
    )

    def __init__(self):
        self._messages: list = []
        self._compress_summary_state: str | None = None
        self._revision = 0

    def update(self, result) -> None:
        """从 RunResult / StreamedRunResult 提取完整消息列表并保存。"""
        self._messages = list(result.all_messages())
        self._revision += 1

    def reset(self) -> None:
        self._messages = []
        self._compress_summary_state = None
        self._revision += 1

    def set_messages(self, messages: list) -> None:
        """直接替换消息列表（供上下文压缩等使用）。"""
        self._messages = list(messages)
        self._compress_summary_state = None
        self._revision += 1
        for message in reversed(self._messages):
            if metadata := _context_summary_metadata(message):
                self._compress_summary_state = metadata["summary"]
                return

    @property
    def compress_summary_state(self) -> str | None:
        """上一轮压缩模型产出的 Markdown 摘要文本，供下次压缩合并。"""
        return self._compress_summary_state

    @compress_summary_state.setter
    def compress_summary_state(self, value: str | None) -> None:
        self._compress_summary_state = value

    @property
    def messages(self) -> list:
        """传入 agent.run(message_history=...) 的只读引用。"""
        return self._messages

    @property
    def revision(self) -> int:
        return self._revision




def _estimate_text_tokens(text):
    # Dense decimal data can tokenize digit by digit. Keep the prose estimate
    # for ASCII punctuation/spacing so ordinary documents are not overcounted.
    return sum(1 if ord(char) > 127 or char.isdigit() else 0.3 for char in text)


_execution_role: ContextVar[str | None] = ContextVar("execution_role", default=None)


current_execution_role = _execution_role.get
execution_role = partial(bind_context, _execution_role)


_CURRENT_TURN_ID: ContextVar[str | None] = ContextVar("agent_turn_id", default=None)
_CURRENT_AGENT_ID: ContextVar[str | None] = ContextVar("agent_id", default=None)
_USAGE_RECORDER: ContextVar[Any] = ContextVar("usage_recorder", default=None)
_CANCELLING_WRITE: ContextVar[Callable[[], bool] | None] = ContextVar("cancelling_write", default=None)
current_usage_recorder = _USAGE_RECORDER.get


current_turn_id = _CURRENT_TURN_ID.get
current_agent_id = _CURRENT_AGENT_ID.get


def short_agent_id(agent_id: str | None) -> str:
    return agent_id.split(":", 1)[-1] if agent_id else ""


def current_short_agent_id() -> str:
    return short_agent_id(current_agent_id())


turn_context = partial(bind_context, _CURRENT_TURN_ID)
agent_context = partial(bind_context, _CURRENT_AGENT_ID)


class TurnTraceStore:
    def __init__(self) -> None:
        self._events: dict[str, list[dict[str, Any]]] = {}

    def record(self, turn_id: str | None, kind: str, **fields: Any) -> None:
        key = turn_id or "unbound"
        event = {
            "at": time.time(),
            "kind": kind,
            **fields,
        }
        self._events.setdefault(key, []).append(event)

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




def make_agent_id(session_key: str, role: str, suffix: str | None = None) -> str:
    return f"{session_key}:{role}" + (f":{suffix}" if suffix else "")


@dataclass(frozen=True)
class SubagentSpec:
    session_id: str
    turn_id: str | None
    workspace: WorkspaceContext
    role: str = "worker"


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


