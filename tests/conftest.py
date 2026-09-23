"""Auxiliary fault checks never use the owner's configuration or saved sessions."""

import os
import json
import subprocess
import sys
import asyncio
import threading
import time
from functools import partial
from unittest.mock import AsyncMock

import httpx
import pytest
import requests

from redlotus.runtime import logging as logger
from redlotus.runtime.network import close_all_clients


@pytest.fixture
def browser_startup(tmp_path, monkeypatch, headless, viewport, locale):
    from types import SimpleNamespace
    from playwright import async_api
    from redlotus.tools.execution import PlaywrightBrowserSession

    (tmp_path / "config.json").write_text(json.dumps({
        "BROWSER_HEADLESS": headless, "browser": {"viewport": viewport, "locale": locale},
    }))
    new_page = AsyncMock(return_value=object())
    launch = AsyncMock(return_value=SimpleNamespace(new_page=new_page))
    stop = AsyncMock()
    start = AsyncMock(return_value=SimpleNamespace(chromium=SimpleNamespace(launch=launch), stop=stop))
    monkeypatch.setattr(async_api, "async_playwright", lambda: SimpleNamespace(start=start))
    return PlaywrightBrowserSession(None), launch, new_page, stop


@pytest.fixture
def reference_inputs(tmp_path, monkeypatch, parallelism):
    from redlotus.runtime.network import ModelInputPolicy
    from redlotus.tools.references import ReferenceStore

    (tmp_path / "config.json").write_text(json.dumps({
        "input_limits": {"parse_concurrency": parallelism},
        "storage": {"file_lock_timeout_seconds": 2, "references_dir": "references"},
    }))
    paths = [tmp_path / f"input-{n}.txt" for n in range(4)]
    for path in paths:
        path.write_text(path.stem, encoding="utf-8")
    policy = ModelInputPolicy(max_files=4, max_file_bytes=1000, reference_download_timeout_seconds=2)
    monkeypatch.setattr(ModelInputPolicy, "for_role", lambda role: policy)
    parse, observed = ReferenceStore.parse, {"active": 0, "peak": 0}

    async def observe(self, reference):
        observed["active"] += 1
        observed["peak"] = max(observed["peak"], observed["active"])
        try:
            await asyncio.sleep(.03)
            return await parse(self, reference)
        finally:
            observed["active"] -= 1

    monkeypatch.setattr(ReferenceStore, "parse", observe)
    return paths, observed


@pytest.fixture(params=["\n", "\r\n"])
def reviewed_file(tmp_path, request):
    from redlotus.tools.base_tools import PendingReviewStore

    path = tmp_path / "review.txt"
    path.write_bytes("a{0}keep{0}z{0}".format(request.param).encode())
    store = PendingReviewStore(threading.Lock())
    store.activate(lambda: None)
    store.write(path, path.name, lambda _: "A{0}keep{0}Z{0}".format(request.param))
    return path, store, request.param


@pytest.fixture
def project_junction(tool_workspace, tmp_path):
    toolkit, _ = tool_workspace
    outside, link = tmp_path / "outside", toolkit.workspace.root / "external"
    outside.mkdir()
    (outside / "secret.txt").write_text("boundary-marker outside")
    (toolkit.workspace.root / "inside.txt").write_text("boundary-marker inside")
    if os.name == "nt":
        command = "New-Item -ItemType Junction -Path '{}' -Target '{}' | Out-Null".format(
            str(link).replace("'", "''"), str(outside).replace("'", "''"))
        subprocess.run(["powershell", "-NoProfile", "-Command", command], check=True, capture_output=True)
    else:
        link.symlink_to(outside, target_is_directory=True)
    return toolkit, link


@pytest.fixture
async def image_responses(tmp_path, monkeypatch):
    monkeypatch.setattr(logger, "_configured_dir", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({
        "BFL_BASE_URL": "https://fixture.invalid/generate", "BFL_API_KEY": "synthetic-test-key", "input_limits": {"max_redirects": 1},
        "image_generation": {"width": 64, "height": 32, "max_wait_seconds": 2, "http_timeout_seconds": 3,
                             "poll_interval_seconds": .01, "progress_every_polls": 1},
    }))
    observed, sleeps, polls = [], [], []

    def respond(request):
        observed.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"id": "fixture", "polling_url": "https://fixture.invalid/poll"})
        if request.url.path == "/poll":
            polls.append(True)
            return httpx.Response(200, json={"status": "Pending"} if len(polls) == 1 else {
                "status": "Ready", "result": {"sample": "https://fixture.invalid/image"}})
        if request.url.path == "/image":
            return httpx.Response(302, headers={"location": "https://cdn.invalid/final-image"})
        return httpx.Response(200, content=b"complete image", headers={"content-type": "image/png"})

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=transport))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(side_effect=sleeps.append))
    with httpx.Client(transport=transport) as legacy:
        monkeypatch.setattr(requests, "post", legacy.post)
        monkeypatch.setattr(requests, "get", legacy.get)
        try:
            yield observed, sleeps
        finally:
            await close_all_clients()


@pytest.fixture
async def image_deadline(tmp_path, monkeypatch, phase):
    monkeypatch.setattr(logger, "_configured_dir", tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({
        "BFL_BASE_URL": "https://fixture.invalid/generate", "BFL_API_KEY": "synthetic-test-key", "input_limits": {"max_redirects": 1},
        "image_generation": {"width": 64, "height": 32, "max_wait_seconds": .02, "http_timeout_seconds": 1,
                             "poll_interval_seconds": .01, "progress_every_polls": 1},
    }))
    stopped, started = [], asyncio.Event()

    async def delayed(request):
        if request.method == "POST" and phase not in {"submission", "cancel"}:
            return httpx.Response(200, json={"id": "fixture", "polling_url": "https://fixture.invalid/poll"})
        if request.url.path == "/poll" and phase == "download":
            return httpx.Response(200, json={"status": "Ready", "result": {"sample": "https://fixture.invalid/image"}})
        started.set()
        try:
            await asyncio.sleep(1)
        finally:
            stopped.append(True)

    def legacy_post(*args, **kwargs):
        time.sleep(.04)
        return httpx.Response(200, json={}, request=httpx.Request("POST", args[0]))

    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(delayed)))
    monkeypatch.setattr(requests, "post", legacy_post)
    try:
        yield stopped, started
    finally:
        await close_all_clients()


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    for name, path in {
        "REDLOTUS_CONFIG_FILE": tmp_path / "config.json",
        "REDLOTUS_DOTENV_FILE": tmp_path / ".env",
        "REDLOTUS_CONFIG_DIR": tmp_path / "global",
        "REDLOTUS_DATA_DIR": tmp_path / "data",
    }.items():
        monkeypatch.setenv(name, str(path))


@pytest.fixture
def journal_policy(tmp_path):
    (tmp_path / ".env").write_text("storage__file_lock_timeout_seconds=30\n")


@pytest.fixture
def tool_workspace(tmp_path, monkeypatch):
    from redlotus.runtime.resources import WorkspaceContext
    from redlotus.tools import registry
    from redlotus.tools.base_tools import BasicToolkit

    root, bundled = tmp_path / "project", tmp_path / "bundled-skills"
    root.mkdir()
    bundled.mkdir()
    (tmp_path / "config.json").write_text(json.dumps({"storage": {
        "runtime_dir": "WorkDatabase/runtime", "references_dir": "references",
    }}))
    monkeypatch.setattr(registry, "skills_dir", lambda: bundled)
    monkeypatch.setattr(registry, "user_skills_dir", lambda: root / "overlay")
    return BasicToolkit(None, workspace=WorkspaceContext.from_path(root), show_diff=lambda *args: (0, 0, 0)), bundled / "asset.png"


@pytest.fixture
def partial_write_failure(monkeypatch):
    from redlotus.runtime import resources

    original = resources.NamedTemporaryFile

    def temporary(*args, **kwargs):
        owned = original(*args, **kwargs)
        write = owned.write

        def fail(content):
            write(content[:1])
            raise OSError("injected disk failure")

        owned.write = fail
        return owned

    monkeypatch.setattr(resources, "NamedTemporaryFile", temporary)


@pytest.fixture
def agent_system(tmp_path):
    from types import SimpleNamespace

    from redlotus.core.system import AgentSystem
    from redlotus.runtime.resources import WorkspaceContext
    from redlotus.sessions.control import SessionController

    system = object.__new__(AgentSystem)
    system.workspace = WorkspaceContext.from_path(tmp_path)
    system._session_file = None
    system._session = SessionController()
    system.presentation = SimpleNamespace(print_warning=lambda text: None)
    return system


@pytest.fixture(params=["disk", "coordinator", "worker"])
def checkpoint_failure(tmp_path, request):
    from functools import partial
    from unittest.mock import Mock
    from filelock import FileLock
    from redlotus.sessions.storage import SessionFile

    (tmp_path / "config.json").write_text('{"storage":{"file_lock_timeout_seconds":0.05}}')
    if request.param == "disk":
        yield None, Mock(side_effect=OSError("injected disk failure")), lambda: None
        return
    storage = SessionFile.create(tmp_path / "sessions", "isolated-lock")
    if request.param == "worker":
        storage = storage.role_file("worker")
    with FileLock(storage.path.with_suffix(".lock")) as lock:
        yield storage, partial(storage.update, metadata={"proof": "unexpected write"}), lock.release
    assert "proof" not in storage.metadata


@pytest.fixture
def interactive_python(tmp_path):
    """Keep a native host's CLI input pipe open while it runs the test source."""
    def run(source):
        environment = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
        with subprocess.Popen(
            [sys.executable, "-c", source], cwd=tmp_path, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        ) as child:
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
                raise
            finally:
                child.stdin.close()
            return subprocess.CompletedProcess(child.args, child.returncode, child.stdout.read(), child.stderr.read())
    return run


@pytest.fixture
def publication(tmp_path, monkeypatch):
    """Real session and memory storage with offline vector and model boundaries."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from redlotus.memory import records, service as memory_service
    from redlotus.memory.perception import MemoryJob
    from redlotus.memory.store import MemoryStore
    from redlotus.runtime.resources import WorkspaceContext
    from redlotus.sessions.storage import SessionFile

    (tmp_path / "config.json").write_text(json.dumps({
        "storage": {"file_lock_timeout_seconds": 0, "project_dir": ".redlotus", "references_dir": "WorkDatabase/references"},
        "memory_perception": {}, "rag_service": {"index_batch_size": 2},
    }), encoding="utf-8")
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
    service, store = object.__new__(memory_service.MemoryService), object.__new__(MemoryStore)
    service.workspace = store.workspace = WorkspaceContext(tmp_path, "isolated")
    store.path, store._db, store._index_lock = tmp_path / "database", None, asyncio.Lock()
    store.last_error = store.retrieval_error = ""
    indexed = set()
    index = SimpleNamespace(index_key="fixture", project_id="isolated", last_error="", config={"final_top_k": 5},
        refresh_embedding_space=AsyncMock(), indexed_record_ids=AsyncMock(side_effect=lambda: set(indexed)),
        retrieve=AsyncMock(return_value=[]), prepare_records=AsyncMock(side_effect=lambda rows: rows),
        write_records=AsyncMock(side_effect=lambda rows: indexed.update(row["record_id"] for row in rows)),
        delete_records=AsyncMock(), _db=SimpleNamespace(ensure_vector_index=AsyncMock()))
    store.indexes = {"global": index, "project": index}
    writes, model_calls = Mock(wraps=store.save), AsyncMock(side_effect=AssertionError("Projection retry replayed model"))
    monkeypatch.setattr(store, "save", writes)
    monkeypatch.setattr(store, "rag_unavailable_reason", lambda scope=None: "")
    service.long_term, service.last_error, service.current = memory, "", None
    service.store, service.owner_memory_allowed, service._processing, service._explicit = store, True, asyncio.Lock(), asyncio.Lock()
    service.session = SessionFile.create(tmp_path / "sessions", "isolated", session_id="fixture")
    service.observations = records.ObservationStore(service.workspace, window_turns=1, overlap_turns=0)
    service.observations.bind(service.session)
    service.observations.finish(event)
    service.evidence = records.EvidenceReader(records.ReferenceStore(service.workspace))
    service.evidence.session = service.session
    job.searches[0]["revision"] = store.revision("global")
    monkeypatch.setattr(service, "_route", lambda: "fixture")
    for level in ("error", "warning"):
        monkeypatch.setattr(memory_service.logger, level, lambda *args, **kwargs: None)
    monkeypatch.setattr(memory_service, "produce_job", model_calls)
    return SimpleNamespace(service=service, store=store, index=index, job=job, row=row, writes=writes,
                           model_calls=model_calls, memory=memory, original=original, records=records)


@pytest.fixture
def task_plan(tmp_path, journal_policy):
    import asyncio
    from redlotus.core.tasks import TaskManager
    from redlotus.sessions.storage import SessionFile

    (tmp_path / "config.json").write_text('{"agent_run_policy":{"max_task_retries":3}}')
    storage = SessionFile.create(tmp_path / "plan", "isolated-project")
    inputs = {"turn": "first", "texts": ["Plan A, then B; C is independent."]}

    async def persist(tasks):
        await asyncio.to_thread(storage.update, metadata={"tasks": tasks})

    manager = TaskManager(persist=persist, input_source=lambda: (inputs["turn"], inputs["texts"]))
    return manager, storage, inputs


@pytest.fixture
async def child_executor(tmp_path, monkeypatch, journal_policy):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from pydantic_ai.messages import ModelRequest

    from redlotus.core.agents import AgentRegistry, SubagentFactory
    from redlotus.runtime import logging as logger
    from redlotus.runtime.resources import WorkspaceContext
    from redlotus.sessions.context import ChatHistory
    from redlotus.sessions.storage import SessionFile
    from redlotus.tools import worker_tools as module

    (tmp_path / "config.json").write_text(
        '{"lifecycle":{"invocation_history_per_session":8,"trace_history_turns":10},"storage":{"project_dir":".redlotus"}}', encoding="utf-8",
    )
    factory, registry = SubagentFactory(max_concurrent=1), AgentRegistry()
    captured = {}

    async def close():
        captured["closed"] = True

    toolkit = SimpleNamespace(
        workspace=WorkspaceContext.from_path(tmp_path),
        clone_for_worker=lambda loop: SimpleNamespace(skills_manager=None, close=close),
    )

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
        toolkit, None, memory=None, registry=registry, persist=AsyncMock(side_effect=lambda operation, **kwargs: operation()), factory=factory,
        user_inputs=lambda: ["Generate directly; no tools."],
    )
    orchestrator.session_file = SessionFile.create(tmp_path / "child", "project")
    orchestrator.set_session_key(orchestrator.session_file.session_id)
    history = ChatHistory()
    history.set_messages([ModelRequest([], instructions="original session instructions")])
    try:
        yield module, orchestrator, history, captured
    finally:
        await factory.close()
