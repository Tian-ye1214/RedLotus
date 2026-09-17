"""LanceDB directories follow explicit configuration and runtime overrides."""

from pathlib import Path

from redlotus.core.config import user_data_dir
from redlotus.memory.retrieval import resolve_lancedb_dir


def test_explicit_runtime_database_override_takes_precedence(tmp_path, monkeypatch):
    override = tmp_path / "override"
    configured = tmp_path / "configured-database"
    monkeypatch.setenv("RAG_DB_PATH", str(override))
    assert Path(resolve_lancedb_dir(str(configured))) == override.resolve()


def test_relative_lancedb_directory_resolves_under_user_data_dir(monkeypatch):
    monkeypatch.delenv("RAG_DB_PATH", raising=False)
    configured = Path("data") / "rag_lancedb" / "stm"
    assert Path(resolve_lancedb_dir(str(configured))) == (
        user_data_dir() / configured
    ).resolve()


def test_absolute_lancedb_directory_is_preserved_without_override(tmp_path, monkeypatch):
    monkeypatch.delenv("RAG_DB_PATH", raising=False)
    configured = tmp_path / "selected-database"
    assert Path(resolve_lancedb_dir(str(configured))) == configured.resolve()
