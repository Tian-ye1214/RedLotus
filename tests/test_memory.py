"""Version/publication fault checks; live memory acceptance is separate."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
from concurrent.futures import Future
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import pytest
from httpx import ReadTimeout
from pydantic_ai import FunctionToolset, ImageUrl, Tool, capture_run_messages
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.usage import RequestUsage, UsageLimits


from redlotus.core import gateway, history
from redlotus.core.system import AgentSystem
from redlotus.memory import records, service as memory_service
from redlotus.memory.perception import MemoryJob, PerceptionTiming, produce_job
from redlotus.memory.service import MemoryService
from redlotus.runtime.resources import WorkspaceContext
from redlotus.sessions.storage import SessionFile
from redlotus.ui.console import AgentCliController


@pytest.mark.parametrize("prepared", [False, True, "internal"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", [None, "save", "cancel", "compress", "again", "other", "auth", "no_checkpoint"])
async def test_perception_capacity_recovery_preserves_evidence_and_output(monkeypatch, stream, failure, prepared):

    from pydantic_ai.models.function import DeltaToolCall, FunctionModel
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
        if prepared == "internal" and len(calls) == 1:
            return ModelResponse([ToolCallPart("read_evidence", {}, "evidence")], usage=RequestUsage(input_tokens=43))
        if len(calls) == 1 + (prepared == "internal") or failure == "again":
            raise error
        assert saved and messages == saved[0]
        return ModelResponse([TextPart("complete")])
    async def chunks(messages, info):
        part = (await respond(messages, info)).parts[0]
        yield {0: DeltaToolCall(name=part.tool_name, json_args="{}", tool_call_id=part.tool_call_id)} if isinstance(part, ToolCallPart) else part.content
    async def compress(**kwargs):
        compressed.append(json.loads(kwargs["user_content"])["transcript"])
        assert any(text in compressed[-1] for text in ("RAW_WINDOW", "RAW_EVIDENCE")) and "SYSTEM_FIXED" not in compressed[-1]
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
    def read_evidence():
        return "RAW_WINDOW"
    agent = gateway.create_agent(target, instructions="SYSTEM_FIXED", role="perception", usage_category="auxiliary", toolsets=[FunctionToolset([read_evidence])],
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
    assert len(calls) == len(timings) == (2 if retries else 1) + (prepared == "internal")
    assert bool(compressed) == (failure not in ("other", "auth", "no_checkpoint"))
    assert "RAW_WINDOW" in str(calls[0])
    if not saved:
        assert "RAW_WINDOW" in str(messages)
    else:
        assert saved[0][0].instructions == "SYSTEM_FIXED"


@pytest.mark.parametrize("failure", [None, "minimum", "other", "segment", "cancel"])
@pytest.mark.parametrize("merged", [False, True])
async def test_compressor_capacity_splits_closed_units_concurrently_and_drains(monkeypatch, failure, merged):
    from pydantic_ai._agent_graph import _clean_message_history
    from redlotus.sessions.context import ChatHistory

    calls, entered, finished = [], asyncio.Event(), []
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
        transcript = calls[-1]
        if len(calls) == 1 or failure == "minimum":
            raise ModelHTTPError(400, "fixture", {"message": "Bad schema" if failure == "other" else "This model's maximum context length is 100 tokens."})
        if len(calls) == 3:
            entered.set()
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if "EVENT_A" in transcript and failure in ("segment", "cancel"):
                raise ValueError("segment failed") if failure == "segment" else asyncio.CancelledError()
            return "## Facts\n" + transcript + "\n## Next\nKeep remaining events."
        finally:
            finished.append(transcript)
    monkeypatch.setattr(history, "_call_compressor_llm", compress)
    monkeypatch.setattr(history, "load_prompt", lambda name: "## Facts\n## Next")
    monkeypatch.setattr(history.logger, "info", lambda *args: None)
    pending = history.prepare_compression(source, role="worker", force=True,
        context={"max_context_windows": 100, "auto_compress_ratio": .8, "compress_head_turns": 0, "compress_tail_turns": 1})
    if failure:
        with pytest.raises({"segment": ValueError, "cancel": asyncio.CancelledError}.get(failure, ModelHTTPError)):
            await pending
    else:
        candidate = await pending
        assert candidate.messages[0].instructions == "SYSTEM_FIXED"
        assert ([part.content for message in candidate.messages[1:] for part in message.parts] == ["EVENT_D"] if merged else candidate.messages[1:] == original[-2:])
        assert all(sum("EVENT_" + label in call for call in calls[1:]) == 1 for label in "ABC")
        assert all(text in calls[1] for text in (("EVENT_A",) if merged else ("EVENT_A", "SOURCE_A", "RESULT_A")))
        assert all("EVENT_" + label in candidate.compress_summary_state for label in "ABC")
    assert len(calls) == (1 if failure == "other" else 5 if failure == "minimum" else 3) and source.messages == original
    assert len(finished) == (0 if failure in ("other", "minimum") else 2)


def test_perception_window_keeps_complete_evidence_units_and_manifest():
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


async def test_non_owner_turns_count_without_reading_or_producing_personal_memory(publication):
    service = publication.service
    service.owner_memory_allowed, service.current = False, None
    del service.long_term, service.store
    event = await service.begin_turn("fixture", "input-id", "Public channel text")
    await service.finish_turn(event, status="success", user_inputs=["Public channel text"], evidence_paths=[])
    assert service.session.completed_turns == 2
    assert service.session.turn(event.id)["turn_id"] == "input-id"
    assert not service.session.pending_jobs()


@pytest.mark.parametrize("switch_at", ["queued", "processing", None])
async def test_background_perception_keeps_its_scheduled_session(monkeypatch, tmp_path, switch_at):
    import threading

    service = object.__new__(MemoryService)
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
    monkeypatch.setattr(memory_service, "MemoryService", lambda **kwargs: producer)
    monkeypatch.setattr(memory_service, "MemoryPerception", lambda *args, **kwargs: None)
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
        new.release_use.assert_called_once()
    else:
        await system.bind_session("new", storage=new, generation=0)
    assert system._session_file is (old if invalidate else new)
    assert old.release_use.call_count == (0 if invalidate else 1)
    system._factory.cancel_session.assert_awaited_once_with("old")




@pytest.mark.parametrize("transition", ["load", "clear", "project"])
async def test_direct_memory_retry_blocks_session_changes(publication, tmp_path, transition):

    service = publication.service
    service._background = None
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


@pytest.mark.parametrize("save_fails", [False, True])
async def test_perception_compaction_saves_before_adoption_and_binds_usage(publication, monkeypatch, tmp_path, save_fails):

    from redlotus.sessions.context import current_usage_recorder

    publication.job.request, publication.job.result = None, None
    publication.job.perception_config = {"model_role": "worker"}
    target = SimpleNamespace(name="fixture", options={"context": {"auto_compress_ratio": .9},
        "limits": {"max_files": 1, "max_file_bytes": 1000, "reference_download_timeout_seconds": 2}})
    publication.service._targets = {publication.job.id: target}
    storage, requests, recorders = publication.service.session, [], []
    candidate = [ModelRequest([UserPromptPart("retained evidence")], instructions="SYSTEM_FIXED")]
    monkeypatch.setattr(history, "compact_request_messages", AsyncMock(return_value=candidate))
    role_file = storage.role_file("perception")
    if save_fails:
        monkeypatch.setattr(role_file, "save_context", Mock(side_effect=OSError("perception checkpoint unavailable")))

    async def produce(payload, references, **callbacks):
        recorder = current_usage_recorder()
        assert recorder is not None
        recorders.append(recorder)
        original = [ModelRequest([UserPromptPart("original evidence")], instructions="SYSTEM_FIXED")]
        request = SimpleNamespace(messages=original, model_request_parameters=SimpleNamespace(
            function_tools=[], output_tools=[], instruction_parts=[]))
        policy = gateway.RequestPolicy("perception", target, SimpleNamespace(settings={}), usage_category="auxiliary",
                               persist_context=callbacks["persist_context"])
        try:
            await policy.before_model_request(None, request)
        except OSError:
            assert request.messages is original
            raise
        assert role_file.model_messages(agent_id=publication.job.id) == candidate
        requests.append(request.messages)
        return publication.records.PerceptionResult(records=[], reason="No new fact")

    publication.service.perception = SimpleNamespace(produce=produce)
    if save_fails:
        with pytest.raises(OSError, match="checkpoint unavailable"):
            await produce_job(publication.service, publication.job)
        assert not requests and publication.job.result is None
    else:
        await produce_job(publication.service, publication.job)
        assert requests == [candidate]
    assert current_usage_recorder() is None
    publication.service.session = SessionFile.create(tmp_path / "other", "isolated", session_id="other")
    receipt = ModelResponse([TextPart("summary")], usage=RequestUsage(input_tokens=23, output_tokens=5),
                            metadata={"usage_category": "auxiliary"})
    await recorders[0]([receipt], role="compressor", invocation="compression", cancelling=False)
    rows = storage.usage_responses()
    assert len(rows) == 1 and rows[0]["role"] == "compressor" and rows[0]["usage"]["input_tokens"] == 23
    assert not publication.service.session.usage_responses()


@pytest.mark.parametrize("newer", [False, True])
@pytest.mark.parametrize("error", [OSError("isolated projection failure"), ReadTimeout(""), records.MemoryConflict("Core changed")])
async def test_projection_retry_restores_stage_and_never_replays_model_or_database(publication, monkeypatch, newer, error):

    replace = os.replace

    def failed_projection(source, target):
        if Path(target) == publication.memory.path:
            raise error
        return replace(source, target)

    monkeypatch.setattr(os, "replace", failed_projection)
    publication.service.current, publication.service._input_source = publication.job.events[0], lambda: ["Remember my tea preference"]
    publication.job.id = hashlib.sha256(json.dumps([publication.job.events[0].id, publication.job.request, "global", "remember", None]).encode()).hexdigest()[:32]
    publication.service._save_job(publication.job)
    receipt = await publication.service.remember(publication.job.request, scope="global")
    assert "正式记忆已保存" in receipt and "投影尚未完成" in receipt
    assert "未保存为记忆" not in receipt
    restored = publication.service._job(publication.job)
    assert restored.error and restored.error == publication.service.last_error == publication.service._processor()["error"] == restored.failures[-1]["error"]
    assert publication.store.get("A").version == 1 and publication.memory.read() == publication.original
    assert restored.timings.get("records_committed_at") and not restored.done
    committed = restored.timings["records_committed_at"]
    if newer:
        publication.store.save([publication.store.get("A").model_copy(update={"version": 2, "state": "deleted", "last_change_id": "newer"})])
    publication.memory.path.write_text(publication.original + "\nUser's later note\n", encoding="utf-8")
    monkeypatch.setattr(os, "replace", replace)
    await publication.service.process_pending(recover=True)
    restored = publication.service._job(publication.job)
    assert restored.done and restored.timings["records_committed_at"] == committed
    assert not publication.model_calls.called and publication.writes.call_count == 1 + newer
    assert "User's later note" in publication.memory.read()
    assert ("Fixture tea" in publication.memory.read()) is not newer
    assert restored.records == ([] if newer else ["A"])


@pytest.mark.parametrize("failure", ["database", "manual", "managed"])
async def test_rejected_candidate_cannot_publish_core_memory(publication, monkeypatch, failure):
    previous = publication.row.model_copy(update={"content": "Original preference", "last_change_id": "before"})
    original = publication.original if failure == "database" else publication.original.replace("## 用户画像\n",
        f"## 用户画像\n\n<!-- memory:{'A' if failure == 'manual' else 'B version:1'} -->\nOriginal preference\n<!-- /memory -->\n")
    publication.job.core_snapshot = original
    if failure == "manual":
        publication.store.save([previous])
        publication.job.bases, publication.job.searches[0]["revision"] = {"A": previous}, publication.store.revision("global")
    elif failure == "managed":
        publication.job.result.records[0].core_old_text = "Original preference"
    else:
        monkeypatch.setattr(publication.store, "save", Mock(side_effect=OSError("isolated database failure")))
    publication.writes.reset_mock()
    edited = original.replace("Original preference", "Manual preference") if failure == "manual" else original
    publication.memory.path.write_text(edited, encoding="utf-8")
    with pytest.raises(OSError if failure == "database" else ValueError,
                       match={"database": "database failure", "manual": "[Cc]ore memory changed", "managed": "managed record"}[failure]):
        await publication.service._apply(publication.job)
    assert not publication.writes.called and publication.memory.read() == edited
    assert not publication.job.done
    assert len(publication.store.all()) == (1 if failure == "manual" else 0)


def test_injection_excludes_uncommitted_or_superseded_managed_blocks(publication):
    body = publication.original + "\nHandwritten note\n<!-- memory:A version:2 -->\nUncommitted preference\n<!-- /memory -->\n"
    publication.memory.path.write_text(body, encoding="utf-8")
    injection = publication.memory.get_injection([publication.row])
    assert "Uncommitted preference" not in injection and "Handwritten note" in injection
    assert publication.memory.read() == body


async def test_project_only_manual_memory_never_starts_global_production(publication):

    result = json.loads(await publication.service.remember("Remember this only in the current project", scope="project"))
    assert result["status"] == "rejected" and not result["records"]
    assert not publication.writes.called and not publication.store.all() and not publication.model_calls.called
    assert publication.memory.read() == publication.original and not publication.service.session.pending_jobs()
    schema = Tool(publication.service.remember).function_schema.json_schema
    assert {"request", "scope"} <= set(schema["required"])


@pytest.mark.parametrize("conflict", ["revision", "active", "deleted"])
async def test_stale_candidate_retry_researches_and_allows_next_job(publication, monkeypatch, conflict):
    generated = []
    base = publication.row.model_copy(update={"last_change_id": "initial"})
    current = base.model_copy(update={"version": 2, "state": "deleted" if conflict == "deleted" else "active", "last_change_id": "newer"})
    publication.store.save([current])
    await publication.store.reconcile()
    publication.job.bases = {"A": base, "stale-only": base.model_copy(update={"id": "stale-only"})}
    publication.job.operation, publication.job.target_id, publication.job.perception_config = "update", "A", {"model_role": "worker"}
    publication.job.usage, publication.job.model_calls, publication.job.failures = [{"input_tokens": 17}], [{"seconds": 2}], [{"error": "previous failure"}]
    if conflict != "revision":
        publication.job.searches[0]["revision"] = publication.store.revision("global")
    with pytest.raises(records.MemoryConflict, match="changed"):
        publication.store.materialize(publication.job, publication.job.result.records[0], 0, lambda scope: "")
    publication.service._save_job(publication.job)
    second = MemoryJob(id="next", events=publication.job.events, result=records.PerceptionResult(reason="No new fact"))
    publication.service._save_job(second)
    await publication.service.process_pending(recover=True)
    failed = publication.service.session.job(publication.job.id)
    assert publication.store.get("A").version == 2 and publication.store.get("A").state == current.state
    assert failed["result"] is None
    assert not failed["searches"]
    assert not failed["bases"]
    assert "records_committed_at" not in failed["timings"]
    assert publication.service.session.pending_jobs() == [publication.job.id, second.id]
    publication.service._targets = {publication.job.id: object()}
    async def produce(payload, references, **callbacks):
        generated.append(payload)
        assert payload["explicit_request"] == publication.job.request
        assert payload["requested_scope"] == "global"
        assert payload["operation"] == "update" and payload["target_id"] == "A"
        assert payload["previous_error"]
        assert [row["id"] for row in payload["existing_records"]] == ["A"]
        assert payload["existing_records"][0]["version"] == 2
        rows = await publication.store.search("Preference", "global")
        callbacks["on_search"](rows, dict(id="fresh", scope="global", revision=publication.store.revision("global"), retrieval_error=publication.store.retrieval_error))
        callbacks["on_usage"]({"input_tokens": 19})
        callbacks["on_call"]({"seconds": 3})
        return publication.job.result.model_copy(update={"records": [publication.job.result.records[0].model_copy(update={"action": "update", "search_id": "fresh"})]})
    publication.service.perception = SimpleNamespace(produce=produce)
    monkeypatch.setattr(memory_service, "produce_job", produce_job)
    await publication.service.process_pending(recover=True)
    saved = publication.service.session.job(publication.job.id)
    assert len(generated) == 1
    assert publication.store.get("A").version == 3
    assert not publication.service.session.pending_jobs()
    assert publication.service.session.job(second.id)["done"] and saved["done"]
    assert saved["searches"][0]["id"] == "fresh"
    assert saved["failures"] == failed["failures"]
    assert saved["usage"] == [*publication.job.usage, {"input_tokens": 19}]
    assert saved["model_calls"] == [*publication.job.model_calls, {"seconds": 3}]
    assert publication.store.materialize(publication.job, publication.job.result.records[0], 0, lambda scope: "") == publication.store.get("A")


async def test_empty_index_error_keeps_pending_job_and_evidence_until_retry(publication):
    publication.job.window = publication.service.observations.window()
    publication.index.prepare_records.side_effect = ReadTimeout("")
    publication.service._save_job(publication.job)
    await publication.service.process_pending(recover=True)
    failed = publication.service.session.job(publication.job.id)
    assert failed["done"] and failed["timings"]["records_committed_at"]
    assert "indexed_at" not in failed["timings"]
    assert failed["error"] == publication.store.last_error == publication.service.last_error == "ReadTimeout"
    assert publication.service.session.pending_jobs() == [publication.job.id]
    assert failed["event_ids"] == [publication.job.events[0].id]
    assert "turn_id" in publication.service.session.turn(publication.job.events[0].id)
    assert not publication.store._rows("global")[0]["indexed"]
    publication.index.prepare_records.side_effect = lambda rows: rows
    await publication.service.process_pending(recover=True)
    saved = publication.service.session.job(publication.job.id)
    assert saved["timings"]["indexed_at"] and saved["timings"]["records_committed_at"] == failed["timings"]["records_committed_at"]
    assert not saved["error"] and not publication.store.last_error and not publication.service.last_error
    assert not publication.service.session.pending_jobs()
    assert not saved["event_ids"]
    assert "turn_id" not in publication.service.session.turn(publication.job.events[0].id)
    assert not publication.model_calls.called and publication.writes.call_count == 1
    assert publication.store._rows("global")[0]["indexed"]


@pytest.mark.parametrize("indexed, stamp, expected, query", [
    (set(), "", "missing", "beverage"),
    ({"A"}, "old-space", "stale", "beverage"),
    ({"A"}, "fixture", "", "beverage"),
    ({"A"}, "fixture", "readtimeout", "beverage"),
    ({"A"}, "fixture", "readtimeout", "tea"),
    (None, "", "", "beverage"),
])
async def test_empty_vector_result_requires_a_complete_current_index(publication, monkeypatch, indexed, stamp, expected, query):
    if indexed is not None:
        publication.store.save([publication.row.model_copy(update={"last_change_id": "before"})])
        publication.store._table().update(where="id = 'A'", values={"indexed": publication.store._rows("global")[0]["body_hash"] + stamp})
    else:
        monkeypatch.setattr(publication.store, "rag_unavailable_reason", lambda scope: "embedding unavailable")
    publication.index.indexed_record_ids.side_effect = lambda: indexed or set()
    publication.index.retrieve.side_effect = ReadTimeout("") if expected == "readtimeout" else None
    assert [row.id for row in await publication.store.search(query, "global")] == (["A"] if query == "tea" else [])
    assert expected in publication.store.retrieval_error.lower() if expected else not publication.store.retrieval_error
    if expected:
        publication.job.searches[0]["retrieval_error"] = publication.store.retrieval_error
        with pytest.raises(ValueError, match="memory_publication_search"):
            await publication.service._check_searches(publication.job)
