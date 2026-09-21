"""Version/publication fault checks; live memory acceptance is separate."""

import pytest
from types import SimpleNamespace


@pytest.mark.parametrize("state", ["active", "deleted"])
def test_stale_candidate_cannot_replace_a_newer_record(tmp_path, monkeypatch, state):
    from redlotus.runtime.resources import WorkspaceContext
    from redlotus.memory.perception import MemoryJob
    from redlotus.memory.records import MemoryDraft, MemoryRecord, ObservedTurn
    from redlotus.memory.store import MemoryStore

    (tmp_path / "config.json").write_text(
        '{"storage":{"references_dir":"WorkDatabase/references"}}', encoding="utf-8",
    )
    workspace = WorkspaceContext.from_path(tmp_path)
    base = MemoryRecord(id="A", project_id=workspace.project_id, goal="Original", version=1, last_change_id="initial")
    current = base.model_copy(update={"version": 2, "goal": "Newer", "state": state, "last_change_id": "newer"})
    event = ObservedTurn(id="event", project_id=workspace.project_id, session_id="session", turn_id="turn", status="success")
    job = MemoryJob(id="stale", events=[event], bases={"A": base})
    draft = MemoryDraft(action="update", target_id="A", goal="Old candidate", source_turn_ids=[event.id])
    store = object.__new__(MemoryStore)
    store.workspace = workspace
    monkeypatch.setattr(store, "get", lambda identity: current)
    with pytest.raises(ValueError, match="changed"):
        store.materialize(job, draft, 0, lambda scope: "")
    assert current.version == 2 and current.state == state
    # A retry of the exact committed change is idempotent, even with its old base.
    current.last_change_id = "stale:0"
    assert store.materialize(job, draft, 0, lambda scope: "") is current


@pytest.mark.parametrize("indexed, stamp, expected", [
    (set(), "", "missing"),
    ({"A"}, "old-space", "stale"),
    ({"A"}, "bodynew-space", ""),
])
async def test_empty_vector_result_requires_a_complete_current_index(monkeypatch, indexed, stamp, expected):
    from redlotus.memory.records import MemoryRecord
    from redlotus.memory.store import MemoryStore

    async def retrieve(query):
        return []

    async def keys():
        return indexed

    async def refresh():
        return "model"

    index = SimpleNamespace(
        retrieve=retrieve, indexed_record_ids=keys, refresh_embedding_space=refresh,
        index_key="new-space", last_error="", config={"final_top_k": 5},
    )
    store = object.__new__(MemoryStore)
    store.indexes = {"project": index, "global": index}
    store.retrieval_error = ""
    record = MemoryRecord(id="A", scope="global", project_id="project", goal="Afternoon coffee")
    monkeypatch.setattr(store, "all", lambda scope: [record])
    monkeypatch.setattr(store, "_rows", lambda scope: [{"id": "A", "body_hash": "body", "indexed": stamp}])
    monkeypatch.setattr(store, "rag_unavailable_reason", lambda scope: "")
    assert await store.search("beverage", "global") == []
    assert expected in store.retrieval_error.lower() if expected else not store.retrieval_error


async def test_empty_authoritative_store_proves_absence_without_an_index(monkeypatch):
    from redlotus.memory.store import MemoryStore

    store = object.__new__(MemoryStore)
    monkeypatch.setattr(store, "all", lambda scope: [])
    monkeypatch.setattr(store, "rag_unavailable_reason", lambda scope: "embedding unavailable")
    assert await store.search("beverage", "global") == []
    assert not store.retrieval_error
