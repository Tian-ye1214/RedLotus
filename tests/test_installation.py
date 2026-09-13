"""Installation boundaries: persistent state must outlive any checkout or bundle."""

import json
import os
import subprocess
import sys

from redlotus.config import app_config
from redlotus.infra import paths
from redlotus.runtime.context import WorkspaceContext
from redlotus.tools.memory.observations import ObservationStore


def test_installed_configuration_ignores_the_working_directory(tmp_path, monkeypatch):
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
    from redlotus.workspace.workspace import set_workspace

    set_workspace(tmp_path)
    assert paths.config_file() == global_file
    monkeypatch.setattr(app_config, "_CONFIG", None)
    assert app_config.get_env("API_KEY") == "global-test-key"


def test_configuration_source_can_be_selected_after_import(tmp_path, monkeypatch):
    selected = tmp_path / "selected.json"
    selected.write_text('{"example": "selected"}', encoding="utf-8")
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(selected))
    monkeypatch.setattr(app_config, "_CONFIG", None)
    assert app_config.load_config()["example"] == "selected"


def test_legacy_project_state_is_copied_once_without_overwriting_new_state(tmp_path):
    project = tmp_path / "project"
    legacy = project / ".redlotus"
    (legacy / "memory").mkdir(parents=True)
    (legacy / "memory" / "perception_state.json").write_text('{"consumed":25}')
    (legacy / "coordinator.jsonl").write_text("original trace\n")
    workspace = WorkspaceContext.from_path(project)
    store = ObservationStore(workspace)
    destination = paths.user_data_dir() / "projects" / workspace.project_id
    assert store.root == destination / "memory"
    assert json.loads(store.cursor_path.read_text()) == {"consumed": 25}
    assert (destination / "coordinator.jsonl").read_text() == "original trace\n"
    store.cursor_path.write_text('{"consumed":45}')
    assert ObservationStore(workspace).cursor() == 45
    assert (
        json.loads((legacy / "memory/perception_state.json").read_text())["consumed"]
        == 25
    )


def test_importing_entrypoint_does_not_initialize_user_storage(tmp_path):
    env = os.environ | {
        "REDLOTUS_CONFIG_DIR": str(tmp_path / "configuration"),
        "REDLOTUS_DATA_DIR": str(tmp_path / "memory"),
    }
    env.pop("REDLOTUS_CONFIG_FILE", None)
    result = subprocess.run(
        [sys.executable, "-c", "import redlotus.agent_core.entrypoint"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "configuration").exists()
    assert not (tmp_path / "memory").exists()


def test_settings_returns_independent_nested_snapshots(tmp_path, monkeypatch):
    import json
    from redlotus.config.app_config import settings

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"models": {"worker": {"name": "selected", "max_tokens": 12345}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(path))
    first = settings()
    first["models"]["worker"]["max_tokens"] = 1
    assert settings()["models"]["worker"]["max_tokens"] == 12345


def test_partial_configuration_adds_roles_without_overwriting_preset_or_shared_context(
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
    assert config["models"]["coordinator"] == selected
    assert set(config["models"]) >= {"coordinator", "manager", "worker", "compressor"}
    assert config["RAG_models"]["embedding"] == "operator-embedding"
    assert config["RAG_models"]["reranker"]
    assert app_config.get_context_config("worker")["default_context_tokens"] == 98765
    assert set(app_config.get_context_profile_roles()) == set(config["models"])


def test_recovery_reloads_the_event_after_acquiring_ownership(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from redlotus.tools.memory import observations

    store = ObservationStore(WorkspaceContext.from_path(tmp_path))
    event = store.begin("session", "turn", "complete a task", [])
    original_lock = observations.FileLock

    @contextmanager
    def finish_before_lock(*args, **kwargs):
        event.status = "success"
        store.finish(event)
        with original_lock(*args, **kwargs):
            yield

    monkeypatch.setattr(observations, "FileLock", finish_before_lock)
    store.recover()
    assert store.read([event.id])[0].status == "success"
    store.close()


def test_config_reader_holds_writer_lock_until_parse_finishes(tmp_path, monkeypatch):
    import threading

    path = tmp_path / "config.json"
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(path))
    app_config.initialize_config()
    entered, release, written = threading.Event(), threading.Event(), threading.Event()
    original = json.load
    errors = []

    def read(stream, *args, **kwargs):
        if threading.current_thread().name == "config-reader":
            entered.set()
            release.wait(5)
        return original(stream, *args, **kwargs)

    def write():
        try:
            app_config.update_config(lambda config: config.update(marker="updated"))
        except Exception as exc:
            errors.append(exc)
        finally:
            written.set()

    monkeypatch.setattr(json, "load", read)
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
        writer.join(5)
    assert not errors
    assert app_config.settings()["marker"] == "updated"
