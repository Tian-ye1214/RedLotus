"""LanceDB owns complete memories; embedding is a recoverable index operation."""

from __future__ import annotations

import json
from pydantic_ai import ToolReturn
from redlotus.memory.records import CREDENTIAL_PATTERN, MemoryRecord

import asyncio
import hashlib
import re
from datetime import timedelta
from pathlib import Path

import lancedb
from copy import deepcopy
from filelock import AsyncFileLock

from redlotus.core import config as logger
from redlotus.memory.retrieval import RAG, missing_rag_settings
from redlotus.core.config import missing_rag_api_keys, settings, file_lock, iso_utc_now
from redlotus.tools.references import ReferenceStore


MEMORY_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")


class MemoryStore:
    TABLE = "memory_records_v3"

    def __init__(self, workspace):
        self.workspace = workspace
        config = deepcopy(settings())
        self.indexes = {
            "project": RAG(
                config["short_term_memory"], project_id=workspace.project_id
            ),
            "global": RAG(
                {
                    **config["short_term_memory"],
                    "table_name": config["long_term_memory"]["table_name"],
                },
                project_id="__global__",
            ),
        }
        self.path = Path(self.indexes["project"]._db.db_path)
        self._db = None
        self._index_lock = asyncio.Lock()
        self.last_error = ""
        self.retrieval_error = ""

    def _table(self):
        if self._db is None:
            self.path.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(
                self.path, read_consistency_interval=timedelta(0)
            )
        if self.TABLE not in self._db.list_tables().tables:
            return None
        return self._db.open_table(self.TABLE)

    def _where(self, scope=None):
        project = f"scope = 'project' AND project_id = '{self.workspace.project_id}'"
        return (
            f"({project})"
            if scope == "project"
            else "scope = 'global'"
            if scope == "global"
            else f"(({project}) OR scope = 'global')"
        )

    def _rows(self, scope=None, *, active_only=True):
        table = self._table()
        if table is None:
            return []
        where = self._where(scope) + (" AND state = 'active'" if active_only else "")
        return table.search().where(where).limit(None).to_arrow().to_pylist()

    def all(self, scope=None, *, active_only=True):
        records = [
            MemoryRecord.model_validate_json(row["payload"])
            for row in self._rows(scope, active_only=active_only)
        ]
        return sorted(records, key=lambda item: (item.created_at, item.id))

    def get(self, identity):
        if not MEMORY_ID_PATTERN.fullmatch(identity):
            raise ValueError("Invalid memory id")
        table = self._table()
        rows = (
            table.search()
            .where(f"{self._where()} AND id = '{identity}'")
            .limit(1)
            .to_arrow()
            .to_pylist()
            if table
            else []
        )
        if not rows:
            raise KeyError(identity)
        return MemoryRecord.model_validate_json(rows[0]["payload"])

    def revision(self, scope):
        """Fingerprint one memory scope for search-to-commit conflict detection."""
        rows = sorted(self._rows(scope, active_only=False), key=lambda row: row["id"])
        records = [(row["id"], row["payload"]) for row in rows]
        return hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()

    def save(self, records: list[MemoryRecord]):
        if not records:
            return
        if any(not MEMORY_ID_PATTERN.fullmatch(record.id) for record in records):
            raise ValueError("Invalid memory id")
        with file_lock(self.path / self.TABLE):
            table = self._table()
            identities = ",".join(f"'{record.id}'" for record in records)
            old = {row["id"]: row for row in table.search().where(
                f"id IN ({identities})").limit(None).to_arrow().to_pylist()} if table else {}
            rows = []
            for record in records:
                if (
                    record.scope == "project"
                    and record.project_id != self.workspace.project_id
                ):
                    raise ValueError("Cannot write another project's memory")
                previous = old.get(record.id)
                if previous:
                    saved = MemoryRecord.model_validate_json(previous["payload"])
                    if saved.scope == "project" and saved.project_id != self.workspace.project_id:
                        raise ValueError("Cannot write another project's memory")
                    if saved.last_change_id == record.last_change_id:
                        continue
                    if record.version != saved.version + 1:
                        raise ValueError(f"Memory {record.id} changed before commit")
                digest = hashlib.sha256(record.text().encode()).hexdigest()
                rows.append(
                    dict(
                        id=record.id,
                        scope=record.scope,
                        project_id=record.project_id,
                        state=record.state,
                        body_hash=digest,
                        payload=record.model_dump_json(),
                        indexed=previous["indexed"]
                        if previous and previous["body_hash"] == digest
                        else "",
                    )
                )
            if rows:
                if table is None:
                    self._db.create_table(self.TABLE, data=rows)
                else:
                    table.merge_insert(
                        "id"
                    ).when_matched_update_all().when_not_matched_insert_all().execute(
                        rows
                    )

    @staticmethod
    def tokens(text):
        words = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", text.lower())
        return {
            token
            for word in words
            for token in (
                [word]
                if word.isascii() or len(word) == 1
                else [word[i : i + 2] for i in range(len(word) - 1)]
            )
        }

    def rag_unavailable_reason(self, scope=None):
        """Describe incomplete optional-RAG setup without attempting a request."""
        names = [scope] if scope else self.indexes
        use_rerank = any(self.indexes[name].config["use_rerank"] for name in names)
        missing = missing_rag_settings(
            use_rerank=use_rerank,
            configuration=settings(),
            api_missing=missing_rag_api_keys(),
        )
        if not missing:
            return ""
        return (
            "RAG 配置不完整：缺少 "
            + "、".join(missing)
            + "；记忆已保存，可使用文本检索。补齐后执行 /STM retry 继续索引。"
        )

    async def search(self, query, scope=None):
        records = await asyncio.to_thread(self.all, scope)
        by_id = {row.id: row for row in records}
        self.retrieval_error = ""
        if not records:
            return []
        if not query.strip():
            self.retrieval_error = "An empty query cannot establish memory absence."
            return []
        reason = self.rag_unavailable_reason(scope)
        ranked, errors = [], []
        if reason:
            errors.append(reason)
        else:
            for name in [scope] if scope else self.indexes:
                index = self.indexes[name]
                try:
                    await index.refresh_embedding_space()
                    space = index.index_key
                    rows = await asyncio.to_thread(self._rows, name)
                    indexed = await index.indexed_record_ids()
                    if any(row["id"] not in indexed for row in rows):
                        errors.append(f"{name}: memory index missing; recall is incomplete.")
                    if any(row["indexed"] != row["body_hash"] + space for row in rows):
                        errors.append(f"{name}: memory index stale; recall is incomplete.")
                    ranked.extend(
                        row["record_id"]
                        for row in await index.retrieve(query)
                        if row["record_id"] in by_id
                    )
                    if index.last_error:
                        errors.append(index.last_error)
                    if index.index_key != space:
                        errors.append(f"{name}: embedding model changed during recall.")
                except Exception as exc:
                    errors.append(str(exc))
                    if str(exc) not in self.retrieval_error:
                        logger.warning("记忆向量召回不可用，保留文本检索：%s", exc)
        self.retrieval_error = "; ".join(errors)
        tokens = self.tokens(query)
        matches = sorted(
            records, key=lambda row: len(tokens & self.tokens(row.text())), reverse=True
        )
        ranked.extend(row.id for row in matches if tokens & self.tokens(row.text()))
        limit = int(self.indexes["project"].config["final_top_k"])
        return [by_id[key] for key in dict.fromkeys(ranked)][:limit]

    async def reconcile(self):
        if reason := self.rag_unavailable_reason():
            self.last_error = reason
            return
        async with self._index_lock:
            try:
                for scope, index in self.indexes.items():
                    rows, indexed = await self._index_snapshot(scope)
                    pending = [
                        row
                        for row in rows
                        if row["indexed"] != row["body_hash"] + index.index_key
                        or row["id"] not in indexed
                    ]
                    batch_size = int(settings()["rag_service"]["index_batch_size"])
                    for start in range(0, len(pending), batch_size):
                        batch = pending[start : start + batch_size]
                        vectors = await index.prepare_records(
                            [
                                dict(
                                    record_id=row["id"],
                                    project_id=index.project_id,
                                    text=MemoryRecord.model_validate_json(
                                        row["payload"]
                                    ).text(),
                                    source=f"lancedb:{self.TABLE}/{row['id']}",
                                )
                                for row in batch
                            ]
                        )
                        await self._commit_index(scope, batch, vectors)
                    # Retry acceleration independently of already persisted embeddings.
                    await index._db.ensure_vector_index()
                self.last_error = ""
            except Exception as exc:
                message = str(exc)
                if message != self.last_error:
                    logger.warning(
                        "记忆索引更新未完成，正文已保存并等待重试：%s", message
                    )
                self.last_error = message

    def _write_lock(self):
        """Share the authoritative writer lock with synchronous save operations."""
        self.path.mkdir(parents=True, exist_ok=True)
        return AsyncFileLock(self.path / (self.TABLE + ".lock"), run_in_executor=False)

    async def _index_snapshot(self, scope):
        """Remove only currently inactive vectors while holding the record lock."""
        index = self.indexes[scope]
        async with self._write_lock():
            rows = await asyncio.to_thread(self._rows, scope)
            indexed = await index.indexed_record_ids()
            obsolete = indexed - {row["id"] for row in rows}
            await index.delete_records(list(obsolete))
            return rows, indexed - obsolete

    async def _commit_index(self, scope, batch, vectors):
        """Reject stale embeddings before writing vectors or marking bodies indexed."""
        index = self.indexes[scope]
        async with self._write_lock():
            current = {row["id"]: row["body_hash"] for row in await asyncio.to_thread(self._rows, scope)}
            valid = {row["id"]: row["body_hash"] for row in batch
                     if current.get(row["id"]) == row["body_hash"]}
            await index.write_records([row for row in vectors if row["record_id"] in valid])
            for identity, digest in valid.items():
                await asyncio.to_thread(self._table().update,
                    where=f"id = '{identity}' AND body_hash = '{digest}' AND state = 'active'",
                    values={"indexed": digest + index.index_key})

    async def clear(self, scope):
        now = iso_utc_now()
        records = await asyncio.to_thread(self.all, scope)
        await asyncio.to_thread(
            self.save,
            [
                row.model_copy(
                    update=dict(
                        state="deleted",
                        version=row.version + 1,
                        last_change_id="clear:" + now,
                        updated_at=now,
                    )
                )
                for row in records
            ],
        )
        await self.reconcile()

    async def snapshot(self, scope):
        index = self.indexes[scope]
        return dict(
            row_count=len(await asyncio.to_thread(self.all, scope)),
            db_path=self.path,
            table_name=self.TABLE,
            vector_table=index._db.table_name,
            index_error=self.last_error,
            retrieval_error=self.retrieval_error,
        )

    async def close(self):
        await asyncio.gather(*(index.close() for index in self.indexes.values()))
        self._db = None

    def materialize(self, job, draft, index, cleared_at):
        explicit = job.request is not None
        if explicit:
            draft = draft.model_copy(update={"kind": "requested"})
        events = {event.id: event for event in job.events}
        new_ids = set(job.window.new_turn_ids if job.window else events)
        draft = draft.validated_sources(
            events, new_ids, job.reference_ids, job.bases.get(draft.target_id)
        )
        draft.validate_behavior(job.sources, job.bases.get(draft.target_id), related=job.bases.values())
        if any(
            events[key].created_at <= cleared_at(draft.scope)
            for key in draft.source_turn_ids
        ):
            return None
        if draft.action != "delete" and CREDENTIAL_PATTERN.search(
            draft.subject + "\n" + draft.text()
        ):
            raise ValueError("Credentials cannot enter memory")
        if (draft.kind == "episode" and draft.scope != "project") or (
            draft.projection != "none" and draft.scope != "global"
        ):
            raise ValueError("Invalid memory scope")
        draft.validated_scope(job.scope)
        identity = (
            draft.target_id
            or hashlib.sha256(f"{job.id}:{index}".encode()).hexdigest()[:32]
        )
        try:
            previous = self.get(identity)
        except KeyError:
            previous = None
        if previous and previous.last_change_id == f"{job.id}:{index}":
            return previous
        base = job.bases.get(identity)
        if (previous.version if previous else 0) != (base.version if base else 0):
            raise ValueError(f"Memory {identity} changed since candidate generation")
        if (
            previous
            and not explicit
            and (previous.scope != draft.scope or previous.origin == "explicit")
        ):
            return None
        source_time = max(events[key].created_at for key in draft.source_turn_ids)
        if (
            previous
            and previous.origin == "explicit"
            and (
                previous.source_updated_at or previous.updated_at,
                previous.request_created_at,
            )
            > (source_time, job.created_at)
        ):
            return None
        if (
            draft.action != "create"
            and previous is None
            and not (explicit and draft.action == "delete" and draft.core_old_text)
        ):
            raise ValueError("Memory update requires an existing target")
        verified = any(
            job.sources.get(key, {}).get("verified")
            and job.sources[key].get("kind") == "tool-return"
            and events[job.sources[key]["event_id"]].status == "success"
            for key in draft.evidence_ids
        )
        if not explicit and draft.scope == "global":
            if (
                all(events[key].requested_record_ids for key in draft.source_turn_ids)
                and not verified
            ):
                return None
            if draft.projection == "experience" and (
                draft.status != "success" or not verified
            ):
                return None
            if not previous and any(
                row.state != "active" and row.subject and row.subject == draft.subject
                for row in job.bases.values()
            ):
                return None
        source_memories = []
        if not explicit and draft.scope == "global" and draft.action != "delete":
            if not draft.behavior_evidence_ids:
                raise ValueError("L2 promotion requires independent user evidence from L1.")
            event_ids = {key.rsplit(":u", 1)[0] for key in draft.behavior_evidence_ids}
            source_memories = [
                row.id for row in job.bases.values()
                if row.scope == "project" and row.state == "active"
                and event_ids & set(row.source_turn_ids)
            ]
            for position, candidate in enumerate(job.result.records):
                if (candidate.scope == "project" and candidate.action != "delete"
                    and event_ids & set(candidate.source_turn_ids)):
                    source_memories.append(candidate.target_id or hashlib.sha256(f"{job.id}:{position}".encode()).hexdigest()[:32])
            if not source_memories:
                raise ValueError("L2 promotion requires a related L1 record.")
        body = draft.model_dump(
            exclude={"action", "target_id", "core_old_text", "evidence_ids", "search_id", "promotion_basis"}
        )
        outcomes = {events[key].status for key in draft.source_turn_ids}
        if draft.kind == "episode":
            body["status"] = (
                next(iter(outcomes))
                if outcomes in ({"failed"}, {"cancelled"})
                else "unverified"
                if outcomes & {"running", "unverified"}
                else draft.status
            )
        record = (
            previous.model_copy(deep=True)
            if previous
            else MemoryRecord(
                id=identity,
                project_id=self.workspace.project_id,
                goal=draft.goal,
                created_at=min(events[key].created_at for key in draft.source_turn_ids),
            )
        )
        for field in ("source_turn_ids", "reference_ids", "behavior_evidence_ids"):
            body[field] = list(dict.fromkeys([*getattr(record, field), *body[field]]))
        body["source_memory_ids"] = list(dict.fromkeys([*record.source_memory_ids, *source_memories]))
        reference_root = ReferenceStore(self.workspace).root
        body["reference_sources"] = {
            **record.reference_sources,
            **{key: str(reference_root / "manifests" / f"{key}.json")
               for key in draft.reference_ids if key in job.reference_ids},
        }
        body["evidence"] = list(
            dict.fromkeys(
                [
                    *record.evidence,
                    *(
                        path
                        for key in draft.source_turn_ids
                        for path in events[key].evidence_paths
                    ),
                ]
            )
        )
        body.update(
            project_id=self.workspace.project_id
            if draft.scope == "project"
            else record.project_id,
            kind="requested" if explicit else draft.kind,
            origin="explicit" if explicit else "automatic",
            state="deleted" if draft.action == "delete" else "active",
            version=previous.version + 1 if previous else 1,
            updated_at=iso_utc_now(),
            last_change_id=f"{job.id}:{index}",
            source_updated_at=source_time,
            request_created_at=job.created_at,
        )
        return record.model_copy(update=body)


class MemoryReader:
    """Read-only memory tools and owner authorization, shared by CLI and Agents."""

    def __init__(self, store, long_term, references, owner_memory_allowed):
        self.store, self.long_term, self.references = store, long_term, references
        self.owner_memory_allowed = owner_memory_allowed

    async def search_memory(
        self, query: str = "", scope: str | None = None, id: str | None = None,
        include_references: bool = False,
    ):
        """Search permitted memories or retrieve one complete record by ID.

        Project scope contains L1 memories of the current project. Global scope contains
        L2 memories available across the owner's projects. Personal memories are not
        available to unauthenticated channels.

        Args:
            query: The complete semantic search query; unused when id is supplied.
            scope: project for L1, global for L2, or omitted to search both permitted scopes.
            id: A known record ID to retrieve instead of performing a search.
            include_references: Include original referenced content when retrieving by ID.

        Returns:
            Matching records or the requested full record, with retrieval errors reported explicitly."""
        if not self.owner_memory_allowed:
            return "Error: Personal memory unavailable."
        if scope not in (None, "project", "global"):
            return "Error: scope must be project (L1) or global (L2)."
        if id is not None:
            return await self.read_memory(id, include_references=include_references, scope=scope)
        rows = await self.store.search(query, scope)
        return json.dumps(
            dict(
                memories=[row.model_dump(mode="json") for row in rows],
                retrieval_error=self.store.retrieval_error,
            ),
            ensure_ascii=False,
        )

    async def read_memory(self, id: str, include_references: bool = False, *, scope=None):
        """Read a permitted complete memory; optionally include original referenced media."""
        if not self.owner_memory_allowed:
            return "Error: Personal memory unavailable."
        if not MEMORY_ID_PATTERN.fullmatch(id):
            return "Error: Invalid memory id."
        try:
            record = self.store.get(id)
        except KeyError:
            return "Error: Memory not found or unavailable."
        if scope is not None and record.scope != scope:
            return "Error: Memory not found in the selected scope."
        if record.state != "active":
            return json.dumps(dict(id=record.id, state=record.state))
        if not include_references:
            return record.model_dump_json()
        references, errors = [], []
        for key in record.reference_ids:
            try:
                source = record.reference_sources.get(key)
                store = (ReferenceStore(self.references.workspace, root=Path(source).parent.parent)
                         if source else self.references)
                reference = await store.parse(await asyncio.to_thread(store.load, key))
                references.extend(await asyncio.to_thread(reference.to_prompt))
            except (OSError, ValueError) as exc:
                errors.append(dict(id=key, error=str(exc)))
        return ToolReturn(
            return_value=json.dumps({**record.model_dump(mode="json"), "reference_errors": errors}, ensure_ascii=False),
            content=references,
        )

    async def search_episodes(self, query: str) -> str:
        """Search task episodes belonging only to the current project."""
        if not self.owner_memory_allowed:
            return "Error: Personal memory unavailable."
        rows = await self.store.search(query, "project")
        return json.dumps(
            dict(
                project_id=self.store.workspace.project_id,
                retrieval_error=self.store.retrieval_error,
                episodes=[
                    row.model_dump(mode="json") for row in rows if row.kind == "episode"
                ],
            ),
            ensure_ascii=False,
        )

    async def read_episode(self, id: str) -> str:
        """Read one current-project episode with its original evidence sources."""
        if not self.owner_memory_allowed:
            return "Error: Personal memory unavailable."
        if not MEMORY_ID_PATTERN.fullmatch(id):
            return "Error: Invalid episode id."
        try:
            row = self.store.get(id)
        except KeyError:
            return "Error: Episode not found in this project."
        if row.scope != "project" or row.kind != "episode" or row.state != "active":
            return "Error: Episode not found in this project."
        return row.model_dump_json()

    async def long_term_snapshot(self):
        return (
            {
                **await self.long_term.snapshot(),
                "global_records": await self.store.snapshot("global"),
            }
            if self.owner_memory_allowed
            else {}
        )
