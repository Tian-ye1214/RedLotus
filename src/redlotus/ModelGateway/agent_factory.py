from __future__ import annotations

from typing import Any

from pydantic_ai import Agent, FunctionToolset
from pydantic_ai.capabilities import Capability

from redlotus.runtime import tool_telemetry
from redlotus.config.app_config import get_agent_run_policy
from redlotus.ModelGateway.model_factory import ModelTarget, create_model


_TOOL_DESCRIPTIONS = {
    "core": "Always-available Worker tools for reading, searching, user input, and coordination.",
    "file_mutation": "Use for writing, editing, appending files, or creating directories.",
    "execution": "Use for running shell commands or executing files.",
    "browser": "Use for browser navigation, screenshots, page interaction, and browser inspection.",
    "media": "Use for reading images or extracting text from documents and attachments.",
    "memory": "Use for querying short-term memory or maintaining long-term memory.",
    "skills": "Use for listing, loading, refreshing, or executing Agent Skills.",
}


def create_function_toolset(
    tools: list,
    *,
    toolset_id: str = "default",
    instructions: str | None = None,
    defer_loading: bool = False,
) -> FunctionToolset:
    wrapped_tools = tool_telemetry.wrap_tools_for_user_notify(
        list(tools), policy=get_agent_run_policy()
    )
    return FunctionToolset(
        wrapped_tools,
        id=toolset_id,
        instructions=instructions,
        defer_loading=defer_loading,
    )


def create_worker_toolsets_and_capabilities(tool_groups):
    """Build resident and deferred tools with the same descriptions, wrapping and IDs."""
    resident, capabilities = [], []
    for group, description in _TOOL_DESCRIPTIONS.items():
        tools = tool_groups.get(group)
        if not tools:
            continue
        identity = "worker_" + group
        deferred = group != "core"
        toolset = create_function_toolset(
            list(tools),
            toolset_id=identity,
            instructions=description,
            defer_loading=deferred,
        )
        if deferred:
            capabilities.append(
                Capability(
                    id=identity,
                    description=description,
                    toolsets=[toolset],
                    defer_loading=True,
                )
            )
        else:
            resident.append(toolset)
    return resident, capabilities


def create_agent(
    model_name: Any,
    parameter: dict | None = None,
    instructions: str | None = None,
    *,
    toolsets: list | None = None,
    capabilities: list | None = None,
    output_type: Any = str,
    role: str | None = None,
    follow_config: bool = False,
    task_state=None,
):
    model = (
        create_model(model_name, parameter)
        if isinstance(model_name, (str, ModelTarget))
        else model_name
    )

    capabilities = list(capabilities or [])
    if role and isinstance(model_name, ModelTarget):
        from redlotus.ModelGateway.request_policy import RequestPolicy

        capabilities.append(
            RequestPolicy(
                role,
                model_name,
                model,
                follow_config=follow_config,
                task_state=task_state,
            )
        )
    return Agent(
        model,
        output_type=output_type,
        toolsets=list(toolsets) if toolsets is not None else None,
        capabilities=capabilities,
        instructions=instructions or "",
    )
