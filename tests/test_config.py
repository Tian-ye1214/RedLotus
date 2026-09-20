"""Isolated configuration flow and metadata fallback regressions."""

import asyncio
import json

import pytest

import redlotus.runtime.config as _runtime_config
import redlotus.runtime.files as _runtime_files
import redlotus.runtime.setup as _runtime_setup


def test_empty_configuration_enters_model_dialog_and_cancel_writes_nothing():
    questions = []
    async def ask(question, **kwargs):
        questions.append(question)
        return None
    assert not asyncio.run(_runtime_setup.prepare_startup_configuration(ask=ask, emit=lambda *args: None))
    assert questions and "models.coordinator.name" in questions[0]
    assert not _runtime_files.config_file().exists()


def test_partial_configuration_collects_model_without_policy_prompts():
    path = _runtime_files.config_sources()[0]
    path.write_text(json.dumps({"BASE_URL": "https://api.example.test/v1", "API_KEY": "synthetic-key"}), encoding="utf-8")
    questions = []
    answers = iter(["openai:test-model", "n", "y"])
    async def ask(question, **kwargs):
        questions.append(question)
        return next(answers)
    with pytest.raises(_runtime_config.ConfigError, match="lifecycle"):
        asyncio.run(_runtime_setup.prepare_startup_configuration(ask=ask, emit=lambda *args: None))
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["models"]["coordinator"]["name"] == "openai:test-model"
    assert "max_context_windows" not in str(result)
    assert not any("max_context_windows" in question for question in questions)


def test_startup_reports_all_missing_policies_and_documented_manual_action():
    cfg = {"models": {"coordinator": {"name": "openai:test-model"}}, "API_KEY": "DO-NOT-PRINT", "BASE_URL": "https://example.test/v1"}
    path = _runtime_files.config_sources()[0]
    path.write_text(json.dumps(cfg), encoding="utf-8")
    with pytest.raises(_runtime_config.ConfigError) as caught:
        asyncio.run(_runtime_setup.prepare_startup_configuration())
    message = str(caught.value)
    for field in ("lifecycle.shutdown_grace_seconds", "storage.sessions_dir", "agent_run_policy.max_command_timeout_seconds", "memory_perception.window_turns", "input_limits"):
        assert field in message
    assert "docs/design.md" in message and str(path) in message
    assert "DO-NOT-PRINT" not in message


@pytest.mark.parametrize("field,value", [
    ("lifecycle.shutdown_grace_seconds", "secret-text"),
    ("lifecycle.invocation_history_per_session", True),
    ("agent_run_policy.max_command_timeout_seconds", False),
    ("memory_perception.window_turns", 1.5),
    ("rag_service.http2", "yes"),
    ("short_term_memory.use_rerank", 1),
    ("input_limits.defaults.max_files", True),
])
def test_policy_type_error_is_source_aware_and_redacted(field, value):
    data = {}
    _runtime_setup.ConfigurationSetup.assign(data, field.split("."), value)
    path = _runtime_files.config_sources()[0]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(_runtime_config.ConfigError) as caught:
        _runtime_config.settings()
    assert field in str(caught.value) and str(path) in str(caught.value)
    assert "secret-text" not in str(caught.value)


def test_startup_accepts_explicit_policies_with_gateway_limits_and_optional_features_absent():
    cfg = {
        "models": {"coordinator": {"name": "openai:test", "gateway": "test", "temperature": 0, "max_context_windows": None}},
        "gateways": {"test": {"base_url": "https://example.test/v1", "api_key": "synthetic", "timeout": 3,
                                "input_limits": {"max_files": 0, "max_file_bytes": 1000, "max_request_bytes": None}}},
        "request_limit": None,
        "lifecycle": {"shutdown_grace_seconds": 5, "invocation_history_per_session": 10},
        "agent_run_policy": {"max_concurrent_threads_per_session": 2, "max_command_timeout_seconds": 5},
        "storage": {"state_dir": "", "project_dir": ".redlotus", "sessions_dir": ".redlotus/sessions",
                    "project_logs_dir": ".redlotus/logs", "runtime_dir": "WorkDatabase/runtime", "references_dir": "WorkDatabase/references"},
        "memory_perception": {"model_role": "coordinator", "window_turns": 20, "overlap_turns": 3},
        "short_term_memory": {"db_path": "memory", "table_name": "project", "final_top_k": 4, "use_rerank": False,
                              "turn_token_limit": 100, "turn_chunk_overlap_tokens": 0, "index": {}},
        "long_term_memory": {"table_name": "global"},
    }
    path = _runtime_files.config_sources()[0]
    original = json.dumps(cfg)
    path.write_text(original, encoding="utf-8")
    assert asyncio.run(_runtime_setup.prepare_startup_configuration(emit=lambda *args: None))
    assert path.read_text(encoding="utf-8") == original
    cfg["memory_perception"]["overlap_turns"] = 20
    with pytest.raises(_runtime_config.ConfigError, match="overlap_turns.*window_turns"):
        _runtime_config.validate_runtime_configuration(cfg)


def test_cancelling_at_confirmation_preserves_original_partial_file():
    path = _runtime_files.config_sources()[0]
    original = '{"BASE_URL":"https://example.test/v1","API_KEY":"synthetic"}'
    path.write_text(original, encoding="utf-8")
    answers = iter(["openai:test", "n", "n"])
    async def ask(*args, **kwargs):
        return next(answers)
    assert not asyncio.run(_runtime_setup.prepare_startup_configuration(ask=ask, emit=lambda *args: None))
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("value", [None, 32000])
def test_context_capacity_uses_metadata_only_when_missing_or_null(monkeypatch, value):
    from redlotus.models.context import get_effective_max_context
    cfg = {"models": {"custom-role": {"name": "openai:test-model", "max_context_windows": value}}}
    monkeypatch.setattr(_runtime_config, "settings", lambda: cfg)
    monkeypatch.setattr("redlotus.models.context.get_model_and_params", lambda role: ("openai:test-model", {}))
    monkeypatch.setattr("redlotus.models.context.lookup_model_context", lambda model: 64000)
    assert _runtime_config.get_agent_roles() == ("custom-role",)
    assert get_effective_max_context("openai:test-model", role="custom-role", context=_runtime_config.get_context_config("custom-role")) == (value or 64000)
    del cfg["models"]["custom-role"]["max_context_windows"]
    assert get_effective_max_context("openai:test-model", role="custom-role", context=_runtime_config.get_context_config("custom-role")) == 64000


@pytest.mark.parametrize("value", [0, -1, True, "64000"])
def test_invalid_capacity_names_the_field_without_model_or_secret_values(value):
    with pytest.raises(_runtime_config.ConfigError, match="max_context_windows"):
        _runtime_config.get_context_config("coordinator", cfg={"models": {"coordinator": {"name": "openai:test", "max_context_windows": value}}})


def test_three_layers_and_role_presets_preserve_priority_and_do_not_copy_credentials():
    local, dotenv, global_path = _runtime_files.config_sources()
    global_path.parent.mkdir(parents=True)
    global_path.write_text(json.dumps({"models": {"coordinator": "shared"}, "model_presets": {"shared": {"name": "openai:global-model"}}, "API_KEY": "global-synthetic"}), encoding="utf-8")
    dotenv.write_text("API_KEY=dotenv-synthetic\nBASE_URL=https://api.example.test/v1\n", encoding="utf-8")
    local.write_text(json.dumps({"models": {"coordinator": {"name": "openai:local-model"}}, "API_KEY": "local-synthetic"}), encoding="utf-8")
    cfg = _runtime_config.settings()
    assert _runtime_config.get_model_and_params("coordinator", cfg=cfg)[0] == "openai:local-model"
    assert _runtime_config.get_env("API_KEY") == "local-synthetic"
    _runtime_config.update_config(lambda values: values["models"]["coordinator"].update(name="openai:changed"))
    assert "global-synthetic" not in local.read_text(encoding="utf-8")
    assert "dotenv-synthetic" not in local.read_text(encoding="utf-8")
