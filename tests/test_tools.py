"""File review transaction faults, exercised against real isolated files."""

import threading

import pytest


@pytest.fixture
def reviewed_file(tmp_path):
    from redlotus.tools.base_tools import PendingReviewStore

    path = tmp_path / "review.txt"
    path.write_text("a\nkeep\nz\n", encoding="utf-8")
    store = PendingReviewStore(threading.Lock())
    store.activate(lambda: None)
    store.write(path, path.name, lambda _: "A\nkeep\nZ\n")
    return path, store


@pytest.mark.parametrize("decision", [None, False, True])
def test_review_refuses_external_edit_before_second_write(reviewed_file, decision):
    path, store = reviewed_file
    entry = store.get(str(path))
    if decision is not None:
        store.decide(entry, 0, decision)
    owner_text = path.read_text(encoding="utf-8") + "owner edit\n"
    path.write_text(owner_text, encoding="utf-8")
    with pytest.raises(ValueError, match="之外"):
        store.write(path, path.name, lambda previous: previous + "agent edit\n")
    assert path.read_text(encoding="utf-8") == owner_text
    assert store.get(str(path)) is entry
    with pytest.raises(ValueError, match="之外"):
        store.decide(entry, 1, True)
    assert path.read_text(encoding="utf-8") == owner_text


def test_second_write_keeps_accepted_changes_as_the_new_baseline(reviewed_file):
    path, store = reviewed_file
    store.decide(store.get(str(path)), 0, False)
    store.write(path, path.name, lambda previous: previous + "later\n")
    entry = store.get(str(path))
    for hunk in entry.hunks:
        store.decide(entry, hunk.index, True)
    assert path.read_text(encoding="utf-8") == "A\nkeep\nz\n"


def test_reject_all_after_partial_rejection_and_another_write(reviewed_file):
    path, store = reviewed_file
    store.decide(store.get(str(path)), 0, True)
    store.write(path, path.name, lambda previous: previous + "later\n")
    entry = store.get(str(path))
    for hunk in entry.hunks:
        store.decide(entry, hunk.index, True)
    assert path.read_text(encoding="utf-8") == "a\nkeep\nz\n"


def test_review_refuses_external_deletion_of_an_empty_original(tmp_path):
    from redlotus.tools.base_tools import PendingReviewStore

    path = tmp_path / "empty.txt"
    path.touch()
    store = PendingReviewStore(threading.Lock())
    store.activate(lambda: None)
    store.write(path, path.name, lambda _: "new")
    entry = store.get(str(path))
    store.decide(entry, 0, True)
    path.unlink()
    with pytest.raises(ValueError, match="之外"):
        store.decide(entry, 0, False)
    assert not path.exists()
    with pytest.raises(ValueError, match="之外"):
        store.write(path, path.name, lambda _: "replacement")
    assert not path.exists()


@pytest.mark.parametrize("operation", ["write", "reject"])
def test_failed_write_preserves_file_and_review_state(reviewed_file, monkeypatch, operation):
    from pathlib import Path

    path, store = reviewed_file
    entry = store.get(str(path))
    original = path.read_bytes()
    original_write = Path.write_text

    def fail_write(target, content, *args, **kwargs):
        original_write(target, content[:1], *args, **kwargs)
        raise OSError("injected disk failure")

    monkeypatch.setattr(Path, "write_text", fail_write)
    with pytest.raises(OSError, match="disk failure"):
        if operation == "write":
            store.write(path, path.name, lambda _: "replacement")
        else:
            store.decide(entry, 0, True)
    assert path.read_bytes() == original
    assert store.get(str(path)) is entry and not entry.decisions


def test_rejecting_new_file_removes_only_the_reviewed_version(tmp_path):
    from redlotus.tools.base_tools import PendingReviewStore

    path = tmp_path / "created.txt"
    store = PendingReviewStore(threading.Lock())
    store.activate(lambda: None)
    store.write(path, path.name, lambda _: "new")
    entry = store.get(str(path))
    assert store.decide(entry, 0, True)
    assert not path.exists()
    assert store.decide(entry, 0, True)
def test_application_structure_keeps_approved_module_and_effective_line_limits():
    import ast
    import io
    import tokenize
    from collections import Counter
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src/redlotus"
    counts = Counter()
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if relative.parts[:2] == ("tools", "skills"):
            continue  # Bundled third-party Skills are not application implementation.
        counts[relative.parts[0]] += 1
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        docstrings = {
            line for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str)
            for line in range(node.body[0].lineno, node.body[0].end_lineno + 1)
        }
        effective = sum(token.type == tokenize.NEWLINE and token.start[0] not in docstrings
                        for token in tokenize.generate_tokens(io.StringIO(source).readline))
        assert effective <= 500, (str(relative), effective)
    assert len(counts) <= 8 and max(counts.values()) <= 5, dict(counts)


async def test_toolset_telemetry_preserves_results_without_execution_policy(monkeypatch):
    from pydantic_ai import ToolReturn

    from redlotus.core.gateway import create_function_toolset
    from redlotus.sessions.context import TRACE_STORE, turn_context
    from redlotus.tools import registry

    result = ToolReturn(return_value="original")
    notices = []
    monkeypatch.setattr(registry.logger, "debug", notices.append)

    def sync_tool():
        return result

    async def async_tool():
        return result

    def failed_tool():
        raise ValueError("actual tool failure")

    tools = create_function_toolset([sync_tool, async_tool, failed_tool]).tools
    with turn_context("telemetry-contract"):
        assert tools["sync_tool"].function() is result
        assert await tools["async_tool"].function() is result
        with pytest.raises(ValueError, match="actual tool failure"):
            tools["failed_tool"].function()
    events = TRACE_STORE.events_for_turn("telemetry-contract")
    assert len(notices) == 3
    assert [(e["tool_name"], e["success"]) for e in events] == [
        ("sync_tool", True), ("async_tool", True), ("failed_tool", False),
    ]


@pytest.mark.parametrize("requested", [None, 300])
async def test_all_external_processes_obey_configured_command_deadline(tmp_path, monkeypatch, requested):
    import json
    import subprocess
    import sys

    from redlotus.tools import execution

    (tmp_path / "config.json").write_text(json.dumps({
        "agent_run_policy": {"max_concurrent_threads_per_session": 1, "max_command_timeout_seconds": 1},
        "storage": {"runtime_dir": "WorkDatabase/runtime"},
    }), encoding="utf-8")
    execute = execution._run_owned_process

    async def verify_timeout(*args, **kwargs):
        assert kwargs["timeout"] == 1
        return await execute(*args, **kwargs)

    monkeypatch.setattr(execution, "_run_owned_process", verify_timeout)
    with pytest.raises(subprocess.TimeoutExpired) as expired:
        await execution.run_subprocess([sys.executable, "-c", "import time; time.sleep(30)"],
                                       shell=False, cwd=str(tmp_path), timeout=requested)
    assert expired.value.timeout == 1


async def test_reference_download_updates_its_http_timeout_and_keeps_contents(tmp_path, monkeypatch):
    import json
    from functools import partial

    import httpx

    from redlotus.runtime.network import ModelInputPolicy, close_all_clients
    from redlotus.runtime.resources import WorkspaceContext
    from redlotus.tools.references import ReferenceStore

    (tmp_path / "config.json").write_text(json.dumps({"storage": {"references_dir": "references"}}), encoding="utf-8")
    observed = []

    def respond(request):
        observed.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, text="unchanged contents")

    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(respond)))
    store = ReferenceStore(WorkspaceContext.from_path(tmp_path))
    try:
        for timeout in (2, 7):
            policy = ModelInputPolicy(max_files=1, max_file_bytes=1000, reference_download_timeout_seconds=timeout)
            reference = await store.import_url("https://example.invalid/input.txt", policy=policy)
            assert reference.parts[0].text == "unchanged contents"
        assert observed == [2, 7]
    finally:
        await close_all_clients()


async def test_browser_updates_separate_action_and_navigation_policies(tmp_path):
    import json
    from types import SimpleNamespace

    from redlotus.tools.execution import PlaywrightBrowserSession

    observed, navigations = {}, []

    async def goto(url, **kwargs):
        navigations.append(kwargs.get("timeout", observed.get("navigation")))

    async def title():
        return "Fixture"

    browser = PlaywrightBrowserSession(None)
    browser._page = SimpleNamespace(url="https://example.invalid", goto=goto, title=title,
                                   set_default_timeout=lambda value: observed.update(action=value),
                                   set_default_navigation_timeout=lambda value: observed.update(navigation=value))
    for action, navigation in ((2, 7), (3, 11)):
        (tmp_path / "config.json").write_text(json.dumps({"browser": {
            "action_timeout_seconds": action, "navigation_timeout_seconds": navigation,
        }}), encoding="utf-8")
        assert "Fixture" in await browser.browser_navigate("https://example.invalid")
        assert observed["action"] == action * 1000 and navigations[-1] == navigation * 1000


@pytest.mark.parametrize("retries", [0, 1])
async def test_task_retry_limit_comes_from_config(tmp_path, retries):
    import json

    from redlotus.core.tasks import TaskManager
    from redlotus.sessions.context import SubagentResult

    (tmp_path / "config.json").write_text(json.dumps({"agent_run_policy": {"max_task_retries": retries}}))
    manager = TaskManager()
    await manager.create_todo_list('[{"id":"A","description":"explicit failure"}]')
    task = manager.tasks["A"]
    assert task.max_retries == retries
    await manager.finish(task, SubagentResult(status="failed", summary="did not execute"))
    assert task.status.value == ("pending" if retries else "failed")
    restored = TaskManager()
    restored.restore(manager.snapshot())
    assert restored.tasks["A"].max_retries == retries
    records = manager.snapshot()
    records[0]["max_retries"] = 3  # Pre-migration checkpoint must not override a new policy.
    (tmp_path / "config.json").write_text('{"agent_run_policy":{"max_task_retries":0}}')
    restored.restore(records)
    await restored.finish(restored.tasks["A"], SubagentResult(status="failed", summary="explicit failure"))
    assert restored.tasks["A"].status.value == "failed"
    assert "max_retries" not in restored.snapshot()[0]


def test_log_retention_and_rotation_follow_config(tmp_path, monkeypatch):
    import json
    import os
    import time
    from contextlib import contextmanager
    from types import SimpleNamespace

    from redlotus.runtime import logging as logger

    (tmp_path / "config.json").write_text(json.dumps({"storage": {"cleanup": {
        "log_retention_days": 1, "session_log_max_bytes": 1,
    }}}))
    monkeypatch.setattr(logger, "_configured_dir", tmp_path)
    path = tmp_path / "fixture.log"
    path.write_text("first", encoding="utf-8")

    class Message(str):
        record = {"extra": {"session": "fixture"}}

    @contextmanager
    def contextualize(**extra):
        Message.record["extra"] = extra
        yield

    monkeypatch.setattr(logger, "_lg", SimpleNamespace(contextualize=contextualize))
    with logger.session_log_context("fixture"):
        with monkeypatch.context() as scoped:
            scoped.setattr(logger, "settings", lambda: pytest.fail("A log record must use its session's policy snapshot"))
            logger._session_sink(Message("second"))
    assert path.read_text() == "second"
    backup = path.with_name("fixture.log.1")
    assert backup.read_text() == "first"
    os.utime(backup, (time.time() - 2 * 86400,) * 2)
    logger.prune_old_logs()
    assert path.is_file() and not backup.exists()
