"""Focused auxiliary regressions for authoritative memory publication."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

import redlotus.runtime.files as _runtime_files
from redlotus.memory import records as record_module
from redlotus.memory import retrieval as retrieval_module
from redlotus.memory import store as store_module
from redlotus.runtime import config as app_config
from redlotus.runtime.context import WorkspaceContext

pass
pass
from redlotus.memory.perception import MemoryJob
from redlotus.memory.records import (
    LongTermMemory,
    MemoryDraft,
    MemoryRecord,
    ObservedTurn,
    PerceptionResult,
)
from redlotus.memory.service import MemoryService
from redlotus.memory.store import MemoryStore


class JobSession:
    def __init__(self):
        self.metadata = {}
        self.jobs = {}
        self.completed_turns = 1

    def update(self, *, jobs=None, metadata=None, **_):
        self.jobs.update(jobs or {})
        self.metadata.update(metadata or {})


def make_store(tmp_path: Path, monkeypatch, name="db"):
    config = {
        "RAG_models": {"embedding": "test-embedding", "reranker": "test-reranker"},
        "short_term_memory": {
            "db_path": str(tmp_path / name),
            "table_name": "test_vectors",
            "vector_search_limit": 8,
            "final_top_k": 4,
            "use_rerank": False,
            "min_similarity": 0.3,
            "turn_token_limit": 128,
            "turn_chunk_overlap_tokens": 8,
            "index": {
                "min_rows": 100,
                "metric": "cosine",
                "rebuild_every_n_adds": 100,
                "rows_per_partition": 100,
                "dimensions_per_sub_vector": 1,
            },
        },
        "long_term_memory": {"table_name": "test_global_vectors"},
        "rag_service": {"index_batch_size": 8},
        "storage": {
            "project_dir": ".redlotus",
            "sessions_dir": ".redlotus/sessions",
            "references_dir": "WorkDatabase/references",
            "runtime_dir": "WorkDatabase/runtime",
            "project_logs_dir": ".redlotus/logs",
        },
    }
    monkeypatch.setattr(app_config, "settings", lambda: config)
    monkeypatch.setattr(store_module, "settings", lambda: config)
    monkeypatch.setattr(retrieval_module, "settings", lambda: config)
    workspace = WorkspaceContext.from_path(tmp_path / (name + "-project"))
    store = MemoryStore.__new__(MemoryStore)
    store.workspace = workspace
    store.records = {}
    store.indexes = {
        "project": SimpleNamespace(config=config["short_term_memory"]),
        "global": SimpleNamespace(config=config["short_term_memory"]),
    }
    store.last_error = store.retrieval_error = ""

    def get(self, identity):
        if identity not in self.records:
            raise KeyError(identity)
        return self.records[identity].model_copy(deep=True)

    def all_records(self, scope=None, *, active_only=True):
        return [
            row.model_copy(deep=True)
            for row in self.records.values()
            if (scope is None or row.scope == scope)
            and (not active_only or row.state == "active")
        ]

    def revision(self, scope):
        payload = sorted(
            (row.id, row.model_dump(mode="json"))
            for row in self.records.values()
            if row.scope == scope
        )
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def save(self, records):
        for row in records:
            current = self.records.get(row.id)
            if current and current.last_change_id == row.last_change_id:
                continue
            if current and row.version != current.version + 1:
                raise ValueError("changed before commit")
            self.records[row.id] = row.model_copy(deep=True)

    store.get = MethodType(get, store)
    store.all = MethodType(all_records, store)
    store.revision = MethodType(revision, store)
    store.save = MethodType(save, store)
    return workspace, store


def memory_record(workspace, *, version=1, change="seed", state="active", content="old"):
    return MemoryRecord(
        id="preference",
        project_id=workspace.project_id,
        scope="global",
        kind="requested",
        projection="profile",
        goal="Remember preference",
        content=content,
        state=state,
        version=version,
        last_change_id=change,
    )


def memory_job(workspace, store, *, action="create", target=None, content="candidate", job_id="job"):
    event = ObservedTurn(
        id="event",
        project_id=workspace.project_id,
        session_id="session",
        turn_id="turn",
        status="success",
        origin="migration",
        user_inputs=["remember this"],
    )
    draft = MemoryDraft(
        action=action,
        target_id=target,
        scope="global",
        projection="profile",
        goal="Remember preference",
        content=content,
        source_turn_ids=[event.id],
        search_id="search",
    )
    identity = target or hashlib.sha256(f"{job_id}:0".encode()).hexdigest()[:32]
    try:
        base = store.get(identity)
    except KeyError:
        base = None
    return MemoryJob(
        id=job_id,
        events=[event],
        request="remember",
        scope="global",
        result=PerceptionResult(records=[draft], reason="test", request_authorized=True),
        bases={identity: base} if base else {},
        base_versions={identity: base.version if base else 0},
        searches=[{
            "id": "search",
            "scope": "global",
            "revision": store.revision("global"),
            "retrieval_complete": True,
            "retrieval_error": "",
        }],
    )


def memory_service(workspace, store, directory):
    service = MemoryService.__new__(MemoryService)
    service.workspace = workspace
    service.store = store
    service.long_term = LongTermMemory(directory)
    service.session = JobSession()
    service.current = None
    service.observations = None
    service.last_error = ""
    service._context_notices = []
    service._processor = lambda value=None: {}
    service._paused = lambda: False
    service._route = lambda: "recipe"
    return service


@pytest.mark.asyncio
async def test_database_failure_cannot_leak_candidate_to_memory_md(tmp_path, monkeypatch):
    workspace, store = make_store(tmp_path, monkeypatch)
    service = memory_service(workspace, store, tmp_path / "core")
    job = memory_job(workspace, store)
    monkeypatch.setattr(store, "save", lambda _records: (_ for _ in ()).throw(OSError("db failed")))

    with pytest.raises(OSError, match="db failed"):
        await service._apply(job)

    assert "candidate" not in service.long_term.read()


@pytest.mark.asyncio
async def test_projection_failure_retries_projection_without_regeneration(tmp_path, monkeypatch):
    workspace, store = make_store(tmp_path, monkeypatch)
    service = memory_service(workspace, store, tmp_path / "core")
    job = memory_job(workspace, store)
    original_write = _runtime_files.atomic_write_text

    def fail_candidate(path, text):
        if Path(path).name == "MEMORY.md" and "candidate" in text:
            raise ValueError("projection failed")
        return original_write(path, text)

    monkeypatch.setattr(record_module, "atomic_write_text", fail_candidate)
    assert await service._produce_and_apply(job) is False
    saved = store.get(hashlib.sha256(b"job:0").hexdigest()[:32])
    assert saved.version == 1
    assert job.result is not None

    monkeypatch.setattr(record_module, "atomic_write_text", original_write)
    assert await service._produce_and_apply(job) is True
    assert store.get(saved.id).version == 1
    assert "candidate" in service.long_term.read()


@pytest.mark.asyncio
async def test_journal_ack_failure_retries_same_formal_change(tmp_path, monkeypatch):
    workspace, store = make_store(tmp_path, monkeypatch)
    service = memory_service(workspace, store, tmp_path / "core")
    job = memory_job(workspace, store)
    original_save_job = service._save_job
    failed = False

    def fail_first_stored_ack(value):
        nonlocal failed
        if "stored_at" in value.timings and not failed:
            failed = True
            raise OSError("ack failed")
        original_save_job(value)

    monkeypatch.setattr(service, "_save_job", fail_first_stored_ack)
    with pytest.raises(OSError, match="ack failed"):
        await service._apply(job)
    identity = hashlib.sha256(b"job:0").hexdigest()[:32]
    assert store.get(identity).version == 1
    assert "candidate" not in service.long_term.read()

    monkeypatch.setattr(service, "_save_job", original_save_job)
    await service._apply(job)
    assert store.get(identity).version == 1
    assert "candidate" in service.long_term.read()


@pytest.mark.asyncio
async def test_manual_projection_edit_is_validated_before_formal_update(tmp_path, monkeypatch):
    workspace, store = make_store(tmp_path, monkeypatch)
    base = memory_record(workspace)
    store.save([base])
    service = memory_service(workspace, store, tmp_path / "core")
    assert service.long_term.apply_record(base)
    service.long_term.path.write_text(
        service.long_term.read().replace("old", "manual edit"), encoding="utf-8"
    )
    job = memory_job(workspace, store, action="update", target=base.id, content="candidate")

    await service._apply(job)

    assert store.get(base.id).version == 1
    assert store.get(base.id).content == "old"
    assert "manual edit" in service.long_term.read()
    assert job.records == []


def test_reversed_commit_does_not_relabel_stale_candidate(tmp_path, monkeypatch):
    workspace, store = make_store(tmp_path, monkeypatch)
    base = memory_record(workspace)
    store.save([base])
    job = memory_job(workspace, store, action="update", target=base.id, content="older", job_id="old-job")
    newer = base.model_copy(update={"content": "newer", "version": 2, "last_change_id": "new-job"})
    store.save([newer])

    assert store.materialize(job, job.result.records[0], 0, lambda _: "") is None
    assert store.get(base.id).content == "newer"


def test_delete_before_old_commit_cannot_resurrect_record(tmp_path, monkeypatch):
    workspace, store = make_store(tmp_path, monkeypatch)
    base = memory_record(workspace)
    store.save([base])
    job = memory_job(workspace, store, action="update", target=base.id, content="stale", job_id="old-job")
    deleted = base.model_copy(update={"state": "deleted", "version": 2, "last_change_id": "delete-job"})
    store.save([deleted])

    assert store.materialize(job, job.result.records[0], 0, lambda _: "") is None
    assert store.get(base.id).state == "deleted"


@pytest.mark.asyncio
async def test_missing_index_distinguishes_degraded_from_complete_empty(tmp_path, monkeypatch):
    workspace, store = make_store(tmp_path, monkeypatch, "populated")
    store.save([memory_record(workspace, content="prefers dogs")])
    monkeypatch.setattr(store, "rag_unavailable_reason", lambda scope=None: "vector index missing")

    assert await store.search("canine", "global") == []
    assert store.retrieval_complete is False
    assert "missing" in store.retrieval_error

    _, empty = make_store(tmp_path, monkeypatch, "empty")
    monkeypatch.setattr(empty, "rag_unavailable_reason", lambda scope=None: "vector index missing")
    assert await empty.search("canine", "global") == []
    assert empty.retrieval_complete is True
    assert empty.retrieval_error == ""


@pytest.mark.asyncio
async def test_search_result_keeps_status_after_later_empty_scope_search(tmp_path, monkeypatch):
    workspace, store = make_store(tmp_path, monkeypatch)
    store.save([memory_record(workspace, content="prefers dogs")])
    monkeypatch.setattr(store, "rag_unavailable_reason", lambda scope=None: "vector missing")

    degraded = await store.search("canine", "global")
    complete_empty = await store.search("canine", "project")

    assert degraded.retrieval_complete is False
    assert degraded.retrieval_error == "vector missing"
    assert complete_empty.retrieval_complete is True
    assert complete_empty.retrieval_error == ""


@pytest.mark.asyncio
async def test_degraded_synonym_search_cannot_authorize_l2_creation(tmp_path, monkeypatch):
    workspace, store = make_store(tmp_path, monkeypatch)
    service = memory_service(workspace, store, tmp_path / "core")
    job = memory_job(workspace, store)
    job.searches[0].update(retrieval_complete=False, retrieval_error="")

    with pytest.raises(ValueError, match="memory_publication_search"):
        await service._check_searches(job)


@pytest.mark.asyncio
async def test_l2_creation_rejects_error_even_if_complete_flag_is_true(tmp_path, monkeypatch):
    workspace, store = make_store(tmp_path, monkeypatch)
    service = memory_service(workspace, store, tmp_path / "core")
    job = memory_job(workspace, store)
    job.searches[0].update(retrieval_complete=True, retrieval_error="vector failed")

    with pytest.raises(ValueError, match="memory_publication_search"):
        await service._check_searches(job)


@pytest.mark.asyncio
async def test_degraded_recall_can_update_a_known_record(tmp_path, monkeypatch):
    workspace, store = make_store(tmp_path, monkeypatch)
    base = memory_record(workspace)
    store.save([base])
    service = memory_service(workspace, store, tmp_path / "core")
    job = memory_job(workspace, store, action="update", target=base.id, content="new")
    job.searches[0].update(retrieval_complete=False, retrieval_error="vector fallback")

    await service._check_searches(job)


@pytest.mark.asyncio
async def test_new_session_injection_reconciles_only_formal_projection(tmp_path, monkeypatch):
    workspace, store = make_store(tmp_path, monkeypatch)
    store.save([memory_record(workspace, content="formal preference")])
    service = memory_service(workspace, store, tmp_path / "core")
    service.owner_memory_allowed = True
    service._injection_snapshot = None
    service.observations = SimpleNamespace(begin=lambda *args: args)

    await service.begin_turn("session", "turn", "hello")

    assert "formal preference" in service.injection_for_session()
    assert "version:1" in service.injection_for_session()


@pytest.mark.asyncio
async def test_stored_projection_retry_retires_candidate_superseded_by_newer_update(tmp_path, monkeypatch):
    workspace, store = make_store(tmp_path, monkeypatch)
    base = memory_record(workspace)
    store.save([base])
    service = memory_service(workspace, store, tmp_path / "core")
    assert service.long_term.apply_record(base)
    job = memory_job(workspace, store, action="update", target=base.id,
                     content="stale projection", job_id="old-job")
    candidate = store.materialize(job, job.result.records[0], 0, lambda _: "")
    store.save([candidate])
    job.records = [candidate.id]
    job.timings["stored_at"] = "2026-09-21T00:00:00Z"
    newer = candidate.model_copy(update={
        "content": "newer formal value",
        "version": candidate.version + 1,
        "last_change_id": "new-job:0",
    })
    store.save([newer])

    await service._apply(job)

    assert job.done is True
    assert job.records == []
    assert store.get(base.id).content == "newer formal value"
    assert "newer formal value" in service.long_term.read()
    assert "stale projection" not in service.long_term.read()
