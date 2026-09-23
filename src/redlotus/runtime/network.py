"""Configured SDK transports, per-loop HTTP pools and model metadata."""
from __future__ import annotations

import asyncio
import inspect
import json
import threading
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from pydantic_ai import ModelSettings
from pydantic_ai.models import (
    create_async_http_client,
    get_user_agent,
    infer_model,
    parse_model_id,
)
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers import infer_provider_class
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.deepseek import DeepSeekProvider

from redlotus.runtime import logging as logger
from redlotus.runtime.config import (
    ConfigError,
    apply_thinking_config,
    config_source_summary,
    credential_value,
    get_context_config,
    get_env,
    get_model_and_params,
    settings,
)
from redlotus.runtime.resources import finish_io

_local = threading.local()
_OPENROUTER_LOCK = threading.Lock()
_OPENROUTER_META_MAP = None

def openai_base_url(value: str | None) -> str | None:
    """Append /v1 for a bare host; preserve provider-specific API prefixes."""
    if not value:
        return None
    base = value.rstrip("/")
    return base if urlsplit(base).path else base + "/v1"

def _clients() -> dict[str, httpx.AsyncClient]:
    """A connection pool belongs to one event loop in one thread."""
    loop = asyncio.get_running_loop()
    pools = getattr(_local, "pools", None)
    if pools is None:
        pools = _local.pools = {}
    return pools.setdefault(loop, {})

def get_client(
    key: str,
    factory: Callable[[], httpx.AsyncClient],
) -> httpx.AsyncClient:
    """Return a named AsyncClient owned by the current event loop."""
    clients = _clients()
    client = clients.get(key)
    if client is None or client.is_closed:
        client = factory()
        clients[key] = client
    return client

async def close_all_clients() -> None:
    """Release this loop's clients, including when another release is interrupted."""
    clients = _clients()
    _local.pools.pop(asyncio.get_running_loop(), None)
    closing = AsyncExitStack()
    for client in clients.values():
        closing.push_async_callback(client.aclose)
    await finish_io(closing.aclose())

class InputLimitError(ValueError):
    """The prepared input cannot fit the selected gateway's declared limits."""

@dataclass(frozen=True)
class ModelInputPolicy:
    max_files: int
    max_file_bytes: int
    reference_download_timeout_seconds: float
    max_request_bytes: int | None = None

    @classmethod
    def for_role(cls, role: str = "coordinator") -> "ModelInputPolicy":
        return cls.from_limits(ModelTarget.for_role(role).options["limits"])

    @classmethod
    def from_limits(cls, values: dict) -> "ModelInputPolicy":
        return cls(
            max_files=int(values["max_files"]),
            max_file_bytes=int(values["max_file_bytes"]),
            reference_download_timeout_seconds=values["reference_download_timeout_seconds"],
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
    def options(self) -> dict:
        return json.loads(self.options_json)

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
        protocol, name = parse_model_id(name)
        if not protocol or not name:
            field_name = f"models.{role}.name" if role else "model name"
            raise ConfigError(
                f"配置 {field_name} 需要 Pydantic AI 的 服务:模型 标识；检查来源: {config_source_summary()}"
            )
        params.pop("provider", None)
        base = (
            gateway.get("base_url")
            if gateway_name
            else get_env("BASE_URL", warn=False, cfg=cfg)
        )
        if gateway_name:
            key = credential_value(gateway_name, cfg)
        else:
            key = get_env("API_KEY", warn=False, cfg=cfg)
        context = get_context_config(role, cfg=cfg) if role else {}
        for field_name in ("max_context_windows", "auto_compress_ratio", "compress_head_turns", "compress_tail_turns"):
            if field_name in params:
                context[field_name] = params.pop(field_name)
        params.pop("context", None)
        limits = cfg.get("input_limits", {})
        params["parallel_tool_calls"] = True
        timeout = float(gateway["timeout"] if "timeout" in gateway else cfg["MODEL_HTTP_TIMEOUT"])
        options = {
            "credential_field": f"gateways.{gateway_name}.api_key" if gateway_name else "API_KEY",
            "connect_timeout": float(gateway.get("connect_timeout", timeout)),
            "limits": {
                **limits.get("defaults", {}),
                **gateway.get("input_limits", {}),
                **params.pop("input_limits", {}),
                **limits.get(role, {}),
            },
            "context": context,
            "settings": params,
        }
        return cls(
            name,
            protocol,
            base or None,
            key,
            json.dumps(options, sort_keys=True),
            timeout,
        )

def context_length_exceeded(error) -> bool:
    """Recognize the capacity rejection observed from the configured service."""
    return (error.status_code == 400 and isinstance(error.body, dict)
            and str(error.body.get("message", "")).startswith("This model's maximum context length is "))


class CompatibleChatModel(OpenAIChatModel):
    def _map_model_response(self, message):
        mapped = super()._map_model_response(message)
        if mapped and not mapped.get("content") and not mapped.get("tool_calls"):
            return None  # Interrupted thinking is not a completed assistant message.
        if mapped and mapped.get("tool_calls") and mapped.get("content") is None:
            mapped["content"] = ""
        return mapped

def _anthropic_uses_httpx2() -> bool:
    """Whether the installed Anthropic SDK requires its newer httpx2 client."""
    from anthropic import AsyncAnthropic

    parameter = inspect.signature(AsyncAnthropic).parameters.get("http_client")
    return parameter is not None and "httpx2" in str(parameter.annotation)

def _create_anthropic_http_client(timeout: float, connect_timeout: float, policy):
    """Create the transport required by current Anthropic SDK releases."""
    import httpx2

    client = httpx2.AsyncClient(
        timeout=httpx2.Timeout(timeout=timeout, connect=connect_timeout),
        headers={"User-Agent": get_user_agent()},
    )
    if policy.max_request_bytes is not None:
        client.event_hooks["request"].append(policy.check_http_request)
    return client

def _adapt_anthropic_message_api(client):
    """Move legacy sampling values into the SDK's supported ``extra_body`` hook."""
    from anthropic import NotGiven, Omit

    create = client.beta.messages.create
    parameters = inspect.signature(create).parameters.values()
    if any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
    ):
        return client
    supported = {parameter.name for parameter in parameters}
    unsupported = {"temperature", "top_p", "top_k"} - supported
    if not unsupported:
        return client

    async def compatible_create(*args, **kwargs):
        moved = {
            key: kwargs.pop(key)
            for key in unsupported
            if key in kwargs
            and not isinstance(kwargs[key], (Omit, NotGiven))
        }
        if moved:
            extra_body = kwargs.get("extra_body")
            if extra_body is None or isinstance(extra_body, (Omit, NotGiven)):
                extra_body = {}
            elif isinstance(extra_body, Mapping):
                extra_body = dict(extra_body)
            else:
                raise ConfigError("Anthropic extra_body 必须是对象，无法保留采样参数。")
            for key, value in moved.items():
                extra_body.setdefault(key, value)
            kwargs["extra_body"] = extra_body
        return await create(
            *args,
            **{key: value for key, value in kwargs.items() if key not in unsupported},
        )

    client.beta.messages.create = compatible_create
    return client

def _create_anthropic_provider(target: ModelTarget, policy: ModelInputPolicy):
    """Bind Anthropic to the HTTP client type supported by its installed SDK."""
    connect_timeout = target.options["connect_timeout"]
    if not _anthropic_uses_httpx2():
        client = get_client(
            f"model:anthropic:{target.timeout}:{connect_timeout}:{policy.max_request_bytes}",
            lambda: _create_http_client(target, policy),
        )
        return AnthropicProvider(
            api_key=target.api_key, base_url=target.base_url, http_client=client
        )

    from anthropic import AsyncAnthropic

    transport = get_client(
        f"model:anthropic-httpx2:{target.timeout}:{connect_timeout}:{policy.max_request_bytes}",
        lambda: _create_anthropic_http_client(
            target.timeout, connect_timeout, policy
        ),
    )
    client = AsyncAnthropic(
        api_key=target.api_key,
        base_url=target.base_url,
        http_client=transport,
    )
    return AnthropicProvider(anthropic_client=_adapt_anthropic_message_api(client))

def _create_http_client(target: ModelTarget, policy: ModelInputPolicy):
    client = create_async_http_client(
        timeout=target.timeout, connect=target.options["connect_timeout"]
    )
    if policy.max_request_bytes is not None:
        client.event_hooks["request"].append(policy.check_http_request)
    return client

def _create_provider(provider_name: str, target: ModelTarget, policy: ModelInputPolicy):
    """Supply configured credentials and transport to the provider selected by the SDK."""
    provider_type = infer_provider_class(provider_name)
    if provider_type is AnthropicProvider:
        return _create_anthropic_provider(target, policy)
    client = get_client(
        f"model:{provider_name}:{target.timeout}:{target.options['connect_timeout']}:{policy.max_request_bytes}",
        lambda: _create_http_client(target, policy),
    )
    parameters = inspect.signature(provider_type).parameters
    if "openai_client" in parameters:
        from openai import AsyncOpenAI

        return provider_type(openai_client=AsyncOpenAI(
            base_url=openai_base_url(target.base_url),
            api_key=target.api_key,
            http_client=client,
        ))
    if "base_url" in parameters:
        return provider_type(base_url=target.base_url, api_key=target.api_key, http_client=client)
    raise ConfigError(f"当前 SDK 的 {provider_type.__name__} 不支持此处配置的自定义服务地址。")

def create_model(model_name: str | ModelTarget, parameter: dict | None = None):
    target = (
        model_name
        if isinstance(model_name, ModelTarget)
        else ModelTarget.from_values(model_name, parameter or {})
    )
    options = target.options
    credential_field = options.get("credential_field", "API_KEY")
    address_field = credential_field.removesuffix("api_key") + "base_url" if credential_field.startswith("gateways.") else "BASE_URL"
    missing = credential_field if not target.api_key else (
        address_field if not target.base_url else None
    )
    if missing:
        raise ConfigError(f"缺少配置 {missing}；检查来源: {config_source_summary()}")
    policy = ModelInputPolicy.from_limits(options["limits"])
    model = infer_model(
        f"{target.protocol}:{target.name}",
        provider_factory=lambda provider: _create_provider(provider, target, policy),
    )
    params = apply_thinking_config(options["settings"], model_name=target.name)
    profile = model.profile.copy()
    if isinstance(model.provider, DeepSeekProvider):
        requested = str(options["settings"].get("reasoning_effort", "")).strip().lower()
        if params.get("thinking") and requested:
            params["openai_reasoning_effort"] = {
                "minimal": "low",
                "medium": "high",
                "xhigh": "high",
                "ultra": "max",
            }.get(requested, requested)
        # Retain the verified DeepSeek wire-field fix; the SDK selects the provider/profile.
        profile["openai_chat_supports_max_completion_tokens"] = False
    model_type = CompatibleChatModel if type(model) is OpenAIChatModel else type(model)
    return model_type(
        model.model_name,
        provider=model.provider,
        profile=profile,
        settings=ModelSettings(**params),
    )

def _ensure_openrouter_maps() -> None:
    global _OPENROUTER_META_MAP
    with _OPENROUTER_LOCK:
        if _OPENROUTER_META_MAP is not None:
            return
        path = logger.get_log_dir() / "cache/openrouter_models.json"
        try:
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
            else:
                metadata = settings()["model_metadata"]
                with httpx.Client(timeout=metadata["timeout"]) as client:
                    response = client.get(metadata["url"])
                    response.raise_for_status()
                    raw = response.json()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            _OPENROUTER_META_MAP = {}
            for row in raw["data"]:
                name = row["id"].lower()
                _OPENROUTER_META_MAP[name] = row
                _OPENROUTER_META_MAP.setdefault(name.rsplit("/", 1)[-1], row)
        except (OSError, ValueError, httpx.HTTPError) as exc:
            _OPENROUTER_META_MAP = {}
            logger.warning("模型元数据不可用；未配置容量的角色将报告缺项：%s", exc)

def _lookup_openrouter_meta(name: str) -> dict | None:
    from pydantic_ai.models import parse_model_id

    _, name = parse_model_id(name)
    _ensure_openrouter_maps()
    rows = _OPENROUTER_META_MAP or {}
    return rows.get(name.lower()) or rows.get(name.lower().rsplit("/", 1)[-1])

def lookup_model_context(model_name: str) -> int | None:
    row = _lookup_openrouter_meta(model_name) or {}
    return row.get("top_provider", {}).get("context_length") or row.get(
        "context_length"
    )

def lookup_model_max_output_tokens(model_name: str) -> int | None:
    row = _lookup_openrouter_meta(model_name) or {}
    return row.get("top_provider", {}).get("max_completion_tokens")
