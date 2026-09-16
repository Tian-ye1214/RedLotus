"""Storage-pressure cleanup only reclaims explicitly owned, inactive artifacts."""

import errno
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from redlotus.core import config
from redlotus.core import session as session_module
from redlotus.core.session import SessionFile
from redlotus.tools.execution import ExecutionEnvironment, _prepare_runtime_dirs


def _storage_config(sessions: Path, runtime: Path) -> dict:
    return {
        "storage": {
            "sessions_dir": str(sessions),
            "runtime_dir": str(runtime),
            "cleanup": {
                "enabled": True,
                "session_retention_days": 7,
                "execution_cache": True,
            },
        },
        "execution": {"cache_dir": "{runtime}/cache"},
    }


def _session(
    root: Path,
    project_id: str,
    session_id: str,
    *,
    active: bool = False,
    pending: bool = False,
) -> SessionFile:
    saved = SessionFile.create(root / project_id, project_id, session_id=session_id)
    if active:
        saved.update(metadata={"active_turn": {"id": "turn"}})
    if pending:
        saved.update(
            jobs={
                "job": {
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "indexed": False,
                }
            }
        )
    old = time.time() - 8 * 24 * 60 * 60
    os.utime(saved.path, (old, old))
    return saved


@contextmanager
def _held_file_lock(path: Path):
    ready = path.with_suffix(".ready")
    code = (
        "import sys,time\n"
        "from pathlib import Path\n"
        "from filelock import FileLock\n"
        "lock=FileLock(sys.argv[1])\n"
        "lock.acquire()\n"
        "Path(sys.argv[2]).write_text('ready', encoding='utf-8')\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", code, str(path), str(ready)])
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not ready.exists():
        process.terminate()
        process.wait(timeout=5)
        raise RuntimeError("could not acquire test file lock")
    try:
        yield
    finally:
        process.terminate()
        process.wait(timeout=5)


def _marked_cache(root: Path, project_id: str) -> Path:
    cache = root / project_id
    cache.mkdir(parents=True)
    (cache / ".redlotus-cache").write_text(
        json.dumps({"project_id": project_id}), encoding="utf-8"
    )
    (cache / "payload.bin").write_bytes(b"cache")
    return cache


def test_non_space_error_is_reraised_without_retry_or_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "settings", lambda: _storage_config(tmp_path / "sessions", tmp_path / "runtime"))
    error = OSError(errno.EACCES, "permission denied")
    retried = []

    with pytest.raises(OSError) as raised:
        config.retry_after_storage_cleanup(tmp_path / "failed", error, lambda: retried.append(True))

    assert raised.value is error
    assert retried == []


def test_unix_host_down_error_does_not_reclaim_old_session(tmp_path, monkeypatch):
    sessions, runtime = tmp_path / "sessions", tmp_path / "runtime"
    monkeypatch.setattr(config, "settings", lambda: _storage_config(sessions, runtime))
    old = _session(sessions, "old", "old")
    error = OSError(112, "host down")
    retried = []

    with pytest.raises(OSError) as raised:
        config.retry_after_storage_cleanup(
            tmp_path / "failed", error, lambda: retried.append(True)
        )

    assert raised.value is error
    assert old.path.exists()
    assert retried == []


def test_old_idle_session_is_retried_without_removing_unknown_sibling(tmp_path, monkeypatch):
    sessions, runtime = tmp_path / "sessions", tmp_path / "runtime"
    monkeypatch.setattr(config, "settings", lambda: _storage_config(sessions, runtime))
    current = _session(sessions, "current", "current")
    old = _session(sessions, "old", "old")
    operator_note = old.path.parent / "operator-note.txt"
    operator_note.write_text("keep", encoding="utf-8")
    attempts, full = [], OSError(errno.ENOSPC, "disk full")

    def retry():
        attempts.append(True)
        if old.path.exists():
            raise full

    config.retry_after_storage_cleanup(current.path, full, retry)

    assert attempts == [True]
    assert not old.path.exists()
    assert operator_note.read_text(encoding="utf-8") == "keep"
    assert current.path.exists()


def test_partial_old_session_is_never_rewritten_deleted_or_recursively_cleaned(
    tmp_path, monkeypatch
):
    sessions, runtime = tmp_path / "sessions", tmp_path / "runtime"
    monkeypatch.setattr(config, "settings", lambda: _storage_config(sessions, runtime))
    current = _session(sessions, "current", "current")
    partial = _session(sessions, "old", "old")
    partial.update(metadata={"title": "complete"})
    raw = partial.path.read_bytes()
    partial.path.write_bytes(raw[:-3] + b',\n{"metadata":{"title":"partial')
    old = time.time() - 8 * 24 * 60 * 60
    os.utime(partial.path, (old, old))
    original_cleanup = config.retry_after_storage_cleanup
    cleanup_calls, retries = [], []
    full = OSError(errno.ENOSPC, "disk full")
    original_replace = session_module.os.replace

    def disk_full_replace(source, target):
        if Path(target) == partial.path:
            raise full
        return original_replace(source, target)

    def no_nested_cleanup(path, error, retry):
        cleanup_calls.append(Path(path))
        if len(cleanup_calls) > 1:
            raise AssertionError("partial-session inspection recursively cleaned")
        return original_cleanup(path, error, retry)

    monkeypatch.setattr(config, "retry_after_storage_cleanup", no_nested_cleanup)
    monkeypatch.setattr(session_module.os, "replace", disk_full_replace)

    with pytest.raises(OSError) as raised:
        no_nested_cleanup(current.path, full, lambda: retries.append(True))

    assert raised.value is full
    assert cleanup_calls == [current.path]
    assert retries == []
    assert partial.path.read_bytes() == raw[:-3] + b',\n{"metadata":{"title":"partial'
    assert not partial.path.with_suffix(".tmp").exists()


def test_cleanup_preserves_protected_sessions_and_reclaims_only_marked_idle_cache(
    tmp_path, monkeypatch
):
    sessions, runtime = tmp_path / "sessions", tmp_path / "runtime"
    monkeypatch.setattr(config, "settings", lambda: _storage_config(sessions, runtime))
    current = _session(sessions, "current", "current")
    active = _session(sessions, "active", "active", active=True)
    pending = _session(sessions, "pending", "pending", pending=True)
    locked = _session(sessions, "locked", "locked")
    locked_marker = locked.path.parent / ".use-123-test.lock"
    malformed = sessions / "broken" / "bad" / "model_messages.json"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("not a session", encoding="utf-8")
    old = time.time() - 8 * 24 * 60 * 60
    os.utime(malformed, (old, old))
    cache_root = runtime / "cache"
    reclaimable = _marked_cache(cache_root, "idle")
    active_cache = _marked_cache(cache_root, "locked")
    current_cache = _marked_cache(cache_root, "current")
    markerless = cache_root / "operator-cache"
    markerless.mkdir(parents=True)
    (markerless / "keep.txt").write_text("keep", encoding="utf-8")
    attempts, full = [], OSError(errno.ENOSPC, "disk full")

    def retry():
        attempts.append(True)
        if reclaimable.exists():
            raise full

    with _held_file_lock(locked_marker):
        config.retry_after_storage_cleanup(current.path, full, retry)

    assert attempts == [True]
    assert active.path.exists()
    assert pending.path.exists()
    assert locked.path.exists()
    assert malformed.exists()
    assert not reclaimable.exists()
    assert active_cache.exists()
    assert current_cache.exists()
    assert (markerless / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_preparing_project_cache_writes_ownership_marker(tmp_path):
    cache = tmp_path / "cache"
    environment = ExecutionEnvironment(
        tmp_path,
        "project",
        tmp_path / "environment",
        tmp_path / "python",
        cache,
        {"TEMP": str(cache / "project" / "tmp")},
    )

    _prepare_runtime_dirs(environment)

    marker = cache / "project" / ".redlotus-cache"
    assert json.loads(marker.read_text(encoding="utf-8")) == {"project_id": "project"}
