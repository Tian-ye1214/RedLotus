"""Native command processes must not share the interactive owner's pending input."""

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("python_utf8", ("0", "1"))
def test_frozen_launcher_discovery_detaches_stdin(monkeypatch, python_utf8):
    from redlotus.execution import commands
    import redlotus.runtime.config as config

    interpreter = sys.executable
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(Path(interpreter).with_name("synthetic-frozen.exe")))
    monkeypatch.setattr(commands.shutil, "which", lambda name: interpreter if name == "py" else None)
    monkeypatch.setattr(config, "get_agent_run_policy", lambda: SimpleNamespace(max_command_timeout_seconds=5))
    run = subprocess.run
    def launcher(argv, **kwargs):
        # The host has no py launcher; run its requested Python command directly.
        assert kwargs.get("stdin") is subprocess.DEVNULL
        kwargs["env"] = {**os.environ, "PYTHONUTF8": python_utf8}
        return run([argv[0], *argv[2:]], **kwargs)
    monkeypatch.setattr(commands.subprocess, "run", launcher)
    assert commands.existing_python() == Path(interpreter).resolve()


@pytest.mark.parametrize(
    ("configured_path", "driver_path"),
    [(None, ""), ("0", "0"), ("D:/caller-browsers", "D:/caller-browsers")],
)
def test_frozen_browser_start_uses_native_cache_policy(
    monkeypatch, tmp_path, configured_path, driver_path
):
    import playwright.async_api

    from redlotus.tools.browser import PlaywrightBrowserSession

    observed_paths = []
    page = SimpleNamespace(set_default_timeout=lambda _timeout: None)
    fake_browser = SimpleNamespace(
        new_page=AsyncMock(return_value=page), close=AsyncMock()
    )
    fake_playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=fake_browser)),
        stop=AsyncMock(),
    )

    async def start():
        observed_paths.append(os.environ.get("PLAYWRIGHT_BROWSERS_PATH"))
        return fake_playwright

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    if configured_path is None:
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    else:
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", configured_path)
    monkeypatch.setattr(
        playwright.async_api,
        "async_playwright",
        lambda: SimpleNamespace(start=start),
    )

    async def scenario():
        browser = PlaywrightBrowserSession(tmp_path)
        await browser._start()
        await browser.close()

    asyncio.run(scenario())
    assert observed_paths == [driver_path]


def test_command_finishes_while_parent_waits_for_input(tmp_path):
    owner = tmp_path / "interactive_owner.py"
    owner.write_text('''import asyncio
import json
import os
import shlex
import subprocess
import sys
import threading
from redlotus.execution.process import _run_owned_process

async def main():
    reading = threading.Event()
    def ask():
        reading.set()
        return input()
    answer = asyncio.create_task(asyncio.to_thread(ask))
    await asyncio.to_thread(reading.wait)
    await asyncio.sleep(0.1)
    command = [sys.executable, "-B", "-c", "print(42)"]
    command = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
    try:
        result = await _run_owned_process(command, shell=True, cwd=os.getcwd(), env=None, timeout=5)
        print(json.dumps({"code": result.returncode, "stdout": result.stdout}), flush=True)
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__}), flush=True)
    finally:
        await answer

asyncio.run(main())
''', encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    process = subprocess.Popen(
        [sys.executable, "-B", str(owner)], cwd=tmp_path, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    output = queue.Queue()
    reader = threading.Thread(target=lambda: output.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        # Keep the pipe open and the owner's input call blocked until the child exits.
        result = json.loads(output.get(timeout=20))
        assert result.get("code") == 0, result
        assert result["stdout"].splitlines() == ["42"]
    finally:
        process.stdin.write("release owner\n")
        process.stdin.flush()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        reader.join(timeout=2)
        process.stdin.close()
        process.stdout.close()
        stderr = process.stderr.read()
        process.stderr.close()
    assert process.returncode == 0, stderr
    assert not reader.is_alive()
