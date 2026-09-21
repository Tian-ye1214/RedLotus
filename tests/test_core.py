"""Storage/context fault checks; live model acceptance is recorded separately."""

import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolSearchCallPart,
)
from pydantic_ai.usage import RequestUsage


def test_runtime_configuration_does_not_load_application_layers():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; from redlotus.runtime import config, resources, logging, network; "
         "assert not config.settings(); assert not any(name.startswith(('redlotus.core.', 'redlotus.api.', "
         "'redlotus.tools.', 'redlotus.memory.')) for name in sys.modules)"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_agent_core_does_not_construct_terminal_interfaces():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import redlotus.core.system; "
         "assert not any(name.startswith(('redlotus.api.', 'redlotus.ui.', "
         "'redlotus.ui.console', 'redlotus.ui.presentation', 'redlotus.ui.tui', "
         "'prompt_toolkit', 'textual')) for name in sys.modules)"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_session_storage_is_independent_of_agent_or_ui_construction():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; from redlotus.sessions.storage import SessionFile; "
         "from redlotus.sessions.control import SessionController; "
         "assert not any(name.startswith(('redlotus.core.', 'redlotus.ui.', 'redlotus.api.', "
         "'redlotus.memory.')) for name in sys.modules)"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_tools_and_memory_receive_orchestration_through_injection():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import redlotus.tools.worker_tools; import redlotus.memory.service; "
         "assert not any(name.startswith(('redlotus.core.', 'redlotus.ui.', 'redlotus.api.')) for name in sys.modules)"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_session_journal_repairs_only_incomplete_tail(tmp_path):
    from redlotus.sessions.storage import SessionFile

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
    from dataclasses import asdict

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


async def test_http_pools_release_only_their_own_loop():
    import httpx

    from redlotus.runtime.network import close_all_clients, get_client

    parent = get_client("isolated", httpx.AsyncClient)
    assert get_client("isolated", httpx.AsyncClient) is parent

    async def child():
        client = get_client("isolated", httpx.AsyncClient)
        assert client is not parent
        await close_all_clients()
        assert client.is_closed and not parent.is_closed

    try:
        await asyncio.to_thread(asyncio.run, child())
    finally:
        await close_all_clients()
    assert parent.is_closed


def test_space_recovery_preserves_active_and_current_sessions(tmp_path):
    import errno
    import os

    from redlotus.runtime.resources import WorkspaceContext, workspace_context
    from redlotus.sessions.cleanup import retry_after_storage_cleanup
    from redlotus.sessions.storage import SessionFile

    (tmp_path / "config.json").write_text(json.dumps({"storage": {
        "sessions_dir": "sessions", "cleanup": {
            "enabled": True, "execution_cache": False, "session_retention_days": 1,
        },
    }}), encoding="utf-8")
    workspace = WorkspaceContext.from_path(tmp_path)
    sessions = {name: SessionFile.create(tmp_path / "sessions", workspace.project_id, session_id=name)
                for name in ("old", "active", "current")}
    sessions["active"].update(metadata={"active_turn": "running"})
    for session in sessions.values():
        os.utime(session.path, (session.path.stat().st_mtime - 2 * 86400,) * 2)
    retried = []
    with workspace_context(workspace):
        retry_after_storage_cleanup(
            sessions["current"].path, OSError(errno.ENOSPC, "isolated disk fault"),
            lambda: retried.append(True),
        )
    assert retried == [True] and not sessions["old"].path.exists()
    assert sessions["active"].path.is_file() and sessions["current"].path.is_file()


def test_turn_counts_survive_replay_and_reload(tmp_path):
    from redlotus.memory import records
    from redlotus.runtime.resources import WorkspaceContext
    from redlotus.sessions.storage import SessionFile
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
    from redlotus.core.history import latest_usage_input_tokens, summarize_messages

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
    from redlotus.core.history import latest_usage_input_tokens, summarize_messages

    messages = [
        ModelResponse([TextPart("known")], usage=RequestUsage(input_tokens=950000)),
        ModelResponse([TextPart("unknown")], model_name="fixture", provider_name="fixture"),
    ]
    assert latest_usage_input_tokens(messages) is None
    assert summarize_messages(messages, price_resolver=lambda model: None).totals.missing_usage_responses == 1


def test_usage_deduplicates_response_identity_across_retries_and_pruning(tmp_path):
    from dataclasses import replace
    from datetime import timedelta

    from redlotus.core.history import read_usage_messages, summarize_messages
    from redlotus.sessions.storage import SessionFile

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


async def test_response_accounting_survives_validation_retry_and_cancellation(tmp_path):
    from pydantic_ai import Agent, ModelRetry
    from pydantic_ai.models.function import FunctionModel

    from redlotus.core.gateway import RequestPolicy
    from redlotus.sessions.control import SessionController
    from redlotus.sessions.storage import SessionFile

    storage = SessionFile.create(tmp_path, "usage-project")
    controller, waiting = SessionController(), asyncio.Event()
    responses = []

    async def respond(messages, info):
        if responses:
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
        task = asyncio.create_task(agent.run("isolated accounting request"))
        await asyncio.wait_for(waiting.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    rows = SessionFile.load(storage.path).usage_responses()
    assert len(rows) == 1 and rows[0]["role"] == "compressor"
    assert rows[0]["usage"]["input_tokens"] == 17
    assert rows[0]["agent_id"] == f"{storage.session_id}:compressor"
    assert rows[0]["invocation"]
    assert rows[0]["category"] == "auxiliary"


@pytest.mark.parametrize("role,category", [
    ("coordinator", "main"), ("worker", "agent"), ("manager", "agent"),
    ("perception", "auxiliary"), ("title", "auxiliary"),
    ("compressor", "auxiliary"), ("future_helper", "auxiliary"),
])
async def test_usage_category_follows_call_purpose_when_reusing_worker_model(tmp_path, monkeypatch, role, category):
    from redlotus.core import history
    from redlotus.core.gateway import RequestPolicy
    from redlotus.sessions.storage import SessionFile

    compacted = []
    async def compact(messages, **kwargs):
        compacted.append(True)
        return messages
    monkeypatch.setattr(history, "compact_request_messages", compact)
    target = SimpleNamespace(name="shared-worker-model", protocol="fixture", options={"context": {"auto_compress_ratio": .9},
                             "limits": {"max_files": 1, "max_file_bytes": 1000, "reference_download_timeout_seconds": 2}})
    policy = RequestPolicy(role, target, SimpleNamespace(settings={}), usage_category=category)
    request = SimpleNamespace(messages=[ModelRequest([])], model_request_parameters=SimpleNamespace(
        function_tools=[], output_tools=[], instruction_parts=[]))
    await policy.before_model_request(None, request)
    assert bool(compacted) == (category != "auxiliary")
    response = ModelResponse([TextPart("receipt")], model_name="shared-worker-model",
                             provider_response_id="same-receipt", usage=RequestUsage(input_tokens=17, output_tokens=3))
    await policy.after_model_request(SimpleNamespace(run_id="first"), request_context=None, response=response)
    storage = SessionFile.create(tmp_path, "fixture")
    storage.record_usage([response], role=role, invocation="first")
    storage.record_usage([response], role=role, invocation="save-retry")
    rows = SessionFile.load(storage.path).usage_responses()
    assert len(rows) == 1 and rows[0]["role"] == role and rows[0]["category"] == category
    assert rows[0]["usage"]["input_tokens"] == 17


async def test_compression_preserves_system_snapshot_and_waits_for_all_saves(monkeypatch):
    from pydantic_ai.messages import UserPromptPart

    from redlotus.core import history as module
    from redlotus.sessions.context import ChatHistory

    inputs, entered, release = [], asyncio.Event(), asyncio.Event()
    config = {"max_context_windows": 100, "auto_compress_ratio": .9,
              "compress_head_turns": 0, "compress_tail_turns": 0}
    monkeypatch.setattr(module, "get_context_config", lambda role: config)
    monkeypatch.setattr(module, "load_prompt", lambda name: "## Facts\n## Next")
    monkeypatch.setattr(module.logger, "info", lambda *args: None)

    async def compress(**kwargs):
        inputs.append(json.loads(kwargs["user_content"]))
        return "## Facts\nThe answer is 42.\n## Next\nRead the file."

    monkeypatch.setattr(module, "_call_compressor_llm", compress)
    sources = {role: ChatHistory() for role in ("coordinator", "manager")}
    for source in sources.values():
        source.set_messages([ModelRequest([UserPromptPart("calculate 19+23")], instructions="SYSTEM_FIXED"),
                             ModelResponse([TextPart("42")])])
    original = {role: list(source.messages) for role, source in sources.items()}

    async def failed_save(candidates):
        assert all(candidate.messages[0].instructions == "SYSTEM_FIXED" for candidate in candidates.values())
        entered.set()
        await release.wait()
        raise OSError("injected checkpoint failure")

    task = asyncio.create_task(module.compress_histories(sources, task_state="pending", persist=failed_save, is_current=lambda: True))
    await asyncio.wait_for(entered.wait(), 5)
    assert all(source.messages == original[role] for role, source in sources.items())
    assert len(inputs) == 2 and all("SYSTEM_FIXED" not in json.dumps(item) for item in inputs)
    release.set()
    with pytest.raises(OSError, match="checkpoint"):
        await task
    assert all(source.messages == original[role] for role, source in sources.items())


async def test_late_usage_keeps_original_session_and_does_not_wait_after_clear(tmp_path, monkeypatch):
    from redlotus.sessions.context import current_usage_recorder
    from redlotus.sessions.control import SessionController
    from redlotus.sessions.storage import SessionFile

    old = SessionFile.create(tmp_path, "usage-project", session_id="old")
    new = SessionFile.create(tmp_path, "usage-project", session_id="new")
    controller = SessionController()
    with controller.usage(old):
        record = current_usage_recorder()
    controller.reset(discard=True)
    response = ModelResponse([TextPart("late")], usage=RequestUsage(input_tokens=9))
    with controller.usage(new):
        await record([response], role="title", invocation="old-title")
    assert len(old.usage_responses()) == 1 and not new.usage_responses()

    def failed_save(*args, **kwargs):
        raise OSError("old-session disk failure")

    monkeypatch.setattr(old, "record_usage", failed_save)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(record([response], role="title", invocation="old-title"), 1)


async def test_cancelled_child_checkpoint_releases_thread_capacity(tmp_path, monkeypatch):
    from redlotus.core import system as system_module
    from redlotus.core.agents import SubagentFactory
    from redlotus.runtime.resources import WorkspaceContext, bind_to_loop
    from redlotus.sessions.context import SubagentSpec
    from redlotus.sessions.control import SessionController

    system = object.__new__(system_module.AgentSystem)
    system.workspace = WorkspaceContext.from_path(tmp_path)
    system._session_file = None
    system._session = SessionController()
    system.presentation = SimpleNamespace(print_warning=lambda text: None)
    owner_loop = asyncio.get_running_loop()
    persist = bind_to_loop(system._durable_write, owner_loop)
    started = asyncio.Event()
    factory = SubagentFactory(max_concurrent=1)

    def broken_disk():
        raise OSError("injected disk failure")

    async def child():
        owner_loop.call_soon_threadsafe(started.set)
        try:
            await asyncio.Event().wait()
        finally:
            await persist(broken_disk, cancelling=bool(asyncio.current_task().cancelling()))

    running = asyncio.create_task(factory.run(
        SubagentSpec("session", "turn", system.workspace), child,
    ))
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.wait_for(factory.cancel_turn("turn"), 2)
    outcome, = await asyncio.gather(running, return_exceptions=True)
    assert isinstance(outcome, asyncio.CancelledError)
    assert not factory.handles and not factory._slots
    assert system._session.storage_paused
    await factory.close()


@pytest.fixture
def task_plan(tmp_path):
    from redlotus.core.tasks import TaskManager
    from redlotus.sessions.storage import SessionFile

    (tmp_path / "config.json").write_text('{"agent_run_policy":{"max_task_retries":3}}')
    storage = SessionFile.create(tmp_path / "plan", "isolated-project")
    inputs = {"turn": "first", "texts": ["Plan A, then B; C is independent."]}

    async def persist(tasks):
        await asyncio.to_thread(storage.update, metadata={"tasks": tasks})

    manager = TaskManager(persist=persist, input_source=lambda: (inputs["turn"], inputs["texts"]))
    return manager, storage, inputs


def test_interrupted_task_restores_unverified_without_replaying_side_effects():
    from redlotus.core.tasks import TaskManager

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
    from redlotus.core.tasks import TaskManager
    from redlotus.sessions.context import SubagentResult

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
    from redlotus.sessions.context import SubagentResult

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
    from redlotus.sessions.context import SubagentResult
    from redlotus.tools.worker_tools import WorkerOrchestrator

    manager, _, _ = task_plan
    await manager.create_todo_list('[{"id":"A","description":"first"}]')
    orchestrator = object.__new__(WorkerOrchestrator)
    orchestrator._task_manager = manager
    saving = asyncio.Event()

    async def blocked_save(tasks):
        if tasks[0]["status"] == "completed":
            saving.set()
            await asyncio.Event().wait()

    async def execute(*args, **kwargs):
        return SubagentResult(status="success", summary="A written", artifacts=["A.txt"])

    monkeypatch.setattr(manager, "_persist", blocked_save)
    monkeypatch.setattr(orchestrator, "_execute", execute)
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
    from redlotus.sessions.context import SubagentResult
    from redlotus.tools.worker_tools import WorkerOrchestrator

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


def test_release_keeps_blocked_worker_context(tmp_path):
    from redlotus.sessions.storage import SessionFile

    storage = SessionFile.create(tmp_path / "roles", "project").role_file("worker")
    for task in ("waiting", "finished"):
        storage.save_context([ModelRequest([], instructions=f"fixed {task}")], turn_id="one", agent_id=task)
    storage.compact(keep_turn_ids=set(), release_turn_id="one", keep_agent_ids={"waiting"})
    restored = SessionFile.load(storage.path)
    assert restored.model_messages(agent_id="waiting")[0].instructions == "fixed waiting"
    assert restored.model_messages(agent_id="finished") == []


@pytest.fixture
async def child_executor(tmp_path, monkeypatch):
    from redlotus.core.agents import AgentRegistry, SubagentFactory
    from redlotus.runtime import logging as logger
    from redlotus.runtime.resources import WorkspaceContext
    from redlotus.sessions.context import ChatHistory
    from redlotus.sessions.storage import SessionFile
    from redlotus.tools import worker_tools as module

    (tmp_path / "config.json").write_text(
        '{"lifecycle":{"invocation_history_per_session":8},"storage":{"project_dir":".redlotus"}}', encoding="utf-8",
    )
    factory, registry = SubagentFactory(max_concurrent=1), AgentRegistry()
    captured = {}

    async def close():
        captured["closed"] = True

    toolkit = SimpleNamespace(
        workspace=WorkspaceContext.from_path(tmp_path),
        clone_for_worker=lambda loop: SimpleNamespace(skills_manager=None, close=close),
    )

    async def persist(operation, **kwargs):
        return operation()

    def create(target, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(module.ModelTarget, "for_role", lambda role: object())
    monkeypatch.setattr(module, "create_worker_toolsets", lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(factory, "create_toolset", lambda *args, **kwargs: object())
    monkeypatch.setattr(module, "get_agent_usage_limits", lambda: None)
    monkeypatch.setattr(logger, "debug", lambda *args, **kwargs: None)
    monkeypatch.setattr(factory, "create_agent", create)
    for role in ("worker", "manager"):
        monkeypatch.setattr(module, f"get_{role}_system_prompt", lambda *args: "rebuilt")
    orchestrator = module.WorkerOrchestrator(
        toolkit, None, memory=None, registry=registry, persist=persist, factory=factory,
    )
    orchestrator.session_file = SessionFile.create(tmp_path / "child", "project")
    orchestrator.set_session_key(orchestrator.session_file.session_id)
    history = ChatHistory()
    history.set_messages([ModelRequest([], instructions="original session instructions")])
    try:
        yield module, orchestrator, history, captured
    finally:
        await factory.close()


@pytest.mark.parametrize("role", ["worker", "manager"])
async def test_child_reuses_prompt_and_commits_compression_before_next_request(child_executor, monkeypatch, role):
    module, orchestrator, history, captured = child_executor
    from redlotus.sessions.context import SubagentResult, make_agent_id

    candidate = [ModelRequest([], instructions="original session instructions")]
    agent_id = make_agent_id(orchestrator._session_key, role, "A")

    async def run(self, **kwargs):
        assert captured["instructions"] == "original session instructions"
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

    async def disk_full(operation, **kwargs):
        raise OSError("injected full disk")

    async def run(self, **kwargs):
        await captured["persist_context"]([ModelRequest([], instructions="fixed")])
        sent.append(True)

    monkeypatch.setattr(type(orchestrator.factory.runner), "run", run)
    orchestrator._persist = disk_full
    if role == "manager":
        with pytest.raises(OSError, match="injected full disk"):
            await orchestrator._execute("follow up", history, role=role, task_id="A", turn_id="one")
    else:
        report = await orchestrator._execute("follow up", history, role=role, task_id="A", turn_id="one")
        assert report.status == "unverified" and "injected full disk" in report.summary
    assert not sent and captured["closed"]


async def test_native_sdk_restoration_does_not_duplicate_deferred_catalog():
    from pydantic_ai import Agent
    from pydantic_ai.capabilities import Capability
    from pydantic_ai.models.function import FunctionModel

    from redlotus.core.gateway import RequestPolicy
    from redlotus.prompts.prompt import session_prompt_from_history

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
