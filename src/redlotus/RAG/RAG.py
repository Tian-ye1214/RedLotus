from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from redlotus.infra import logger
from redlotus.config.app_config import settings
from redlotus.RAG.DataBase import EmbedDataBase
from redlotus.RAG.embedding_function import embed_texts, rerank_documents


class RAG:
    """Project-scoped chunking, embedding, vector search and optional reranking."""

    def __init__(self, config: dict, *, project_id: str):
        self.config = deepcopy(config)
        self.project_id = project_id
        self.embedding_model = settings()["RAG_models"]["embedding"]
        space = hashlib.sha256(self.embedding_model.encode()).hexdigest()[:12]
        table_name = str(config["table_name"]) + "_records_v2_" + space
        self._db = EmbedDataBase(
            str(config["db_path"]),
            table_name=table_name,
            index_config=config["index"],
        )
        self.index_key = json.dumps(
            [
                self._db.db_path,
                table_name,
                config["turn_token_limit"],
                config["turn_chunk_overlap_tokens"],
            ]
        )
        self.last_error = ""

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
            return 0
        record_ids = ",".join(
            "'" + record["record_id"].replace("'", "''") + "'" for record in records
        )
        previous_ids = await self._db.keys(
            "id", f"{self.where} AND record_id IN ({record_ids})"
        )
        vectors = await embed_texts(
            [row["text"] for row in rows], model=self.embedding_model
        )
        if len(vectors) != len(rows):
            raise ValueError("Embedding count does not match the submitted chunks")
        for row, vector in zip(rows, vectors):
            row["vector"] = vector
        count = await self._db.upsert_vectors(rows)
        if count != len(rows):
            raise RuntimeError("The vector database did not confirm all chunk writes")
        obsolete = previous_ids - {row["id"] for row in rows}
        if obsolete:
            ids = ",".join("'" + value.replace("'", "''") + "'" for value in obsolete)
            await self._db.delete_where(f"{self.where} AND id IN ({ids})")
        try:
            await self._db.ensure_vector_index()
        except Exception as exc:
            # Exact vector search remains available without an acceleration index.
            self.last_error = f"Index acceleration unavailable: {exc}"
            logger.warning(self.last_error)
        return len(records)

    async def retrieve(self, query: str) -> list[dict]:
        if not query.strip():
            return []
        vector = (
            await embed_texts(
                settings()["rag_service"]["query_instruction"] + query,
                model=self.embedding_model,
            )
        )[0]
        candidates = await self._db.vector_search(
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
        return await self._db.row_count(self.where)

    async def indexed_record_ids(self) -> set[str]:
        return await self._db.keys("record_id", self.where)

    async def delete_records(self, record_ids: list[str]) -> None:
        if record_ids:
            ids = ",".join("'" + value.replace("'", "''") + "'" for value in record_ids)
            await self._db.delete_where(f"{self.where} AND record_id IN ({ids})")

    async def clear_project(self) -> None:
        await self._db.delete_where(self.where)

    async def close(self) -> None:
        await self._db.close()
