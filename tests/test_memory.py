"""Version/publication fault checks; live memory acceptance is separate."""

import asyncio
import json
import os
from pathlib import Path
from concurrent.futures import Future
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", [None, "save", "cancel", "compress", "again", "other", "auth", "no_checkpoint"])
async def test_first_perception_capacity_recovery_preserves_evidence_and_output(monkeypatch, stream, failure, prepared):

    from pydantic_ai import capture_run_messages
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.usage import UsageLimits

    from redlotus.core import gateway, history
    from redlotus.memory.perception import PerceptionTiming
    from redlotus.runtime.network import ModelTarget

    calls, saved, compressed, timings = [], [], [], []
    error = ModelHTTPError(status_code=401 if failure == "auth" else 400, model_name="fixture", body={
        "message": "Bad tool schema" if failure == "other" else "This model's maximum context length is 100 tokens. However, you requested 120 tokens (80 in the messages, 40 in the completion). Please reduce the length of the messages or completion.",
        "type": "invalid_request_error", "param": None, "code": "invalid_request_error",
    })
    async def respond(messages, info):
        calls.append(list(messages))
        assert info.model_settings["max_tokens"] == 40
        assert info.instructions == "SYSTEM_FIXED"
        if len(calls) == 1 or failure == "again":
            raise error
        assert saved and messages == saved[0]
        return ModelResponse([TextPart("complete")])
    async def chunks(messages, info):
        yield (await respond(messages, info)).parts[0].content
    async def compress(**kwargs):
        compressed.append(json.loads(kwargs["user_content"])["transcript"])
        assert "RAW_WINDOW" in compressed[-1] and "SYSTEM_FIXED" not in compressed[-1]
        if failure == "compress":
            raise ValueError("compress failed")
        return "## Facts\nWindow facts retained.\n## Next\nVerify sources."
    async def persist(messages):
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "save":
            raise OSError("save failed")
        saved.append(list(messages))
    model = FunctionModel(respond, stream_function=chunks, settings={"max_tokens": 40})
    target = ModelTarget("fixture", "fixture", None, "", json.dumps({
        "context": {"max_context_windows": 100, "auto_compress_ratio": .9, "compress_head_turns": 0, "compress_tail_turns": 0},
        "settings": {"max_tokens": 40}, "limits": {"max_files": 1, "max_file_bytes": 1000, "reference_download_timeout_seconds": 2},
    }), 5)
    monkeypatch.setattr(gateway, "create_model", lambda *args: model)
    monkeypatch.setattr(history, "_call_compressor_llm", compress)
    monkeypatch.setattr(history, "load_prompt", lambda name: "## Facts\n## Next")
    monkeypatch.setattr(history.logger, "info", lambda *args: None)
    agent = gateway.create_agent(target, instructions="SYSTEM_FIXED", role="perception", usage_category="auxiliary",
        persist_context=None if failure == "no_checkpoint" else persist, capabilities=[PerceptionTiming(timings.append)])
    evidence = [ModelRequest([UserPromptPart("RAW_EVIDENCE")])] if prepared else []
    async def run():
        if stream:
            result = await gateway.AgentRunner().run(agent=agent, prompt="RAW_WINDOW", message_history=evidence, usage_limits=UsageLimits())
            return result.output
        return (await agent.run("RAW_WINDOW", message_history=evidence)).output
    with capture_run_messages() as messages:
        if failure:
            expected = {"save": OSError, "cancel": asyncio.CancelledError, "compress": ValueError}.get(failure, ModelHTTPError)
            with pytest.raises(expected):
                await run()
        else:
            assert await run() == "complete"
    retries = failure in (None, "again")
    assert len(calls) == len(timings) == (2 if retries else 1)
    assert bool(compressed) == (failure not in ("other", "auth", "no_checkpoint"))
    assert "RAW_WINDOW" in str(calls[0])
    if not saved:
        assert "RAW_WINDOW" in str(messages)
    else:
        assert saved[0][0].instructions == "SYSTEM_FIXED"


@pytest.mark.parametrize("failure", [None, "minimum", "other"])
@pytest.mark.parametrize("merged", [False, True])
async def test_compressor_capacity_reduces_only_complete_prefix(monkeypatch, failure, merged):
    from pydantic_ai._agent_graph import _clean_message_history
    from redlotus.core import history
    from redlotus.sessions.context import ChatHistory

    calls = []
    original = [ModelRequest([UserPromptPart("EVENT_A")], instructions="SYSTEM_FIXED"),
                ModelResponse([ToolCallPart("read_file", {}, "pair")]),
                ModelRequest([ToolReturnPart("read_file", "SOURCE_A", "pair")]),
                ModelResponse([TextPart("RESULT_A")])]
    for label in ("B", "C", "D"):
        original.extend([ModelRequest([UserPromptPart("EVENT_" + label)]), ModelResponse([TextPart("RESULT_" + label)])])
    if merged:
        original = _clean_message_history([ModelRequest([UserPromptPart("EVENT_" + label)], instructions="SYSTEM_FIXED") for label in "ABCD"])
    source = ChatHistory()
    source.set_messages(original)
    async def compress(**kwargs):
        calls.append(json.loads(kwargs["user_content"])["transcript"])
        if len(calls) == 1 or failure:
            raise ModelHTTPError(400, "fixture", {"message": "Bad schema" if failure == "other" else "This model's maximum context length is 100 tokens."})
        return "## Facts\nSOURCE_A\n## Next\nKeep remaining events."
    monkeypatch.setattr(history, "_call_compressor_llm", compress)
    monkeypatch.setattr(history, "load_prompt", lambda name: "## Facts\n## Next")
    monkeypatch.setattr(history.logger, "info", lambda *args: None)
    pending = history.prepare_compression(source, role="worker", force=True,
        context={"max_context_windows": 100, "auto_compress_ratio": .8, "compress_head_turns": 0, "compress_tail_turns": 1})
    if failure:
        with pytest.raises(ModelHTTPError):
            await pending
    else:
        candidate = await pending
        assert candidate.messages[0].instructions == "SYSTEM_FIXED"
        assert ([part.content for message in candidate.messages[1:] for part in message.parts] == ["EVENT_B", "EVENT_C", "EVENT_D"] if merged else candidate.messages[1:] == original[4:])
        assert "EVENT_B" not in calls[1] and all(text in calls[1] for text in (("EVENT_A",) if merged else ("EVENT_A", "SOURCE_A", "RESULT_A")))
    assert len(calls) == (1 if failure == "other" else 2) and source.messages == original


def test_perception_window_keeps_complete_evidence_units_and_manifest():
    from pydantic_ai import ImageUrl
    from redlotus.prompts.prompt import window_prompt_content

    payload = {"session_id": "session", "new_turn_ids": ["A", "B"], "evidence_ids": ["A:u0", "B:t0"],
               "events": [{"id": "A", "user_inputs": ["original A"], "image_urls": [{"url": "https://example.test/a.png"}]},
                          {"id": "B", "operations": [{"id": "B:t0", "text": "original B"}]}]}
    original = json.loads(json.dumps(payload))
    messages = window_prompt_content(payload)
    assert len(messages) == 3
    assert json.loads(messages[0].parts[0].content[0])["events"] == [payload["events"][0]]
    assert isinstance(messages[0].parts[0].content[1], ImageUrl)
    assert json.loads(messages[1].parts[0].content[0])["events"][0]["operations"][0]["text"] == "original B"
    manifest = json.loads(messages[2].parts[0].content[0])
    assert manifest["evidence_ids"] == ["A:u0", "B:t0"] and manifest["new_turn_ids"] == ["A", "B"]
    assert "events" not in manifest and payload == original


async def test_memory_control_wait_uses_config_without_cancelling_production(tmp_path):

    from redlotus.core.system import AgentSystem
    from redlotus.memory.service import MemoryService

    (tmp_path / "config.json").write_text(json.dumps({
        "memory_perception": {"quiescence_wait_timeout_seconds": .01},
    }), encoding="utf-8")
    pending = Future()
    memory, system = object.__new__(MemoryService), object.__new__(AgentSystem)
    memory._background = SimpleNamespace(_future=pending)
    memory._processing = asyncio.Lock()
    system._current_turn, system._memory = None, memory
    try:
        async with asyncio.timeout(.2):
            assert not await system.wait_for_memory_quiescent()
        assert not pending.cancelled()
    finally:
        pending.set_result(None)
        await asyncio.sleep(0)


async def test_non_owner_turns_count_without_reading_or_producing_personal_memory(tmp_path):

    from redlotus.memory.records import ObservationStore
    from redlotus.memory.service import MemoryService
    from redlotus.runtime.resources import WorkspaceContext
    from redlotus.sessions.storage import SessionFile

    (tmp_path / "config.json").write_text(json.dumps({
        "memory_perception": {"window_turns": 20, "overlap_turns": 3},
        "storage": {"project_dir": ".redlotus"},
    }), encoding="utf-8")
    workspace = WorkspaceContext.from_path(tmp_path)
    service = object.__new__(MemoryService)
    service.owner_memory_allowed, service.current = False, None
    service.session = SessionFile.create(tmp_path / "sessions", workspace.project_id, session_id="channel")
    service.observations = ObservationStore(workspace)
    service.observations.bind(service.session)
    # No long_term, store or factory exists: non-owner input must never use them.
    event = await service.begin_turn("channel", "input-id", "Public channel text")
    await service.finish_turn(event, status="success", user_inputs=["Public channel text"], evidence_paths=[])
    assert service.session.completed_turns == 1
    assert service.session.turn(event.id)["turn_id"] == "input-id"
    assert not service.session.pending_jobs()


@pytest.mark.parametrize("switch_at", ["queued", "processing", None])
async def test_background_perception_keeps_its_scheduled_session(monkeypatch, tmp_path, switch_at):
    import threading

    from redlotus.memory import service as module
    from redlotus.runtime.resources import WorkspaceContext

    service = object.__new__(module.MemoryService)
    old = SimpleNamespace(session_id="old", completed_turns=20, pending_jobs=lambda: ["old-job"])
    new = SimpleNamespace(session_id="new", completed_turns=40, pending_jobs=lambda: ["new-job"])
    service.workspace = WorkspaceContext.from_path(tmp_path)
    service.owner_memory_allowed, service.last_error = True, ""
    service.observations, service.evidence = SimpleNamespace(bind=lambda storage: None), SimpleNamespace()
    service._schedule_lock, service._background_running = threading.Lock(), False
    service._targets = {"old-job": "old-target"}
    service._perception_factory = SimpleNamespace(
        start_background=Mock(return_value=SimpleNamespace(_future=Future())), create_registry=lambda: None,
        create_agent=None,
    )
    service.bind_session(old)
    monkeypatch.setattr(service, "seal_windows", lambda: None)
    processed = []
    producer = SimpleNamespace(last_error="old error", close=AsyncMock())
    producer.bind_session = lambda storage: setattr(producer, "session", storage)

    def switch_session():
        service.unbind_session()
        service.bind_session(new)
        service._pending_end, service._targets = 40, {"new-job": "new-target"}
        service.last_error, service._background_running = "new error", True

    async def process_pending(*, through):
        processed.append((producer.session, through, producer._targets))
        if switch_at == "processing":
            switch_session()

    producer.process_pending = process_pending
    monkeypatch.setattr(module, "MemoryService", lambda **kwargs: producer)
    monkeypatch.setattr(module, "MemoryPerception", lambda *args, **kwargs: None)
    service.schedule_processing()
    spec, run = service._perception_factory.start_background.call_args.args
    assert spec.session_id == "old"
    if switch_at == "queued":
        switch_session()
    await run()
    assert processed == ([] if switch_at == "queued" else [(old, 20, {"old-job": "old-target"})])
    if switch_at:
        assert service.last_error == "new error" and service._background_running
        assert service._pending_end == 40 and service._targets == {"new-job": "new-target"}
    else:
        assert service.last_error == "old error" and not service._background_running
    if processed:
        producer.close.assert_awaited_once()


@pytest.mark.parametrize("invalidate", [False, True])
async def test_session_switch_waits_for_old_agents_before_releasing_storage(invalidate):

    from redlotus.core.system import AgentSystem

    system = object.__new__(AgentSystem)
    old = SimpleNamespace(session_id="old", release_use=Mock())
    new = SimpleNamespace(session_id="new", project_id="project", acquire_use=Mock(), release_use=Mock())
    system._session_file, system.workspace = old, SimpleNamespace(project_id="project")
    system._session, system._shutdown_done = SimpleNamespace(generation=0), False
    system.registry = SimpleNamespace(ensure_agent=AsyncMock())
    system._memory = SimpleNamespace(unbind_session=Mock(), reset_injection_snapshot=Mock(), bind_session=Mock())
    system._orchestrator = SimpleNamespace(set_session_key=Mock())

    async def cancel(identity):
        assert identity == "old" and system._session_file is old
        old.release_use.assert_not_called()
        if invalidate:
            system._session.generation += 1

    system._factory = SimpleNamespace(cancel_session=AsyncMock(side_effect=cancel))
    if invalidate:
        with pytest.raises(ValueError, match="加载已取消"):
            await system.bind_session("new", storage=new, generation=0)
        assert system._session_file is old
        old.release_use.assert_not_called()
        new.release_use.assert_called_once()
    else:
        await system.bind_session("new", storage=new, generation=0)
        assert system._session_file is new
        old.release_use.assert_called_once()
    system._factory.cancel_session.assert_awaited_once_with("old")


@pytest.fixture
def publication(tmp_path, monkeypatch):
    from redlotus.memory import records
    from redlotus.memory import service as module
    from redlotus.memory.perception import MemoryJob
    from redlotus.sessions.storage import SessionFile

    (tmp_path / "config.json").write_text('{"storage":{"file_lock_timeout_seconds":0}}', encoding="utf-8")
    memory = records.LongTermMemory(tmp_path / "core-memory")
    original = memory.read()
    row = records.MemoryRecord(id="A", project_id="isolated", scope="global", kind="requested",
                               projection="profile", goal="Preference", content="Fixture tea", origin="explicit",
                               last_change_id="publication:0")
    event = records.ObservedTurn(id="event", project_id="isolated", session_id="fixture", turn_id="turn", origin="migration")
    draft = records.MemoryDraft(target_id="A", scope="global", kind="requested", projection="profile",
                                goal=row.goal, content=row.content, source_turn_ids=[event.id], search_id="search")
    job = MemoryJob(id="publication", request="Remember the fixture preference", scope="global", events=[event],
                    core_snapshot=original, result=records.PerceptionResult(records=[draft], reason="fixture", request_authorized=True),
                    searches=[dict(id="search", scope="global", revision="fixture", retrieval_error="")])
    formal, writes, model_calls = {}, [], []

    def save(rows):
        writes.append([record.id for record in rows])
        formal.update((record.id, record.model_copy(deep=True)) for record in rows)

    async def unexpected_production(*args):
        model_calls.append(True)
        raise AssertionError("A projection retry must not produce another model candidate")

    service = object.__new__(module.MemoryService)
    service.long_term, service.last_error, service.current = memory, "", None
    service.session = SessionFile.create(tmp_path / "sessions", "isolated", session_id="fixture")
    service.observations = SimpleNamespace(read=lambda identities: [event])
    service.store = SimpleNamespace(get=lambda identity: formal[identity], save=save, revision=lambda scope: "fixture",
                                    materialize=lambda *args: row)
    monkeypatch.setattr(service, "_route", lambda: "fixture")
    monkeypatch.setattr(module.logger, "error", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "produce_job", unexpected_production)
    return SimpleNamespace(service=service, job=job, row=row, formal=formal, writes=writes,
                           model_calls=model_calls, memory=memory, original=original, records=records)


@pytest.mark.parametrize("transition", ["load", "clear", "project"])
async def test_direct_memory_retry_blocks_session_changes(publication, tmp_path, transition):

    from redlotus.ui.console import AgentCliController

    service = publication.service
    service.owner_memory_allowed, service._background = True, None
    service._processing, service._processor, service._paused = asyncio.Lock(), Mock(), lambda: False
    service._save_job(publication.job)
    started, finish = asyncio.Event(), asyncio.Event()

    async def execute(*args, **kwargs):
        started.set()
        await finish.wait()
        return False

    service._execute = execute
    pending = asyncio.create_task(service.process_pending(recover=True))
    system = SimpleNamespace(_memory=service, reset_session=AsyncMock(), switch_workspace=AsyncMock(),
                             last_rejected_input="keep")
    controller, history, restore = AgentCliController(system), Mock(), AsyncMock()
    try:
        await asyncio.wait_for(started.wait(), 2)
        with pytest.raises(ValueError, match="记忆"):
            await controller.reset_session(history, workspace=tmp_path if transition == "project" else None,
                                           restore=restore if transition == "load" else None)
        assert not await service.wait_idle(timeout=0)
        assert controller._ready.is_set() and system.last_rejected_input == "keep"
        system.reset_session.assert_not_awaited()
        system.switch_workspace.assert_not_awaited()
        restore.assert_not_awaited()
        history.reset.assert_not_called()
    finally:
        finish.set()
        await pending


@pytest.mark.parametrize("first_finished", [0, 1])
@pytest.mark.parametrize("picker", [False, True])
async def test_memory_retry_cannot_enter_during_session_transition(monkeypatch, first_finished, picker):

    from redlotus.ui.cli_commands import SlashCommands
    from redlotus.ui.console import AgentCliController

    memory = SimpleNamespace(short_term_snapshot=None, process_pending=AsyncMock(), _processing=asyncio.Lock())
    controller = AgentCliController(SimpleNamespace(_memory=memory))
    entered, finish = [asyncio.Event(), asyncio.Event()], [asyncio.Event(), asyncio.Event()]

    async def restore(index):
        entered[index].set()
        await finish[index].wait()

    async def choose(**kwargs):
        return await controller.reset_session(None, restore=lambda: restore(0))

    monkeypatch.setattr(controller, "_choose_current_workspace", choose)
    tasks = [asyncio.create_task(controller.enter_current_workspace() if picker else choose())]
    try:
        await asyncio.wait_for(entered[0].wait(), 2)
        tasks.append(asyncio.create_task(controller.reset_session(None, restore=lambda: restore(1))))
        await asyncio.wait_for(entered[1].wait(), 2)
        finish[first_finished].set()
        await tasks[first_finished]
        assert controller.is_transitioning
        with pytest.raises(ValueError, match="会话"):
            await SlashCommands(controller, None, "/STM retry").run()
        memory.process_pending.assert_not_awaited()
    finally:
        for event in finish:
            event.set()
        await asyncio.gather(*tasks)
    assert not controller.is_transitioning


async def test_database_failure_cannot_publish_core_memory(publication, monkeypatch):
    p = publication

    def failed_save(rows):
        raise OSError("isolated database failure")

    monkeypatch.setattr(p.service.store, "save", failed_save)
    with pytest.raises(OSError, match="database failure"):
        await p.service._apply(p.job)
    assert p.memory.read() == p.original and not p.formal
    assert not p.job.done


@pytest.mark.parametrize("save_fails", [False, True])
async def test_perception_compaction_saves_before_adoption_and_binds_usage(publication, monkeypatch, tmp_path, save_fails):

    from pydantic_ai.usage import RequestUsage

    from redlotus.core import history
    from redlotus.core.gateway import RequestPolicy
    from redlotus.memory.perception import produce_job
    from redlotus.runtime.resources import WorkspaceContext
    from redlotus.sessions.context import current_usage_recorder
    from redlotus.sessions.storage import SessionFile

    p = publication
    p.job.request, p.job.result = None, None
    p.job.perception_config = {"model_role": "worker"}
    p.service.workspace = WorkspaceContext.from_path(tmp_path)
    p.service.evidence = SimpleNamespace(collect=AsyncMock(return_value=([], {}, [])))
    p.service.store.all = lambda **kwargs: []
    monkeypatch.setattr(p.service, "_cleared_at", lambda scope: "")
    target = SimpleNamespace(name="fixture", options={"context": {"auto_compress_ratio": .9},
        "limits": {"max_files": 1, "max_file_bytes": 1000, "reference_download_timeout_seconds": 2}})
    p.service._targets = {p.job.id: target}
    storage, requests, recorders = p.service.session, [], []
    candidate = [ModelRequest([UserPromptPart("retained evidence")], instructions="SYSTEM_FIXED")]
    monkeypatch.setattr(history, "compact_request_messages", AsyncMock(return_value=candidate))
    role_file = storage.role_file("perception")
    if save_fails:
        def fail(*args, **kwargs):
            raise OSError("perception checkpoint unavailable")
        monkeypatch.setattr(role_file, "save_context", fail)

    async def produce(payload, references, **callbacks):
        recorder = current_usage_recorder()
        assert recorder is not None
        recorders.append(recorder)
        original = [ModelRequest([UserPromptPart("original evidence")], instructions="SYSTEM_FIXED")]
        request = SimpleNamespace(messages=original, model_request_parameters=SimpleNamespace(
            function_tools=[], output_tools=[], instruction_parts=[]))
        policy = RequestPolicy("perception", target, SimpleNamespace(settings={}), usage_category="auxiliary",
                               persist_context=callbacks["persist_context"])
        try:
            await policy.before_model_request(None, request)
        except OSError:
            assert request.messages is original
            raise
        assert role_file.model_messages(agent_id=p.job.id) == candidate
        requests.append(request.messages)
        return p.records.PerceptionResult(records=[], reason="No new fact")

    p.service.perception = SimpleNamespace(produce=produce)
    if save_fails:
        with pytest.raises(OSError, match="checkpoint unavailable"):
            await produce_job(p.service, p.job)
        assert not requests and p.job.result is None
    else:
        await produce_job(p.service, p.job)
        assert requests == [candidate]
    assert current_usage_recorder() is None
    p.service.session = SessionFile.create(tmp_path / "other", "isolated", session_id="other")
    receipt = ModelResponse([TextPart("summary")], usage=RequestUsage(input_tokens=23, output_tokens=5),
                            metadata={"usage_category": "auxiliary"})
    await recorders[0]([receipt], role="compressor", invocation="compression", cancelling=False)
    rows = storage.usage_responses()
    assert len(rows) == 1 and rows[0]["role"] == "compressor" and rows[0]["usage"]["input_tokens"] == 23
    assert not p.service.session.usage_responses()


@pytest.mark.parametrize("newer", [False, True])
async def test_projection_retry_restores_stage_and_never_replays_model_or_database(publication, monkeypatch, newer):

    p = publication
    replace = os.replace

    def failed_projection(source, target):
        if Path(target) == p.memory.path:
            raise OSError("isolated projection failure")
        return replace(source, target)

    monkeypatch.setattr(os, "replace", failed_projection)
    assert not await p.service._produce_and_apply(p.job)
    assert p.formal["A"].version == 1 and p.memory.read() == p.original
    assert p.job.timings.get("records_committed_at") and not p.job.done
    restored = p.service._job(p.job)
    assert restored.timings["records_committed_at"] == p.job.timings["records_committed_at"]
    if newer:
        p.formal["A"] = p.row.model_copy(update={"version": 2, "state": "deleted", "last_change_id": "newer"})
    p.memory.path.write_text(p.original + "\nUser's later note\n", encoding="utf-8")
    monkeypatch.setattr(os, "replace", replace)
    assert await p.service._produce_and_apply(restored)
    assert not p.model_calls and p.writes == [["A"]]
    assert "User's later note" in p.memory.read()
    assert ("Fixture tea" in p.memory.read()) is not newer
    assert restored.records == ([] if newer else ["A"])


async def test_explicit_candidate_cannot_overwrite_later_manual_core_edit(publication):
    p = publication
    previous = p.row.model_copy(update={"content": "Original preference", "last_change_id": "before"})
    original = p.original.replace("## 用户画像\n", "## 用户画像\n\n<!-- memory:A -->\nOriginal preference\n<!-- /memory -->\n")
    p.row.version = 2
    p.job.bases = {"A": previous}
    p.job = p.job.model_copy(update={"core_snapshot": original})
    edited = original.replace("Original preference", "Manual preference")
    p.memory.path.write_text(edited, encoding="utf-8")
    with pytest.raises(ValueError, match="[Cc]ore memory changed"):
        await p.service._apply(p.job)
    assert not p.writes and p.memory.read() == edited


def test_injection_excludes_uncommitted_or_superseded_managed_blocks(publication):
    p = publication
    body = p.original + "\nHandwritten note\n<!-- memory:A version:2 -->\nUncommitted preference\n<!-- /memory -->\n"
    p.memory.path.write_text(body, encoding="utf-8")
    injection = p.memory.get_injection([p.row])
    assert "Uncommitted preference" not in injection and "Handwritten note" in injection
    assert p.memory.read() == body


async def test_candidate_cannot_replace_text_inside_another_managed_record(publication):
    p = publication
    original = p.original.replace("## 用户画像\n", "## 用户画像\n\n<!-- memory:B version:1 -->\nOther record\n<!-- /memory -->\n")
    p.job.core_snapshot = original
    p.job.result.records[0].core_old_text = "Other record"
    p.memory.path.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match="managed record"):
        await p.service._apply(p.job)
    assert not p.formal and p.memory.read() == original


async def test_projection_failure_receipt_reports_formal_commit(publication, monkeypatch):

    p = publication
    replace = os.replace

    def failed_projection(source, target):
        if Path(target) == p.memory.path:
            raise OSError("isolated projection failure")
        return replace(source, target)

    p.service.current, p.service.owner_memory_allowed = p.job.events[0], True
    p.service._explicit = asyncio.Lock()
    p.service._input_source = lambda: ["Remember my tea preference"]
    monkeypatch.setattr(p.service, "_job", lambda job: p.job)
    monkeypatch.setattr(os, "replace", failed_projection)
    receipt = await p.service.remember("Remember my tea preference", scope="global")
    assert "正式记忆已保存" in receipt and "投影尚未完成" in receipt
    assert "未保存为记忆" not in receipt and p.formal["A"].version == 1


async def test_project_only_manual_memory_never_starts_global_production(publication):

    from pydantic_ai import Tool

    p = publication
    result = json.loads(await p.service.remember("Remember this only in the current project", scope="project"))
    assert result["status"] == "rejected" and not result["records"]
    assert not p.writes and not p.formal and not p.model_calls
    assert p.memory.read() == p.original and not p.service.session.pending_jobs()
    schema = Tool(p.service.remember).function_schema.json_schema
    assert {"request", "scope"} <= set(schema["required"])


@pytest.mark.parametrize("state", ["active", "deleted"])
def test_stale_candidate_cannot_replace_a_newer_record(tmp_path, monkeypatch, state):
    from redlotus.memory.perception import MemoryJob
    from redlotus.memory.records import MemoryDraft, MemoryRecord, ObservedTurn
    from redlotus.memory.store import MemoryStore
    from redlotus.runtime.resources import WorkspaceContext

    (tmp_path / "config.json").write_text(
        '{"storage":{"references_dir":"WorkDatabase/references"}}', encoding="utf-8",
    )
    workspace = WorkspaceContext.from_path(tmp_path)
    base = MemoryRecord(id="A", project_id=workspace.project_id, goal="Original", version=1, last_change_id="initial")
    current = base.model_copy(update={"version": 2, "goal": "Newer", "state": state, "last_change_id": "newer"})
    event = ObservedTurn(id="event", project_id=workspace.project_id, session_id="session", turn_id="turn", status="success")
    job = MemoryJob(id="stale", events=[event], bases={"A": base})
    draft = MemoryDraft(action="update", target_id="A", goal="Old candidate", source_turn_ids=[event.id])
    store = object.__new__(MemoryStore)
    store.workspace = workspace
    monkeypatch.setattr(store, "get", lambda identity: current)
    with pytest.raises(ValueError, match="changed"):
        store.materialize(job, draft, 0, lambda scope: "")
    assert current.version == 2 and current.state == state
    # A retry of the exact committed change is idempotent, even with its old base.
    current.last_change_id = "stale:0"
    assert store.materialize(job, draft, 0, lambda scope: "") is current


@pytest.mark.parametrize("indexed, stamp, expected", [
    (set(), "", "missing"),
    ({"A"}, "old-space", "stale"),
    ({"A"}, "bodynew-space", ""),
])
async def test_empty_vector_result_requires_a_complete_current_index(monkeypatch, indexed, stamp, expected):
    from redlotus.memory.records import MemoryRecord
    from redlotus.memory.store import MemoryStore

    async def retrieve(query):
        return []

    async def keys():
        return indexed

    async def refresh():
        return "model"

    index = SimpleNamespace(
        retrieve=retrieve, indexed_record_ids=keys, refresh_embedding_space=refresh,
        index_key="new-space", last_error="", config={"final_top_k": 5},
    )
    store = object.__new__(MemoryStore)
    store.indexes = {"project": index, "global": index}
    store.retrieval_error = ""
    record = MemoryRecord(id="A", scope="global", project_id="project", goal="Afternoon coffee")
    monkeypatch.setattr(store, "all", lambda scope: [record])
    monkeypatch.setattr(store, "_rows", lambda scope: [{"id": "A", "body_hash": "body", "indexed": stamp}])
    monkeypatch.setattr(store, "rag_unavailable_reason", lambda scope: "")
    assert await store.search("beverage", "global") == []
    assert expected in store.retrieval_error.lower() if expected else not store.retrieval_error


async def test_empty_authoritative_store_proves_absence_without_an_index(monkeypatch):
    from redlotus.memory.store import MemoryStore

    store = object.__new__(MemoryStore)
    monkeypatch.setattr(store, "all", lambda scope: [])
    monkeypatch.setattr(store, "rag_unavailable_reason", lambda scope: "embedding unavailable")
    assert await store.search("beverage", "global") == []
    assert not store.retrieval_error
