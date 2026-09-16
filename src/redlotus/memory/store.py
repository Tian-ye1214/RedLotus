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

from redlotus.core import config as logger
from redlotus.memory.retrieval import RAG
from redlotus.core.config import missing_rag_api_keys, settings, file_lock, iso_utc_now


class MemoryStore:
    TABLE = "memory_records_v3"

    def __init__(self, workspace):
        self.workspace = workspace
        config = settings()
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
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
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

    def save(self, records: list[MemoryRecord]):
        if not records:
            return
        with file_lock(self.path / self.TABLE):
            table = self._table()
            old = {row["id"]: row for row in self._rows(active_only=False)}
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

    async def search(self, query, scope=None):
        records = await asyncio.to_thread(self.all, scope)
        by_id = {row.id: row for row in records}
        if not query.strip() or not records:
            return []
        ranked, errors = [], []
        if missing_rag_api_keys():
            errors.append("向量服务未配置，使用项目内文本检索。")
        else:
            for name in [scope] if scope else self.indexes:
                index = self.indexes[name]
                try:
                    ranked.extend(
                        row["record_id"]
                        for row in await index.retrieve(query)
                        if row["record_id"] in by_id
                    )
                    if index.last_error:
                        errors.append(index.last_error)
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
        if missing_rag_api_keys():
            self.last_error = "向量服务未配置；记忆已保存，可使用文本检索。"
            return
        async with self._index_lock:
            try:
                for scope, index in self.indexes.items():
                    rows = await asyncio.to_thread(self._rows, scope)
                    indexed = await index.indexed_record_ids()
                    await index.delete_records(
                        list(indexed - {row["id"] for row in rows})
                    )
                    pending = [
                        row
                        for row in rows
                        if row["indexed"] != row["body_hash"] + index.index_key
                        or row["id"] not in indexed
                    ]
                    batch_size = int(settings()["rag_service"]["index_batch_size"])
                    for start in range(0, len(pending), batch_size):
                        batch = pending[start : start + batch_size]
                        await index.upsert_records(
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
                        for row in batch:
                            await asyncio.to_thread(
                                self._table().update,
                                where=f"id = '{row['id']}' AND body_hash = '{row['body_hash']}'",
                                values={"indexed": row["body_hash"] + index.index_key},
                            )
                self.last_error = ""
            except Exception as exc:
                message = str(exc)
                if message != self.last_error:
                    logger.warning(
                        "记忆索引更新未完成，正文已保存并等待重试：%s", message
                    )
                self.last_error = message

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
        body = draft.model_dump(
            exclude={"action", "target_id", "core_old_text", "evidence_ids"}
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
        for field in ("source_turn_ids", "reference_ids"):
            body[field] = list(dict.fromkeys([*getattr(record, field), *body[field]]))
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

    async def search_memory(self, query: str) -> str:
        """Recall current-project episodes and the owner's global knowledge using RAG."""
        rows = await self.store.search(query) if self.owner_memory_allowed else []
        return json.dumps(
            dict(
                memories=[row.model_dump(mode="json") for row in rows],
                retrieval_error=self.store.retrieval_error,
            ),
            ensure_ascii=False,
        )

    async def read_memory(self, id: str, include_references: bool = False):
        """Read a permitted complete memory; optionally include original referenced media."""
        if not self.owner_memory_allowed:
            return "Error: Personal memory unavailable."
        record = self.store.get(id)
        if record.state != "active":
            return json.dumps(dict(id=record.id, state=record.state))
        if not include_references:
            return record.model_dump_json()
        references = await asyncio.gather(
            *(
                self.references.parse(self.references.load(key))
                for key in record.reference_ids
            )
        )
        return ToolReturn(
            return_value=record.model_dump_json(),
            content=[
                part for reference in references for part in reference.to_prompt()
            ],
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
