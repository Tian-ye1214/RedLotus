"""One production pipeline for explicit requests, windows and legacy migration."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict

from filelock import AsyncFileLock
from pydantic import BaseModel, Field
from pydantic_ai import ToolReturn
from pydantic_ai.messages import TextContent
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

from redlotus.config.app_config import settings
from redlotus.prompt import load_prompt
from redlotus.ModelGateway.model_factory import ModelTarget
from redlotus.infra import logger
from redlotus.infra.persist_utils import iso_utc_now, save_locked_json, read_locked_json
from redlotus.runtime.context import WorkspaceContext
from redlotus.runtime.subagents import SubagentFactory
from redlotus.references.store import ReferenceStore
from redlotus.tools.memory.evidence import EvidenceReader
from redlotus.tools.memory.ltm import LongTermMemory, CREDENTIAL_PATTERN
from redlotus.tools.memory.models import (
    MemoryRecord,
    ObservedTurn,
    PerceptionResult,
    WindowManifest,
)
from redlotus.tools.memory.observations import ObservationStore
from redlotus.tools.memory.perception import MemoryPerception
from redlotus.tools.memory.store import MemoryStore
from redlotus.workspace.workspace import current_workspace


class MemoryJob(BaseModel):
    id: str
    created_at: str = Field(default_factory=iso_utc_now)
    events: list[ObservedTurn]
    window: WindowManifest | None = None
    request: str | None = None
    scope: str = "auto"
    result: PerceptionResult | None = None
    sources: dict = Field(default_factory=dict)
    reference_ids: list[str] = Field(default_factory=list)
    bases: dict[str, MemoryRecord] = Field(default_factory=dict)
    fragments: list[dict] = Field(default_factory=list)
    usage: list[dict] = Field(default_factory=list)
    records: list[str] = Field(default_factory=list)
    done: bool = False
    error: str = ""
    blocked_recipe: str = ""


class MemoryService:
    def __init__(self, *, workspace=None, owner_memory_allowed=True, factory=None):
        self.workspace = workspace or WorkspaceContext.from_path(current_workspace())
        self.owner_memory_allowed = owner_memory_allowed
        self.long_term = LongTermMemory()
        self.store = MemoryStore(self.workspace)
        self.observations = ObservationStore(self.workspace)
        self.references = ReferenceStore(self.workspace)
        self.evidence = EvidenceReader(self.references)
        self.jobs_dir = self.observations.root / "jobs"
        self.state_path = self.observations.root / "processor.json"
        self._owns_factory = factory is None
        self._perception_factory = factory or SubagentFactory(
            settings()["memory_perception"]["max_concurrent"]
        )
        self.perception = None
        self.current = None
        self._input_source = lambda: self.current.user_inputs if self.current else []
        self._injection_snapshot: str | None = None
        self._context_notices: list = []
        self._processing = asyncio.Lock()
        self._explicit = asyncio.Lock()
        self._recovered = False
        self.last_error = ""

    def bind_runner(self, registry, *, input_source):
        self.perception = MemoryPerception(
            self.workspace, self._perception_factory, registry
        )
        self._input_source = input_source

    @property
    def worker_tools(self):
        return (
            [
                self.search_memory,
                self.read_memory,
                self.remember,
                self.search_episodes,
                self.read_episode,
                self.long_term.list_memory,
            ]
            if self.owner_memory_allowed
            else []
        )

    def injection_for_session(self):
        return self._injection_snapshot or ""

    def reset_injection_snapshot(self, snapshot: str | None = None):
        self._injection_snapshot = snapshot

    def take_context_notices(self):
        notices, self._context_notices = self._context_notices, []
        return notices

    async def begin_turn(self, session_id, turn_id, user_text, *, references=()):
        if self.owner_memory_allowed:
            if self._injection_snapshot is None:
                self._injection_snapshot = await asyncio.to_thread(
                    self.long_term.get_injection
                )
            self.current = self.observations.begin(
                session_id, turn_id, user_text, [ref.id for ref in references]
            )
        return self.current

    async def finish_turn(
        self, event, *, status, user_inputs, evidence_paths, error=""
    ):
        if event:
            event.status, event.user_inputs, event.evidence_paths, event.error = (
                status,
                list(user_inputs),
                list(dict.fromkeys(evidence_paths)),
                error,
            )
            self.observations.finish(event)
            self.current = None

    @staticmethod
    def _read_state(path):
        return read_locked_json(path) if path.exists() else {}

    def _route(self):
        config = settings()["memory_perception"]
        recipe = {
            **asdict(ModelTarget.for_role(config["model_role"])),
            "perception": config,
            "prompt": load_prompt("memory_perception_system.md"),
        }
        return hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()

    def _paused(self):
        state = self._read_state(self.state_path)
        paused = state.get("blocked_route") == self._route()
        if paused:
            self.last_error = state.get(
                "error", "记忆服务已暂停，请在恢复服务后使用 /STM retry。"
            )
        return paused

    def _clear_path(self, scope):
        root = self.long_term.directory if scope == "global" else self.observations.root
        return root / "clear_state.json"

    def _cleared_at(self, scope):
        return self._read_state(self._clear_path(scope)).get("cleared_at", "")

    def _job(self, job):
        path = self._job_path(job)
        if path.exists():
            return MemoryJob.model_validate(read_locked_json(path))
        self._save_job(job)
        return job

    def _save_job(self, job):
        save_locked_json(self._job_path(job), job.model_dump(mode="json"))

    def _job_path(self, job):
        root = (
            self.long_term.directory / "migration_backup/jobs"
            if job.events[0].origin == "migration"
            else self.jobs_dir
        )
        return root / f"{job.id}.json"

    async def _produce(self, job):
        config = settings()["memory_perception"]
        packets, job.sources, references = await self.evidence.collect(job.events)
        job.reference_ids = [ref.id for ref in references]
        query = " ".join(text for event in job.events for text in event.user_inputs)[
            : config["query_max_chars"]
        ]
        candidates = [
            *(await asyncio.to_thread(self.store.all, "project", active_only=False))[
                -config["existing_record_limit"] :
            ],
            *await self.store.search(query),
            *(
                row
                for row in await asyncio.to_thread(
                    self.store.all, "global", active_only=False
                )
                if row.state != "active"
            ),
        ]
        job.bases = {row.id: row for row in candidates}
        payload = dict(
            mode="migration"
            if job.events[0].origin == "migration"
            else "explicit_request"
            if job.request is not None
            else "perception",
            previous_error=job.error,
            project=str(self.workspace.root),
            events=packets,
            existing_records=[
                row.model_dump(mode="json") for row in job.bases.values()
            ],
            new_turn_ids=job.window.new_turn_ids
            if job.window
            else [event.id for event in job.events],
            overlap_turn_ids=job.window.overlap_turn_ids if job.window else [],
            core_memory=await self.long_term.list_memory(),
            explicit_request=job.request,
            requested_scope=job.scope,
            memory_cleared_at={
                scope: self._cleared_at(scope) for scope in ("project", "global")
            },
            references=[
                dict(
                    id=ref.id,
                    name=ref.name,
                    source=ref.source,
                    media_type=ref.media_type,
                )
                for ref in references
            ],
        )

        def progress(fragments):
            job.fragments = fragments
            self._save_job(job)

        def usage(value):
            job.usage.append(value)
            self._save_job(job)

        result = await self.perception.produce(
            job.id,
            payload,
            references,
            fragments=job.fragments,
            on_fragment=progress,
            on_usage=usage,
        )
        if job.request is not None and not result.request_authorized:
            job.result, job.done = result, True
            self._save_job(job)
            return
        if job.request is not None and not result.records:
            raise ValueError(result.reason)
        job.result = result
        self._save_job(job)

    def _record(self, job, draft, index):
        explicit = job.request is not None
        if explicit:
            draft = draft.model_copy(update={"kind": "requested"})
        events = {event.id: event for event in job.events}
        new_ids = set(job.window.new_turn_ids if job.window else events)
        draft = draft.validated_sources(
            events, new_ids, job.reference_ids, job.bases.get(draft.target_id)
        )
        if any(
            events[key].created_at <= self._cleared_at(draft.scope)
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
        if job.scope != "auto" and draft.scope != job.scope:
            raise ValueError("The requested memory scope was not honored")
        identity = (
            draft.target_id
            or hashlib.sha256(f"{job.id}:{index}".encode()).hexdigest()[:32]
        )
        try:
            previous = self.store.get(identity)
        except KeyError:
            previous = None
        if previous and previous.last_change_id == f"{job.id}:{index}":
            return previous
        if previous and (
            previous.scope != draft.scope
            or (previous.origin == "explicit" and not explicit)
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

    async def _apply(self, job):
        self.long_term.directory.mkdir(parents=True, exist_ok=True)
        async with AsyncFileLock(
            self.long_term.directory / "publication.lock", run_in_executor=False
        ):
            changes = [
                (record, draft)
                for index, draft in enumerate(job.result.records)
                if (record := await asyncio.to_thread(self._record, job, draft, index))
            ]
            committed = []
            for record, draft in changes:
                if record.scope == "global" and not await asyncio.to_thread(
                    self.long_term.apply_record,
                    record,
                    job.bases.get(record.id),
                    core_old_text=draft.core_old_text,
                ):
                    continue
                committed.append(record)
            await asyncio.to_thread(self.store.save, committed)
            job.records = [record.id for record in committed]
            if job.window:
                self.observations.commit(job.window)
            else:
                for event in job.events:
                    if event.origin == "user":
                        observed = (
                            self.current
                            if self.current and self.current.id == event.id
                            else self.observations.read([event.id])[0]
                        )
                        observed.requested_record_ids = list(
                            dict.fromkeys(
                                [*observed.requested_record_ids, *job.records]
                            )
                        )
                        self.observations.save(observed)
            job.done, job.error = True, ""
            self._save_job(job)

    async def _execute(self, job, *, retry=False):
        lock = self._job_path(job).with_suffix(".execute.lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncFileLock(lock, run_in_executor=False):
            latest = self._job(job)
            for name in MemoryJob.model_fields:
                setattr(job, name, getattr(latest, name))
            if retry and job.blocked_recipe:
                job.blocked_recipe = ""
                self._save_job(job)
            return await self._produce_and_apply(job)

    async def _produce_and_apply(self, job):
        if job.done:
            return True
        if self._paused():
            self.last_error = self._read_state(self.state_path).get(
                "error", "记忆服务已暂停。"
            )
            return False
        recipe = self._route()
        if job.blocked_recipe == recipe:
            self.last_error = job.error
            return False
        try:
            if job.result is None:
                await self._produce(job)
            if not job.done:
                await self._apply(job)
            self.last_error = ""
            save_locked_json(self.state_path, {})
            return True
        except Exception as exc:
            if isinstance(exc, ValueError):
                job.result = None
            job.error = self.last_error = str(exc)
            if isinstance(exc, UnexpectedModelBehavior) and str(exc).startswith(
                "Model token limit ("
            ):
                job.blocked_recipe = recipe
            self._save_job(job)
            state = dict(error=str(exc), event_count=len(self.observations.order()))
            if isinstance(exc, ModelHTTPError) and exc.status_code in (
                400,
                401,
                402,
                403,
            ):
                state["blocked_route"] = self._route()
            save_locked_json(self.state_path, state)
            logger.error("记忆生产未完成，原始事件已保留：%s", exc)
            return False

    async def remember(self, request: str, scope: str = "auto") -> str:
        """Save, correct or forget memory explicitly requested by the current user. Only claim success from a saved receipt."""
        if not self.owner_memory_allowed or self.current is None:
            return "Error: Explicit memory requires an authenticated current user turn."
        async with self._explicit:
            event = self.current.model_copy(deep=True)
            event.user_inputs = list(self._input_source())
            identity = hashlib.sha256(
                f"{event.id}:{request}:{scope}".encode()
            ).hexdigest()[:32]
            job = self._job(
                MemoryJob(id=identity, events=[event], request=request, scope=scope)
            )
            if not await self._execute(job):
                return "Error: 请求已登记但未保存为记忆：" + self.last_error
            if not job.result.request_authorized:
                return json.dumps(
                    dict(status="rejected", reason=job.result.reason, records=[]),
                    ensure_ascii=False,
                )
            await self.store.reconcile()
            return json.dumps(
                dict(
                    status="saved" if job.records else "superseded",
                    records=[
                        dict(
                            id=row.id,
                            scope=row.scope,
                            state=row.state,
                            projection=row.projection,
                        )
                        for row in (self.store.get(key) for key in job.records)
                    ],
                    index_error=self.store.last_error,
                ),
                ensure_ascii=False,
            )

    async def process_pending(self, *, flush=False, recover=False):
        if not self.owner_memory_allowed:
            return
        flush_path = self.observations.root / "flush_pending.json"
        if flush:
            save_locked_json(flush_path, {"requested_at": iso_utc_now()})
        async with self._processing:
            flush = flush or flush_path.exists()
            self.observations.root.mkdir(parents=True, exist_ok=True)
            async with AsyncFileLock(
                self.observations.root / "processing.lock", run_in_executor=False
            ):
                if not self._recovered:
                    from redlotus.tools.memory.migration import (
                        migrate_observations,
                        migrate_record_files,
                        migrate_pending_jobs,
                    )

                    await migrate_observations(
                        self.observations,
                        rag_config=self.store.indexes["project"].config,
                    )
                    await asyncio.to_thread(
                        migrate_record_files,
                        self.store,
                        self.observations.root,
                        self.long_term.directory,
                    )
                    self.observations.recover()
                    await asyncio.to_thread(migrate_pending_jobs, self)
                    self._recovered = True
                    recover = True  # One recovery attempt per new runtime, including a restored service balance.
                if recover:
                    save_locked_json(self.state_path, {})
                if self._paused():
                    return
                state = self._read_state(self.state_path)
                if (
                    state.get("error")
                    and not recover
                    and len(self.observations.order()) <= state.get("event_count", 0)
                ):
                    return
                jobs = [
                    MemoryJob.model_validate(read_locked_json(path))
                    for path in self.jobs_dir.glob("*.json")
                ]
                for job in sorted(jobs, key=lambda item: (item.created_at, item.id)):
                    if not job.done and not await self._execute(job, retry=recover):
                        return
                await self._migrate_core()
                while window := self.observations.window(flush=flush):
                    job = self._job(
                        MemoryJob(
                            id=window.id,
                            window=window,
                            events=self.observations.read(
                                [*window.overlap_turn_ids, *window.new_turn_ids]
                            ),
                        )
                    )
                    if not await self._execute(job):
                        return
                    logger.info(
                        "记忆感知：处理 %s 回合，保存 %s 条记忆。",
                        len(job.events),
                        len(job.records),
                    )
                await self.store.reconcile()
                flush_path.unlink(missing_ok=True)

    async def _migrate_core(self):
        marker = self.long_term.directory / "migration_backup/core_records_v3.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncFileLock(
            marker.with_name("migration.lock"), run_in_executor=False
        ):
            await self._migrate_core_locked(marker)

    async def _migrate_core_locked(self, marker):
        if marker.exists():
            return
        source = marker.with_name("core_source.md")
        legacy = (
            source.read_text(encoding="utf-8")
            if source.exists()
            else await asyncio.to_thread(self.long_term.legacy_content)
        )
        if not legacy:
            return
        if not source.exists():
            source.write_text(legacy, encoding="utf-8")
        _, sections = self.long_term._parse(legacy)
        if any(sections.values()):
            identity = hashlib.sha256(("legacy-core:" + legacy).encode()).hexdigest()[
                :32
            ]
            event = ObservedTurn(
                id=identity,
                project_id=self.workspace.project_id,
                session_id="migration",
                turn_id=identity,
                origin="migration",
                user_inputs=["迁移本人旧记忆，保留事实：\n" + legacy],
            )
            job = self._job(
                MemoryJob(
                    id=identity,
                    events=[event],
                    scope="global",
                    request="迁移原有记忆：详细项目与知识存长期记录，画像和通用经验保留核心投影。每条给出 core_old_text。",
                )
            )
            if not await self._execute(job):
                return
        self.long_term.finish_legacy_migration(legacy)
        save_locked_json(marker, dict(done=True))

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
                project_id=self.workspace.project_id,
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

    async def short_term_snapshot(self):
        total, consumed = len(self.observations.order()), self.observations.cursor()
        return {
            **await self.store.snapshot("project"),
            "observed_turns": total,
            "consumed_turns": consumed,
            "pending_turns": total - consumed,
            "window_turns": self.observations.window_turns,
            "overlap_turns": self.observations.overlap_turns,
            "perception_error": self.last_error
            or self._read_state(self.state_path).get("error", ""),
        }

    async def _clear(self, scope):
        if not self.owner_memory_allowed:
            return
        self.long_term.directory.mkdir(parents=True, exist_ok=True)
        async with AsyncFileLock(
            self.long_term.directory / "publication.lock", run_in_executor=False
        ):
            save_locked_json(self._clear_path(scope), dict(cleared_at=iso_utc_now()))
            await self.store.clear(scope)
            if scope == "global":
                await self.long_term.clear_all()
            else:
                save_locked_json(
                    self.observations.cursor_path,
                    dict(consumed=len(self.observations.order())),
                )
            self._context_notices.append(
                [
                    TextContent(
                        f"[记忆操作结果] 用户已确认清空 {'全局长期记忆' if scope == 'global' else '当前项目情景记忆'}。"
                        "该范围内的旧记忆已失效，不要从会话快照恢复；新的明确授权可重新保存。",
                        metadata={"origin": "memory_control"},
                    )
                ]
            )

    async def clear_long_term(self):
        await self._clear("global")

    async def clear_short_term(self):
        async with self._processing:
            await self._clear("project")

    async def wait_idle(self, timeout=15):
        try:
            await asyncio.wait_for(self._processing.acquire(), timeout)
            self._processing.release()
            return True
        except asyncio.TimeoutError:
            return False

    async def close(self):
        if self._owns_factory:
            await self._perception_factory.close()
        self.observations.close()
        await self.store.close()
