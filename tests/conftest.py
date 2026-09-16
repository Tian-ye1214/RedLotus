"""Tests must never read or mutate the operator's configuration or memory."""

import os
import sys
import uuid
import json
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


@pytest.fixture(autouse=True)
def isolate_background_services(monkeypatch, tmp_path):
    memory_root = os.environ.get("REDLOTUS_TEST_MEMORY_ROOT")
    state = Path(memory_root) / uuid.uuid4().hex if memory_root else tmp_path / "state"
    monkeypatch.setenv("REDLOTUS_DATA_DIR", str(state))
    config = tmp_path.parent / "config_files" / (tmp_path.name + ".json")
    config.parent.mkdir(parents=True, exist_ok=True)
    settings = json.loads((ROOT / "src/redlotus/core/config.json").read_text(encoding="utf-8"))
    settings["short_term_memory"]["db_path"] = str(state / "rag")
    settings["storage"]["sessions_dir"] = str(tmp_path / "sessions")
    settings["storage"]["references_dir"] = str(tmp_path / "references")
    settings["storage"]["runtime_dir"] = str(tmp_path / "runtime")
    config.write_text(json.dumps(settings), encoding="utf-8")
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(config))
    # Keep native LanceDB on the test volume, including Windows exFAT fallback.
    monkeypatch.setenv("RAG_DB_PATH", str(state / "rag"))
    monkeypatch.setattr(
        "redlotus.core.history._ensure_openrouter_maps", lambda: None
    )
