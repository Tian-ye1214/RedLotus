import json

import httpx
import pydantic_ai.models
import pytest
from pydantic_ai import Agent

from redlotus.core.gateway import create_model


def configure_gateway(monkeypatch, respond, base):
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", True)
    monkeypatch.setattr(
        "redlotus.core.gateway.get_client", lambda key, factory: client
    )
    monkeypatch.setattr(
        "redlotus.core.gateway.get_env",
        lambda key, **kw: {"BASE_URL": base, "API_KEY": "test-only"}.get(
            key, kw.get("default", "")
        ),
    )
    return client


def read_a() -> str:
    return "a"


def read_b() -> str:
    return "b"


@pytest.mark.parametrize(
    "base",
    [
        "https://gateway.example",
        "https://gateway.example/v1",
        "https://gateway.example/custom/v1/",
    ],
)
async def test_openai_wire_parallel_and_url(base, monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4o",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 1,
                    "total_tokens": 11,
                },
            },
        )

    client = configure_gateway(monkeypatch, respond, base)

    model = create_model(
        "gpt-4o", {"thinking": "disabled", "max_tokens": 64, "provider": "openai"}
    )
    result = await Agent(model, tools=[read_a, read_b]).run("test")
    assert result.output == "done"
    payload = json.loads(requests[0].content)
    assert payload["parallel_tool_calls"] is True and len(payload["tools"]) == 2
    expected = (
        base.rstrip("/")
        + ("/v1" if base.endswith(".example") else "")
        + "/chat/completions"
    )
    assert str(requests[0].url) == expected
    await client.aclose()


async def test_anthropic_parallel_mapping(monkeypatch):
    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        )

    client = configure_gateway(monkeypatch, respond, "https://native.example")

    model = create_model(
        "claude-sonnet-4-5",
        {"thinking": "disabled", "max_tokens": 64, "provider": "anthropic"},
    )
    result = await Agent(model, tools=[read_a]).run("test")
    assert result.output == "done"
    assert payloads[0]["tool_choice"]["disable_parallel_tool_use"] is False
    await client.aclose()


@pytest.mark.parametrize(
    "base",
    ["https://rag.example", "https://rag.example/v1", "https://rag.example/custom/v1/"],
)
async def test_embedding_rerank_wire_and_prefix(base, monkeypatch):
    from redlotus.memory import retrieval as embedding

    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0.0, 1.0]},
                        {"index": 0, "embedding": [1.0, 0.0]},
                    ]
                },
            )
        return httpx.Response(
            200, json={"results": [{"index": 1, "relevance_score": 0.9}]}
        )

    monkeypatch.setattr(
        embedding,
        "get_env",
        lambda key, **kw: {
            "SILICONFLOW_BASE": base,
            "SILICONFLOW_KEY": "test-only",
        }.get(key, ""),
    )
    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        embedding.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(respond), **kwargs
        ),
    )
    monkeypatch.setattr(embedding.app_config, "missing_rag_api_keys", lambda: ())
    config = embedding.settings()
    config["RAG_models"] = {"embedding": "embed-model", "reranker": "rerank-model"}
    config["rag_service"]["timeout"] = 13
    monkeypatch.setattr(embedding, "settings", lambda: config)
    client = embedding._get_shared_client()
    assert client.timeout.read == 13
    assert await embedding.embed_texts(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert (await embedding.rerank_documents("query", ["a", "b"], top_n=1))[0][
        "index"
    ] == 1
    prefix = base.rstrip("/") + ("/v1" if base.endswith(".example") else "")
    assert str(requests[0].url) == prefix + "/embeddings"
    assert str(requests[1].url) == prefix + "/rerank"
    assert json.loads(requests[0].content)["model"] == "embed-model"
    payload = json.loads(requests[1].content)
    assert payload["model"] == "rerank-model" and payload["top_n"] == 1
    await client.aclose()


async def test_google_tools_use_native_protocol(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "done"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 1,
                    "totalTokenCount": 11,
                },
            },
        )

    client = configure_gateway(monkeypatch, respond, "https://native.example")

    model = create_model(
        "gemini-2.5-flash",
        {"thinking": "disabled", "max_tokens": 64, "provider": "google"},
    )
    assert (await Agent(model, tools=[read_a, read_b]).run("test")).output == "done"
    payload = json.loads(requests[0].content)
    assert len(payload["tools"][0]["functionDeclarations"]) == 2
    assert (
        "parallel_tool_calls" not in payload
    )  # Gemini accepts multiple calls natively.
    await client.aclose()
