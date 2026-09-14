"""Storage paths follow explicit configuration without native volume probing."""

import os
from pathlib import Path

from redlotus.RAG.storage_path import resolve_lancedb_dir


def test_explicit_runtime_database_override_takes_precedence(monkeypatch):
    override = Path(os.environ["RAG_DB_PATH"]).resolve()
    configured = override.parent / "configured-database"
    assert Path(resolve_lancedb_dir(str(configured))) == override


def test_configured_database_path_is_preserved_without_override(tmp_path, monkeypatch):
    monkeypatch.delenv("RAG_DB_PATH", raising=False)
    configured = tmp_path / "selected-database"
    assert Path(resolve_lancedb_dir(str(configured))) == configured.resolve()
