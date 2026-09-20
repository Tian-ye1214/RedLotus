"""Core goals responsibilities."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from pydantic_ai.messages import (
    BaseToolReturnPart,
    ModelRequest,
    ModelResponse,
    TextPart,
)

from redlotus.documents.interaction import UserMessage
from redlotus.prompts.message_text import split_messages_into_turns
from redlotus.prompts.prompt import load_prompt


class GoalSignal(str, Enum):
    CONTINUE = "CONTINUE"
    DONE = "DONE"


GOAL_MARKER_RE = re.compile(
    r"<!--\s*REDLOTUS_GOAL\s*:\s*(CONTINUE|DONE)\s*-->",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GoalParseResult:
    signal: GoalSignal
    cleaned_text: str
    marker_count: int

    @property
    def missing_marker(self) -> bool:
        return self.marker_count == 0


def parse_goal_output(text: str) -> GoalParseResult:
    """Strip goal-mode sentinel markers and return the effective signal.

    If no marker is present, goal mode treats the turn as CONTINUE so the next
    prompt can remind the model to add an explicit status marker.
    """
    body = text or ""
    matches = list(GOAL_MARKER_RE.finditer(body))
    cleaned = GOAL_MARKER_RE.sub("", body).strip()
    if not matches:
        return GoalParseResult(GoalSignal.CONTINUE, cleaned, 0)
    signal = GoalSignal(matches[-1].group(1).upper())
    return GoalParseResult(signal, cleaned, len(matches))


def summarize_last_coordinator_turn(messages: list) -> str:
    """Build goal-mode previous_output from the latest turn's assistant text and tool returns."""
    turns = split_messages_into_turns(messages)
    if not turns:
        return ""
    sections: list[str] = []
    for msg in turns[-1]:
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, TextPart):
                    text = (part.content or "").strip()
                    if text:
                        sections.append(text)
        elif isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, BaseToolReturnPart):
                    content = (
                        part.model_response_str()
                        if hasattr(part, "model_response_str")
                        else str(part.content)
                    )
                    tool_name = part.tool_name or "tool"
                    sections.append(f"[{tool_name}]\n{content}")
    return "\n\n".join(sections).strip()


def build_goal_iteration_prompt(
    *,
    original_goal: str,
    iteration: int,
    previous_output: str = "",
    missing_marker_reminder: bool = False,
) -> str:
    template = load_prompt("goal_iteration.md")
    return template.format(
        original_goal=original_goal.strip(),
        iteration=iteration,
        previous_output=previous_output.strip(),
        user_updates="",
        missing_marker_reminder=str(bool(missing_marker_reminder)).lower(),
    )


async def run_goal_loop(
    system: Any,
    *,
    message: UserMessage,
    history: Any,
    turn_id: str | None,
    conversation_log_hint: str,
    set_iteration: Callable[[int], None] | None = None,
) -> None:
    original_goal = message.text or ""
    previous_output = ""
    missing_marker = False
    iteration = 0

    while True:
        iteration += 1
        if set_iteration is not None:
            set_iteration(iteration)

        prompt_text = build_goal_iteration_prompt(
            original_goal=original_goal,
            iteration=iteration,
            previous_output=previous_output,
            missing_marker_reminder=missing_marker,
        )

        parse_result: GoalParseResult | None = None

        def output_transform(raw_output: str) -> str:
            nonlocal parse_result
            parse_result = parse_goal_output(raw_output)
            return parse_result.cleaned_text

        prompt_message = replace(message, text=prompt_text)
        _history, output = await system.run_agent_system(
            prompt_message,
            history,
            conversation_log_hint=conversation_log_hint,
            conversation_log_extra={
                "turn_id": turn_id,
                "goal_mode": True,
                "goal_iteration": iteration,
            },
            turn_id=turn_id,
            output_transform=output_transform,
            _inside_goal=True,
        )

        parsed = parse_result or parse_goal_output(output)
        previous_output = summarize_last_coordinator_turn(_history.messages) or output
        missing_marker = parsed.missing_marker

        if parsed.signal == GoalSignal.DONE:
            return


MODEL_MESSAGES_GLOB = "*/model_messages.json"
