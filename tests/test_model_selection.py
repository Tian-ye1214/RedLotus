"""Protocol and request-boundary checks are auxiliary, not real-service acceptance."""

import json

from pydantic_ai.models.openai import OpenAIChatModel

from redlotus.core import config as app_config
from redlotus.core.gateway import create_model


async def test_model_name_does_not_choose_transport_protocol(monkeypatch):
    app_config.set_api(api_key="test-only", base_url="https://chat.example/v1")
    model = create_model("claude-through-a-chat-gateway", {"thinking": "disabled"})
    assert isinstance(model, OpenAIChatModel)
    from redlotus.core.config import close_all_clients

    await close_all_clients()


def test_named_preset_binds_settings_credentials_and_limits(tmp_path, monkeypatch):
    selected = tmp_path / "config.json"
    selected.write_text(
        json.dumps(
            {
                "TEST_NATIVE_KEY": "preset-test-key",
                "gateways": {
                    "native": {
                        "protocol": "openai-responses",
                        "base_url": "https://responses.example/custom/v1",
                        "api_key_env": "TEST_NATIVE_KEY",
                        "input_limits": {"max_file_bytes": 123456},
                    }
                },
                "model_presets": {
                    "fast": {
                        "gateway": "native",
                        "name": "model-a",
                        "settings": {"temperature": 0.4, "max_tokens": 900},
                    }
                },
                "models": {"coordinator": {"preset": "fast"}},
                "context": {"coordinator": {"default_context_tokens": 32000}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(selected))
    monkeypatch.setenv("TEST_NATIVE_KEY", "host-must-not-override-json")
    monkeypatch.setenv("API_KEY", "unrelated-test-key")
    from redlotus.core.gateway import ModelTarget

    target = ModelTarget.for_role("coordinator")
    assert (target.name, target.protocol, target.base_url) == (
        "model-a",
        "openai-responses",
        "https://responses.example/custom/v1",
    )
    assert target.api_key == "preset-test-key"
    assert target.settings == {
        "temperature": 0.4,
        "max_tokens": 900,
        "parallel_tool_calls": True,
    }
    assert target.limits["max_file_bytes"] == 123456
    altered = target.settings
    altered["max_tokens"] = 1
    assert target.settings["max_tokens"] == 900
    assert "preset-test-key" not in repr(target)


def test_nested_role_override_keeps_other_preset_settings(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "models": {
                    "coordinator": {"preset": "fast", "settings": {"max_tokens": 1234}}
                },
                "model_presets": {
                    "fast": {
                        "name": "selected",
                        "settings": {"temperature": 0.3, "max_tokens": 900},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(path))
    assert app_config.get_model_and_params("coordinator") == (
        "selected",
        {"temperature": 0.3, "max_tokens": 1234},
    )


def test_empty_gateway_environment_reference_keeps_explicit_key(tmp_path, monkeypatch):
    from redlotus.core.gateway import ModelTarget

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "models": {"coordinator": {"name": "selected", "gateway": "custom"}},
                "gateways": {
                    "custom": {
                        "protocol": "openai-chat",
                        "base_url": "https://example.test/v1",
                        "api_key": "inline-test-only",
                        "api_key_env": "MISSING_TEST_CREDENTIAL",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(path))
    monkeypatch.delenv("MISSING_TEST_CREDENTIAL", raising=False)
    assert ModelTarget.for_role("coordinator").api_key == "inline-test-only"


def test_selecting_preset_replaces_old_role_settings_without_touching_other_roles(
    tmp_path, monkeypatch
):
    selected = tmp_path / "config.json"
    selected.write_text(
        json.dumps(
            {
                "models": {
                    "coordinator": {"name": "old", "temperature": 1},
                    "worker": {"name": "unchanged"},
                },
                "model_presets": {
                    "fast": {"name": "new", "settings": {"temperature": 0.2}}
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(selected))
    app_config.set_model_name("coordinator", "fast")
    assert app_config.get_model_and_params("coordinator") == (
        "new",
        {"temperature": 0.2},
    )
    assert json.loads(selected.read_text())["models"]["worker"] == {"name": "unchanged"}
