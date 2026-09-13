from __future__ import annotations

from datetime import timedelta
from copy import deepcopy
from pathlib import Path

import lancedb
import pyarrow as pa
from filelock import AsyncFileLock
from lancedb.index import IvfPq

from redlotus.infra import logger
from redlotus.RAG.storage_path import resolve_lancedb_dir


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
        async with AsyncFileLock(
            Path(self.db_path) / (self.table_name + ".write.lock"),
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
