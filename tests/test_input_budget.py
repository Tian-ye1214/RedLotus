import json

import httpx
import pytest
from pydantic_ai import BinaryContent

from redlotus.ModelGateway.agent_factory import create_agent
from redlotus.ModelGateway.model_factory import ModelTarget


async def test_encoded_attachment_budget_rejects_before_network(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "models": {"coordinator": {"name": "model-a"}},
                "context": {
                    "coordinator": {
                        "default_context_tokens": 32000,
                        "auto_compress_ratio": 0.8,
                    }
                },
                "input_limits": {
                    "defaults": {"max_file_bytes": 10000, "max_request_bytes": 3500}
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(path))
    monkeypatch.setenv("BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("API_KEY", "test-only")
    monkeypatch.setattr("pydantic_ai.models.ALLOW_MODEL_REQUESTS", True)
    from redlotus.ModelGateway.input_policy import InputLimitError

    def unexpected(request):
        pytest.fail("Oversized encoded content reached the network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        monkeypatch.setattr(
            "redlotus.ModelGateway.model_factory.get_client",
            lambda key, factory: client,
        )
        agent = create_agent(ModelTarget.for_role("coordinator"), role="coordinator")
        with pytest.raises(InputLimitError, match="3500|3,500"):
            await agent.run(
                ["Read this", BinaryContent(data=b"x" * 3000, media_type="image/png")]
            )
