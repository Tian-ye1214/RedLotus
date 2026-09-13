"""Tests must never read or mutate the operator's configuration or memory."""

import os
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["REDLOTUS_DATA_DIR"] = str(ROOT / ".test-runtime" / "data")
os.environ["REDLOTUS_CONFIG_DIR"] = str(ROOT / ".test-runtime" / "config")
os.environ["OPENAI_API_KEY"] = "test-only"

import pydantic_ai.models

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

import pytest


@pytest.fixture(autouse=True)
def isolate_background_services(monkeypatch, tmp_path):
    monkeypatch.setenv("REDLOTUS_DATA_DIR", str(tmp_path / "state"))
    # Keep native LanceDB on the test volume, including Windows exFAT fallback.
    memory_root = os.environ.get("REDLOTUS_TEST_MEMORY_ROOT")
    database = Path(memory_root) / uuid.uuid4().hex if memory_root else tmp_path / "rag"
    monkeypatch.setenv("RAG_DB_PATH", str(database))
    monkeypatch.setattr(
        "redlotus.ModelGateway.ModelChecker._ensure_openrouter_maps", lambda: None
    )
