"""Memory ownership and stale indexing regression; no real model calls."""

import asyncio
from pathlib import Path

import pytest

from redlotus.core.agents import WorkspaceContext
from redlotus.core.config import user_data_dir
from redlotus.memory.store import MemoryStore
from redlotus.memory.records import MemoryRecord


def test_all_memory_databases_use_global_configured_root(tmp_path):
    store = MemoryStore(WorkspaceContext.from_path(tmp_path / "project"))
    root = user_data_dir().resolve()
    assert store.path.is_relative_to(root)
    assert all(Path(index._db.db_path).is_relative_to(root) for index in store.indexes.values())
    assert not store.path.suffix == ".sqlite3"


def record(workspace, identity="one", **values):
    return MemoryRecord(id=identity, project_id=workspace.project_id, goal="验证记忆",
                        last_change_id=identity + ":1", **values)


def test_global_database_keeps_project_isolation_and_scope_correction(tmp_path):
    a, b = (WorkspaceContext.from_path(tmp_path / name) for name in ("甲", "乙"))
    first, second = MemoryStore(a), MemoryStore(b)
    original = record(a, scope="global", kind="requested", origin="explicit")
    first.save([original, record(a, "private")])
    assert {row.id for row in second.all()} == {"one"}
    with pytest.raises(KeyError):
        second.get("private")
    revised = original.model_copy(update=dict(scope="project", version=2, last_change_id="one:2"))
    first.save([revised])
    assert second.all() == []
    with pytest.raises(KeyError):
        second.get("one")
    assert first.get("one").scope == "project"


def test_batch_version_conflict_writes_nothing_and_replay_is_idempotent(tmp_path):
    store = MemoryStore(WorkspaceContext.from_path(tmp_path))
    original = record(store.workspace, scope="global")
    store.save([original])
    store.save([original])
    bad = original.model_copy(update=dict(version=9, last_change_id="one:9"))
    with pytest.raises(ValueError, match="changed before commit"):
        store.save([record(store.workspace, "must-not-persist"), bad])
    assert [row.id for row in store.all()] == ["one"]


def test_shared_database_rejects_another_projects_record_id(tmp_path):
    first = MemoryStore(WorkspaceContext.from_path(tmp_path / "a"))
    second = MemoryStore(WorkspaceContext.from_path(tmp_path / "b"))
    first.save([record(first.workspace, content="A 的私有正文")])
    with pytest.raises(ValueError, match="another project"):
        second.save([record(second.workspace, content="B 不得覆盖")])
    assert first.get("one").content == "A 的私有正文"


def test_unchanged_text_keeps_index_stamp_and_deleted_record_stays_deleted(tmp_path):
    store = MemoryStore(WorkspaceContext.from_path(tmp_path))
    original = record(store.workspace)
    store.save([original])
    store._table().update(where="id = 'one'", values={"indexed": "valid-stamp"})
    updated = original.model_copy(update=dict(version=2, last_change_id="one:2", evidence=["source"]))
    store.save([updated])
    assert store._rows()[0]["indexed"] == "valid-stamp"
    store.save([updated.model_copy(update=dict(version=3, last_change_id="one:3", state="deleted"))])
    assert MemoryStore(store.workspace).all() == []
    assert MemoryStore(store.workspace).get("one").state == "deleted"


def paused_embedding(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def embed(texts, **kwargs):
        calls.append(texts)
        if len(calls) == 1:
            started.set()
            await asyncio.wait_for(release.wait(), 10)
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr("redlotus.memory.retrieval.embed_texts", embed)
    monkeypatch.setattr("redlotus.memory.store.missing_rag_api_keys", lambda: ())
    return started, release, calls


@pytest.mark.parametrize("change", ["correct", "forget", "change_scope"])
async def test_old_embedding_cannot_overwrite_later_memory_change(tmp_path, monkeypatch, change):
    first = MemoryStore(WorkspaceContext.from_path(tmp_path))
    second = MemoryStore(first.workspace)
    original = record(first.workspace, content="旧正文")
    first.save([original])
    started, release, _ = paused_embedding(monkeypatch)
    stale = asyncio.create_task(first.reconcile())
    try:
        await asyncio.wait_for(started.wait(), 10)
        if change == "forget":
            await second.clear("project")
        else:
            revised = original.model_copy(update=dict(content="新正文", version=2,
                scope="global" if change == "change_scope" else "project", last_change_id="one:2"))
            second.save([revised])
            await second.reconcile()
    finally:
        release.set()
    await asyncio.wait_for(stale, 10)
    index = first.indexes["project"]
    if change == "correct":
        table = await index._db._table()
        rows = (await table.query().where(index.where).to_arrow()).to_pylist()
        assert [row["text"] for row in rows] == [revised.text()]
    else:
        assert await index.row_count() == 0
    await first.close()
    await second.close()


async def test_index_failure_keeps_body_pending_and_retry_uses_saved_body(tmp_path, monkeypatch):
    store = MemoryStore(WorkspaceContext.from_path(tmp_path))
    store.save([record(store.workspace)])
    _, release, calls = paused_embedding(monkeypatch)
    release.set()
    backend = store.indexes["project"]._db
    write = backend.upsert_vectors

    async def interrupted(rows):
        raise OSError("simulated vector write interruption")

    monkeypatch.setattr(backend, "upsert_vectors", interrupted)
    await store.reconcile()
    assert "simulated vector write interruption" in store.last_error
    assert store.get("one").version == 1
    assert store._rows()[0]["indexed"] == ""
    monkeypatch.setattr(backend, "upsert_vectors", write)
    await store.reconcile()
    assert store.last_error == ""
    assert await store.indexes["project"].indexed_record_ids() == {"one"}
    assert len(calls) == 2
    await store.close()


async def test_acceleration_failure_retries_without_embedding_again(tmp_path, monkeypatch):
    store = MemoryStore(WorkspaceContext.from_path(tmp_path))
    store.save([record(store.workspace)])
    _, release, calls = paused_embedding(monkeypatch)
    release.set()
    attempts = []

    async def build():
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("simulated acceleration failure")
        return True

    monkeypatch.setattr(store.indexes["project"]._db, "ensure_vector_index", build)
    await store.reconcile()
    assert "simulated acceleration failure" in store.last_error
    await store.reconcile()
    assert len(attempts) == 2 and len(calls) == 1
    assert store.last_error == ""
    await store.close()
