from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

from redlotus.config.app_config import get_agent_usage_limits
from redlotus.infra import logger
from redlotus.infra.shared_http import close_all_clients
from redlotus.ModelGateway.agent_factory import create_agent
from redlotus.ModelGateway.model_factory import ModelTarget
from redlotus.prompt import with_runtime_context


async def complete_text(
    role: str, system_prompt: str, user_text: str, *, output_validator=None
) -> str:
    """Auxiliary calls use the foreground Agent's provider routing."""
    target = ModelTarget.for_role(role)
    agent = create_agent(target, instructions=system_prompt, role=role)
    if output_validator is not None:
        agent.output_validator(output_validator)
    result = await agent.run(
        with_runtime_context(user_text), usage_limits=get_agent_usage_limits()
    )
    logger.info_file_only(
        "[model_usage] %s",
        json.dumps(
            {"role": role, "model": target.name, **asdict(result.usage)},
            ensure_ascii=False,
        ),
    )
    return str(result.output or "")


def complete_text_sync(
    role: str, system_prompt: str, user_text: str, *, output_validator=None
) -> str:
    """Compression workers own and close their event-loop resources."""

    async def run() -> str:
        try:
            return await complete_text(
                role, system_prompt, user_text, output_validator=output_validator
            )
        finally:
            await close_all_clients()

    return asyncio.run(run())
