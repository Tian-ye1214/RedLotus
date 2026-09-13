"""Auxiliary checks of configuration ownership and the actual child runtime."""

import json
import threading

import httpx

from redlotus.agent_core.memory_service import MemoryService
from redlotus.ModelGateway.model_factory import ModelTarget
from redlotus.runtime.context import WorkspaceContext
from redlotus.runtime.lifecycle import AgentRegistry
from redlotus.runtime.subagents import SubagentFactory
from redlotus.tools.memory.perception import MemoryPerception


async def test_perception_uses_worker_target_in_owned_child_thread(
    tmp_path, monkeypatch
):
    from redlotus.config.app_config import settings

    baseline = dict(settings())
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                **baseline,
                "models": {
                    "worker": {"name": "deepseek-v4-flash", "max_tokens": 393216},
                    "compressor": {"name": "compression-only", "max_tokens": 16384},
                },
                "context": {"worker": {"default_context_tokens": 1000000}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(config))
    monkeypatch.setenv("BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("API_KEY", "auxiliary-test-only")
    monkeypatch.setattr("pydantic_ai.models.ALLOW_MODEL_REQUESTS", True)
    parent = threading.get_ident()
    requests = []

    async def context(*args, **kwargs):
        return 1000000

    def respond(request):
        payload = json.loads(request.content)
        requests.append((threading.get_ident(), payload))
        assert payload.get("response_format") == {"type": "json_object"}
        assert not payload.get("tools")
        return httpx.Response(
            200,
            json={
                "id": "auxiliary",
                "object": "chat.completion",
                "created": 0,
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"records":[],"reason":"No new event"}',
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 10,
                    "total_tokens": 20,
                },
            },
        )

    monkeypatch.setattr(
        "redlotus.tools.memory.perception.get_effective_max_context_async", context
    )
    monkeypatch.setattr(
        "redlotus.ModelGateway.model_factory.create_async_http_client",
        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    factory = SubagentFactory(1)
    workspace = WorkspaceContext.from_path(tmp_path)
    perception = MemoryPerception(workspace, factory, AgentRegistry())
    try:
        result = await perception.produce(
            "job",
            dict(
                mode="perception",
                new_turn_ids=["turn"],
                events=[dict(id="turn", user_inputs=["hello"], operations=[])],
            ),
            [],
        )
        assert result.records == []
        assert requests[0][0] != parent
        assert requests[0][1]["model"] == "deepseek-v4-flash"
        assert requests[0][1]["max_tokens"] == 393216
        assert not factory.handles
        assert ModelTarget.for_role("compressor").settings["max_tokens"] == 16384

        memory = MemoryService(workspace=workspace)
        initial = memory._route()
        changed = json.loads(config.read_text(encoding="utf-8"))
        changed["models"]["compressor"]["max_tokens"] = 8192
        config.write_text(json.dumps(changed), encoding="utf-8")
        assert memory._route() == initial, (
            "Compression configuration changed perception identity"
        )
        changed["models"]["worker"]["max_tokens"] = 131072
        config.write_text(json.dumps(changed), encoding="utf-8")
        assert memory._route() != initial
        await memory.close()
    finally:
        await factory.close()
