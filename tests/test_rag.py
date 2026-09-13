"""Offline RAG algorithm checks; live acceptance uses the actual configured services."""

import importlib

from redlotus.RAG.DataBase import EmbedDataBase
from redlotus.RAG.RAG import RAG
from redlotus.config.app_config import settings


async def test_lancedb_scoped_upsert_search_count_and_clear(tmp_path):
    index = settings()["short_term_memory"]["index"]
    first = EmbedDataBase(
        str(tmp_path), table_name="test", vector_dim=4, index_config=index
    )
    second = EmbedDataBase(
        str(tmp_path), table_name="test", vector_dim=4, index_config=index
    )
    a = dict(
        id="a:1",
        record_id="a",
        project_id="a",
        text="a original",
        vector=[1.0, 0.0, 0.0, 0.0],
    )
    b = dict(
        id="b:1",
        record_id="b",
        project_id="b",
        text="b private",
        vector=[1.0, 0.0, 0.0, 0.0],
    )
    assert await first.upsert_vectors([a]) == 1
    await second.upsert_vectors([b])
    await first.upsert_vectors([{**a, "text": "a updated"}])
    assert await first.row_count("project_id = 'a'") == 1
    rows = await first.vector_search([1.0, 0.0, 0.0, 0.0], 10, where="project_id = 'a'")
    assert [r["text"] for r in rows] == ["a updated"]
    await first.delete_where("project_id = 'a'")
    assert await second.row_count("project_id = 'b'") == 1
    await first.close()
    await second.close()


async def test_rag_chunking_rerank_dedup_and_fallback(tmp_path, monkeypatch):
    module = importlib.import_module("redlotus.RAG.RAG")
    rerank_calls = []

    async def embed(texts, **kwargs):
        return [
            [1.0, 0.0, 0.0, 0.0] for _ in ([texts] if isinstance(texts, str) else texts)
        ]

    async def rerank(query, texts, **kwargs):
        rerank_calls.append(texts)
        return [
            dict(index=i, relevance_score=float(i))
            for i in range(len(texts) - 1, -1, -1)
        ]

    monkeypatch.setattr(module, "embed_texts", embed)
    monkeypatch.setattr(module, "rerank_documents", rerank)
    config = settings()["short_term_memory"]
    config.update(
        db_path=str(tmp_path),
        use_rerank=True,
        turn_token_limit=256,
        turn_chunk_overlap_tokens=32,
    )
    rag = RAG(config, project_id="a")
    foreign = RAG(config, project_id="b")
    await rag.upsert_records(
        [dict(record_id="one", project_id="a", text="数据库迁移" * 100)]
    )
    await foreign.upsert_records(
        [dict(record_id="one", project_id="b", text="不得召回的秘密")]
    )
    assert await rag.row_count() > 1
    rows = await rag.retrieve("迁移")
    assert rerank_calls and all("不得召回" not in text for text in rerank_calls[0])
    assert len(rows) == 1 and rows[0]["record_id"] == "one"
    await rag.upsert_records(
        [dict(record_id="one", project_id="a", text="简短的新结果")]
    )
    assert (
        await rag.row_count() == 1
    )  # stale chunks are removed after a successful rewrite

    async def unavailable(*args, **kwargs):
        raise RuntimeError("rerank offline")

    monkeypatch.setattr(module, "rerank_documents", unavailable)
    assert len(await rag.retrieve("迁移")) == 1
    assert "vector ranking" in rag.last_error

    async def empty(*args, **kwargs):
        return []

    monkeypatch.setattr(module, "rerank_documents", empty)
    assert len(await rag.retrieve("迁移")) == 1
    assert "no candidates" in rag.last_error
    await rag.clear_project()
    assert await foreign.row_count() == 1
    await rag.close()
    await foreign.close()


async def test_missing_vectors_recovered_despite_old_checkpoint(tmp_path, monkeypatch):
    from redlotus.runtime.context import WorkspaceContext
    from redlotus.tools.memory.store import MemoryStore
    from redlotus.tools.memory.models import MemoryRecord

    module = importlib.import_module("redlotus.RAG.RAG")
    calls = []

    async def embed(texts, **kwargs):
        calls.append(texts)
        return [
            [1.0, 0.0, 0.0, 0.0] for _ in ([texts] if isinstance(texts, str) else texts)
        ]

    monkeypatch.setattr(module, "embed_texts", embed)
    monkeypatch.setattr("redlotus.tools.memory.store.missing_rag_api_keys", lambda: ())
    from redlotus.config.app_config import settings

    config = {
        **settings(),
        "short_term_memory": {
            **settings()["short_term_memory"],
            "db_path": str(tmp_path / "db"),
            "use_rerank": False,
        },
    }
    monkeypatch.setattr("redlotus.tools.memory.store.settings", lambda: config)
    memory = MemoryStore(WorkspaceContext.from_path(tmp_path / "project"))
    rag = memory.indexes["project"]

    memory.save(
        [
            MemoryRecord(
                id="one",
                project_id=memory.workspace.project_id,
                status="success",
                goal="事务迁移",
                last_change_id="one",
            )
        ]
    )
    await memory.reconcile()
    assert await rag.row_count() == 1
    await rag.clear_project()
    assert memory.get("one").goal == "事务迁移"
    await memory.reconcile()
    assert await rag.row_count() == 1 and len(calls) == 2
    await memory.reconcile()
    assert len(calls) == 2
    other = MemoryStore(memory.workspace)
    assert await other.indexes["project"].indexed_record_ids() == {"one"}
    await rag.upsert_records(
        [dict(record_id="two", project_id=memory.workspace.project_id, text="新信息")]
    )
    assert await other.indexes["project"].indexed_record_ids() == {"one", "two"}
    await memory.close()
    await other.close()


async def test_index_settings_build_acceleration_at_threshold(tmp_path):
    db = EmbedDataBase(
        str(tmp_path),
        table_name="accelerated",
        vector_dim=8,
        index_config=settings()["short_term_memory"]["index"],
    )

    def row(i):
        return dict(
            id=str(i),
            record_id=str(i),
            project_id="a",
            text=str(i),
            vector=[1.0, i / 256, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
        )

    await db.upsert_vectors([row(0)])
    assert not await db.ensure_vector_index()
    await db.upsert_vectors([row(i) for i in range(1, 256)])
    assert await db.ensure_vector_index()
    assert not await db.ensure_vector_index()
    assert (await db.vector_search(row(0)["vector"], 3, where="project_id = 'a'"))[0][
        "record_id"
    ] == "0"
    await db.close()


async def test_deep_windows_path_uses_stable_writable_index_directory(tmp_path):
    import sys
    from pathlib import Path
    import pytest

    if sys.platform != "win32":
        pytest.skip("Windows Lance writer path limit")
    configured = tmp_path / ("project_" + "x" * 80) / "db"
    table_name = "conversation_turns_records_v2_caa978c0b354"
    db = EmbedDataBase(str(configured), table_name=table_name)
    again = EmbedDataBase(str(configured), table_name=table_name)
    assert db.db_path == again.db_path and Path(db.db_path) != configured
    await db.upsert_vectors(
        [
            dict(
                id="one",
                record_id="one",
                project_id="a",
                text="路径检查",
                vector=[1.0, 0.0],
            )
        ]
    )
    assert await again.row_count() == 1
    await db.close()
    await again.close()
