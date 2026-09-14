from __future__ import annotations

import json
from dataclasses import asdict

from pydantic import BaseModel, Field, field_validator
from pydantic_ai import PromptedOutput

from redlotus.config.app_config import get_agent_usage_limits, settings
from redlotus.infra import logger
from redlotus.infra.persist_utils import safe_name
from redlotus.ModelGateway.agent_factory import create_agent
from redlotus.ModelGateway.model_factory import ModelTarget
from redlotus.prompt import load_prompt, with_runtime_context


class TaskTitle(BaseModel):
    """The only value accepted from the title model."""

    title: str = Field(min_length=1)

    @field_validator("title")
    @classmethod
    def single_line(cls, value: str) -> str:
        value = value.strip()
        if not value or "\n" in value or "\r" in value:
            raise ValueError("title must be a non-empty single line")
        return value


def _max_chars() -> int:
    return int(settings()["task_title"]["max_chars"])


def _fallback(user_text: str, max_chars: int) -> str:
    first_line = next(
        (line.strip() for line in user_text.splitlines() if line.strip()), ""
    )
    return safe_name(first_line, max_len=max_chars, fallback="task")


def _title_from_output(output: object, max_chars: int) -> str:
    if not isinstance(output, TaskTitle):
        raise ValueError("title response did not match the structured output")
    if len(output.title) > max_chars:
        raise ValueError(f"title exceeds configured limit of {max_chars} characters")
    return output.title


async def generate_task_title(user_text: str) -> str:
    """Generate a short task title with the dedicated configured title role."""
    max_chars = _max_chars()
    fallback = _fallback(user_text, max_chars)
    try:
        target = ModelTarget.for_role("title")
        agent = create_agent(
            target,
            instructions=load_prompt("title_system.md"),
            output_type=PromptedOutput(TaskTitle),
            role="title",
        )
        result = await agent.run(
            with_runtime_context(user_text),
            usage_limits=get_agent_usage_limits(),
        )
        logger.info_file_only(
            "[model_usage] %s",
            json.dumps(
                {"role": "title", "model": target.name, **asdict(result.usage)},
                ensure_ascii=False,
            ),
        )
        return safe_name(
            _title_from_output(result.output, max_chars),
            max_len=max_chars,
            fallback=fallback,
        )
    except Exception as exc:
        logger.warning("LLM 标题生成失败，使用用户输入命名: %s", exc)
        return fallback
