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
    }, "API_KEY": "isolated-fixture", "request_limit": 17}
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
        os.utime(session.path, (0, 0))
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
        {"id": "A", "description": "done", "status": "completed", "result": "saved", "artifacts": ["A.txt"]},
        {"id": "B", "description": "in flight", "dependencies": ["A"], "status": "running", "artifacts": ["B.txt"]},
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
    target = SimpleNamespace(name="fixture", protocol="fixture", limits={"max_files": 1, "max_file_bytes": 1000})

    def agent(instructions):
        return Agent(model, instructions=instructions, capabilities=[
            Capability(id="fixture_files", description="Read isolated fixtures", defer_loading=True),
            RequestPolicy("fixture", target, model),
        ])

    first = await agent("Original role snapshot").run("first")
    restored = session_prompt_from_history(first.all_messages())
    assert restored == "Original role snapshot"
    # Old serialized requests lack boundary metadata; the native SDK catalog is not role text.
    legacy = ModelRequest([], instructions=requests[0])
    assert session_prompt_from_history([legacy]) == restored
    await agent(restored).run("second", message_history=first.all_messages())
    assert requests[0] == requests[1]
