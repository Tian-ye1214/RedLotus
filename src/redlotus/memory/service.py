"""Current-session perception scheduling and explicit memory requests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from typing import Literal

from filelock import AsyncFileLock
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import TextContent

from redlotus.core.config import (
    settings,
    file_lock,
    iso_utc_now,
    read_locked_json,
    save_locked_json,
)
from redlotus.core import config as logger
from redlotus.core.gateway import ModelTarget
from redlotus.prompts.prompt import load_prompt
from redlotus.tools.references import ReferenceStore
from redlotus.core.agents import WorkspaceContext, AgentRegistry, SubagentFactory, SubagentSpec
from redlotus.memory.records import EvidenceReader, LongTermMemory, ObservationStore
from redlotus.memory.perception import MemoryPerception, MemoryJob, produce_job
from redlotus.memory.store import MemoryStore, MemoryReader
from redlotus.core.session import current_workspace


class MemoryService:
    def __init__(self, *, workspace=None, owner_memory_allowed=True, factory=None):
        self.workspace = workspace or WorkspaceContext.from_path(current_workspace())
        self.owner_memory_allowed = owner_memory_allowed
        self.long_term, self.store = LongTermMemory(), MemoryStore(self.workspace)
        self.observations = ObservationStore(self.workspace)
        self.references = ReferenceStore(self.workspace)
        self.evidence = EvidenceReader(self.references)
        self.reader = MemoryReader(self.store, self.long_term, self.references, owner_memory_allowed)
        self._owns_factory = factory is None
        self._perception_factory = factory or SubagentFactory()
        self.perception, self.current, self.session = None, None, None
        self._input_source = lambda: self.current.user_inputs if self.current else []
        self._injection_snapshot = None
        self._context_notices = []
        self._processing, self._explicit = asyncio.Lock(), asyncio.Lock()
        self.last_error = ""
        self._background = None
        self._background_running = False
        self._schedule_lock = threading.Lock()
        self._pending_end = 0
        self._targets = {}

    def bind_session(self, session):
        """Bind storage only: new/load must never schedule automatic perception."""
        self.session = session
        self.observations.bind(session)
        self.evidence.session = session

    def unbind_session(self):
        """Drop volatile state after the owner has cancelled this session's Agents."""
        self.session = self.current = None
        self.observations.session = self.evidence.session = None
        self.last_error = ""
        self._context_notices = []
        with self._schedule_lock:
            self._background = None
            self._background_running = False
            self._pending_end = 0
            self._targets = {}

    def _processor(self, value=None):
        """Read or update this session's retry state in its sole JSON file."""
        if value is not None:
            self.session.update(metadata={"memory_processor": value})
        return self.session.metadata.get("memory_processor", {}) if self.session else {}

    def bind_runner(self, registry, *, input_source):
        # Explicit requests reuse the caller loop, so a full Worker pool cannot deadlock.
        self.perception = MemoryPerception(self.workspace, None, registry)
        self._input_source = input_source

    @property
    def worker_tools(self):
        return (
            [
                self.reader.search_memory,
                self.reader.read_memory,
                self.remember,
                self.reader.search_episodes,
                self.reader.read_episode,
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

    def _route(self):
        config = settings()["memory_perception"]
        recipe = {
            **asdict(ModelTarget.for_role(config["model_role"])),
            "perception": config,
            "prompt": load_prompt("memory_perception_system.md"),
        }
        return hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()

    def _paused(self):
        state = self._processor()
        paused = state.get("blocked_route") == self._route()
        if paused:
            self.last_error = state.get("error", "记忆服务已暂停，请恢复服务后使用 /STM retry。")
        return paused

    def _clear_path(self, scope):
        root = self.long_term.directory if scope == "global" else self.observations.root
        return root / "clear_state.json"

    def _cleared_at(self, scope):
        path = self._clear_path(scope)
        return read_locked_json(path).get("cleared_at", "") if path.exists() else ""

    def _job(self, job):
        saved = self.session.job(job.id)
        if saved is not None:
            saved["events"] = self.observations.read(saved.pop("event_ids"))
            return MemoryJob.model_validate(saved)
        self._save_job(job)
        return job

    def _save_job(self, job):
        """Persist job deltas and evidence links, never another conversation copy."""
        value = job.model_dump(mode="json", exclude={"events", "sources"})
        value["event_ids"] = [event.id for event in job.events]
        value["sources"] = {key: {name: item for name, item in row.items() if name != "text"}
                            for key, row in job.sources.items()}
        value["indexed"] = "indexed_at" in job.timings
        if value["indexed"]:
            value.update(event_ids=[], sources={}, bases={}, prompt_snapshot="", model_snapshot={}, perception_config={})
            value["result"] = {"records": [], "reason": job.result.reason,
                               "request_authorized": job.result.request_authorized}
        self.session.update(jobs={job.id: value})

    async def _apply(self, job):
        self.long_term.directory.mkdir(parents=True, exist_ok=True)
        async with AsyncFileLock(
            self.long_term.directory / "publication.lock", run_in_executor=False
        ):
            changes = [
                (record, draft)
                for index, draft in enumerate(job.result.records)
                if (record := await asyncio.to_thread(self.store.materialize, job, draft, index, self._cleared_at))
            ]
            committed = []
            for record, draft in changes:
                if not await asyncio.to_thread(
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
            job.timings["committed_at"] = iso_utc_now()
            self._save_job(job)

    async def _execute(self, job, *, retry=False):
        lock = self.session.path.parent / f"{job.id}.execute.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncFileLock(lock, run_in_executor=False):
            latest = self._job(job)
            for name in MemoryJob.model_fields:
                setattr(job, name, getattr(latest, name))
            if job.error and not job.failures:
                job.failures.append(dict(recorded_at=iso_utc_now(), error=job.error))
                self._save_job(job)
            if retry and job.blocked_recipe:
                job.blocked_recipe = ""
                self._save_job(job)
            return await self._produce_and_apply(job)

    async def _produce_and_apply(self, job):
        if job.done:
            return True
        if self._paused():
            self.last_error = self._processor().get(
                "error", "记忆服务已暂停。"
            )
            return False
        recipe = self._route()
        if job.blocked_recipe == recipe:
            self.last_error = job.error
            return False
        try:
            if job.result is None:
                await produce_job(self, job)
            if not job.done:
                await self._apply(job)
            self.last_error = ""
            self._processor({})
            return True
        except Exception as exc:
            if isinstance(exc, ValueError):
                job.result = None
            job.error = self.last_error = str(exc)
            job.failures.append(dict(recorded_at=iso_utc_now(), error=job.error))
            if isinstance(exc, UnexpectedModelBehavior) and str(exc).startswith(
                "Model token limit ("
            ):
                job.blocked_recipe = recipe
            self._save_job(job)
            state = dict(error=str(exc), event_count=self.session.completed_turns)
            if isinstance(exc, ModelHTTPError) and exc.status_code in (
                400,
                401,
                402,
                403,
            ):
                state["blocked_route"] = self._route()
            self._processor(state)
            logger.error("记忆生产未完成，原始事件已保留：%s", exc)
            return False

    async def remember(
        self, request: str, scope: Literal["auto", "project", "global"] = "auto"
    ) -> str:
        """Save, correct or forget an explicitly requested memory; report the actual receipt.

        project means this current workspace, not any named project in the text.
        Use global for cross-project preferences or knowledge ABOUT another project,
        naming its applicability in the request. Each call handles only its stated proposal.
        """
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

    def seal_windows(self, *, through=None):
        """Reserve only full windows of new completed turns in the bound session."""
        if not self.owner_memory_allowed or self.session is None:
            return
        through = self.session.completed_turns if through is None else through
        with file_lock(self.session.path.parent / "schedule"):
            start = self.observations.reserved_cursor()
            while window := self.observations.window(through=through, start=start):
                config = deepcopy(settings()["memory_perception"])
                frozen = ModelTarget.for_role(config["model_role"])
                self._targets[window.id] = frozen
                target = asdict(frozen)
                target.pop("api_key")
                events = self.observations.read([*window.overlap_turn_ids, *window.new_turn_ids])
                self._job(MemoryJob(
                    id=window.id, window=window, events=events, model_snapshot=target,
                    perception_config=config, prompt_snapshot=load_prompt("memory_perception_system.md"),
                    timings={"ready_at": events[-1].finished_at, "sealed_at": iso_utc_now()},
                ))
                self.observations.reserve(window)
                start = window.end_position

    def schedule_processing(self):
        """Schedule current-session work after a completed turn, without scanning old jobs."""
        if not self.owner_memory_allowed or self.session is None:
            return
        self.seal_windows()
        with self._schedule_lock:
            self._pending_end = self.session.completed_turns
            if self._background_running or not self.session.pending_jobs():
                return
            self._background_running = True
            spec = SubagentSpec(self.session.session_id, None, self.workspace, role="perception")
            self._background = self._perception_factory.start_background(spec, self._drain_background)
            self._background._future.add_done_callback(self._background_finished)

    def _background_finished(self, future):
        with self._schedule_lock:
            if self._background is not None and self._background._future is future:
                self._background_running = False

    async def _drain_background(self):
        """Own model/database resources in the admitted thread; never inspect other sessions."""
        producer = MemoryService(workspace=self.workspace, factory=self._perception_factory)
        producer.bind_session(self.session)
        producer.perception = MemoryPerception(self.workspace, None, AgentRegistry())
        try:
            while True:
                with self._schedule_lock:
                    through = self._pending_end
                    producer._targets = deepcopy(self._targets)
                await producer.process_pending(through=through)
                self.last_error = producer.last_error
                with self._schedule_lock:
                    if through == self._pending_end:
                        self._background_running = False
                        return
        finally:
            await producer.close()

    async def _index_job(self, job):
        if "indexed_at" in job.timings:
            return
        if job.records:
            job.timings["index_started_at"] = iso_utc_now()
            self._save_job(job)
            await self.store.reconcile()
            if self.store.last_error:
                job.error = self.last_error = self.store.last_error
                self._save_job(job)
                return
        job.timings["indexed_at"] = iso_utc_now()
        self._save_job(job)
        if job.window and "ready_at" in job.timings:
            ready = datetime.fromisoformat(job.timings["ready_at"])
            elapsed = (
                datetime.fromisoformat(job.timings["indexed_at"]) - ready
            ).total_seconds()
            api_seconds = sum(call["seconds"] for call in job.model_calls)
            job.elapsed = dict(
                total_seconds=elapsed,
                model_api_seconds=api_seconds,
                other_seconds=max(0, elapsed - api_seconds),
            )
            self._save_job(job)
            logger.info_file_only(
                "记忆感知：新增 %s 回合，保存 %s 条，总计 %.2f 秒（模型 API %.2f 秒），窗口 %s。",
                len(job.window.new_turn_ids),
                len(job.records),
                elapsed,
                api_seconds,
                job.id,
            )
        self._prune_consumed()

    def _prune_consumed(self):
        """Release old bodies only after their window is indexed; preserve pending evidence."""
        if self.session.pending_jobs():
            return
        boundary = max(0, self.observations.cursor() - self.observations.overlap_turns)
        turns = self.session.pending_turns(boundary)
        keep = {identity for row in turns for identity in (row["id"], row.get("turn_id", row["id"]))}
        if active := self.session.metadata.get("active_turn"):
            keep.update((active["id"], active["turn_id"]))
        self.session.compact(keep_turn_ids=keep)

    async def process_pending(self, *, recover=False, through=None):
        """Retry registered work only; restored sessions never invent a partial window."""
        if not self.owner_memory_allowed or self.session is None:
            return
        async with self._processing:
            async with AsyncFileLock(self.session.path.parent / "processing.lock", run_in_executor=False):
                through = self.session.completed_turns if through is None else through
                if recover:
                    self._processor({})
                if self._paused():
                    return
                for identity in self.session.pending_jobs():
                    saved = self.session.job(identity)
                    saved["events"] = self.observations.read(saved.pop("event_ids"))
                    job = MemoryJob.model_validate(saved)
                    if job.window and job.window.end_position > through:
                        continue
                    if not job.done and not await self._execute(job, retry=recover):
                        return
                    await self._index_job(job)

    async def short_term_snapshot(self):
        total, consumed = (self.session.completed_turns if self.session else 0), self.observations.cursor()
        return {
            **await self.store.snapshot("project"),
            "observed_turns": total,
            "consumed_turns": consumed,
            "pending_turns": total - consumed,
            "window_turns": self.observations.window_turns,
            "overlap_turns": self.observations.overlap_turns,
            "perception_error": self.last_error
            or self._processor().get("error", ""),
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
        if self._background is None:
            return True
        future = asyncio.wrap_future(self._background._future)
        done, _ = await asyncio.wait([future], timeout=timeout)
        if not done or future.cancelled():
            return False
        future.result()
        return True

    async def close(self):
        if self._owns_factory:
            await self._perception_factory.close()
        self.unbind_session()
        await self.store.close()
