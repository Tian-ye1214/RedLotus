from __future__ import annotations

import json
import re
from typing import Any

from pydantic_ai.messages import (
    BaseToolReturnPart,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextContent,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)


def pydantic_messages_to_text(messages: list) -> str:
    """Render complete textual evidence without clipping model-visible content."""
    lines = []
    for message in messages:
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                items = (
                    [part.content] if isinstance(part.content, str) else part.content
                )
                for index, item in enumerate(items):
                    origin = (getattr(item, "metadata", None) or {}).get("origin")
                    if origin == "runtime_context":
                        continue
                    label = {
                        "context_summary": "CONTEXT SUMMARY",
                        "memory_control": "MEMORY CONTROL",
                    }.get(origin, "USER")
                    text = (
                        item.content
                        if isinstance(item, TextContent)
                        else item
                        if isinstance(item, str)
                        else "[original media reference]"
                    )
                    # UserMessage keeps original user text first, then emits labelled
                    # reference parts. The full originals remain in snapshots/journals.
                    if index > 0 and text.startswith(("[REFERENCE FILE ", "【引用文件 ")):
                        label = "REFERENCE FILE"
                    lines.append(f"[{label}]: {text}")
            elif isinstance(part, TextPart):
                lines.append(f"[ASSISTANT]: {part.content}")
            elif isinstance(part, ToolCallPart):
                args = (
                    part.args
                    if isinstance(part.args, str)
                    else json.dumps(part.args, ensure_ascii=False)
                )
                lines.append(f"[TOOL_CALL:{part.tool_name}]: {args}")
            elif isinstance(part, BaseToolReturnPart):
                text = part.model_response_str()
                lines.append(
                    f"[TOOL_RESULT:{part.tool_name}]: {text}"
                )
            elif isinstance(part, RetryPromptPart):
                lines.append(f"[RETRY:{part.tool_name}]: {part.content}")
    return "\n\n".join(lines)


def message_has_user_prompt(msg: Any) -> bool:
    return isinstance(msg, ModelRequest) and any(
        isinstance(p, UserPromptPart) for p in msg.parts
    )


def turn_has_agent_content(messages: list) -> bool:
    for msg in messages:
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, (TextPart, ToolCallPart)):
                    return True
    return False


def split_messages_into_turns(messages: list) -> list[list]:
    """Split complete user turns by UserPromptPart boundaries."""
    if not messages:
        return []
    turns: list[list] = []
    i = 0
    n = len(messages)
    while i < n:
        if not message_has_user_prompt(messages[i]):
            i += 1
            continue
        start = i
        i += 1
        while i < n and not message_has_user_prompt(messages[i]):
            i += 1
        chunk = messages[start:i]
        if turn_has_agent_content(chunk):
            turns.append(chunk)
    return turns
