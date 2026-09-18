"""Installation boundaries: persistent state must outlive any checkout or bundle."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from redlotus.core import config as app_config
from redlotus.core import config as paths
from redlotus.core.agents import WorkspaceContext
from redlotus.memory.records import ObservationStore


def test_installed_configuration_uses_explicit_developer_dotenv(tmp_path, monkeypatch):
    global_root = tmp_path / "global"
    global_root.mkdir()
    global_file = global_root / "config.json"
    global_file.write_text('{"API_KEY":"", "BASE_URL":""}', encoding="utf-8")
    (global_root / ".env").write_text("API_KEY=global-test-key\n", encoding="utf-8")
    (tmp_path / ".env").write_text("API_KEY=wrong-project-key\n", encoding="utf-8")
    monkeypatch.delenv("REDLOTUS_CONFIG_FILE", raising=False)
    monkeypatch.delenv("REDLOTUS_CONFIG_DIR", raising=False)
    monkeypatch.delenv("REDLOTUS_DOTENV_FILE", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setattr(paths, "user_config_dir", lambda: global_root)
    monkeypatch.chdir(tmp_path)
    from redlotus.core.session import set_workspace

    set_workspace(tmp_path)
    assert paths.config_file() == global_file
    monkeypatch.setattr(app_config, "_CONFIG", None)
    assert app_config.get_env("API_KEY") == "wrong-project-key"


def test_configuration_source_can_be_selected_after_import(tmp_path, monkeypatch):
    selected = tmp_path / "selected.json"
    selected.write_text('{"example": "selected"}', encoding="utf-8")
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(selected))
    monkeypatch.setattr(app_config, "_CONFIG", None)
    assert app_config.load_config()["example"] == "selected"


def test_new_session_does_not_import_legacy_project_progress(tmp_path):
    from memory_helpers import bound_observations
    legacy = tmp_path / ".redlotus/memory"
    legacy.mkdir(parents=True)
    (legacy / "perception_state.json").write_text('{"consumed":25}')
    store = bound_observations(WorkspaceContext.from_path(tmp_path))
    assert store.cursor() == 0 and store.window() is None
    assert store.session.completed_turns == 0
    assert not (paths.project_data_dir(store.workspace) / "legacy_import.json").exists()


@pytest.mark.parametrize(
    "module",
    [
        "core.session", "core.agents", "core.config", "core.gateway",
        "core.history", "core.system", "core.console", "core.cli_commands",
        "core.presentation", "core.tui", "tools.execution",
        "tools.registry", "tools.references", "tools.interaction", "tools.toolkit",
        "memory.service", "memory.perception", "memory.records", "memory.store",
        "memory.retrieval", "prompts.prompt", "prompts.message_text", "api.base",
    ],
)
def test_import_order_does_not_initialize_user_storage(tmp_path, module):
    env = os.environ | {
        "REDLOTUS_CONFIG_DIR": str(tmp_path / "configuration"),
        "REDLOTUS_DATA_DIR": str(tmp_path / "memory"),
    }
    env.pop("REDLOTUS_CONFIG_FILE", None)
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import sys; from pathlib import Path; "
            "sys.path.insert(0, sys.argv[1]); import redlotus; "
            "redlotus.__path__ = [str(Path(sys.argv[1]) / 'redlotus')]; "
            "__import__('redlotus.' + sys.argv[2])",
            str(Path(__file__).resolve().parents[1] / "src"), module,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "configuration").exists()
    assert not (tmp_path / "memory").exists()


def test_settings_returns_independent_nested_snapshots(tmp_path, monkeypatch):
    import json
    from redlotus.core.config import settings

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"models": {"worker": {"name": "selected", "max_tokens": 12345}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(path))
    first = settings()
    first["models"]["worker"]["max_tokens"] = 1
    assert settings()["models"]["worker"]["max_tokens"] == 12345


def test_loading_existing_configuration_never_migrates_or_rewrites_it(
    tmp_path, monkeypatch
):
    from copy import deepcopy

    original = app_config.settings()
    original["execution"]["inherit_env"] = ["PATH", "SystemRoot"]
    original["execution"]["permissions"]["blocked_code_patterns"] = [r"\.kill\("]
    original["execution"].pop("variables")
    saved = deepcopy(original)
    selected = tmp_path / "old-config.json"
    selected.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(selected))
    original_bytes = selected.read_bytes()
    app_config.load_config()
    assert json.loads(selected.read_text(encoding="utf-8")) == saved
    assert not (tmp_path / "config-backups").exists()
    app_config.load_config()
    assert selected.read_bytes() == original_bytes


def test_partial_configuration_falls_back_to_explicit_global_roles(
    tmp_path, monkeypatch
):
    path = tmp_path / "config.json"
    selected = {"preset": "chosen"}
    context = {
        "default_context_tokens": 98765,
        "auto_compress_ratio": 0.7,
        "head_turns": 1,
        "tail_turns": 1,
    }
    path.write_text(
        json.dumps(
            {
                "models": {"coordinator": selected},
                "model_presets": {"chosen": {"name": "operator-model"}},
                "context": context,
                "RAG_models": {"embedding": "operator-embedding"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(path))
    config = app_config.settings()
    assert config["models"]["coordinator"]["preset"] == selected["preset"]
    assert set(config["models"]) >= {"coordinator", "manager", "worker", "compressor"}
    assert config["RAG_models"]["embedding"] == "operator-embedding"
    assert config["RAG_models"]["reranker"]
    assert config["context"]["default_context_tokens"] == 98765
    assert app_config.get_context_config("worker")["default_context_tokens"] == 98765
    assert set(app_config.get_context_profile_roles()) == set(config["models"])


def test_recovery_reads_latest_completed_status_without_reclassifying_it(tmp_path):
    from memory_helpers import bound_observations
    workspace = WorkspaceContext.from_path(tmp_path)
    store = bound_observations(workspace)
    event = store.begin("session", "turn", "complete a task", [])
    second = bound_observations(workspace)
    event.status = "success"
    store.finish(event)
    assert second.read([event.id])[0].status == "success"
    assert second.session.completed_turns == 1
    assert second.window() is None


def test_config_reader_holds_writer_lock_until_parse_finishes(tmp_path, monkeypatch):
    import threading

    path = tmp_path / "config.json"
    path.write_text(json.dumps(app_config.settings()), encoding="utf-8")
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(path))
    entered, release, written = threading.Event(), threading.Event(), threading.Event()
    original = json.loads
    errors = []

    def read(data, *args, **kwargs):
        if threading.current_thread().name == "config-reader" and isinstance(
            data, bytes
        ):
            entered.set()
            release.wait(5)
        return original(data, *args, **kwargs)

    def write():
        try:
            app_config.update_config(lambda config: config.update(marker="updated"))
        except Exception as exc:
            errors.append(exc)
        finally:
            written.set()

    monkeypatch.setattr(json, "loads", read)
    reader = threading.Thread(target=app_config.load_config, name="config-reader")
    writer = threading.Thread(target=write)
    reader.start()
    try:
        assert entered.wait(3)
        writer.start()
        assert not written.wait(0.05)
    finally:
        release.set()
        reader.join(5)
        if writer.ident is not None:
            writer.join(5)
    assert not errors
    assert app_config.settings()["marker"] == "updated"
