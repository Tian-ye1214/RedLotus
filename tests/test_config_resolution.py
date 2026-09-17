"""Configuration selection must not depend on hidden templates or host credentials."""

import json
import os
from pathlib import Path

import pytest

from redlotus.core import config
from redlotus.core.gateway import ModelTarget


@pytest.fixture
def layers(tmp_path, monkeypatch):
    baseline = config.settings()
    baseline.update(API_KEY="", BASE_URL="")
    root = tmp_path / "开发 目录"
    local = root / "src/redlotus/config.json"
    local.parent.mkdir(parents=True)
    global_file = tmp_path / "home/.redlotus/config.json"
    global_file.parent.mkdir(parents=True)
    global_file.write_text(json.dumps(baseline), encoding="utf-8")
    dotenv = root / ".env"
    monkeypatch.chdir(root)
    monkeypatch.delenv("REDLOTUS_CONFIG_FILE", raising=False)
    monkeypatch.delenv("REDLOTUS_DOTENV_FILE", raising=False)
    monkeypatch.setenv("REDLOTUS_CONFIG_DIR", str(global_file.parent))
    monkeypatch.setattr(config, "_CONFIG", None)
    return local, dotenv, global_file


def write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_priority_is_per_field_and_ignores_host_environment(layers, monkeypatch):
    local, dotenv, global_file = layers
    global_data = json.loads(global_file.read_text())
    global_data.update(API_KEY="global-key", BASE_URL="https://global.test", marker="global")
    write(global_file, global_data)
    write(local, {"API_KEY": "local-key", "BASE_URL": "  "})
    dotenv.write_text("API_KEY=dotenv-key\nBASE_URL=https://dotenv.test\n", encoding="utf-8")
    monkeypatch.setenv("API_KEY", "wrong-host-key")
    monkeypatch.setenv("BASE_URL", "https://wrong-host.test")
    assert config.get_env("API_KEY") == "local-key"
    assert config.get_env("BASE_URL") == "https://dotenv.test"
    assert config.settings()["marker"] == "global"


def test_nested_dotenv_and_false_values_are_preserved(layers):
    local, dotenv, _ = layers
    write(local, {"request_limit": None, "memory_perception": {"enabled": False}})
    dotenv.write_text(
        "models__worker__max_tokens=12345\n"
        "execution__inherit_env=[\"PATH\",\"HOME\"]\n"
        "request_limit=99\nmemory_perception__enabled=true\n"
        "custom__zero=0\ncustom__empty=[]\ncustom__disabled=false\n",
        encoding="utf-8",
    )
    result = config.settings()
    assert result["models"]["worker"]["max_tokens"] == 12345
    assert result["execution"]["inherit_env"] == ["PATH", "HOME"]
    assert result["request_limit"] is None
    assert result["memory_perception"]["enabled"] is False
    assert result["custom"] == {"zero": 0, "empty": [], "disabled": False}
    result["models"]["worker"]["max_tokens"] = 1
    assert config.settings()["models"]["worker"]["max_tokens"] == 12345


def test_global_dotenv_and_parent_directory_are_not_extra_sources(layers):
    local, _, global_file = layers
    global_file.parent.joinpath(".env").write_text("API_KEY=wrong-global-env\n")
    Path.cwd().parent.joinpath(".env").write_text("API_KEY=wrong-parent-env\n")
    assert config.get_env("API_KEY", warn=False) == ""
    assert config.config_file() == global_file
    assert not local.exists()


@pytest.mark.parametrize("payload", ['{"API_KEY":', '[1,2]', '{"models": []}', '{"API_KEY": [1]}'])
def test_broken_local_file_is_reported_instead_of_falling_back(layers, payload):
    local, _, _ = layers
    local.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match="config.json"):
        config.settings()
    assert local.read_text(encoding="utf-8") == payload


@pytest.mark.parametrize("payload", ["models=scalar\nmodels__worker__name=test\n", "models__worker__max_tokens=[1]\n"])
def test_bad_dotenv_structure_reports_source(layers, payload):
    _, dotenv, _ = layers
    dotenv.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match=r"\.env"):
        config.settings()


def test_dotenv_does_not_expand_host_variables(layers, monkeypatch):
    _, dotenv, _ = layers
    monkeypatch.setenv("SECRET_FROM_HOST", "should-not-be-read")
    dotenv.write_text("custom__literal=${SECRET_FROM_HOST}\n", encoding="utf-8")
    assert config.settings()["custom"]["literal"] == "${SECRET_FROM_HOST}"


def test_missing_required_field_reports_path_and_sources_without_creation(layers):
    local, dotenv, global_file = layers
    global_file.unlink()
    with pytest.raises(ValueError, match="models") as error:
        config.get_model_and_params("coordinator")
    assert str(global_file) in str(error.value)
    assert not any(path.exists() for path in (local, dotenv, global_file))


async def test_missing_model_key_cannot_fall_back_to_sdk_environment(layers, monkeypatch):
    from redlotus.core import gateway

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-use-host-key")
    monkeypatch.setattr(gateway, "create_async_http_client", lambda **kwargs: pytest.fail("client created before validation"))
    with pytest.raises(config.ConfigError, match="API_KEY") as error:
        gateway.create_model("test-model")
    assert str(layers[2]) in str(error.value)


async def test_optional_rag_credentials_are_checked_only_when_used(layers):
    from redlotus.memory import retrieval

    write(layers[0], {"API_KEY": "model-only", "BASE_URL": "https://model.test"})
    assert ModelTarget.for_role("coordinator").api_key == "model-only"
    with pytest.raises(config.ConfigError, match="SILICONFLOW") as error:
        await retrieval.embed_texts("query")
    assert str(layers[2]) in str(error.value)


async def test_missing_default_model_address_is_reported_before_client_creation(layers, monkeypatch):
    from redlotus.core import gateway

    write(layers[0], {"API_KEY": "selected-key"})
    monkeypatch.setattr(gateway, "create_async_http_client", lambda **kwargs: pytest.fail("client created before validation"))
    with pytest.raises(config.ConfigError, match="BASE_URL"):
        gateway.create_model("selected-model")


async def test_named_gateway_missing_address_cannot_use_sdk_environment(layers, monkeypatch):
    from redlotus.core import gateway

    write(layers[0], {"gateways": {"named": {"protocol": "openai-chat", "api_key": "selected-key"}}})
    monkeypatch.setenv("OPENAI_BASE_URL", "https://must-not-use-host.test")
    monkeypatch.setattr(gateway, "create_async_http_client", lambda **kwargs: pytest.fail("client created before validation"))
    with pytest.raises(config.ConfigError, match=r"gateways.named.base_url"):
        gateway.create_model("selected-model", {"gateway": "named"})


@pytest.mark.parametrize("document, field", [
    ({"models": {"worker": []}}, "models.worker"),
    ({"RAG_models": {"embedding": 123}}, "RAG_models.embedding"),
    ({"storage": {"state_dir": []}}, "storage.state_dir"),
    ({"gateways": {"named": {"timeout": "slow"}}}, "gateways.named.timeout"),
])
def test_nested_type_errors_identify_source_and_field(layers, document, field):
    write(layers[0], document)
    with pytest.raises(config.ConfigError) as error:
        config.settings()
    assert str(layers[0]) in str(error.value) and field in str(error.value)


def test_edit_only_persists_changes_in_selected_json(layers):
    local, dotenv, global_file = layers
    write(local, {"custom": "keep-local"})
    dotenv.write_text("API_KEY=must-not-be-written\n", encoding="utf-8")
    original_global = global_file.read_bytes()
    config.set_model_name("worker", "user-selected")
    saved = json.loads(local.read_text(encoding="utf-8"))
    assert saved["models"]["worker"]["name"] == "user-selected"
    assert "API_KEY" not in saved and "RAG_models" not in saved
    assert saved["custom"] == "keep-local"
    assert global_file.read_bytes() == original_global
    assert config.get_env("API_KEY") == "must-not-be-written"


def test_edit_without_local_file_writes_global_only(layers):
    local, dotenv, global_file = layers
    dotenv.write_text("API_KEY=old-key\n", encoding="utf-8")
    config.set_api(api_key="new-explicit-key")
    assert json.loads(global_file.read_text())["API_KEY"] == "new-explicit-key"
    assert not local.exists()
    assert dotenv.read_text() == "API_KEY=old-key\n"
    # A deliberate developer override still has priority over the global edit.
    assert config.get_env("API_KEY") == "old-key"


@pytest.mark.parametrize("source", ["local", "dotenv", "global"])
def test_reload_tracks_each_source_even_with_unchanged_timestamp(layers, source):
    local, dotenv, global_file = layers
    write(local, {"custom": {"local": 1}})
    dotenv.write_text("custom__dotenv=1\n")
    document = json.loads(global_file.read_text())
    document["custom"] = {"global": 1}
    write(global_file, document)
    assert config.settings()["custom"][source] == 1
    selected = {"local": local, "dotenv": dotenv, "global": global_file}[source]
    stamp = selected.stat()
    if source == "dotenv":
        dotenv.write_text("custom__dotenv=2\n")
    else:
        document = json.loads(selected.read_text())
        document["custom"][source] = 2
        write(selected, document)
    os.utime(selected, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert config.settings()["custom"][source] == 2


@pytest.mark.parametrize("high_source", ["inline", "reference"])
def test_named_gateway_credential_obeys_layer_priority(layers, high_source):
    local, dotenv, global_file = layers
    base = json.loads(global_file.read_text())
    base["gateways"] = {"test": {"protocol": "openai-chat", "base_url": "https://test.invalid", "api_key": "global-inline", "api_key_env": "CUSTOM_KEY"}}
    base["models"]["coordinator"] = {"name": "test-model", "gateway": "test"}
    write(global_file, base)
    if high_source == "inline":
        write(local, {"gateways": {"test": {"api_key": "local-inline"}}})
        dotenv.write_text("CUSTOM_KEY=dotenv-reference\n")
        expected = "local-inline"
    else:
        write(local, {"CUSTOM_KEY": "local-reference"})
        dotenv.write_text("gateways__test__api_key=dotenv-inline\n")
        expected = "local-reference"
    assert ModelTarget.for_role("coordinator").api_key == expected


def test_global_directory_never_defaults_to_appdata(tmp_path, monkeypatch):
    monkeypatch.delenv("REDLOTUS_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert config.user_config_dir() == tmp_path / ".redlotus"


def test_frozen_local_config_is_next_to_executable(layers, monkeypatch):
    local, _, _ = layers
    write(local, {"API_KEY": "external-exe-key"})
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "executable", str(Path.cwd() / "RedLotus.exe"))
    monkeypatch.setattr(config.sys, "_MEIPASS", str(Path.cwd() / "wrong-temp"), raising=False)
    monkeypatch.chdir(Path.cwd().parent)
    assert config.config_file() == local
    assert config.get_env("API_KEY") == "external-exe-key"


def test_local_shared_context_overrides_lower_global_role_context(layers):
    local, _, _ = layers
    write(local, {"context": {"default_context_tokens": 98765}})
    assert config.get_context_config("worker")["default_context_tokens"] == 98765
    assert set(config.get_context_profile_roles()) == set(config.get_agent_roles())
