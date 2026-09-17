"""Tests must never read or mutate the operator's configuration or memory."""

import os
import sys
import uuid
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# The shared dependency venv is editable-installed from the develop checkout.
# Isolate this worktree's namespace packages so tests cannot import its old modules.
SOURCE = ROOT / "src"
sys.path[:] = [str(SOURCE), *[
    path for path in sys.path
    if Path(path).resolve() != SOURCE
    and not (Path(path).name == "src" and (Path(path) / "redlotus").is_dir())
]]
os.environ["REDLOTUS_DATA_DIR"] = str(ROOT / ".test-runtime" / "data")
os.environ["REDLOTUS_CONFIG_DIR"] = str(ROOT / ".test-runtime" / "config")
os.environ["OPENAI_API_KEY"] = "test-only"

import pydantic_ai.models

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Keep pytest and subprocess temporary files on the project volume by default."""
    base = config.option.basetemp
    if base is None:
        base = ROOT / "WorkDatabase" / "runtime" / f"pytest-{uuid.uuid4().hex}"
        config.option.basetemp = base
    else:
        base = Path(base).resolve()
    base.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = str(base)
    os.environ["TMP"] = str(base)
    tempfile.tempdir = None


@pytest.fixture(autouse=True)
def isolate_background_services(monkeypatch, tmp_path):
    memory_root = os.environ.get("REDLOTUS_TEST_MEMORY_ROOT")
    state = Path(memory_root) / uuid.uuid4().hex if memory_root else tmp_path / "state"
    monkeypatch.setenv("REDLOTUS_DATA_DIR", str(state))
    config = tmp_path.parent / "config_files" / (tmp_path.name + ".json")
    config.parent.mkdir(parents=True, exist_ok=True)
    settings = json.loads((ROOT / "src/redlotus/config.json").read_text(encoding="utf-8"))
    settings.update(API_KEY="test-only", BASE_URL="https://example.test/v1")
    settings["storage"].update(
        state_dir=str(state),
        project_dir=".redlotus",
        sessions_dir=".redlotus/sessions",
        project_logs_dir=".redlotus/logs",
        references_dir="WorkDatabase/references",
        runtime_dir="WorkDatabase/runtime",
    )
    config.write_text(json.dumps(settings), encoding="utf-8")
    global_config = tmp_path.parent / "global_settings" / tmp_path.name / "config.json"
    global_config.parent.mkdir(parents=True)
    global_config.write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setenv("REDLOTUS_CONFIG_DIR", str(global_config.parent))
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(config))
    monkeypatch.setenv("REDLOTUS_DOTENV_FILE", str(tmp_path / ".env"))
    monkeypatch.delenv("RAG_DB_PATH", raising=False)
    from redlotus.core import agents

    monkeypatch.setattr(agents, "_workspace", tmp_path.resolve())
    monkeypatch.setattr(
        "redlotus.core.history._ensure_openrouter_maps", lambda: None
    )
