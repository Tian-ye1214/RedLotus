"""Auxiliary fault checks never use the owner's configuration or saved sessions."""

import os
import subprocess
import sys

import pytest


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

