import json

import httpx
import pytest
from pydantic_ai import Agent

from redlotus.core.gateway import create_model


@pytest.mark.parametrize(
    "model_name,effort,expected",
    [
        ("deepseek-v4-flash", "max", "max"),
        ("deepseek-v4-flash", "xhigh", "high"),
        ("deepseek-v4-flash", "ultra", "max"),
        ("deepseek-v4-flash", " HIGH ", "high"),
        ("gpt-5", "max", "xhigh"),
    ],
)
async def test_configured_effort_reaches_the_provider(
    model_name, effort, expected, monkeypatch
):
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "effort-test",
                "object": "chat.completion",
                "created": 1,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "total_tokens": 4,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr("pydantic_ai.models.ALLOW_MODEL_REQUESTS", True)
        monkeypatch.setattr(
            "redlotus.core.gateway.get_client",
            lambda key, factory: client,
        )
        monkeypatch.setenv("API_KEY", "test-only")
        monkeypatch.setenv("BASE_URL", "https://example.test/v1")
        model = create_model(
            model_name,
            {
                "thinking": "enabled",
                "reasoning_effort": effort,
                "max_tokens": 64,
                "parallel_tool_calls": False,
            },
        )
        assert model.settings["parallel_tool_calls"] is False
        assert (await Agent(model).run("Respond briefly")).output == "done"
    assert sent[0]["reasoning_effort"] == expected
    if model_name.startswith("deepseek"):
        assert sent[0].get("max_tokens") == 64
        assert "max_completion_tokens" not in sent[0]
