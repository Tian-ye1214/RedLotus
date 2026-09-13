from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from redlotus.ModelGateway.agent_factory import create_agent, create_function_toolset
from redlotus.ModelGateway.model_factory import ModelTarget
from redlotus.prompt import get_coordinator_system_prompt
from redlotus.skills.SkillsManager import SkillsManager


async def create_coordinator_agent(
    skills_manager: SkillsManager,
    memory_injection: str,
    routing_tools: Sequence[Any],
    worker_tools: Sequence[Any],
    task_state=None,
    *,
    instructions: str | None = None,
):
    target = ModelTarget.for_role("coordinator")
    if instructions is None:
        instructions = await asyncio.to_thread(
            get_coordinator_system_prompt, skills_manager, memory_injection
        )
    toolsets = [
        create_function_toolset(list(tools), toolset_id=name)
        for name, tools in (("delegation", routing_tools), ("execution", worker_tools))
        if tools
    ]
    return create_agent(
        target,
        instructions=instructions,
        toolsets=toolsets,
        role="coordinator",
        follow_config=True,
        task_state=task_state,
    )
