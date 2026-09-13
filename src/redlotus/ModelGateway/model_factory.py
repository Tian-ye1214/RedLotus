"""Resolve configuration once; let Pydantic AI own each provider's wire protocol."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field

from pydantic_ai import ModelSettings
from pydantic_ai.models import create_async_http_client
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.profiles.deepseek import deepseek_model_profile
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider

from redlotus.config.app_config import (
    apply_thinking_config,
    get_context_config,
    get_env,
    get_model_and_params,
    settings,
)
from redlotus.infra.shared_http import get_client, openai_base_url


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
            key = gateway.get("api_key", "")
            if variable := gateway.get("api_key_env"):
                key = get_env(variable, warn=False, cfg=cfg) or key
        else:
            key = get_env("API_KEY", warn=False, cfg=cfg)
        limits = cfg.get("input_limits", {})
        options = {
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
    from redlotus.ModelGateway.input_policy import ModelInputPolicy

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
