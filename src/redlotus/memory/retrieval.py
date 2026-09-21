"""RAG embedding, native LanceDB and project-scoped recall."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from redlotus.runtime import logging as logger, config as app_config
from redlotus.runtime.resources import user_data_dir
from redlotus.runtime.config import get_env, settings
from redlotus.runtime.network import get_client, openai_base_url
from typing import Any
import httpx
from datetime import timedelta
from copy import deepcopy
import lancedb
import pyarrow as pa
from filelock import AsyncFileLock
from lancedb.index import IvfPq
import json


def resolve_lancedb_dir(configured_path: str, *, table_name: str = "") -> str:
    """Use the configured database path, with an explicit process override for tests."""
    p = Path(os.environ.get("RAG_DB_PATH") or configured_path).expanduser()
    if not p.is_absolute():
        p = (user_data_dir() / p).resolve()
    else:
        p = p.resolve()

    return str(p)


def missing_rag_settings(
    *,
    use_rerank: bool = False,
    configuration: dict | None = None,
    api_missing: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Return incomplete optional-RAG fields without exposing their values."""
    configuration = settings() if configuration is None else configuration
    models = configuration.get("RAG_models", {})
    models = models if isinstance(models, dict) else {}
    missing = list(
        api_missing
        if api_missing is not None
        else (
            name
            for name in ("SILICONFLOW_BASE", "SILICONFLOW_KEY")
            if not str(configuration.get(name, "") or "").strip()
        )
    )
    required = [("RAG_models.embedding", models.get("embedding", ""))]
    if use_rerank:
        required.append(("RAG_models.reranker", models.get("reranker", "")))
    missing.extend(name for name, value in required if not str(value or "").strip())
    return tuple(dict.fromkeys(missing))


def _require_rag_model(role: str) -> str:
    name = settings()["RAG_models"][role].strip()
    if not name:
        raise app_config.ConfigError(f"缺少配置 RAG_models.{role}；检查来源: {app_config.config_source_summary()}")
    return name


def _get_shared_client() -> httpx.AsyncClient:
    """当前事件循环的 embedding/rerank 连接池；配置地址变化时使用新池。"""
    config = settings()["rag_service"]
    kwargs = dict(
        base_url=openai_base_url(get_env("SILICONFLOW_BASE", warn=False)),
        http2=config["http2"],
        timeout=config["timeout"],
    )
    return get_client(f"{httpx.AsyncClient.__name__}:{kwargs}", lambda: httpx.AsyncClient(**kwargs))


def _require_rag_api() -> None:
    missing = app_config.missing_rag_api_keys()
    if missing:
        raise app_config.ConfigError("缺少配置 " + ", ".join(missing) + "；检查来源: " + app_config.config_source_summary())


async def _rag_api_post(endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
    """RAG 接口统一 POST：构造鉴权头、校验状态、解析 JSON。"""
    response = await _get_shared_client().post(
        endpoint,
        headers={
            "Authorization": f"Bearer {get_env('SILICONFLOW_KEY', warn=False).strip()}",
            "Content-Type": "application/json",
        },
        json=body,
    )
    response.raise_for_status()
    return response.json()


async def embed_texts(
    texts: str | list[str],
    *,
    model: str | None = None,
) -> list[list[float]]:
    """异步获取文本向量；支持单条字符串或多条批量，超过上限自动分批请求。"""
    _require_rag_api()
    if isinstance(texts, str):
        texts = [texts]
    logger.debug("RAG embed: batch_size=%d", len(texts))
    model = model or _require_rag_model("embedding")
    batch_size = int(settings()["rag_service"]["embedding_batch_size"])
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        body = {"model": model, "input": texts[start : start + batch_size]}
        data = await _rag_api_post("/embeddings", body)
        items = data.get("data") or []
        n = len(items)
        if n > 1 and [x.get("index", 0) for x in items] != list(range(n)):
            items = sorted(items, key=lambda x: x.get("index", 0))
        vectors.extend(row["embedding"] for row in items)
    return vectors


async def rerank_documents(
    query: str,
    documents: list[str],
    *,
    top_n: int | None = None,
) -> list[dict[str, Any]]:
    """调用与 OpenAI 兼容的 /v1/rerank，返回按相关度排序的结果（含原始下标与分数）。"""
    _require_rag_api()
    if not documents:
        return []
    logger.debug("RAG rerank: n_docs=%d, top_n=%s", len(documents), top_n)
    model = _require_rag_model("reranker")
    body: dict[str, Any] = {
        "model": model,
        "query": query,
        "documents": documents,
        "return_documents": False,
    }
    if top_n is not None:
        body["top_n"] = top_n

    data = await _rag_api_post("/rerank", body)

    return [
        dict(
            index=int(row["index"]),
            text=documents[int(row["index"])],
            relevance_score=float(row.get("relevance_score", 0)),
        )
        for row in data.get("results", [])
    ]


class EmbedDataBase:
    """Native async LanceDB operations; no worker-thread or DataFrame conversion wrappers."""

    COLUMNS = (
        "id",
        "record_id",
        "project_id",
        "text",
        "source",
        "created_at",
        "agent",
        "session_key",
    )

    def __init__(self, db_path, table_name, vector_dim=None, *, index_config=None):
        self.db_path = resolve_lancedb_dir(db_path, table_name=table_name)
        self.table_name, self.vector_dim = table_name, vector_dim
        self._index_config = deepcopy(index_config or {})
        self._db = None
        self._rows_since_index = 0

    async def _table(self):
        if self._db is None:
            Path(self.db_path).mkdir(parents=True, exist_ok=True)
            self._db = await lancedb.connect_async(
                self.db_path, read_consistency_interval=timedelta(0)
            )
        if self.table_name not in (await self._db.list_tables()).tables:
            return None
        return await self._db.open_table(self.table_name)

    def _write_lock_path(self) -> str:
        """Keep the writer lock beside the actual database it protects."""
        lock_path = Path(self.db_path) / (self.table_name + ".write.lock")
        rendered = str(lock_path)
        if os.name != "nt" or len(rendered) < 260:
            return rendered

        # `filelock` accepts the standard Windows extended-length spelling.  It
        # still names the sidecar in the configured database directory, so two
        # processes that share that database also share this lock.
        resolved = str(lock_path.resolve())
        if resolved.startswith("\\\\"):
            return "\\\\?\\UNC\\" + resolved[2:]
        return "\\\\?\\" + resolved

    async def upsert_vectors(self, rows):
        if not rows:
            return 0
        self.vector_dim = len(rows[0]["vector"])
        schema = pa.schema(
            [
                *(pa.field(key, pa.string()) for key in self.COLUMNS),
                pa.field("vector", pa.list_(pa.float32(), self.vector_dim)),
            ]
        )
        data = pa.Table.from_pylist(
            [
                {
                    **{key: row.get(key, "") or "" for key in self.COLUMNS},
                    "vector": row["vector"],
                }
                for row in rows
            ],
            schema=schema,
        )
        Path(self.db_path).mkdir(parents=True, exist_ok=True)
        lock_path = self._write_lock_path()
        async with AsyncFileLock(
            lock_path,
            run_in_executor=False,
        ):
            table = await self._table()
            if table is None:
                await self._db.create_table(self.table_name, data=data)
            else:
                await (
                    table.merge_insert("id")
                    .when_matched_update_all()
                    .when_not_matched_insert_all()
                    .execute(data)
                )
        self._rows_since_index += len(rows)
        return len(rows)

    async def row_count(self, where=None):
        table = await self._table()
        return await table.count_rows(where) if table else 0

    async def keys(self, column, where):
        table = await self._table()
        if table is None:
            return set()
        data = await table.query().where(where).select([column]).to_arrow()
        return set(data[column].to_pylist())

    async def delete_where(self, where):
        table = await self._table()
        if table is not None:
            await table.delete(where)
            logger.debug(
                "RAG DB: delete_where table=%s where=%s", self.table_name, where
            )

    async def ensure_vector_index(self):
        if not self._index_config:
            return False
        table = await self._table()
        if table is None:
            return False
        count = await table.count_rows()
        if count < int(self._index_config["min_rows"]):
            return False
        if await table.list_indices() and self._rows_since_index < int(
            self._index_config["rebuild_every_n_adds"]
        ):
            return False
        dim = (await table.schema()).field("vector").type.list_size
        await table.create_index(
            "vector",
            replace=True,
            config=IvfPq(
                distance_type=self._index_config["metric"],
                num_partitions=max(
                    1, count // self._index_config["rows_per_partition"]
                ),
                num_sub_vectors=max(
                    1, dim // self._index_config["dimensions_per_sub_vector"]
                ),
            ),
        )
        self._rows_since_index = 0
        return True

    async def vector_search(self, query_embedding, top_k, *, where=None):
        table = await self._table()
        if table is None:
            return []
        query = table.query()
        if where:
            query = query.where(where)
        rows = (
            await query.nearest_to(query_embedding)
            .distance_type(self._index_config["metric"])
            .limit(top_k)
            .to_arrow()
        )
        return rows.to_pylist()

    async def close(self):
        if self._db:
            self._db.close()
        self._db = None


class RAG:
    """Project-scoped chunking, embedding, vector search and optional reranking."""

    def __init__(self, config: dict, *, project_id: str):
        self.config = deepcopy(config)
        self.project_id = project_id
        self.embedding_model = ""
        self._db = None
        models = settings().get("RAG_models", {})
        self._set_embedding_space(
            str(models.get("embedding", "") if isinstance(models, dict) else "").strip()
        )
        self.last_error = ""

    def _set_embedding_space(self, model: str) -> None:
        """Point this index at the table owned by one embedding model name."""
        self.embedding_model = model
        space = hashlib.sha256(model.encode()).hexdigest()[:12]
        table_name = str(self.config["table_name"]) + "_records_v2_" + space
        self._db = EmbedDataBase(
            str(self.config["db_path"]),
            table_name=table_name,
            index_config=self.config["index"],
        )
        self.index_key = json.dumps(
            [
                self._db.db_path,
                table_name,
                self.config["turn_token_limit"],
                self.config["turn_chunk_overlap_tokens"],
            ]
        )

    async def refresh_embedding_space(self) -> str:
        """Bind late-provided configuration before any vector operation writes."""
        model = _require_rag_model("embedding")
        if model == self.embedding_model:
            return model
        previous = self._db
        self._set_embedding_space(model)
        if previous is not None:
            await previous.close()
        return model

    @property
    def where(self) -> str:
        return "project_id = '" + self.project_id.replace("'", "''") + "'"

    def _chunks(self, text: str) -> list[str]:
        # A conservative multilingual budget keeps long imported episodes embeddable.
        limit = int(self.config["turn_token_limit"])
        overlap = int(self.config["turn_chunk_overlap_tokens"])
        if not 0 <= overlap < limit:
            raise ValueError("RAG chunk overlap must be smaller than its chunk budget")
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + limit, len(text))
            if end < len(text):
                boundary = max(
                    text.rfind("\n", start + limit // 2, end),
                    text.rfind("。", start + limit // 2, end),
                )
                if boundary > start:
                    end = boundary + 1
            chunks.append(text[start:end])
            if end == len(text):
                break
            start = max(start + 1, end - overlap)
        return chunks or [""]

    async def upsert_records(self, records: list[dict]) -> int:
        count = await self.write_records(await self.prepare_records(records))
        try:
            await self._db.ensure_vector_index()
        except Exception as exc:
            # Exact vector search remains available without an acceleration index.
            self.last_error = f"Index acceleration unavailable: {exc}"
            logger.warning(self.last_error)
        return count

    async def prepare_records(self, records: list[dict]) -> list[dict]:
        """Embed complete record chunks without holding any database write lock."""
        model = await self.refresh_embedding_space()
        rows = []
        for episode in records:
            if episode["project_id"] != self.project_id:
                raise ValueError("Cannot index an episode from another project")
            for index, chunk in enumerate(self._chunks(episode["text"])):
                rows.append(
                    {
                        **episode,
                        "id": f"{self.project_id}:{episode['record_id']}:{index}",
                        "text": chunk,
                    }
                )
        if not rows:
            return []
        vectors = await embed_texts(
            [row["text"] for row in rows], model=model
        )
        if len(vectors) != len(rows):
            raise ValueError("Embedding count does not match the submitted chunks")
        for row, vector in zip(rows, vectors):
            row["vector"] = vector
            row["_embedding_model"] = model
        return rows

    async def write_records(self, rows: list[dict]) -> int:
        """Commit prepared vectors; callers can validate authoritative versions first."""
        if not rows:
            return 0
        model = await self.refresh_embedding_space()
        if any(row.get("_embedding_model", model) != model for row in rows):
            raise RuntimeError("Embedding model changed before vector write")
        if any(row["project_id"] != self.project_id for row in rows):
            raise ValueError("Cannot index an episode from another project")
        identities = {row["record_id"] for row in rows}
        record_ids = ",".join("'" + identity.replace("'", "''") + "'" for identity in identities)
        previous_ids = await self._db.keys("id", f"{self.where} AND record_id IN ({record_ids})")
        count = await self._db.upsert_vectors(rows)
        if count != len(rows):
            raise RuntimeError("The vector database did not confirm all chunk writes")
        obsolete = previous_ids - {row["id"] for row in rows}
        if obsolete:
            ids = ",".join("'" + value.replace("'", "''") + "'" for value in obsolete)
            await self._db.delete_where(f"{self.where} AND id IN ({ids})")
        return len(identities)

    async def retrieve(self, query: str) -> list[dict]:
        if not query.strip():
            return []
        model = await self.refresh_embedding_space()
        # A later request may refresh self._db while embedding awaits.
        database = self._db
        if not await database.row_count(self.where):
            return []
        vector = (
            await embed_texts(
                query,
                model=model,
            )
        )[0]
        candidates = await database.vector_search(
            vector, int(self.config["vector_search_limit"]), where=self.where
        )
        minimum = float(self.config["min_similarity"])
        metric = self.config["index"]["metric"]
        candidates = [
            row
            for row in candidates
            if row["project_id"] == self.project_id
            and row.get("_distance") is not None
            and (1 / (1 + row["_distance"]) if metric == "l2" else 1 - row["_distance"])
            >= minimum
        ]
        self.last_error = ""
        if candidates and self.config["use_rerank"]:
            try:
                ranked = await rerank_documents(
                    query, [row["text"] for row in candidates], top_n=len(candidates)
                )
                if not ranked:
                    raise ValueError("Reranker returned no candidates")
                ranked_rows = [
                    {
                        **candidates[row["index"]],
                        "relevance_score": row["relevance_score"],
                    }
                    for row in ranked
                ]
                seen = {row["id"] for row in ranked_rows}
                candidates = [
                    *ranked_rows,
                    *(row for row in candidates if row["id"] not in seen),
                ]
            except Exception as exc:
                self.last_error = f"Rerank unavailable; using vector ranking: {exc}"
                logger.warning(self.last_error)
        # Chunk hits reference one complete episode; return it only once.
        unique = {}
        for row in candidates:
            unique.setdefault(row["record_id"], row)
        return list(unique.values())[: int(self.config["final_top_k"])]

    async def row_count(self) -> int:
        await self.refresh_embedding_space()
        return await self._db.row_count(self.where)

    async def indexed_record_ids(self) -> set[str]:
        await self.refresh_embedding_space()
        return await self._db.keys("record_id", self.where)

    async def delete_records(self, record_ids: list[str]) -> None:
        if record_ids:
            await self.refresh_embedding_space()
            ids = ",".join("'" + value.replace("'", "''") + "'" for value in record_ids)
            await self._db.delete_where(f"{self.where} AND record_id IN ({ids})")

    async def clear_project(self) -> None:
        await self._db.delete_where(self.where)

    async def close(self) -> None:
        await self._db.close()
