from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

from redlotus.infra import logger
from redlotus.prompt import with_runtime_context

from redlotus.ModelGateway.agent_factory import create_agent
from redlotus.ModelGateway.model_factory import ModelTarget
from redlotus.config.app_config import get_agent_usage_limits
from redlotus.infra.shared_http import close_all_clients


async def complete_text(role: str, system_prompt: str, user_text: str) -> str:
    """Auxiliary calls use the foreground Agent's provider routing."""
    target = ModelTarget.for_role(role)
    agent = create_agent(target, instructions=system_prompt, role=role)
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


def complete_text_sync(role: str, system_prompt: str, user_text: str) -> str:
    """Compression workers own and close their event-loop resources."""

    async def run() -> str:
        try:
            return await complete_text(role, system_prompt, user_text)
        finally:
            await close_all_clients()

    return asyncio.run(run())
