"""Configuration-driven SDK model construction, request execution, and title generation."""

from __future__ import annotations

import json
import asyncio
from dataclasses import dataclass, field, asdict
from copy import deepcopy
from pydantic_ai import (
    Agent,
    FunctionToolset,
    ModelRequestNode,
    ModelSettings,
    PromptedOutput,
    RunContext,
)
from pydantic_ai.models import create_async_http_client
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.profiles.deepseek import deepseek_model_profile
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from redlotus.core.config import (
    apply_thinking_config,
    ConfigError,
    config_source_summary,
    credential_value,
    get_context_config,
    get_env,
    get_model_and_params,
    settings,
    get_client,
    openai_base_url,
    get_agent_run_policy,
    get_agent_usage_limits,
    close_all_clients,
    safe_name,
)
from pydantic_ai.capabilities import AbstractCapability, Capability
from typing import Any
from collections.abc import Awaitable, Callable, Sequence
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import (
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from redlotus.core import config as logger
from redlotus.prompts.prompt import with_runtime_context, load_prompt
from pydantic import BaseModel, Field, field_validator


class InputLimitError(ValueError):
    """The prepared input cannot fit the selected gateway's declared limits."""


@dataclass(frozen=True)
class ModelInputPolicy:
    max_files: int
    max_file_bytes: int
    max_request_bytes: int | None = None

    @classmethod
    def for_role(cls, role: str = "coordinator") -> "ModelInputPolicy":
        return cls.from_limits(ModelTarget.for_role(role).limits)

    @classmethod
    def from_limits(cls, values: dict) -> "ModelInputPolicy":
        return cls(
            max_files=int(values["max_files"]),
            max_file_bytes=int(values["max_file_bytes"]),
            max_request_bytes=values.get("max_request_bytes"),
        )

    def check(self, sizes: list[int]) -> None:
        if len(sizes) > self.max_files:
            raise InputLimitError(
                f"最多引用 {self.max_files} 个文件，本次为 {len(sizes)} 个。"
            )
        if any(size > self.max_file_bytes for size in sizes):
            raise InputLimitError(
                f"单个引用文件超过网关限额 {self.max_file_bytes:,} 字节。"
            )
        if self.max_request_bytes is not None and sum(sizes) > self.max_request_bytes:
            raise InputLimitError(
                f"引用文件总量超过网关请求限额 {self.max_request_bytes:,} 字节。"
            )

    def check_request_bytes(self, size: int) -> None:
        if self.max_request_bytes is not None and size > self.max_request_bytes:
            raise InputLimitError(
                f"编码后的请求为 {size:,} 字节，超过网关限额 {self.max_request_bytes:,} 字节。"
            )

    async def check_http_request(self, request) -> None:
        self.check_request_bytes(len(request.content))

    def check_messages(self, messages) -> None:
        from pydantic_ai import BinaryContent, TextContent

        encoded = 0
        for message in messages:
            for part in message.parts:
                content = getattr(part, "content", getattr(part, "args", ""))
                items = content if isinstance(content, (list, tuple)) else [content]
                for item in items:
                    if isinstance(item, BinaryContent):
                        self.check([len(item.data)])
                        encoded += 4 * ((len(item.data) + 2) // 3)
                    else:
                        text = item.content if isinstance(item, TextContent) else item
                        encoded += len(
                            (
                                text
                                if isinstance(text, str)
                                else json.dumps(text, ensure_ascii=False, default=str)
                            ).encode("utf-8")
                        )
        instructions = next(
            (
                m.instructions
                for m in reversed(messages)
                if getattr(m, "instructions", None)
            ),
            "",
        )
        self.check_request_bytes(encoded + len(instructions.encode("utf-8")))


@dataclass(frozen=True)
class ModelTarget:
    name: str
    protocol: str
    base_url: str | None
    api_key: str = field(repr=False)
    options_json: str = field(repr=False)
    timeout: float

    @property
    def settings(self) -> dict:
        return json.loads(self.options_json)["settings"]

    @property
    def limits(self) -> dict:
        return json.loads(self.options_json)["limits"]

    @property
    def context(self) -> dict:
        return json.loads(self.options_json)["context"]

    @property
    def connect_timeout(self) -> float:
        return json.loads(self.options_json)["connect_timeout"]

    @classmethod
    def for_role(cls, role: str) -> ModelTarget:
        config = deepcopy(settings())
        name, parameters = get_model_and_params(role, cfg=config)
        return cls.from_values(name, parameters, role=role, config=config)

    @classmethod
    def from_values(
        cls, name: str, parameters: dict, *, role: str = "", config=None
    ) -> ModelTarget:
        params = deepcopy(parameters)
        cfg = settings() if config is None else config
        gateway_name = params.pop("gateway", None)
        gateway = cfg["gateways"][gateway_name] if gateway_name else {}
        defaults = cfg["model_gateway"]
        protocol = params.pop("provider", gateway.get("protocol", defaults["protocol"]))
        protocol = {"openai": "openai-chat"}.get(protocol, protocol)
        if protocol not in ("openai-chat", "openai-responses", "anthropic", "google"):
            raise ValueError(f"Unsupported gateway protocol: {protocol}")
        base = (
            gateway.get("base_url")
            if gateway_name
            else get_env("BASE_URL", warn=False, cfg=cfg)
        )
        if gateway_name:
            key = credential_value(gateway_name, cfg)
        else:
            key = get_env("API_KEY", warn=False, cfg=cfg)
        limits = cfg.get("input_limits", {})
        options = {
            "credential_field": f"gateways.{gateway_name}.api_key" if gateway_name else "API_KEY",
            "connect_timeout": float(
                gateway.get("connect_timeout", defaults["connect_timeout"])
            ),
            "limits": {
                **limits.get("defaults", {}),
                **gateway.get("input_limits", {}),
                **params.pop("input_limits", {}),
                **limits.get(role, {}),
            },
            "context": {
                **(get_context_config(role, cfg=cfg) if role else {}),
                **params.pop("context", {}),
            },
            "settings": {**deepcopy(defaults["settings"]), **params},
        }
        timeout = float(
            gateway.get(
                "timeout",
                cfg["MODEL_HTTP_TIMEOUT"],
            )
        )
        return cls(
            name,
            protocol,
            base or None,
            key,
            json.dumps(options, sort_keys=True),
            timeout,
        )


class CompatibleChatModel(OpenAIChatModel):
    def _map_model_response(self, message):
        mapped = super()._map_model_response(message)
        if mapped and not mapped.get("content") and not mapped.get("tool_calls"):
            return None  # Interrupted thinking is not a completed assistant message.
        if mapped and mapped.get("tool_calls") and mapped.get("content") is None:
            mapped["content"] = ""
        return mapped


def create_model(model_name: str | ModelTarget, parameter: dict | None = None):
    target = (
        model_name
        if isinstance(model_name, ModelTarget)
        else ModelTarget.from_values(model_name, parameter or {})
    )
    credential_field = json.loads(target.options_json).get("credential_field", "API_KEY")
    address_field = credential_field.removesuffix("api_key") + "base_url" if credential_field.startswith("gateways.") else "BASE_URL"
    missing = credential_field if not target.api_key else (
        address_field if not target.base_url else None
    )
    if missing:
        raise ConfigError(f"缺少配置 {missing}；检查来源: {config_source_summary()}")
    policy = ModelInputPolicy.from_limits(target.limits)

    def new_client():
        client = create_async_http_client(
            timeout=target.timeout, connect=target.connect_timeout
        )
        if policy.max_request_bytes is not None:
            client.event_hooks["request"].append(policy.check_http_request)
        return client

    client = get_client(
        f"model:{target.timeout}:{target.connect_timeout}:{policy.max_request_bytes}",
        new_client,
    )
    params = apply_thinking_config(target.settings, model_name=target.name)
    if "deepseek" in target.name.lower() and target.protocol.startswith("openai"):
        requested = str(target.settings.get("reasoning_effort", "")).strip().lower()
        if params.get("thinking") and requested:
            params["openai_reasoning_effort"] = {
                "minimal": "low",
                "medium": "high",
                "xhigh": "high",
                "ultra": "max",
            }.get(requested, requested)
    provider_args = dict(api_key=target.api_key, http_client=client)
    if target.protocol == "google":
        provider = GoogleProvider(base_url=target.base_url, **provider_args)
        return GoogleModel(
            target.name, provider=provider, settings=ModelSettings(**params)
        )
    if target.protocol == "anthropic":
        provider = AnthropicProvider(base_url=target.base_url, **provider_args)
        return AnthropicModel(
            target.name, provider=provider, settings=ModelSettings(**params)
        )
    provider = OpenAIProvider(
        base_url=openai_base_url(target.base_url), **provider_args
    )
    if target.protocol == "openai-responses":
        return OpenAIResponsesModel(
            target.name, provider=provider, settings=ModelSettings(**params)
        )
    name = target.name.rsplit("/", 1)[-1]
    profile = OpenAIProvider.model_profile(name)
    if "deepseek" in name.lower():
        profile = deepseek_model_profile(name)
        profile.update(
            openai_supports_tool_choice_required=False,
            openai_chat_supports_max_completion_tokens=False,
            openai_chat_thinking_field="reasoning_content",
            openai_chat_send_back_thinking_parts="field",
        )
    return CompatibleChatModel(
        target.name,
        provider=provider,
        profile=profile,
        settings=ModelSettings(**params),
    )


class RequestPolicy(AbstractCapability):
    def __init__(self, role, target, model, *, follow_config=False, task_state=None):
        self.role, self.target, self.model = role, target, model
        self.follow_config = follow_config
        self.task_state = task_state

    async def before_model_request(self, ctx, request_context):
        target = ModelTarget.for_role(self.role) if self.follow_config else self.target
        if target != self.target:
            model = create_model(target)
        else:
            model = self.model
        parameters = request_context.model_request_parameters
        tool_definitions = [*parameters.function_tools, *parameters.output_tools]
        if self.role in ("coordinator", "manager", "worker"):
            from redlotus.core.history import compact_request_messages

            request_context.messages = await compact_request_messages(
                request_context.messages,
                role=self.role,
                target=target,
                task_state=self.task_state() if self.task_state else "",
                tools=tool_definitions,
            )
        else:
            from redlotus.core.history import get_effective_max_context_async
            from redlotus.core.history import estimate_context_tokens

            limit = await get_effective_max_context_async(
                target.name, role=self.role, context=target.context
            )
            if (
                estimate_context_tokens(
                    request_context.messages, tools=tool_definitions
                )
                + int(target.settings.get("max_tokens") or 0)
                >= limit
            ):
                raise InputLimitError(
                    "Input and configured output budget exceed the target context capacity."
                )
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
            "model_target": {
                "name": self.target.name,
                "protocol": self.target.protocol,
            },
        }
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
                if on_complete:
                    on_complete()
                self._close_interrupted_calls(
                    run.ctx.state.message_history, results, exc
                )
                if on_node:
                    await on_node(run)
                if isinstance(exc, InputLimitError) and not response_received:
                    # The rejected input remains in the journal, outside the next request's view.
                    run.ctx.state.message_history[:] = original_history
                    if on_node:
                        await on_node(run)
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
                    "error": "Execution interrupted; completion is unverified.",
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
                    TextPart(
                        f"Execution {status}. Unfinished actions are unverified; await the next user instruction."
                    )
                ],
                metadata={"origin": "execution_status", "status": status},
            )
        )


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
        capabilities.append(
            RequestPolicy(
                role,
                model_name,
                model,
                follow_config=follow_config,
                task_state=task_state,
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
        follow_config=True,
        task_state=task_state,
    )


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
