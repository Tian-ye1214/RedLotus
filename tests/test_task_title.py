"""Auxiliary title contract tests; the wire case uses MockTransport, not a live model."""

import json
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai import PromptedOutput


@dataclass
class _Usage:
    input_tokens: int = 12
    output_tokens: int = 3
    cache_read_tokens: int = 8
    cache_write_tokens: int = 0


@pytest.fixture
def title_module():
    from redlotus.core import gateway as task_title

    return task_title


async def test_title_uses_dedicated_role_and_structured_output(
    title_module, monkeypatch
):
    calls = {}

    class FakeAgent:
        async def run(self, content, *, usage_limits):
            calls["content"] = content
            calls["usage_limits"] = usage_limits
            return SimpleNamespace(
                output=title_module.TaskTitle(title="整理个人博客"), usage=_Usage()
            )

    def create_agent(target, **kwargs):
        calls["target"] = target
        calls["kwargs"] = kwargs
        return FakeAgent()

    monkeypatch.setattr(
        title_module,
        "settings",
        lambda: {"task_title": {"max_chars": 50}},
    )
    monkeypatch.setattr(
        title_module.ModelTarget,
        "for_role",
        lambda role: SimpleNamespace(name="deepseek-v4-flash"),
    )
    monkeypatch.setattr(title_module, "create_agent", create_agent)
    monkeypatch.setattr(title_module, "load_prompt", lambda _: "title instructions")
    monkeypatch.setattr(title_module, "with_runtime_context", lambda value: [value])

    result = await title_module.generate_task_title("请整理我的个人博客")

    assert result == "整理个人博客"
    assert calls["target"].name == "deepseek-v4-flash"
    assert calls["kwargs"]["role"] == "title"
    assert isinstance(calls["kwargs"]["output_type"], PromptedOutput)
    assert "toolsets" not in calls["kwargs"]
    assert "memory" not in calls["kwargs"]


async def test_invalid_title_falls_back_to_the_first_user_line(
    title_module, monkeypatch
):
    warnings = []

    class FakeAgent:
        async def run(self, content, *, usage_limits):
            return SimpleNamespace(output=object(), usage=_Usage())

    monkeypatch.setattr(
        title_module,
        "settings",
        lambda: {"task_title": {"max_chars": 50}},
    )
    monkeypatch.setattr(
        title_module.ModelTarget,
        "for_role",
        lambda role: SimpleNamespace(name="deepseek-v4-flash"),
    )
    monkeypatch.setattr(
        title_module, "create_agent", lambda *args, **kwargs: FakeAgent()
    )
    monkeypatch.setattr(title_module, "load_prompt", lambda _: "title instructions")
    monkeypatch.setattr(title_module, "with_runtime_context", lambda value: [value])
    monkeypatch.setattr(
        title_module.logger,
        "warning",
        lambda message, *args: warnings.append((message, args)),
    )

    result = await title_module.generate_task_title("先处理第一项\n然后处理第二项")

    assert result == "先处理第一项"
    assert warnings


async def test_title_wire_uses_real_gateway_model_without_tools(monkeypatch):
    from pydantic_ai import models
    from pydantic_ai.usage import UsageLimits

    from redlotus.core import gateway as task_title
    from redlotus.core import gateway as model_factory
    from redlotus.core.gateway import ModelTarget

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "title-test",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": '{"title":"写日报"}',
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    config = {
        "task_title": {"max_chars": 50},
        "models": {"title": {"name": "deepseek-v4-flash"}},
        "gateways": {
            "title": {
                "protocol": "openai-chat",
                "base_url": "https://title.example/v1",
                "api_key": "test-only",
            }
        },
        "model_gateway": {
            "protocol": "openai-chat",
            "connect_timeout": 2,
            "settings": {"parallel_tool_calls": True},
        },
        "input_limits": {"defaults": {"max_files": 20, "max_file_bytes": 20_000_000}},
        "context": {"title": {"default_context_tokens": 10000}},
        "MODEL_HTTP_TIMEOUT": 10,
    }
    target = ModelTarget.from_values(
        "deepseek-v4-flash",
        {"gateway": "title", "thinking": "disabled", "max_tokens": 64},
        role="title",
        config=config,
    )
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)
    monkeypatch.setattr(task_title, "settings", lambda: config)
    monkeypatch.setattr(task_title.ModelTarget, "for_role", lambda role: target)
    monkeypatch.setattr(task_title, "load_prompt", lambda _: "title instructions")
    monkeypatch.setattr(
        task_title, "get_agent_usage_limits", lambda: UsageLimits(request_limit=None)
    )
    monkeypatch.setattr(
        model_factory,
        "get_client",
        lambda key, factory: client,
    )

    try:
        assert await task_title.generate_task_title("写一份今日工作日报") == "写日报"
    finally:
        await client.aclose()

    assert requests
    payload = requests[0]
    assert not payload.get("tools")
    assert payload.get("thinking") == {"type": "disabled"}
    assert payload.get("reasoning_effort") != "max"
