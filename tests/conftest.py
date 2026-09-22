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

