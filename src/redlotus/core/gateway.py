"""Agent request orchestration, completion checkpoints and auxiliary model calls."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from pydantic import BaseModel, Field, field_validator
from pydantic_ai import (
    Agent,
    FunctionToolset,
    ModelRequestNode,
    PromptedOutput,
    RunContext,
)
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import (
    FunctionToolResultEvent,
    InstructionPart,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)

from redlotus.prompts.prompt import load_prompt, with_runtime_context
from redlotus.runtime import logging as logger
from redlotus.runtime.config import get_agent_run_policy, get_agent_usage_limits
from redlotus.runtime.network import (
    InputLimitError,
    ModelInputPolicy,
    ModelTarget,
    create_model,
)
from redlotus.sessions.context import agent_context, current_agent_id, current_usage_recorder


class RequestPolicy(AbstractCapability):
    def __init__(self, role, target, model, *, usage_category, follow_config=False, task_state=None, persist_context=None):
        self.role, self.target, self.model = role, target, model
        self.usage_category = usage_category
        self.follow_config = follow_config
        self.task_state = task_state
        self.persist_context = persist_context

    async def before_model_request(self, ctx, request_context):
        target = ModelTarget.for_role(self.role) if self.follow_config else self.target
        if target != self.target:
            model = create_model(target)
        else:
            model = self.model
        parameters = request_context.model_request_parameters
        tool_definitions = [*parameters.function_tools, *parameters.output_tools]
        candidate = request_context.messages
        if self.usage_category != "auxiliary" and "auto_compress_ratio" in target.context:
            from redlotus.core.history import compact_request_messages

            candidate = await compact_request_messages(
                request_context.messages,
                role=self.role,
                target=target,
                task_state=self.task_state() if self.task_state else "",
                tools=tool_definitions,
            )
        request = candidate[-1]
        request.metadata = {
            **(request.metadata or {}),
            "instruction_prefix_length": len(InstructionPart.join([
                part for part in parameters.instruction_parts or [] if not part.dynamic
            ]) or ""),
        }
        if candidate is not request_context.messages and self.persist_context:
            await self.persist_context(candidate)
        request_context.messages = candidate
        self.target, self.model = target, model
        ModelInputPolicy.from_limits(target.limits).check_messages(
            request_context.messages
        )
        request_context.model = model
        request_context.model_settings = model.settings or {}
        return request_context

    async def after_model_request(self, ctx, *, request_context, response):
        response.metadata = {
            **(response.metadata or {}),
            "usage_category": self.usage_category,
            "model_target": {
                "name": self.target.name,
                "protocol": self.target.protocol,
            },
        }
        if record := current_usage_recorder():
            await record([response], role=self.role, invocation=ctx.run_id,
                         agent_id=current_agent_id(), cancelling=bool(asyncio.current_task().cancelling()))
        return response

    async def on_model_request_error(self, ctx, *, request_context, error):
        cause = error
        while cause is not None:
            if isinstance(cause, InputLimitError):
                raise cause
            cause = cause.__cause__
        raise error


class AgentRunner:
    """The inner loop: finish a tool batch, assemble steering, request the model."""

    async def run(
        self,
        *,
        agent: Any,
        prompt: Any,
        message_history: list,
        usage_limits: Any,
        take_urgent: Callable[[], Awaitable[list]] | None = None,
        before_request: Callable[[Any, Any], Awaitable[None]] | None = None,
        on_node: Callable[[Any], Awaitable[None]] | None = None,
        on_complete: Callable[[], None] | None = None,
        event_stream_handler=None,
    ):
        original_history = list(message_history)
        response_received = False
        async with agent.iter(
            prompt, message_history=message_history, usage_limits=usage_limits
        ) as run:
            results = []
            prepared_request = None
            try:
                node = run.next_node
                while not agent.is_end_node(node):
                    if agent.is_model_request_node(node):
                        if take_urgent and node is not prepared_request:
                            node.request.parts.extend(
                                UserPromptPart(text) for text in await take_urgent()
                            )
                        prepared_request = None
                        if on_node:
                            # Audit the pending tool batch before a compressor changes the model view.
                            run.ctx.state.message_history.append(node.request)
                            try:
                                await on_node(run)
                            finally:
                                run.ctx.state.message_history.pop()
                        if before_request:
                            await before_request(run, node)

                    results = []
                    if agent.is_model_request_node(node) or agent.is_call_tools_node(
                        node
                    ):
                        async with node.stream(run.ctx) as stream:

                            async def events():
                                async for event in stream:
                                    if isinstance(event, FunctionToolResultEvent):
                                        results.append(event.part)
                                    yield event

                            if event_stream_handler:
                                await event_stream_handler(run.ctx, events())
                            else:
                                async for _ in events():
                                    pass
                    next_node = await run.next(node)
                    if agent.is_model_request_node(node):
                        response_received = True
                    if results and agent.is_model_request_node(next_node):
                        ids = {p.tool_call_id for p in results}
                        other = [
                            p
                            for p in next_node.request.parts
                            if getattr(p, "tool_call_id", None) not in ids
                        ]
                        next_node.request.parts[:] = [*results, *other]
                    if on_node:
                        await on_node(run)
                    # Steering arriving during a final model response still belongs to this turn.
                    if agent.is_end_node(next_node) and take_urgent:
                        urgent = await take_urgent()
                        if urgent:
                            next_node = ModelRequestNode(
                                ModelRequest(parts=[UserPromptPart(t) for t in urgent])
                            )
                            prepared_request = next_node
                    if agent.is_end_node(next_node) and on_complete:
                        on_complete()  # Later input is a new turn, even while trace writes are draining.
                    node = next_node
            except BaseException as exc:
                try:
                    if on_complete:
                        on_complete()
                    self._close_interrupted_calls(
                        run.ctx.state.message_history, results, exc
                    )
                    if on_node:
                        await on_node(run)
                    if isinstance(exc, InputLimitError) and not response_received:
                        # Retain the rejected input only in the journal.
                        run.ctx.state.message_history[:] = original_history
                        if on_node:
                            await on_node(run)
                except BaseException as recording_error:
                    raise exc from recording_error
                raise
        return run.result

    @staticmethod
    def _close_interrupted_calls(messages, results, error):
        pending = {}
        for message in messages:
            for part in message.parts:
                kind = getattr(part, "part_kind", "")
                if kind == "tool-call":
                    pending[part.tool_call_id] = part
                elif kind in ("tool-return", "retry-prompt"):
                    pending.pop(getattr(part, "tool_call_id", ""), None)
        completed = [part for part in results if part.tool_call_id in pending]
        for part in completed:
            pending.pop(part.tool_call_id, None)
        status = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
        completed.extend(
            ToolReturnPart(
                tool_name=part.tool_name,
                tool_call_id=key,
                content={
                    "status": status,
                    "execution_outcome": "unknown",
                    "error": type(error).__name__,
                },
                outcome="failed",
                metadata={
                    "origin": "runtime_control",
                    "execution_outcome": "unknown",
                    "interruption_status": status,
                },
            )
            for key, part in pending.items()
        )
        if completed:
            messages.append(ModelRequest(parts=completed))
        messages.append(
            ModelResponse(
                parts=[
                    TextPart(json.dumps({"execution_status": status}))
                ],
                metadata={"origin": "execution_status", "status": status},
            )
        )


def create_function_toolset(
    tools: list,
    *,
    toolset_id: str = "default",
    instructions: str | None = None,
    defer_loading: bool = False,
) -> FunctionToolset:
    from redlotus.tools import registry as tool_telemetry

    wrapped_tools = tool_telemetry.wrap_tools_for_user_notify(
        list(tools), policy=get_agent_run_policy()
    )
    return FunctionToolset(
        wrapped_tools,
        id=toolset_id,
        instructions=instructions,
        defer_loading=defer_loading,
    )


def _validate_current_text(ctx: RunContext, output: str) -> str:
    # The SDK may recover pre-tool or previous-turn text after an empty response.
    if not any(
        isinstance(part, TextPart) and part.content.strip()
        for part in ctx.messages[-1].parts
    ):
        raise UnexpectedModelBehavior(
            "模型本次没有返回有效答复，不能将先前的回复当作当前结果。"
            "已有执行记录已保留，可继续当前任务；本次未自动重跑工具。"
        )
    return output


def create_agent(
    model_name: Any,
    parameter: dict | None = None,
    instructions: str | None = None,
    *,
    toolsets: list | None = None,
    capabilities: list | None = None,
    output_type: Any = str,
    role: str | None = None,
    usage_category: str = "agent",
    follow_config: bool = False,
    task_state=None,
    persist_context=None,
):
    model = (
        create_model(model_name, parameter)
        if isinstance(model_name, (str, ModelTarget))
        else model_name
    )

    capabilities = list(capabilities or [])
    if role and isinstance(model_name, ModelTarget):
        capabilities.append(
            RequestPolicy(
                role,
                model_name,
                model,
                usage_category=usage_category,
                follow_config=follow_config,
                task_state=task_state,
                persist_context=persist_context,
            )
        )
    agent = Agent(
        model,
        output_type=output_type,
        toolsets=list(toolsets) if toolsets is not None else None,
        capabilities=capabilities,
        instructions=instructions or "",
    )
    if output_type is str:
        agent.output_validator(_validate_current_text)
    return agent


async def create_coordinator_agent(
    skills_manager: Any,
    memory_injection: str,
    routing_tools: Sequence[Any],
    worker_tools: Sequence[Any],
    task_state=None,
    *,
    instructions: str | None = None,
    persist_context=None,
):
    from redlotus.prompts.prompt import get_coordinator_system_prompt

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
        usage_category="main",
        follow_config=True,
        task_state=task_state,
        persist_context=persist_context,
    )


async def complete_text(
    role: str, system_prompt: str, user_text: str, *, output_validator=None, output_type=str
):
    """Auxiliary calls use the foreground Agent's provider routing."""
    target = ModelTarget.for_role(role)
    agent = create_agent(target, instructions=system_prompt, role=role, output_type=output_type,
                         usage_category="auxiliary")
    if output_validator is not None:
        agent.output_validator(output_validator)

    async def save_usage(run):
        if record := current_usage_recorder():
            for message in run.all_messages():
                if isinstance(message, ModelResponse):
                    message.metadata = {"usage_category": "auxiliary", **(message.metadata or {})}
            await record(run.all_messages(), role=role, invocation=run.ctx.state.run_id,
                         cancelling=bool(asyncio.current_task().cancelling()))

    with agent_context(None):
        result = await AgentRunner().run(
            agent=agent, prompt=with_runtime_context(user_text), message_history=[],
            usage_limits=get_agent_usage_limits(), on_node=save_usage,
        )
    return result.output


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


async def generate_task_title(user_text: str) -> str:
    """Generate a short task title with the dedicated configured title role."""
    fallback = next(
        (line.strip() for line in user_text.splitlines() if line.strip()),
        "",
    )
    try:
        result = await complete_text(
            "title", load_prompt("title_system.md"), user_text,
            output_type=PromptedOutput(TaskTitle),
        )
        return result.title
    except Exception as exc:
        logger.warning("LLM 标题生成失败，使用用户输入命名: %s", exc)
        return fallback
