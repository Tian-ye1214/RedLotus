import json

import httpx
from pydantic_ai import FunctionToolset

from redlotus.core import config as app_config
from redlotus.core.gateway import create_agent
from redlotus.core.gateway import ModelTarget


async def test_switch_at_tool_boundary_reuses_completed_result(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "models": {
                    "coordinator": {
                        "name": "model-a",
                        "max_tokens": 64,
                        "thinking": "disabled",
                    }
                },
                "context": {
                    "coordinator": {
                        "default_context_tokens": 32000,
                        "auto_compress_ratio": 0.8,
                        "head_turns": 2,
                        "tail_turns": 2,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(path))
    monkeypatch.setenv("BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("API_KEY", "test-only")
    monkeypatch.setattr("pydantic_ai.models.ALLOW_MODEL_REQUESTS", True)
    requests, executed = [], []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        message = {"role": "assistant", "content": "done"}
        if len(requests) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "step-one",
                        "type": "function",
                        "function": {"name": "calculate", "arguments": "{}"},
                    }
                ],
            }
        return httpx.Response(
            200,
            json={
                "id": "reply",
                "object": "chat.completion",
                "created": 1,
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if len(requests) == 1 else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
            },
        )

    def calculate() -> int:
        executed.append("calculated")
        app_config.set_model_name("coordinator", "model-b")
        return 17

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            "redlotus.core.gateway.get_client",
            lambda key, factory: client,
        )
        agent = create_agent(
            ModelTarget.for_role("coordinator"),
            role="coordinator",
            follow_config=True,
            toolsets=[FunctionToolset([calculate])],
        )
        result = await agent.run("Calculate, then explain")
    assert result.output == "done"
    assert [r["model"] for r in requests] == ["model-a", "model-b"]
    assert executed == ["calculated"]
    returned = [m for m in requests[1]["messages"] if m["role"] == "tool"]
    assert len(returned) == 1
    assert returned[0]["tool_call_id"] == "step-one"
    assert returned[0]["content"] == "17"
