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
    preference = dict(
        scope="global",
        kind="requested",
        goal="Code style preference",
        source_turn_ids=["turn"],
    )
    unrelated = dict(
        scope="project",
        kind="requested",
        goal="Project-specific rule",
        source_turn_ids=["turn"],
    )
    outputs = iter(
        [
            dict(records=[], reason="No new event"),
            dict(
                records=[preference, unrelated],
                reason="Mixed user requirements",
                request_authorized=True,
            ),
            dict(
                records=[preference],
                reason="Only the requested global preference",
                request_authorized=True,
            ),
            dict(records=[], reason="The preference is already saved", request_authorized=True),
            dict(records=[{**preference, "action": "update", "target_id": "preference"}], reason="Confirm existing memory with this source", request_authorized=True),
        ]
    )

    async def context(*args, **kwargs):
        return 1000000

    def respond(request):
        payload = json.loads(request.content)
        requests.append((threading.get_ident(), payload))
        assert not payload.get("response_format")
        assert {tool["function"]["name"] for tool in payload["tools"]} >= {"search_episodes", "read_episode"}
        needs_search = not any(message.get("tool_calls") for message in payload["messages"])
        scope = "search_memory" if "requested_scope" in json.dumps(payload["messages"]) else "search_episodes"
        message = dict(role="assistant", content=None, tool_calls=[dict(id="search", type="function", function=dict(name=scope, arguments=json.dumps(dict(query="current task"))))]) if needs_search else dict(role="assistant", content=None, tool_calls=[dict(id="result", type="function", function=dict(name="final_result", arguments=json.dumps(next(outputs))))])
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
                        "finish_reason": "tool_calls",
                        "message": message,
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

        scoped = await perception.produce(
            "scoped-job",
            dict(
                mode="explicit_request",
                explicit_request="Remember the global code preference only",
                requested_scope="global",
                new_turn_ids=["turn"],
                events=[
                    dict(
                        id="turn",
                        user_inputs=[
                            "Remember my code preference and the separate project rule"
                        ],
                        operations=[],
                    )
                ],
            ),
            [],
        )
        assert [row.scope for row in scoped.records] == ["global"]
        assert len(requests) == 5, "Wrong scope must be corrected within the same perception run"
        repeated = await perception.produce(
            "repeat-job",
            dict(mode="explicit_request", explicit_request="Remember the same code preference", requested_scope="global", new_turn_ids=["turn"], existing_records=[{**preference, "id": "preference", "project_id": workspace.project_id, "origin": "explicit"}], events=[dict(id="turn", user_inputs=["Remember my code preference"], operations=[])]),
            [],
        )
        assert repeated.records[0].target_id == "preference"
        assert repeated.records[0].action == "update"
        assert len(requests) == 8

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
