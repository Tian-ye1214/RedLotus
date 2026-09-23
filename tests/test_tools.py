"""Tool execution, file review, and reference parsing on isolated inputs."""

import asyncio
import csv
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from filelock import FileLock, Timeout

from redlotus.core.agents import SubagentHandle
from redlotus.runtime import logging as logger, resources
from redlotus.runtime.network import ModelInputPolicy, close_all_clients
from redlotus.runtime.resources import WorkspaceContext, file_lock
from redlotus.sessions.context import SubagentResult, SubagentSpec, TRACE_STORE, TurnTraceStore, turn_context
from redlotus.tools import base_tools, execution, registry
from redlotus.tools.base_tools import BasicToolkit, PendingReviewStore, generate_image_from_flux
from redlotus.tools.execution import PlaywrightBrowserSession, _terminate_process_tree
from redlotus.tools.references import DocumentReader, ReferenceStore


@pytest.mark.parametrize("decision", [None, False, True])
def test_review_refuses_external_edit_before_second_write(reviewed_file, decision):
    path, store, _ = reviewed_file
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
    path, store, _ = reviewed_file
    store.decide(store.get(str(path)), 0, False)
    store.write(path, path.name, lambda previous: previous + "later\n")
    entry = store.get(str(path))
    for hunk in entry.hunks:
        store.decide(entry, hunk.index, True)
    assert path.read_text(encoding="utf-8") == "A\nkeep\nz\n"


def test_reject_all_after_partial_rejection_and_another_write(reviewed_file):
    path, store, newline = reviewed_file
    store.decide(store.get(str(path)), 0, True)
    store.write(path, path.name, lambda previous: previous + "later\n")
    entry = store.get(str(path))
    for hunk in entry.hunks:
        store.decide(entry, hunk.index, True)
    assert path.read_bytes() == f"a{newline}keep{newline}z{newline}".encode()


def test_review_refuses_external_deletion_of_an_empty_original(tmp_path):
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
def test_failed_write_preserves_file_and_review_state(reviewed_file, partial_write_failure, operation):

    path, store, _ = reviewed_file
    entry = store.get(str(path))
    original = path.read_bytes()
    with pytest.raises(OSError, match="disk failure"):
        if operation == "write":
            store.write(path, path.name, lambda _: "replacement")
        else:
            store.decide(entry, 0, True)
    assert path.read_bytes() == original
    assert store.get(str(path)) is entry and not entry.decisions
    assert set(path.parent.iterdir()) == {path}


def test_rejecting_new_file_removes_only_the_reviewed_version(tmp_path):
    path = tmp_path / "created.txt"
    store = PendingReviewStore(threading.Lock())
    store.activate(lambda: None)
    store.write(path, path.name, lambda _: "new")
    entry = store.get(str(path))
    assert store.decide(entry, 0, True)
    assert not path.exists()
    assert store.decide(entry, 0, True)


@pytest.mark.parametrize("content,encoding", [("line\n雪\r\n", "utf-8"), ("line\n雪\r\n", "utf-16"), (bytes(range(256)), None)],
                         ids=["utf8-text", "utf16-text", "raw-bytes"])
def test_atomic_write_preserves_native_encoding_and_original_on_replace_failure(tmp_path, monkeypatch, content, encoding):

    path, native = tmp_path / "owned.bin", tmp_path / "native.bin"
    legacy = path.with_suffix(".bin.tmp")
    legacy.write_bytes(b"owner's unrelated temporary file")
    if isinstance(content, str):
        native.write_text(content, encoding=encoding)
    else:
        native.write_bytes(content)
    resources.atomic_write(path, content, encoding=encoding)
    assert path.read_bytes() == native.read_bytes()
    assert legacy.read_bytes() == b"owner's unrelated temporary file"
    assert set(tmp_path.iterdir()) == {path, native, legacy}

    monkeypatch.setattr(resources.os, "replace", MagicMock(side_effect=PermissionError("isolated replace failure")))
    with pytest.raises(PermissionError, match="replace failure"):
        resources.atomic_write(path, content[:1], encoding=encoding)
    assert path.read_bytes() == native.read_bytes()
    assert legacy.read_bytes() == b"owner's unrelated temporary file"
    assert set(tmp_path.iterdir()) == {path, native, legacy}


def test_atomic_write_uses_distinct_owned_temporaries_for_concurrent_writers(tmp_path, monkeypatch):
    path, legacy = tmp_path / "report.txt", tmp_path / "report.txt.tmp"
    legacy.write_bytes(b"owner")
    replace, barrier, commit, names = resources.os.replace, threading.Barrier(2), threading.Lock(), []

    def overlap(source, destination):
        names.append(Path(source))
        barrier.wait(timeout=3)
        with commit:  # Staging overlaps; native Windows destination renames are ordered.
            replace(source, destination)

    monkeypatch.setattr(resources.os, "replace", overlap)
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(resources.atomic_write, path, value) for value in (b"A" * 4096, b"B" * 4096)]
        for future in futures:
            future.result(timeout=5)
    assert len(set(names)) == 2 and all(name.parent == tmp_path for name in names)
    assert path.read_bytes() in (b"A" * 4096, b"B" * 4096)
    assert legacy.read_bytes() == b"owner" and set(tmp_path.iterdir()) == {path, legacy}


def test_finishing_review_keeps_a_concurrent_worker_version(reviewed_file, monkeypatch):
    path, store, _ = reviewed_file
    old = store.get(str(path))
    for hunk in old.hunks:
        assert store.decide(old, hunk.index, False)
    captured, resume = threading.Event(), threading.Event()
    entries = store.entries

    def snapshot():
        result = entries()
        captured.set()
        assert resume.wait(3)
        return result

    monkeypatch.setattr(store, "entries", snapshot)
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(store.finish_decided)
        try:
            assert captured.wait(3)
            store.write(path, path.name, lambda previous: previous + "Worker's new version\n")
            current = store.get(str(path))
        finally:
            resume.set()
        pending.result(timeout=5)
    assert current is not old and store.get(str(path)) is current and not current.decisions
    store.finish_decided()
    assert store.get(str(path)) is current
    for hunk in current.hunks:
        assert store.decide(current, hunk.index, False)
    store.finish_decided()
    assert store.get(str(path)) is None


def test_search_does_not_read_files_beyond_a_project_junction(project_junction):
    toolkit, link = project_junction
    assert (link / "secret.txt").read_text() == "boundary-marker outside"
    with pytest.raises(ValueError, match="Path not allowed"):
        toolkit._readable_path(str(link / "secret.txt"))
    result = toolkit.search_in_files("boundary-marker", ".txt")
    assert "inside.txt:1: boundary-marker inside" in result
    assert "outside" not in result and "secret.txt" not in result


@pytest.mark.parametrize("destination", ["project", "absolute-skill", "skill-alias"])
async def test_browser_screenshot_preserves_read_only_skills(tool_workspace, destination):
    toolkit, skill = tool_workspace
    skill.write_bytes(b"packaged skill")
    assert toolkit.read_file(str(skill)).return_value == "packaged skill"
    browser = toolkit._browser_session
    browser._page = SimpleNamespace(screenshot=AsyncMock(side_effect=lambda path, **kwargs: Path(path).write_bytes(b"screenshot")))
    name = {"project": "screens/new.png", "absolute-skill": str(skill), "skill-alias": "skills/asset.png"}[destination]
    if destination == "project":
        assert "Screenshot saved" in await browser.browser_screenshot.__wrapped__(browser, name, full_page=True)
        assert (toolkit.workspace.root / name).read_bytes() == b"screenshot"
        browser._page.screenshot.assert_awaited_once_with(path=str(toolkit.workspace.root / name), full_page=True)
    else:
        with pytest.raises(ValueError, match="Path not under current project"):
            await browser.browser_screenshot.__wrapped__(browser, name)
        browser._page.screenshot.assert_not_awaited()
    assert skill.read_bytes() == b"packaged skill"


@pytest.mark.parametrize("timeout", [0, .05])
def test_shared_file_lock_obeys_configured_wait_under_real_contention(tmp_path, timeout):
    path = tmp_path / "owned.json"

    def acquire():
        with file_lock(path):
            return "acquired"

    with ThreadPoolExecutor(max_workers=1) as executor:
        with file_lock(path, timeout=1):
            (tmp_path / "config.json").write_text(json.dumps({"storage": {"file_lock_timeout_seconds": timeout}}))
            waiting = executor.submit(acquire)
            with pytest.raises(Timeout):
                waiting.result(timeout=1)
    assert not path.exists()


async def test_first_configuration_commit_uses_the_pending_lock_policy(tmp_path, monkeypatch):

    from redlotus.api.base import ConfigurationSetup

    observed = []

    class ObservedLock(FileLock):
        def acquire(self, timeout=None, *args, **kwargs):
            observed.append(timeout)
            return super().acquire(timeout, *args, **kwargs)

    monkeypatch.setattr(resources, "FileLock", ObservedLock)
    setup = ConfigurationSetup(AsyncMock(side_effect=["0", "y"]), emit=lambda text: None)
    assert not (tmp_path / "global/config.json").exists()
    assert await setup.fill(("storage", "file_lock_timeout_seconds"))
    assert await setup.commit()
    assert observed and set(observed) == {0}
    assert json.loads((tmp_path / "global/config.json").read_text()) == {"storage": {"file_lock_timeout_seconds": 0}}


def test_application_structure_keeps_approved_module_and_effective_line_limits():
    import ast
    import io
    import tokenize
    from collections import Counter

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


@pytest.mark.parametrize("original", ["", "original", "north\r\nsouth\r\n", "north\nsouth\n", r"north\r\nsouth\r\n", "北\r\n南\n末\r", 'quote"\\\t\0'])
async def test_toolset_telemetry_preserves_results_without_execution_policy(tmp_path, monkeypatch, original):
    from pydantic_ai import ToolReturn

    from redlotus.core.gateway import create_function_toolset

    (tmp_path / "config.json").write_text('{"storage":{"runtime_dir":"WorkDatabase/runtime","references_dir":"references"},"lifecycle":{"trace_history_turns":10},"ui":{"tool_argument_preview_chars":80,"tool_keyword_limit":5,"tool_positional_limit":3}}')
    (tmp_path / "original.txt").write_bytes(original.encode("utf-8"))
    toolkit = BasicToolkit(None, workspace=WorkspaceContext.from_path(tmp_path), show_diff=MagicMock(return_value=(0, 0, 0)))
    result = toolkit.read_file("original.txt")
    assert isinstance(result, ToolReturn)
    assert result.return_value == json.loads(result.content[0])["text"] == (original or "File is empty")
    (tmp_path / "copy.txt").write_bytes(b"before\r\n")
    toolkit._review_store.activate(MagicMock())
    assert toolkit.write_file("copy.txt", content=original + "ordinary\r\n").startswith("Saved")
    assert (tmp_path / "copy.txt").read_bytes() == (original + "ordinary\r\n").encode("utf-8")
    assert toolkit.write_file("copy.txt", copy_from="original.txt").startswith("Saved")
    assert (tmp_path / "copy.txt").read_bytes() == (tmp_path / "original.txt").read_bytes() == original.encode("utf-8")
    (tmp_path / "invalid.txt").write_bytes(b"\xff")
    entry = toolkit._review_store.get(str(tmp_path / "copy.txt"))
    for arguments in ({}, {"content": original, "copy_from": "original.txt"}, {"copy_from": "missing.txt"}, {"copy_from": "invalid.txt"}):
        assert toolkit.write_file("copy.txt", **arguments).startswith("Error")
        assert (tmp_path / "copy.txt").read_bytes() == original.encode("utf-8") and toolkit._review_store.get(str(tmp_path / "copy.txt")) is entry
    for hunk in entry.hunks:
        toolkit._review_store.decide(entry, hunk.index, True)
    assert (tmp_path / "copy.txt").read_bytes() == b"before\r\n"
    notices = []
    monkeypatch.setattr(registry.logger, "debug", notices.append)

    def sync_tool():
        return result

    async def async_tool():
        return result

    def failed_tool():
        raise ValueError("actual tool failure")

    tools = create_function_toolset([sync_tool, async_tool, failed_tool]).tools
    with turn_context(str(tmp_path)):
        assert tools["sync_tool"].function() is result
        assert await tools["async_tool"].function() is result
        with pytest.raises(ValueError, match="actual tool failure"):
            tools["failed_tool"].function()
    events = TRACE_STORE.events_for_turn(str(tmp_path))
    assert len(notices) == 3
    assert [(e["tool_name"], e["success"]) for e in events] == [
        ("sync_tool", True), ("async_tool", True), ("failed_tool", False),
    ]


def test_trace_retention_reads_configuration_only_when_recording(tmp_path):


    for limit in (2, 1):
        trace = TurnTraceStore()  # Import and construction must work before initial setup.
        (tmp_path / "config.json").write_text(json.dumps({"lifecycle": {"trace_history_turns": limit}}))
        for number in range(3):
            trace.record(str(number), "fixture")
        assert not trace.events_for_turn("0")
        assert bool(trace.events_for_turn("1")) == (limit == 2)
        assert trace.events_for_turn("2")[0]["kind"] == "fixture"


@pytest.mark.parametrize("requested", [None, 300])
async def test_all_external_processes_obey_configured_command_deadline(tmp_path, monkeypatch, requested):
    import subprocess
    import sys


    (tmp_path / "config.json").write_text(json.dumps({
        "agent_run_policy": {"max_concurrent_threads_per_session": 1, "max_command_timeout_seconds": 1},
        "lifecycle": {"process_termination_timeout_seconds": 2},
        "storage": {"runtime_dir": "WorkDatabase/runtime"},
    }), encoding="utf-8")
    execute = AsyncMock(wraps=execution._run_owned_process)
    monkeypatch.setattr(execution, "_run_owned_process", execute)
    with pytest.raises(subprocess.TimeoutExpired) as expired:
        await execution.run_subprocess([sys.executable, "-c", "import time; time.sleep(30)"],
                                       shell=False, cwd=str(tmp_path), timeout=requested)
    assert expired.value.timeout == execute.await_args.kwargs["timeout"] == 1


async def test_inherited_pipe_failure_obeys_configured_cleanup_deadline(tmp_path):


    (tmp_path / "config.json").write_text('{"lifecycle":{"process_termination_timeout_seconds":0.02}}')

    with pytest.raises(RuntimeError, match="inherited pipes remain open"):
        await asyncio.wait_for(_terminate_process_tree(SimpleNamespace(returncode=0, communicate=asyncio.Event().wait)), .5)


@pytest.mark.parametrize("shell", [False, True])
def test_owned_command_cannot_read_cli_input(interactive_python, tmp_path, shell):
    (tmp_path / "config.json").write_text('{"lifecycle":{"process_termination_timeout_seconds":2}}')
    (tmp_path / "command.py").write_text("import sys\nassert sys.stdin.read() == ''\nprint(73*79)\n")
    result = interactive_python(f'''
import asyncio, shlex, subprocess, sys
from redlotus.tools.execution import _run_owned_process
args = [sys.executable, "-S", "command.py"]
shell_args = subprocess.list2cmdline(args) if sys.platform == "win32" else shlex.join(args)
result = asyncio.run(_run_owned_process(
    shell_args if {shell!r} else args,
    shell={shell!r}, cwd=".", env=None, timeout=3,
))
assert result.returncode == 0
print(result.stdout, end="")
''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "5767"


@pytest.mark.parametrize("parallelism", [1, 2])
async def test_reference_parser_keeps_input_order_and_obeys_its_own_concurrency(reference_inputs, tmp_path, parallelism):
    from redlotus.sessions.control import load_file_refs

    paths, observed = reference_inputs
    result = await load_file_refs(" ".join(f'@"{path}"' for path in paths), workspace=WorkspaceContext.from_path(tmp_path))
    assert observed == {"peak": parallelism, "active": 0}
    assert [reference.parts[0].text for reference in result] == [path.stem for path in paths]


@pytest.mark.parametrize("headless,expected,viewport,locale", [
    (False, False, {"width": 900, "height": 700}, "en-US"),
    (0, False, {"width": 800, "height": 600}, "fr-FR"),
    ("YES", True, {"width": 640, "height": 480}, "zh-CN"),
])
async def test_browser_launch_uses_explicit_configuration(browser_startup, headless, expected, viewport, locale):
    browser, launch, new_page, stop = browser_startup
    await browser._start()
    launch.assert_awaited_once_with(headless=expected)
    new_page.assert_awaited_once_with(viewport=viewport, locale=locale)
    stop.assert_not_awaited()


async def test_subagent_close_joins_its_thread_without_blocking_the_owner_loop(tmp_path):


    release = threading.Event()
    handle = SubagentHandle(SubagentSpec("fixture", None, WorkspaceContext.from_path(tmp_path)), None)
    handle._future.set_result("cleanup still finishing")
    handle.thread = threading.Thread(target=release.wait)
    handle.thread.start()
    close = asyncio.create_task(handle.close())
    try:
        await asyncio.sleep(.02)
        assert handle.thread.is_alive() and not close.done()
    finally:
        release.set()
        await asyncio.wait_for(close, 1)
    assert not handle.thread.is_alive()


def test_cancelled_close_does_not_keep_the_owner_executor_alive(tmp_path):


    started, release, exited = threading.Event(), threading.Event(), threading.Event()

    async def delayed_cleanup():
        started.set()
        while not release.is_set():
            try:
                await asyncio.sleep(.01)
            except asyncio.CancelledError:
                pass  # Reproduce a child which cannot finish cleanup before the exit deadline.

    handle = SubagentHandle(SubagentSpec("fixture", None, WorkspaceContext.from_path(tmp_path)), delayed_cleanup)
    handle.start()

    async def cancel_close():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(handle.close(), .05)

    def owner():
        asyncio.run(cancel_close())
        exited.set()

    owner_thread = threading.Thread(target=owner)
    try:
        assert started.wait(1)
        owner_thread.start()
        assert exited.wait(.5), "A pending join must not trap asyncio.run in executor shutdown"
    finally:
        release.set()
        if owner_thread.ident is not None:
            owner_thread.join(2)
        handle.thread.join(2)
    assert not owner_thread.is_alive() and not handle.thread.is_alive()


async def test_reference_download_updates_its_http_timeout_and_keeps_contents(tmp_path, monkeypatch):



    observed = []

    def respond(request):
        observed.append(request.extensions["timeout"]["read"])
        if request.url.path == "/input.txt":
            return httpx.Response(302, headers={"location": "/final.txt"})
        return httpx.Response(200, text="unchanged contents")

    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(respond)))
    try:
        for timeout, redirects in ((2, 0), (2, 1), (7, 1)):
            (tmp_path / "config.json").write_text(json.dumps({
                "storage": {"references_dir": "references"}, "input_limits": {"max_redirects": redirects},
            }), encoding="utf-8")
            store = ReferenceStore(WorkspaceContext.from_path(tmp_path))
            policy = ModelInputPolicy(max_files=1, max_file_bytes=1000, reference_download_timeout_seconds=timeout)
            if not redirects:
                with pytest.raises(httpx.TooManyRedirects):
                    await store.import_url("https://example.invalid/input.txt", policy=policy)
            else:
                reference = await store.import_url("https://example.invalid/input.txt", policy=policy)
                assert reference.parts[0].text == "unchanged contents"
        assert observed == [2, 2, 2, 7, 7]
    finally:
        await close_all_clients()


@pytest.mark.parametrize("overrides", [{}, {"width": 128, "height": 96, "max_wait_time": 5}])
async def test_image_generation_shares_configured_http_and_preserves_explicit_arguments(image_responses, overrides):
    observed, sleeps = image_responses
    result = await generate_image_from_flux("isolated fixture", **overrides)
    assert result[:2] == (b"complete image", "image/png")
    assert json.loads(observed[0].content) == {"prompt": "isolated fixture", "width": overrides.get("width", 64), "height": overrides.get("height", 32)}
    assert all(request.extensions["timeout"]["read"] == 3 for request in observed)
    assert sleeps == [.01]
    assert "x-key" not in observed[-1].headers


@pytest.mark.parametrize("phase", ["submission", "poll", "download", "cancel"])
async def test_image_deadline_includes_submission_and_cancels_async_io(image_deadline, phase):
    stopped, started = image_deadline
    task = asyncio.create_task(generate_image_from_flux("isolated fixture"))
    if phase == "cancel":
        await asyncio.wait_for(started.wait(), .5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await asyncio.wait_for(task, .5)
        assert "timed out after 0.02 seconds" in result
    assert stopped == [True]


async def test_browser_updates_separate_action_and_navigation_policies(tmp_path):


    observed, navigations = {}, []

    browser = PlaywrightBrowserSession(None)
    browser._page = SimpleNamespace(url="https://example.invalid",
                                   goto=AsyncMock(side_effect=lambda url, **kwargs: navigations.append(kwargs.get("timeout", observed.get("navigation")))),
                                   title=AsyncMock(return_value="Fixture"),
                                   set_default_timeout=lambda value: observed.update(action=value),
                                   set_default_navigation_timeout=lambda value: observed.update(navigation=value))
    for action, navigation in ((2, 7), (3, 11)):
        (tmp_path / "config.json").write_text(json.dumps({"browser": {
            "action_timeout_seconds": action, "navigation_timeout_seconds": navigation,
        }}), encoding="utf-8")
        assert "Fixture" in await browser.browser_navigate("https://example.invalid")
        assert observed["action"] == action * 1000 and navigations[-1] == navigation * 1000


@pytest.mark.parametrize("max_results", [None, 2])
def test_web_search_reads_config_and_preserves_explicit_limit(tmp_path, monkeypatch, max_results):


    policy = {"max_results": 1, "region": "wt-wt", "timeout_seconds": 7,
              "safesearch": "moderate", "timelimit": None, "backend": "auto"}
    (tmp_path / "config.json").write_text(json.dumps({"web_search": policy}))
    factory = MagicMock()
    search = factory.return_value.__enter__.return_value.text
    search.return_value = [{"title": "Fixture", "href": "https://example.invalid", "body": "complete snippet"}]
    monkeypatch.setattr(base_tools, "DDGS", factory)
    result = base_tools.BasicToolkit.search_web(None, "isolated query", max_results=max_results)
    factory.assert_called_once_with(timeout=7)
    expected = {key: value for key, value in policy.items() if key != "timeout_seconds"}
    expected["max_results"] = policy["max_results"] if max_results is None else max_results
    search.assert_called_once_with("isolated query", **expected)
    assert "Fixture" in result and "https://example.invalid" in result and "complete snippet" in result
    assert json.loads((tmp_path / "config.json").read_text())["web_search"] == policy


@pytest.mark.parametrize("limit,newline,expected", [(4, "\n", "a|b\n"), (8, "\r\n", "a|b\r\n1|2")])
def test_csv_detection_sample_does_not_truncate_parsed_rows(tmp_path, monkeypatch, limit, newline, expected):

    (tmp_path / "config.json").write_text(json.dumps({"input_limits": {"csv_sniff_chars": limit}}))
    path = tmp_path / "table.csv"
    path.write_bytes(newline.join(("a|b", "1|2", "3|4", "")).encode())
    sniff = MagicMock(wraps=csv.Sniffer().sniff)
    monkeypatch.setattr(csv.Sniffer, "sniff", sniff)
    parts = DocumentReader().csv(path, tmp_path)
    assert sniff.call_args.args[0] == expected
    assert json.loads(parts[0].text) == [["a", "b"], ["1", "2"], ["3", "4"]]


@pytest.mark.parametrize("retries", [0, 1])
async def test_task_retry_limit_comes_from_config(tmp_path, retries):

    from redlotus.core.tasks import TaskManager

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


    (tmp_path / "config.json").write_text(json.dumps({"storage": {"filename_max_chars": 7, "cleanup": {
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

    toolkit = object.__new__(BasicToolkit)
    toolkit._WORK_DATABASE_ROOT = tmp_path
    assert toolkit.set_task_directory("  fixture/child  ").name == "fixture"
    assert toolkit.set_task_directory("").name == "default"
    monkeypatch.setattr(logger, "_lg", SimpleNamespace(contextualize=contextualize))
    with logger.session_log_context("fixture-long"):
        with monkeypatch.context() as scoped:
            scoped.setattr(logger, "settings", lambda: pytest.fail("A log record must use its session's policy snapshot"))
            logger._session_sink(Message("second"))
    assert path.read_text() == "second"
    backup = path.with_name("fixture.log.1")
    assert backup.read_text() == "first"
    os.utime(backup, (time.time() - 2 * 86400,) * 2)
    logger.prune_old_logs()
    assert path.is_file() and not backup.exists()


async def test_pdf_references_preserve_external_and_internal_link_destinations(tmp_path):
    import pymupdf

    path = tmp_path / "links.pdf"
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), "First page; external reference and next page")
        document.new_page().insert_text((72, 72), "Second page receipt")
        document[1].draw_rect(pymupdf.Rect(72, 100, 140, 150))
        document[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 60, 140, 75),
                                 "uri": "https://example.com/hidden-target"})
        document[0].insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(150, 60, 210, 75),
                                 "page": 1, "to": pymupdf.Point(72, 72)})
        document.save(path)
    store = ReferenceStore(WorkspaceContext.from_path(tmp_path), tmp_path / "references")
    reference = await store.capture_file(path, policy=ModelInputPolicy(max_files=1, max_file_bytes=100000, reference_download_timeout_seconds=1))
    old_parts = [part for part in DocumentReader().pdf(path, tmp_path) if not part.locator.endswith(", links")]
    (store.root / "manifests").mkdir()
    (store.root / "manifests" / f"{reference.id}.json").write_text(reference.model_copy(update={"parts": old_parts, "parser_version": 3}).model_dump_json())
    (reference.snapshot.parent / "parts-v3.pdf.json").write_text(json.dumps([part.model_dump(mode="json") for part in old_parts]))
    parts = (await store.parse(reference)).parts
    assert [(part.kind, part.locator, part.text.strip()) for part in parts[:3]] == [
        ("text", "Page 1", "First page; external reference and next page"), ("text", "Page 2", "Second page receipt"), ("image", "Page 2", "")]
    links = json.loads(next(part.text for part in parts if part.locator == "Page 1, links"))
    assert next(link for link in links if link["kind"] == pymupdf.LINK_URI)["uri"] == "https://example.com/hidden-target"
    assert next(link for link in links if link["kind"] == pymupdf.LINK_GOTO)["page"] == 1
