"""Synthetic, hand-written legacy journals for historical count correction."""
import json

import pytest

from redlotus.runtime.resources import WorkspaceContext
from redlotus.sessions.storage import SessionFile
from redlotus.memory.records import ObservationStore

STAMP = "2026-01-01T00:00:00+00:00"


def legacy(tmp_path, rows, messages=None, *, jobs=None, metadata=None, updates=()):
    workspace = WorkspaceContext.from_path(tmp_path)
    path = tmp_path / "sessions" / "legacy" / "model_messages.json"
    path.parent.mkdir(parents=True)
    data = dict(session_id="legacy", project_id=workspace.project_id, title="Synthetic", created_at=STAMP,
                updates=[dict(turns={row["id"]: row for row in rows}, messages=messages or {},
                              jobs=jobs or {}, metadata=dict(completed_turns=len(rows), **(metadata or {}))), *updates])
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, workspace


def row(identity, number, status="success", **fields):
    return dict(id=identity, turn_id=identity, session_id="legacy", project_id="synthetic",
                number=number, status=status, user_inputs=["same request"], reference_ids=[],
                created_at=STAMP, finished_at=STAMP, **fields)


def response(turn_id, text="Complete answer", origin=None, agent_id=""):
    return dict(turn_id=turn_id, agent_id=agent_id, message=dict(kind="response", timestamp=STAMP,
                parts=[dict(part_kind="text", content=text)], metadata=dict(origin=origin) if origin else {}))


def resume(turn_id, previous):
    return dict(turn_id=turn_id, message=dict(kind="request", parts=[dict(part_kind="user-prompt", timestamp=STAMP,
                content=[dict(kind="text-content", content=json.dumps(dict(command="resume", turn_id=previous)),
                              metadata=dict(origin="runtime_control"))])]))


def load(path, workspace):
    return SessionFile.load(path, workspace=workspace)


def test_resume_chain_preserves_records_and_audit_ids(tmp_path):
    rows = [row("a", 1, "cancelled"), row("b", 2, "cancelled"), row("c", 3), row("d", 4)]
    messages = {"0": response("a", "partial"), "1": resume("b", "a"), "2": resume("c", "b"),
                "3": response("c"), "4": response("d")}
    path, workspace = legacy(tmp_path, rows, messages)
    store = load(path, workspace)
    assert store.completed_turns == 2
    assert list(store._records) == list(messages)
    assert [(item["id"], item["number"], item["turn_id"]) for item in store._turns.values()] == [
        (item["id"], item["number"], item["turn_id"]) for item in rows]
    assert store.turn("c")["logical_turn_id"] == "a"
    assert store.turn("c")["audit_turn_ids"] == ["a", "b", "c"]
    assert len(store.read_turn("c")) == 4
    assert [item["id"] for item in store.pending_turns(0)] == ["c", "d"]


def test_partial_status_subagent_and_summary_are_not_final_evidence(tmp_path):
    rows = [row("cancel", 1, "cancelled"), row("failed", 2, "failed"), row("summary", 3), row("child", 4)]
    messages = {"0": response("cancel", "partial"), "1": response("failed", origin="execution_status"),
                "2": response("summary", origin="context_summary"), "3": response("child", agent_id="worker")}
    path, workspace = legacy(tmp_path, rows, messages)
    store = load(path, workspace)
    # Successful sealed receipts still confirm quantity, but have no reusable main evidence.
    assert store.completed_turns == 2
    assert store.pending_turns(0) == []
    assert store.metadata["turn_count_incomplete"] is False


def test_missing_receipt_does_not_invent_count(tmp_path):
    path, workspace = legacy(tmp_path, [row("unknown", 1, "unverified")], {"0": response("unknown")},
                             metadata={"perception_consumed": 9})
    store = load(path, workspace)
    assert store.completed_turns == 0
    assert store.metadata["turn_count_incomplete"] is True
    assert store.metadata["legacy_turn_count"]["completed_turns"] == 1


def test_pruned_and_noncontiguous_committed_coverage_do_not_block_new_window(tmp_path):
    rows = [row(str(index), index + 1) for index in range(5)]
    messages = {str(index): response(str(index)) for index in range(1, 5)}
    jobs = {"old": dict(id="old", created_at=STAMP, window=dict(new_turn_ids=["2"], overlap_turn_ids=[],
             start_position=2, end_position=3), event_ids=["2"], timings={"records_committed_at": STAMP}, indexed=False)}
    path, workspace = legacy(tmp_path, rows, messages, jobs=jobs)
    store = load(path, workspace)
    observations = ObservationStore(workspace, window_turns=2, overlap_turns=0)
    observations.bind(store)
    window = observations.window()
    assert window.new_turn_ids == ["1", "3"]
    assert window.end_position == 4
    assert store.job("old")["window"]["end_position"] == 3
    assert store.job("old")["turn_count_migration"]["covered_turn_ids"] == ["2"]
    assert store.pending_jobs() == ["old"]


def test_uncommitted_candidates_superseded_and_repeated_load_is_noop(tmp_path):
    candidate = dict(records=[], reason="retained candidate")
    job = dict(id="pending", created_at=STAMP, window=dict(new_turn_ids=["a"], overlap_turn_ids=[],
               start_position=0, end_position=1), event_ids=["a"], timings={"sealed_at": STAMP},
               result=candidate, indexed=False)
    path, workspace = legacy(tmp_path, [row("a", 1)], {"0": response("a")}, jobs={"pending": job},
                             metadata={"perception_reserved": 1})
    store = load(path, workspace)
    assert store.pending_jobs() == []
    assert store.job("pending")["superseded_by_turn_count_version"] == 2
    assert store.job("pending")["indexed"] is False
    assert store.job("pending")["result"] == candidate
    first = path.read_bytes()
    load(path, workspace)
    assert path.read_bytes() == first


def test_interrupted_correction_retries_atomically(tmp_path, monkeypatch):
    path, workspace = legacy(tmp_path, [row("cancel", 1, "cancelled")])
    original = SessionFile._write_update
    def fail(self, update):
        raise OSError("synthetic interrupted correction")
    monkeypatch.setattr(SessionFile, "_write_update", fail)
    with pytest.raises(OSError, match="synthetic interrupted"):
        load(path, workspace)
    assert json.loads(path.read_text())["updates"][-1]["metadata"]["completed_turns"] == 1
    monkeypatch.setattr(SessionFile, "_write_update", original)
    assert load(path, workspace).completed_turns == 0


def test_scan_invalidates_old_cache_and_exposes_incompleteness(tmp_path):
    path, workspace = legacy(tmp_path, [row("a", 1, "unverified")])
    root = path.parent.parent
    stat = path.stat()
    (root / "index.json").write_text(json.dumps(dict(version=1, sessions={"legacy/model_messages.json": dict(
        signature=dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns), info=dict(session_id="legacy",
        project_id=workspace.project_id, title="Synthetic", saved_at=STAMP, completed_turns=1, status="completed"))})))
    info = SessionFile.scan_info(root)[0].info
    assert info["completed_turns"] == 0
    assert info["turn_count_incomplete"] is True


def test_paused_metadata_links_pruned_resume_receipts(tmp_path):
    updates = [dict(metadata=dict(paused_turn=dict(turn_id="a", request=dict(id="request-1")))),
               dict(metadata=dict(paused_turn=dict(turn_id="b", request=dict(id="request-1"))))]
    path, workspace = legacy(tmp_path, [row("a", 1), row("b", 2)], updates=updates)
    store = load(path, workspace)
    assert store.completed_turns == 1
    assert store.turn("b")["logical_turn_id"] == "a"
    assert store.pending_turns(0) == []


@pytest.mark.asyncio
async def test_evidence_reader_retains_raw_source_ids_across_aliases(tmp_path):
    from redlotus.memory.records import EvidenceReader
    rows = [row("a", 1, "cancelled"), row("b", 2)]
    messages = {"0": response("a", "partial"), "1": resume("b", "a"), "2": response("b")}
    path, workspace = legacy(tmp_path, rows, messages)
    store = load(path, workspace)
    observations = ObservationStore(workspace, window_turns=1, overlap_turns=0)
    observations.bind(store)
    reader = EvidenceReader(None)
    reader.session = store
    packets, sources, refs = await reader.collect(observations.read(["b"]))
    assert sources["a:0:0"]["text"] == "partial"
    assert sources["b:1:0"]["text"] == "Complete answer"
    assert sources["a:0:0"]["source_event_id"] == "a"


def test_compaction_keeps_pending_and_superseded_alias_evidence(tmp_path):
    rows = [row("a", 1, "cancelled"), row("b", 2)]
    messages = {"0": response("a", "partial"), "1": resume("b", "a"), "2": response("b")}
    jobs = {"pending": dict(id="pending", created_at=STAMP, event_ids=["b"], indexed=False,
            window=dict(new_turn_ids=["b"], overlap_turn_ids=[], start_position=0, end_position=2))}
    path, workspace = legacy(tmp_path, rows, messages, jobs=jobs)
    store = load(path, workspace)
    store.compact(keep_turn_ids=set())
    assert set(load(path, workspace)._records) == set(messages)


def test_unlinked_success_and_contradictory_status_stay_incomplete(tmp_path):
    rows = [row("bad", 0), row("status", 1)]
    path, workspace = legacy(tmp_path, rows, {"0": response("status", origin="execution_status")})
    store = load(path, workspace)
    assert store.completed_turns == 0
    assert store.metadata["turn_count_incomplete"] is True


def test_explained_failure_complete_reply_counts_and_resume_does_not_recount(tmp_path):
    rows = [row("a", 1, "cancelled"), row("b", 2, "failed", final_response_completed=True)]
    messages = {"0": resume("b", "a"), "1": response("b", "The task failed; full explanation.")}
    path, workspace = legacy(tmp_path, rows, messages)
    store = load(path, workspace)
    assert store.completed_turns == 1
    store.save_context(store.read_turn("a"), turn_id="a", completed_turn=rows[0])
    assert store.completed_turns == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("done", [False, True])
async def test_committed_legacy_job_recovers_without_reproduction(tmp_path, done):
    from unittest.mock import AsyncMock, Mock
    from redlotus.memory.service import MemoryService
    from redlotus.memory.perception import MemoryJob
    from redlotus.memory.records import WindowManifest, ObservedTurn, PerceptionResult
    rows = [row("a", 1)]
    window = WindowManifest(id="old", project_id="synthetic", new_turn_ids=["a"], start_position=9, end_position=10)
    job = MemoryJob(id="old", events=[ObservedTurn.model_validate(rows[0])], window=window,
                    result=PerceptionResult(records=[], reason="original candidate"), done=done,
                    timings=dict(records_committed_at=STAMP))
    saved = job.model_dump(mode="json", exclude={"events"})
    saved.update(event_ids=["a"], indexed=False)
    path, workspace = legacy(tmp_path, rows, {"0": response("a")}, jobs={"old": saved})
    store = load(path, workspace)
    service = MemoryService.__new__(MemoryService)
    service.session, service.owner_memory_allowed = store, True
    import asyncio
    service._processing = asyncio.Lock()
    service._processor, service._paused = Mock(), Mock(return_value=False)
    service.observations = ObservationStore(workspace, window_turns=1, overlap_turns=0)
    service.observations.bind(store)
    service._execute, service._index_job = AsyncMock(return_value=True), AsyncMock()
    await service.process_pending(recover=True)
    assert service._execute.await_count == int(not done)
    service._index_job.assert_awaited_once()
    recovered = service._index_job.await_args.args[0]
    assert recovered.result.reason == "original candidate"
    service._save_job(recovered)
    assert store.job("old")["turn_count_migration"]["committed"] is True
    service.observations.commit(window)
    assert service.observations.cursor() == 1


def test_torn_correction_tail_is_recovered_and_recomputed(tmp_path, monkeypatch):
    path, workspace = legacy(tmp_path, [row("a", 1, "cancelled")])
    original = SessionFile._write_update
    def torn(self, update):
        with self.path.open("r+b") as stream:
            stream.seek(self._append_offset)
            stream.write(b',\n{"metadata":{"turn_count_version":')
            stream.truncate()
        raise OSError("synthetic torn correction")
    monkeypatch.setattr(SessionFile, "_write_update", torn)
    with pytest.raises(OSError, match="synthetic torn"):
        load(path, workspace)
    monkeypatch.setattr(SessionFile, "_write_update", original)
    store = load(path, workspace)
    assert store.recovered_partial_write
    assert store.completed_turns == 0
    assert store.metadata["turn_count_version"] == 2
    assert len(json.loads(path.read_text())["updates"]) == 2


def test_project_correction_only_visits_matching_project_and_is_idempotent(tmp_path):
    path, workspace = legacy(tmp_path, [row("a", 1, "cancelled")])
    other = path.parent.parent / "other" / path.name
    other.parent.mkdir()
    foreign = json.loads(path.read_text())
    foreign["project_id"] = "other-project"
    other.write_text(json.dumps(foreign))
    original = other.read_bytes()
    first = SessionFile.correct_project_turn_counts(path.parent.parent, workspace=workspace)
    assert first == dict(sessions=1, corrected=1, incomplete=0, completed_turns=0)
    assert other.read_bytes() == original
    assert SessionFile.correct_project_turn_counts(path.parent.parent, workspace=workspace)["corrected"] == 0


def test_resume_alias_without_interrupted_receipt_keeps_original_evidence(tmp_path):
    path, workspace = legacy(tmp_path, [row("b", 1)],
                             {"0": response("a", "partial"), "1": resume("b", "a"), "2": response("b")})
    store = load(path, workspace)
    assert store.completed_turns == 1
    assert store.turn("b")["logical_turn_id"] == "a"
    assert len(store.read_turn("b")) == 3


def test_completion_ordinals_follow_finishing_audit_order(tmp_path):
    rows = [row("a", 1, "cancelled"), row("independent", 2), row("resumed", 3)]
    path, workspace = legacy(tmp_path, rows, {"0": response("independent"),
                             "1": resume("resumed", "a"), "2": response("resumed")})
    store = load(path, workspace)
    assert [event["id"] for event in store.pending_turns(0)] == ["independent", "resumed"]


def test_unsealed_orphan_body_is_unknown_not_zero_history(tmp_path):
    path, workspace = legacy(tmp_path, [], {"0": response("orphan")})
    store = load(path, workspace)
    assert store.completed_turns == 0
    assert store.metadata["turn_count_incomplete"] is True


def test_true_legacy_compacted_four_field_receipts_use_sealed_job_linkage(tmp_path):
    rows = [dict(id="old-event", number=1, session_id="legacy", status="success"),
            dict(id="unlinked", number=2, session_id="legacy", status="success"), row("new", 3)]
    jobs = {"indexed": dict(id="indexed", created_at=STAMP, indexed=True, event_ids=[],
            window=dict(new_turn_ids=["old-event"], overlap_turn_ids=[], start_position=0, end_position=1),
            timings=dict(sealed_at=STAMP, indexed_at=STAMP))}
    path, workspace = legacy(tmp_path, rows, {"0": response("new")}, jobs=jobs)
    store = load(path, workspace)
    assert store.completed_turns == 2
    assert store.metadata["turn_count_incomplete"] is True
    assert store.turn("old-event")["completion_number"] == 1
    assert store.turn("old-event")["evidence_ready"] is False
    assert "turn_id" not in store.turn("old-event")
    assert len(store.read_turn("new")) == 1
    observations = ObservationStore(workspace, window_turns=1, overlap_turns=0)
    observations.bind(store)
    assert observations.window().new_turn_ids == ["new"]


def test_pruned_committed_job_can_restore_event_without_fabricating_raw_identity(tmp_path):
    rows = [dict(id="old-event", number=1, session_id="legacy", status="success")]
    jobs = {"committed": dict(id="committed", created_at=STAMP, indexed=False, event_ids=["old-event"],
            window=dict(new_turn_ids=["old-event"], overlap_turn_ids=[], start_position=0, end_position=1),
            timings=dict(records_committed_at=STAMP))}
    path, workspace = legacy(tmp_path, rows, jobs=jobs)
    store = load(path, workspace)
    observations = ObservationStore(workspace, window_turns=1, overlap_turns=0)
    observations.bind(store)
    event = observations.read(["old-event"])[0]
    assert event.id == "old-event"
    assert event.turn_id == ""
    assert event.origin == "legacy"
    assert "turn_id" not in store.turn("old-event")


def test_migrated_cancelled_turn_becomes_ready_after_successful_resume(tmp_path):
    from pydantic_ai.messages import ModelResponse, TextPart
    details = row("a", 1, "cancelled")
    path, workspace = legacy(tmp_path, [details], {"0": response("a", "partial")})
    store = load(path, workspace)
    assert store.turn("a")["evidence_ready"] is False
    store.save_context([*store.read_turn("a"), ModelResponse(parts=[TextPart("Complete answer")])],
                       turn_id="a", completed_turn=dict(details, status="success"))
    store = load(path, workspace)
    assert store.completed_turns == 1
    assert store.turn("a")["evidence_ready"] is True
    assert [event["id"] for event in store.pending_turns(0)] == ["a"]
    observations = ObservationStore(workspace, window_turns=1, overlap_turns=0)
    observations.bind(store)
    assert observations.window().new_turn_ids == ["a"]


@pytest.mark.parametrize("compact", [False, True])
def test_completed_alias_restores_counted_representative_evidence_without_recount(tmp_path, compact):
    from pydantic_ai.messages import ModelResponse, TextPart
    rows = [row("a", 1, "cancelled"), row("b", 2)]
    path, workspace = legacy(tmp_path, rows, {"0": resume("b", "a")})
    store = load(path, workspace)
    assert store.turn("b")["evidence_ready"] is False
    if compact:
        store.compact(keep_turn_ids=set())
    store.save_context([ModelResponse(parts=[TextPart("Complete answer")])],
                       turn_id="a", completed_turn=dict(rows[0], status="success"))
    store = load(path, workspace)
    assert store.completed_turns == 1
    assert store.turn("b")["completion_number"] == 1
    assert store.turn("b")["evidence_ready"] is True
    assert store.turn("a")["completion_number"] is None
    assert [event["id"] for event in store.pending_turns(0)] == ["b"]
    observations = ObservationStore(workspace, window_turns=1, overlap_turns=0)
    observations.bind(store)
    window = observations.window()
    assert window.new_turn_ids == ["b"]
    assert window.reference_ids == []
    assert len(observations.read(window.new_turn_ids)) == 1
    import asyncio
    from redlotus.memory.records import EvidenceReader
    reader = EvidenceReader(None)
    reader.session = store
    packets, sources, _ = asyncio.run(reader.collect(observations.read(window.new_turn_ids)))
    assert sources["a:u0"]["text"] == "same request"
    assert "same request" in packets[0]["user_inputs"]
    if compact:
        assert "b:u0" not in sources
    observations.reserve(window)
    observations.commit(window)
    assert observations.window() is None
    assert load(path, workspace).completed_turns == 1


@pytest.mark.parametrize("status,completed", [("success", True), ("failed", True), ("cancelled", False),
                                              ("failed", False), ("needs_input", False), ("running", False)])
def test_only_atomic_final_checkpoint_counts(tmp_path, status, completed):
    from pydantic_ai.messages import ModelResponse, TextPart
    workspace = WorkspaceContext.from_path(tmp_path)
    store = SessionFile.create(tmp_path / "sessions", workspace.project_id, workspace=workspace)
    event = dict(row("event", 1, status), session_id=store.session_id, turn_id="request")
    store.save_context([ModelResponse(parts=[TextPart("response")])], turn_id="request",
                       completed_turn=event if completed else None)
    store.finish_turn("event", event)
    assert store.completed_turns == int(completed)
    reloaded = load(store.path, workspace)
    assert reloaded.completed_turns == int(completed)
    assert bool(reloaded.pending_turns(0)) is completed


def test_final_checkpoint_failed_write_does_not_publish_count(tmp_path, monkeypatch):
    from pydantic_ai.messages import ModelResponse, TextPart
    workspace = WorkspaceContext.from_path(tmp_path)
    store = SessionFile.create(tmp_path / "sessions", workspace.project_id, workspace=workspace)
    store.correct_turn_counts()
    saved = store.path.read_bytes()
    monkeypatch.setattr(store, "_write_update", lambda update: (_ for _ in ()).throw(OSError("synthetic")))
    with pytest.raises(OSError):
        store.save_context([ModelResponse(parts=[TextPart("final")])], turn_id="a", completed_turn=row("a", 1))
    assert store.path.read_bytes() == saved
    assert load(store.path, workspace).completed_turns == 0


def test_status_only_turn_never_recounts_during_later_projection(tmp_path):
    from pydantic_ai.messages import ModelResponse, TextPart
    workspace = WorkspaceContext.from_path(tmp_path)
    store = SessionFile.create(tmp_path / "sessions", workspace.project_id, workspace=workspace)
    store.save_context([], turn_id="interrupted")
    store.finish_turn("interrupted", row("interrupted", 1, "success"))
    store.save_context([ModelResponse(parts=[TextPart("final")])], turn_id="completed",
                       completed_turn=row("completed", 2))
    assert store.completed_turns == 1

@pytest.mark.asyncio
async def test_alias_user_sources_keep_original_identity_and_origin(tmp_path):
    from redlotus.memory.records import EvidenceReader
    rows = [dict(row("a", 1, "cancelled"), origin="user"), dict(row("b", 2), origin="user")]
    path, workspace = legacy(tmp_path, rows, {"0": resume("b", "a"), "1": response("b")})
    store = load(path, workspace)
    observations = ObservationStore(workspace, window_turns=1, overlap_turns=0)
    observations.bind(store)
    reader = EvidenceReader(None)
    reader.session = store
    _, sources, _ = await reader.collect(observations.read(["b"]))
    assert sources["a:u0"]["source_event_id"] == "a"
    assert sources["a:u0"]["verified"] is True
    assert sources["b:u0"]["source_event_id"] == "b"


def test_recovered_noncontiguous_committed_window_does_not_consume_unprocessed_turns(tmp_path):
    from redlotus.memory.records import WindowManifest
    rows = [row(str(index), index + 1) for index in range(4)]
    window = dict(id="old", project_id="synthetic", new_turn_ids=["2"], overlap_turn_ids=[], start_position=8, end_position=9)
    jobs = {"old": dict(id="old", created_at=STAMP, window=window, event_ids=["2"],
                        timings={"records_committed_at": STAMP}, indexed=False)}
    path, workspace = legacy(tmp_path, rows, {str(i): response(str(i)) for i in range(4)}, jobs=jobs)
    store = load(path, workspace)
    observations = ObservationStore(workspace, window_turns=2, overlap_turns=0)
    observations.bind(store)
    observations.commit(WindowManifest.model_validate(window))
    assert observations.window().new_turn_ids == ["0", "1"]


def test_equal_message_bytes_in_independent_turns_keep_distinct_audit_records(tmp_path):
    from pydantic_ai.messages import ModelResponse, TextPart
    workspace = WorkspaceContext.from_path(tmp_path)
    store = SessionFile.create(tmp_path / "sessions", workspace.project_id, workspace=workspace)
    first = ModelResponse(parts=[TextPart("identical response")])
    second = first.model_copy(deep=True) if hasattr(first, "model_copy") else __import__("copy").deepcopy(first)
    store.save_context([first], turn_id="a", completed_turn=row("a", 1))
    store.save_context([first, second], turn_id="b", completed_turn=row("b", 2))
    assert [record["turn_id"] for record in store._records.values()] == ["a", "b"]
    assert len(store.read_turn("b")) == 1

@pytest.mark.asyncio
async def test_pruned_prefix_retains_original_message_source_offset(tmp_path):
    from redlotus.memory.records import EvidenceReader
    path, workspace = legacy(tmp_path, [dict(row("a", 1), origin="user")],
                             {"0": response("a", "prefix"), "1": response("a", "final")})
    store = load(path, workspace)
    store._records.pop("0")
    store.compact(keep_turn_ids={"a"})
    store = load(path, workspace)
    reader = EvidenceReader(None)
    reader.session = store
    observations = ObservationStore(workspace, window_turns=1, overlap_turns=0)
    observations.bind(store)
    _, sources, _ = await reader.collect(observations.read(["a"]))
    assert sources["a:1:0"]["text"] == "final"

@pytest.mark.parametrize("partial", [False, True])
def test_project_correction_recovers_only_its_partial_tail(tmp_path, partial):
    path, workspace = legacy(tmp_path, [row("a", 1, "cancelled")])
    if partial:
        text = path.read_text()
        path.write_text(text[:text.rfind("]")] + ', {"metadata":')
    result = SessionFile.correct_project_turn_counts(path.parent.parent, workspace=workspace)
    assert result["completed_turns"] == 0
    assert load(path, workspace).completed_turns == 0


def test_compaction_preserves_next_audit_id_and_source_offset(tmp_path):
    from pydantic_ai.messages import ModelResponse, TextPart
    path, workspace = legacy(tmp_path, [row("a", 1)],
                             {"0": response("a", "prefix"), "1": response("a", "final"), "2": response("removed")})
    store = load(path, workspace)
    store._records.pop("0")
    store._records.pop("2")
    store.compact(keep_turn_ids={"a"})
    store.save_context([ModelResponse(parts=[TextPart("new")])], turn_id="a")
    assert list(store._records) == ["1", "3"]
    assert store._records["3"]["source_index"] == 2


def test_resuming_older_audit_turn_appends_completion_ordinal(tmp_path):
    from pydantic_ai.messages import ModelResponse, TextPart
    path, workspace = legacy(tmp_path, [row("a", 1, "cancelled"), row("b", 2)], {"0": response("b")})
    store = load(path, workspace)
    store.save_context([ModelResponse(parts=[TextPart("resumed final")])], turn_id="a", completed_turn=row("a", 1))
    assert store.turn("b")["completion_number"] == 1
    assert store.turn("a")["completion_number"] == 2
    assert store.turn("a")["number"] == 1


def test_missing_legacy_rows_report_incomplete_confirmed_count(tmp_path):
    path, workspace = legacy(tmp_path, [])
    data = json.loads(path.read_text())
    data["updates"][0]["metadata"]["completed_turns"] = 7
    path.write_text(json.dumps(data))
    store = load(path, workspace)
    assert store.completed_turns == 0
    assert store.metadata["turn_count_incomplete"] is True

@pytest.mark.asyncio
async def test_alias_references_and_current_unsaved_inputs_remain_available(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from redlotus.memory.records import EvidenceReader
    rows = [dict(row("a", 1, "cancelled"), origin="user"), dict(row("b", 2), origin="user")]
    rows[0]["reference_ids"] = ["old-ref"]
    path, workspace = legacy(tmp_path, rows, {"0": resume("b", "a"), "1": response("b")})
    store = load(path, workspace)
    observations = ObservationStore(workspace, window_turns=1, overlap_turns=0)
    observations.bind(store)
    event = observations.read(["b"])[0]
    event.user_inputs.append("new urgent input")
    reader = EvidenceReader(SimpleNamespace(load=lambda key: SimpleNamespace(id=key), parse=AsyncMock(side_effect=lambda row: row)))
    reader.session = store
    packets, sources, refs = await reader.collect([event])
    assert [ref.id for ref in refs] == ["old-ref"]
    assert sources["b:u1"]["text"] == "new urgent input"
    assert packets[0]["reference_ids"] == ["old-ref"]

@pytest.mark.parametrize("later_committed", [False, True])
def test_reopened_evidence_does_not_repeat_later_reserved_or_committed_window(tmp_path, later_committed):
    from pydantic_ai.messages import ModelResponse, TextPart
    rows = [row("a", 1, "cancelled"), row("b", 2), row("c", 3)]
    path, workspace = legacy(tmp_path, rows, {"0": resume("b", "a"), "1": response("c")})
    store = load(path, workspace)
    observations = ObservationStore(workspace, window_turns=1, overlap_turns=0)
    observations.bind(store)
    later = observations.window()
    assert later.new_turn_ids == ["c"]
    store.update(jobs={later.id: dict(id=later.id, created_at=STAMP, indexed=False, window=later.model_dump())})
    observations.reserve(later)
    if later_committed:
        observations.commit(later)
    store.save_context([ModelResponse(parts=[TextPart("restored")])], turn_id="a", completed_turn=rows[0])
    observations.commit(later)
    restored = observations.window()
    assert restored.new_turn_ids == ["b"]
    observations.commit(restored)
    assert observations.window() is None
    assert store.completed_turns == 2

@pytest.mark.asyncio
@pytest.mark.parametrize("incomplete", [False, True, None])
async def test_memory_snapshot_exposes_incomplete_history(incomplete):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from redlotus.memory.service import MemoryService
    service = MemoryService.__new__(MemoryService)
    service.session = None if incomplete is None else SimpleNamespace(completed_turns=2, metadata={"turn_count_incomplete": incomplete})
    service.store = SimpleNamespace(snapshot=AsyncMock(return_value={}))
    service.observations = SimpleNamespace(cursor=lambda: 0, window_turns=2, overlap_turns=0)
    service.last_error = ""
    service._processor = lambda: {}
    result = await service.short_term_snapshot()
    assert result["turn_count_incomplete"] is bool(incomplete)
