"""Storage/context fault checks; live model acceptance is recorded separately."""

import asyncio
import json
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolSearchCallPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage

from redlotus.core import history, system as system_module

from redlotus.core.agents import SubagentFactory
from redlotus.core.gateway import RequestPolicy
from redlotus.core.history import latest_usage_input_tokens, read_usage_messages, summarize_messages
from redlotus.core.tasks import TaskManager
from redlotus.runtime.resources import WorkspaceContext, bind_to_loop, workspace_context
from redlotus.sessions.context import ChatHistory, SubagentResult, SubagentSpec, current_usage_recorder, make_agent_id
from redlotus.sessions.control import SessionController, UserMessage
from redlotus.sessions.storage import SessionFile
from redlotus.tools.worker_tools import WorkerOrchestrator


@pytest.mark.parametrize(("imports", "blocked"), [
    ("from redlotus.runtime import config, resources, logging, network\nassert not config.settings()",
     ("redlotus.core.", "redlotus.api.", "redlotus.tools.", "redlotus.memory.")),
    ("import redlotus.core.system",
     ("redlotus.api.", "redlotus.ui.", "prompt_toolkit", "textual")),
    ("from redlotus.sessions.storage import SessionFile\nfrom redlotus.sessions.control import SessionController",
     ("redlotus.core.", "redlotus.ui.", "redlotus.api.", "redlotus.memory.")),
    ("import redlotus.tools.worker_tools\nimport redlotus.memory.service",
     ("redlotus.core.", "redlotus.ui.", "redlotus.api.")),
], ids=("runtime", "core", "sessions", "injected-tools-memory"))
def test_module_dependency_boundaries(imports, blocked):
    result = subprocess.run(
        [sys.executable, "-c", "\n".join((
            "import sys", imports,
            f"assert not any(name.startswith({blocked!r}) for name in sys.modules)",
        ))],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_session_journal_repairs_only_incomplete_tail(tmp_path, journal_policy):

    storage = SessionFile.create(tmp_path, "isolated-project", session_id="journal")
    storage.update(metadata={"title": "first"})
    first = storage.path.read_bytes()
    storage.update(metadata={"title": "second"})
    complete = storage.path.read_bytes()
    # Truncate the final transaction, leaving a valid first commit and JSON prefix.
    tail = complete.rfind(b'"checksum"')
    storage.path.write_bytes(complete[:tail + 12])
    restored = SessionFile.load(storage.path)
    assert restored.recovered_partial_write and restored.metadata["title"] == "first"
    restored.update(metadata={"title": "third"})
    assert SessionFile.load(storage.path).metadata["title"] == "third"
    damaged = complete.replace(b'"first"', b'"wrong"', 1)
    storage.path.write_bytes(damaged)
    with pytest.raises(ValueError, match="校验失败"):
        SessionFile.load(storage.path)
    assert storage.path.read_bytes() == damaged and first != complete


def test_config_layers_dynamic_roles_and_minimal_writeback(tmp_path):
    from redlotus.runtime.config import (
        get_agent_roles,
        get_model_and_params,
        settings,
        update_config,
    )

    global_file = tmp_path / "global" / "config.json"
    global_file.parent.mkdir()
    lower = {"models": {"researcher": "shared"}, "model_presets": {
        "shared": {"name": "openai:fixture", "temperature": 0.4},
    }, "API_KEY": "isolated-fixture", "request_limit": 17, "storage": {"file_lock_timeout_seconds": 0}}
    global_file.write_text(json.dumps(lower), encoding="utf-8")
    (tmp_path / ".env").write_text("request_limit=11\nmodel_presets__shared__temperature=0.2\n", encoding="utf-8")
    local = tmp_path / "config.json"
    local.write_text('{"API_KEY":"", "request_limit":0}', encoding="utf-8")
    values = settings()
    assert values["API_KEY"] == "isolated-fixture" and values["request_limit"] == 0
    assert get_agent_roles() == ("researcher",)
    assert get_model_and_params("researcher") == ("openai:fixture", {"temperature": 0.2})
    values["models"]["researcher"] = "changed copy"
    assert settings()["models"]["researcher"] == "shared"
    update_config(lambda cfg: cfg.__setitem__("request_limit", 9))
    assert json.loads(local.read_text(encoding="utf-8")) == {"API_KEY": "", "request_limit": 9}
    assert json.loads(global_file.read_text(encoding="utf-8")) == lower


def test_model_target_options_survive_roundtrip_without_shared_mutation():

    from redlotus.runtime.network import ModelTarget

    options = {"settings": {"extra_body": {"thinking": {"type": "enabled"}}},
               "limits": {"max_files": 2}, "context": {"auto_compress_ratio": .9},
               "connect_timeout": 7, "credential_field": "API_KEY"}
    target = ModelTarget("fixture", "openai", "https://example.invalid", "fixture-key", json.dumps(options), 13)
    persisted = asdict(target)
    selected = target.options
    selected["settings"]["extra_body"]["thinking"]["type"] = "disabled"
    selected["limits"]["max_files"] = 99
    selected["context"].clear()
    assert target.options == options
    assert ModelTarget(**persisted).options == options


@pytest.mark.parametrize("failure", [None, asyncio.CancelledError, RuntimeError])
async def test_http_pools_release_only_their_own_loop(failure, monkeypatch):
    import httpx

    from redlotus.runtime import network

    released = []
    class ClosingTransport(httpx.AsyncBaseTransport):
        async def aclose(self):
            await asyncio.sleep(0)
            if failure:
                raise failure("interrupted close")
            released.append(True)

    monkeypatch.setattr(network.logger, "debug", lambda *args: None)
    parent = network.get_client("isolated", httpx.AsyncClient)
    assert network.get_client("isolated", httpx.AsyncClient) is parent

    async def child():
        client = network.get_client("isolated", lambda: httpx.AsyncClient(transport=ClosingTransport()))
        other = network.get_client("other", httpx.AsyncClient)
        assert client is not parent
        try:
            closing = asyncio.create_task(network.close_all_clients())
            if failure is None:
                asyncio.get_running_loop().call_soon(closing.cancel)
            with pytest.raises(failure or asyncio.CancelledError, match="interrupted close" if failure is RuntimeError else None):
                await closing
            assert released == ([] if failure else [True])
            assert client.is_closed and other.is_closed and not parent.is_closed
            assert asyncio.get_running_loop() not in network._local.pools
        finally:
            await other.aclose()
            await network.close_all_clients()

    try:
        await asyncio.to_thread(asyncio.run, child())
    finally:
        await network.close_all_clients()
    assert parent.is_closed


@pytest.mark.parametrize("state", [
    "pending", "running", "failed", "cancelled", "unverified", "pending_confirmation", "interrupted",
])
def test_space_recovery_preserves_active_and_current_sessions(tmp_path, journal_policy, state):
    import errno
    import os

    from redlotus.sessions.cleanup import _active_cache_projects, retry_after_storage_cleanup

    (tmp_path / "config.json").write_text(json.dumps({"storage": {
        "sessions_dir": "sessions", "cleanup": {
            "enabled": True, "execution_cache": False, "session_retention_days": 7,
        },
    }}), encoding="utf-8")
    workspace = WorkspaceContext.from_path(tmp_path)
    recoverable = SessionFile.create(tmp_path / "sessions", workspace.project_id, session_id="recoverable")
    completed = [{"id": "A", "description": "saved", "status": "completed", "artifacts": ["A.txt"]},
                 {"id": "B", "description": "depends on A", "dependencies": ["A"], "status": "completed"}]
    recoverable.update(metadata={"tasks": completed})
    assert not _active_cache_projects(tmp_path / "sessions", workspace.project_id)
    tasks = [completed[0], dict(completed[1], status="completed" if state == "interrupted" else state)]
    recoverable.update(metadata={"tasks": tasks, "interrupted_turn": {"id": "turn"} if state == "interrupted" else None})
    worker_messages = [ModelRequest([UserPromptPart("Continue B after A")], instructions="saved worker context")]
    worker = recoverable.role_file("worker")
    worker.save_context(worker_messages, turn_id="turn", agent_id="B")
    active_projects = _active_cache_projects(tmp_path / "sessions", workspace.project_id)
    os.utime(recoverable.path, (recoverable.path.stat().st_mtime - 8 * 86400 - 1,) * 2)
    sessions = {name: SessionFile.create(tmp_path / "sessions", workspace.project_id, session_id=name)
                for name in ("old", "active", "current")}
    sessions["old"].update(metadata={"tasks": completed})
    sessions["active"].update(metadata={"active_turn": "running"})
    for session in sessions.values():
        os.utime(session.path, (session.path.stat().st_mtime - 8 * 86400,) * 2)
    retried = []
    with workspace_context(workspace):
        retry_after_storage_cleanup(
            sessions["current"].path, OSError(errno.ENOSPC, "isolated disk fault"),
            lambda: retried.append(True),
        )
    assert retried == [True] and not sessions["old"].path.exists()
    assert sessions["active"].path.is_file() and sessions["current"].path.is_file()
    restored = SessionFile.load(recoverable.path)
    assert restored.metadata["tasks"] == tasks and worker.path.is_file()
    assert restored.role_messages("worker", agent_id="B") == worker_messages
    assert workspace.project_id in active_projects


def test_turn_counts_survive_replay_and_reload(tmp_path, journal_policy):
    from redlotus.memory import records
    from redlotus.ui.cli_commands import list_workspace_snapshots

    (tmp_path / "config.json").write_text(
        '{"memory_perception":{"window_turns":20,"overlap_turns":3},'
        '"storage":{"project_dir":".redlotus"}}', encoding="utf-8",
    )
    workspace = WorkspaceContext.from_path(tmp_path)
    root = tmp_path / "sessions"
    session = SessionFile.create(root, workspace.project_id)
    store = records.ObservationStore(workspace)
    store.bind(session)
    for number in range(1, 4):
        event = store.begin(session.session_id, str(number), "fixture", [])
        store.finish(event)
        store.finish(event)
        assert session.completed_turns == number
        assert list_workspace_snapshots(root=root)[0].completed_turns == number
        session = SessionFile.load(session.path)
        store.bind(session)
    assert [row["number"] for row in session.pending_turns(0)] == [1, 2, 3]


@pytest.mark.parametrize("receipt", [
    ModelResponse([TextPart("cancelled")], metadata={"origin": "execution_status"}),
    ModelResponse([ToolSearchCallPart(tool_name="search", tool_call_id="auto_load_fixture")]),
])
def test_local_receipts_do_not_hide_real_model_usage(receipt):

    response = ModelResponse(
        [TextPart("done")], model_name="fixture", provider_name="fixture",
        usage=RequestUsage(input_tokens=950000, output_tokens=2),
    )
    messages = [response, ModelRequest([]), receipt]
    assert latest_usage_input_tokens(messages) == 950000
    summary = summarize_messages(messages, price_resolver=lambda model: None)
    assert summary.totals.responses == 1
    assert summary.totals.missing_usage_responses == 0


def test_provider_missing_usage_is_still_unknown():

    messages = [
        ModelResponse([TextPart("known")], usage=RequestUsage(input_tokens=950000)),
        ModelResponse([TextPart("unknown")], model_name="fixture", provider_name="fixture"),
    ]
    assert latest_usage_input_tokens(messages) is None
    assert summarize_messages(messages, price_resolver=lambda model: None).totals.missing_usage_responses == 1


def test_usage_deduplicates_response_identity_across_retries_and_pruning(tmp_path, journal_policy):
    from datetime import timedelta


    storage = SessionFile.create(tmp_path, "usage-project")
    known = ModelResponse([TextPart("answer")], model_name="fixture", provider_name="fixture",
                          provider_response_id="charged-once", usage=RequestUsage(input_tokens=11, output_tokens=2))
    storage.record_usage([known], role="title", invocation="first")
    retry = replace(known, timestamp=known.timestamp + timedelta(seconds=1))
    storage.record_usage([retry], role="title", invocation="retried-save")
    missing = ModelResponse([TextPart("unknown")], model_name="fixture", provider_name="fixture",
                            provider_response_id="usage-unreported")
    storage.record_usage([missing], role="compressor", invocation="compress")
    storage.compact(keep_turn_ids=set())
    messages, meta = read_usage_messages(storage.path)
    summary = summarize_messages(messages, meta=meta, price_resolver=lambda model: None)
    assert summary.totals.responses == 2
    assert summary.totals.input_tokens == 11 and summary.totals.missing_usage_responses == 1
    assert set(summary.by_agent) == {"title", "compressor"}


@pytest.mark.parametrize("concurrent", [1, 2])
async def test_response_accounting_survives_validation_retry_and_cancellation(tmp_path, concurrent, journal_policy):


    storage = SessionFile.create(tmp_path, "usage-project")
    controller, waiting = SessionController(), asyncio.Event()
    responses = []

    async def respond(messages, info):
        if responses:
            responses.append(None)
            messages[-1].timestamp = responses[0].timestamp
            if len(responses) == concurrent + 1:
                waiting.set()
            await asyncio.Future()
        response = ModelResponse([TextPart("invalid")], model_name="fixture", provider_name="fixture",
                                 provider_response_id="billed-before-cancel", usage=RequestUsage(input_tokens=17, output_tokens=3))
        responses.append(response)
        return response

    model = FunctionModel(respond)
    target = SimpleNamespace(name="fixture", protocol="fixture", options={"context": {},
                             "limits": {"max_files": 1, "max_file_bytes": 1000, "reference_download_timeout_seconds": 2}})
    agent = Agent(model, capabilities=[RequestPolicy("compressor", target, model, usage_category="auxiliary")])

    @agent.output_validator
    def reject(output):
        raise ModelRetry("Required summary missing")

    with controller.usage(storage):
        calls = [asyncio.create_task(agent.run("isolated accounting request")) for _ in range(concurrent)]
        await asyncio.wait_for(waiting.wait(), 5)
        for task in calls:
            task.cancel()
        outcomes = await asyncio.gather(*calls, return_exceptions=True)
        assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
    rows = SessionFile.load(storage.path).usage_responses()
    assert len(rows) == concurrent + 1 and all(row["role"] == "compressor" for row in rows)
    assert all(row["usage"]["input_tokens"] == 0 and row["category"] == "auxiliary" for row in rows[1:])
    assert rows[0]["usage"]["input_tokens"] == 17
    assert rows[0]["agent_id"] == f"{storage.session_id}:compressor"
    assert rows[0]["invocation"]
    assert rows[0]["category"] == "auxiliary"
    assert summarize_messages(read_usage_messages(storage.path)[0], price_resolver=lambda model: None).totals.missing_usage_responses == concurrent


@pytest.mark.parametrize("checkpoint", [False, True])
@pytest.mark.parametrize("role,category", [
    ("coordinator", "main"), ("worker", "agent"), ("manager", "agent"),
    ("perception", "auxiliary"), ("title", "auxiliary"),
    ("compressor", "auxiliary"), ("future_helper", "auxiliary"),
])
async def test_usage_category_follows_call_purpose_when_reusing_worker_model(tmp_path, monkeypatch, role, category, checkpoint, journal_policy):

    compacted = []
    async def compact(messages, **kwargs):
        compacted.append(True)
        return messages
    monkeypatch.setattr(history, "compact_request_messages", compact)
    target = SimpleNamespace(name="shared-worker-model", protocol="fixture", options={"context": {"auto_compress_ratio": .9},
                             "limits": {"max_files": 1, "max_file_bytes": 1000, "reference_download_timeout_seconds": 2}})
    policy = RequestPolicy(role, target, SimpleNamespace(settings={}), usage_category=category,
                           persist_context=compact if checkpoint else None)
    request = SimpleNamespace(messages=[ModelRequest([])], model_request_parameters=SimpleNamespace(
        function_tools=[], output_tools=[], instruction_parts=[]))
    await policy.before_model_request(None, request)
    assert bool(compacted) == (category != "auxiliary" or checkpoint)
    response = ModelResponse([TextPart("receipt")], model_name="shared-worker-model",
                             provider_response_id="same-receipt", usage=RequestUsage(input_tokens=17, output_tokens=3))
    await policy.after_model_request(SimpleNamespace(run_id="first"), request_context=None, response=response)
    storage = SessionFile.create(tmp_path, "fixture")
    storage.record_usage([response], role=role, invocation="first")
    storage.record_usage([response], role=role, invocation="save-retry")
    rows = SessionFile.load(storage.path).usage_responses()
    assert len(rows) == 1 and rows[0]["role"] == role and rows[0]["category"] == category
    assert rows[0]["usage"]["input_tokens"] == 17


@pytest.mark.parametrize("capacity,output,ratio,used,threshold,compresses", [
    (100, 40, .9, 59, 60, False), (None, 40, .9, 60, 60, True),
    (100, None, .9, 89, 90, False), (None, None, .9, 90, 90, True),
    (1024000, 393216, .8, 630783, 630784, False), (1024000, 393216, .8, 630784, 630784, True),
    (100, 10, .5, 49, 50, False), (100, 10, .5, 50, 50, True),
    (25, None, .28, 7, 7, True),
])
async def test_compression_preserves_system_snapshot_and_waits_for_all_saves(monkeypatch, capacity, output, ratio, used, threshold, compresses):


    inputs, entered, release = [], asyncio.Event(), asyncio.Event()
    config = {"max_context_windows": capacity, "max_tokens": output, "auto_compress_ratio": ratio,
              "compress_head_turns": 0, "compress_tail_turns": 0}
    monkeypatch.setattr(history, "get_context_config", lambda role: config)
    monkeypatch.setattr(history, "get_model_and_params", lambda role: ("fixture", {}))
    monkeypatch.setattr(history, "lookup_model_context", lambda model: 100)
    monkeypatch.setattr(history, "load_prompt", lambda name: "## Facts\n## Next")
    monkeypatch.setattr(history.logger, "info", lambda *args: None)

    async def compress(**kwargs):
        inputs.append(json.loads(kwargs["user_content"]))
        return "## Facts\nThe answer is 42.\n## Next\nRead the file."

    monkeypatch.setattr(history, "_call_compressor_llm", compress)
    sources = {role: ChatHistory() for role in ("coordinator", "manager")}
    for source in sources.values():
        source.set_messages([ModelRequest([UserPromptPart("calculate 19+23")], instructions="SYSTEM_FIXED"),
                             ModelResponse([TextPart("42")], usage=RequestUsage(input_tokens=used)),
                             ModelRequest([UserPromptPart("continue")], instructions="SYSTEM_FIXED")])
    original = {role: list(source.messages) for role, source in sources.items()}
    assert history.context_usage_breakdown("coordinator", source.messages)["threshold"] == threshold
    assert bool(await history.prepare_compression(source, role="coordinator", force=False)) == compresses
    target = SimpleNamespace(name="fixture", options={"context": {key: value for key, value in config.items() if key != "max_tokens"},
                                                     "settings": {"max_tokens": output}})
    monkeypatch.setitem(config, "max_tokens", 99)
    assert (await history.compact_request_messages(source.messages, role="coordinator", target=target) is not source.messages) == compresses
    inputs.clear()

    async def failed_save(candidates):
        assert all(candidate.messages[0].instructions == "SYSTEM_FIXED" for candidate in candidates.values())
        entered.set()
        await release.wait()
        raise OSError("injected checkpoint failure")

    task = asyncio.create_task(history.compress_histories(sources, task_state="pending", persist=failed_save, is_current=lambda: True))
    await asyncio.wait_for(entered.wait(), 5)
    assert all(source.messages == original[role] for role, source in sources.items())
    assert len(inputs) == 2 and all("SYSTEM_FIXED" not in json.dumps(item) for item in inputs)
    release.set()
    with pytest.raises(OSError, match="checkpoint"):
        await task
    assert all(source.messages == original[role] for role, source in sources.items())


@pytest.mark.parametrize("discard", [False, True])
async def test_late_usage_keeps_original_session_and_does_not_wait_after_reset(tmp_path, monkeypatch, discard, journal_policy):

    old = SessionFile.create(tmp_path, "usage-project", session_id="old")
    new = SessionFile.create(tmp_path, "usage-project", session_id="new")
    controller = SessionController()
    with controller.usage(old):
        record = current_usage_recorder()
    cancelled_context = controller.generation, controller.turn_id
    controller.reset(discard=discard)
    async with controller.turn("independent queued input"):
        controller.add_notice("cancelled previous input", context=cancelled_context)
        controller.add_notice("current input notice")
        assert controller.take_notices() == ["current input notice"]
    response = ModelResponse([TextPart("late")], usage=RequestUsage(input_tokens=9))
    with controller.usage(new):
        await record([response], role="title", invocation="old-title")
    assert len(old.usage_responses()) == 1 and not new.usage_responses()

    monkeypatch.setattr(old, "record_usage", MagicMock(side_effect=OSError("old-session disk failure")))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(record([response], role="title", invocation="old-title"), 1)


async def test_cancelled_child_checkpoint_releases_thread_capacity(agent_system, checkpoint_failure):
    system = agent_system
    system._session_file = checkpoint_failure[0]
    owner_loop = asyncio.get_running_loop()
    persist = bind_to_loop(system._durable_write, owner_loop)
    started = asyncio.Event()
    factory = SubagentFactory(max_concurrent=1)

    async def child():
        owner_loop.call_soon_threadsafe(started.set)
        try:
            await asyncio.Event().wait()
        finally:
            await persist(checkpoint_failure[1], cancelling=bool(asyncio.current_task().cancelling()))

    running = asyncio.create_task(factory.run(
        SubagentSpec("session", "turn", system.workspace), child,
    ))
    await asyncio.wait_for(started.wait(), 2)
    stopping = asyncio.create_task(factory.cancel_turn("turn"))
    try:
        await asyncio.wait({stopping}, timeout=2)
        assert stopping.done(), "Cancellation must finish while the transaction lock is held"
    finally:
        checkpoint_failure[2]()
        await stopping
    outcome, = await asyncio.gather(running, return_exceptions=True)
    assert isinstance(outcome, asyncio.CancelledError)
    assert not factory.handles and not factory._slots
    assert system._session.storage_paused
    await factory.close()


async def test_urgent_inputs_reach_reloaded_observation_and_immediate_memory(publication, agent_system, monkeypatch):
    from redlotus.tools.references import ReferenceFile

    system, service = agent_system, publication.service
    system._memory, system._session_file, system._current_attachments = service, service.session, []
    service._context_notices, service._input_source = [], lambda: system._session.user_inputs
    inputs = ["Plan the fixture task.", "Remember that I prefer fixture tea.", "Keep that preference globally."]
    references = [ReferenceFile(id=f"reference-{index}", project_id=service.workspace.project_id, name="fixture.txt",
        source="fixture", media_type="text/plain", byte_size=0, sha256=str(index), snapshot=service.workspace.root / "fixture.txt")
        for index in range(2)]

    async def produce(owner, job):
        job.result = publication.records.PerceptionResult(records=[], reason="Offline boundary", request_authorized=False)
        job.done = True

    publication.model_calls.side_effect = produce
    monkeypatch.setattr(system_module.logger, "debug", lambda *args: None)
    async with system._session.turn(inputs[0], turn_id="urgent"):
        service.current = service.observations.begin(service.session.session_id, "urgent", inputs[0], [references[0].id])
        for index, text in enumerate(inputs[1:]):
            message = UserMessage("prepared text", original_text=text, references=[references[index]], attachments=[f"attachment-{index}"])
            admission = system._session.admit(service.workspace, urgent=True)
            system._session.queue_urgent(admission, asyncio.sleep(0, result=message))
        assert len(await system._take_inner_inputs()) == 2
        service.observations.bind(SessionFile.load(service.session.path))
        result = json.loads(await service.remember(inputs[1], "global"))
        events = (*service.observations.read([service.current.id]), *publication.model_calls.call_args.args[1].events)
        assert result["status"] == "rejected" and len(events) == 2
        assert [event.user_inputs for event in events] == [inputs, inputs]
        assert all(event.reference_ids == [ref.id for ref in references] for event in events)
        assert system._current_attachments == [part for index, ref in enumerate(references)
                                               for part in [f"attachment-{index}", *ref.to_prompt()]]


def test_interrupted_task_restores_unverified_without_replaying_side_effects():

    manager = TaskManager()
    manager.restore([
        {"id": "A", "description": "done", "max_retries": 2, "status": "completed", "result": "saved", "artifacts": ["A.txt"]},
        {"id": "B", "description": "in flight", "max_retries": 2, "dependencies": ["A"], "status": "running", "artifacts": ["B.txt"]},
    ])
    assert manager.tasks["B"].status.value == "unverified"
    assert manager.get_all_ready_tasks() == []
    assert manager.tasks["A"].result == "saved"
    assert manager.tasks["B"].artifacts == ["B.txt"]


async def test_resume_uses_new_real_input_and_keeps_completed_results(task_plan):

    manager, storage, inputs = task_plan
    await manager.create_todo_list(json.dumps([
        {"id": "A", "description": "needs user's format"},
        {"id": "B", "description": "uses A", "dependencies": ["A"]},
        {"id": "C", "description": "already done"},
    ]))
    assert len(storage.metadata["tasks"]) == 3
    await manager.start(manager.tasks["A"])
    await manager.finish(manager.tasks["A"], SubagentResult(status="needs_input", summary="Which format?"))
    await manager.finish(manager.tasks["C"], SubagentResult(status="success", summary="C kept", artifacts=["C.txt"]))
    assert "Error:" in await manager.resume("A")
    await manager.create_todo_list('[{"id":"A","description":"needs user\u0027s format"}]')
    assert manager.get_all_ready_tasks() == []
    inputs["texts"].append("Use CSV.")
    assert "Error:" not in await manager.resume("A")
    assert manager.tasks["A"].user_updates == ["Use CSV."]
    assert [task.id for task in manager.get_all_ready_tasks()] == ["A"]
    await manager.start(manager.tasks["A"])
    await manager.finish(manager.tasks["A"], SubagentResult(status="success", summary="A ready"))
    restored = TaskManager()
    restored.restore(storage.metadata["tasks"])
    assert [task.id for task in restored.get_all_ready_tasks()] == ["B"]
    assert restored.tasks["C"].artifacts == ["C.txt"]
    assert "C kept" in restored.tasks["C"].result
    assert "Error:" in await manager.resume("C")


@pytest.mark.parametrize("status", ["cancelled", "unverified"])
async def test_unverified_outcomes_never_enter_automatic_retry(task_plan, status):

    manager, storage, inputs = task_plan
    await manager.create_todo_list('[{"id":"A","description":"operation with side effects"}]')
    await manager.start(manager.tasks["A"])
    await manager.finish(manager.tasks["A"], SubagentResult(status=status, summary="Check effect first", artifacts=["partial.txt"]))
    assert manager.get_all_ready_tasks() == []
    assert storage.metadata["tasks"][0]["status"] == status
    assert manager.tasks["A"].retry_count == 0
    inputs.update(turn="second", texts=["I checked partial.txt; explicitly retry A."])
    assert "Error:" not in await manager.resume("A")


async def test_cancellation_while_saving_does_not_erase_completed_work(task_plan, monkeypatch):

    manager, _, _ = task_plan
    await manager.create_todo_list('[{"id":"A","description":"first"}]')
    orchestrator = object.__new__(WorkerOrchestrator)
    orchestrator._task_manager = manager
    saving = asyncio.Event()

    async def blocked_save(tasks):
        if tasks[0]["status"] == "completed":
            saving.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(manager, "_persist", blocked_save)
    monkeypatch.setattr(orchestrator, "_execute", AsyncMock(return_value=SubagentResult(status="success", summary="A written", artifacts=["A.txt"])))
    running = asyncio.create_task(orchestrator.execute_all_tasks_parallel("goal", turn_id="first"))
    try:
        await asyncio.wait_for(saving.wait(), 2)
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    assert manager.tasks["A"].status.value == "completed"
    assert "A written" in manager.tasks["A"].result
    assert manager.tasks["A"].artifacts == ["A.txt"]


async def test_dependency_waits_for_durable_completion(task_plan, monkeypatch):

    manager, storage, _ = task_plan
    await manager.create_todo_list('[{"id":"A","description":"first"},{"id":"B","description":"second","dependencies":["A"]}]')
    orchestrator = object.__new__(WorkerOrchestrator)
    orchestrator._task_manager = manager
    blocked, release = asyncio.Event(), asyncio.Event()
    executed = []
    original = manager._persist

    async def persist(tasks):
        if tasks[0]["status"] == "completed" and not release.is_set():
            blocked.set()
            await release.wait()
        await original(tasks)

    async def execute(prompt, history, *, task_id, **kwargs):
        if task_id == "B":
            assert storage.metadata["tasks"][0]["status"] == "completed"
        executed.append(task_id)
        return SubagentResult(status="success", summary=task_id)

    monkeypatch.setattr(manager, "_persist", persist)
    monkeypatch.setattr(orchestrator, "_execute", execute)
    running = asyncio.create_task(orchestrator.execute_all_tasks_parallel("goal", turn_id="first"))
    try:
        await asyncio.wait_for(blocked.wait(), 2)
        assert executed == ["A"]
        assert storage.metadata["tasks"][0]["status"] == "running"
        release.set()
        await asyncio.wait_for(running, 2)
        assert executed == ["A", "B"]
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


def test_release_keeps_blocked_worker_context(tmp_path, journal_policy):

    storage = SessionFile.create(tmp_path / "roles", "project").role_file("worker")
    for task in ("waiting", "finished"):
        storage.save_context([ModelRequest([], instructions=f"fixed {task}")], turn_id="one", agent_id=task)
    storage.compact(keep_turn_ids=set(), release_turn_id="one", keep_agent_ids={"waiting"})
    restored = SessionFile.load(storage.path)
    assert restored.model_messages(agent_id="waiting")[0].instructions == "fixed waiting"
    assert restored.model_messages(agent_id="finished") == []


@pytest.mark.parametrize("role", ["worker", "manager"])
async def test_child_reuses_prompt_and_commits_compression_before_next_request(child_executor, monkeypatch, role):
    module, orchestrator, history, captured = child_executor

    candidate = [ModelRequest([], instructions="original session instructions")]
    agent_id = make_agent_id(orchestrator._session_key, role, "A")

    async def run(self, **kwargs):
        assert captured["instructions"] == "original session instructions"
        assert "Generate directly; no tools." in kwargs["prompt"][0]
        await captured["persist_context"](candidate)
        assert orchestrator.session_file.role_messages(role, agent_id=agent_id) == candidate
        return SimpleNamespace(output=SubagentResult(status="success", summary="done"), all_messages=lambda: candidate)

    monkeypatch.setattr(type(orchestrator.factory.runner), "run", run)
    report = await orchestrator._execute("follow up", history, role=role, task_id="A", turn_id="one")
    assert report.success and captured["closed"]


@pytest.mark.parametrize("role", ["worker", "manager"])
async def test_child_does_not_send_after_compression_save_failure(child_executor, monkeypatch, role):
    module, orchestrator, history, captured = child_executor
    sent = []

    async def run(self, **kwargs):
        await captured["persist_context"]([ModelRequest([], instructions="fixed")])
        sent.append(True)

    monkeypatch.setattr(type(orchestrator.factory.runner), "run", run)
    orchestrator._persist = AsyncMock(side_effect=OSError("injected full disk"))
    if role == "manager":
        with pytest.raises(OSError, match="injected full disk"):
            await orchestrator._execute("follow up", history, role=role, task_id="A", turn_id="one")
    else:
        report = await orchestrator._execute("follow up", history, role=role, task_id="A", turn_id="one")
        assert report.status == "unverified" and "injected full disk" in report.summary
    assert not sent and captured["closed"]


async def test_native_sdk_restoration_does_not_duplicate_deferred_catalog():
    from pydantic_ai.capabilities import Capability

    from redlotus.prompts.prompt import format_prompt_current_time, session_prompt_from_history, with_runtime_context

    assert json.loads(with_runtime_context("first")[1].content)["current_time"] == date.fromisoformat(format_prompt_current_time()).isoformat()

    requests = []

    async def respond(messages, info):
        requests.append(messages[-1].instructions)
        return ModelResponse([TextPart("fixture")])

    model = FunctionModel(respond)
    target = SimpleNamespace(name="fixture", protocol="fixture", options={"context": {}, "limits": {"max_files": 1, "max_file_bytes": 1000, "reference_download_timeout_seconds": 2}})

    def agent(instructions):
        return Agent(model, instructions=instructions, capabilities=[
            Capability(id="fixture_files", description="Read isolated fixtures", defer_loading=True),
            RequestPolicy("fixture", target, model, usage_category="agent"),
        ])

    first = await agent("Original role snapshot").run("first")
    restored = session_prompt_from_history(first.all_messages())
    assert restored == "Original role snapshot"
    # Old serialized requests lack boundary metadata; the native SDK catalog is not role text.
    legacy = ModelRequest([], instructions=requests[0])
    assert session_prompt_from_history([legacy]) == restored
    await agent(restored).run("second", message_history=first.all_messages())
    assert requests[0] == requests[1]
