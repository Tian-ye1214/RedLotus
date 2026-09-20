"""Storage cleanup responsibilities."""

from __future__ import annotations

import errno
import json
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator

from filelock import FileLock, Timeout
from loguru import logger as _lg

from redlotus.runtime.config import settings
from redlotus.runtime.files import _storage_workspace, session_data_dir

_REPARSE_POINT = 0x400


def _storage_full(error: OSError) -> bool:
    return error.errno == errno.ENOSPC or getattr(error, "winerror", None) == 112


def _ordinary_storage_path(path: Path) -> bool:
    try:
        return not path.is_symlink() and not (
            getattr(path.lstat(), "st_file_attributes", 0) & _REPARSE_POINT
        )
    except OSError:
        return False


def _storage_children(root: Path) -> tuple[Path, ...] | None:
    if not _ordinary_storage_path(root) or not root.is_dir():
        return None
    try:
        return tuple(root.iterdir())
    except OSError:
        return None


def _safe_storage_child(parent: Path, child: Path) -> bool:
    try:
        return (
            child.parent == parent
            and _ordinary_storage_path(child)
            and child.resolve().is_relative_to(parent.resolve())
        )
    except OSError:
        return False


def _same_storage_volume(first: Path, second: Path) -> bool:
    while not first.exists() and first != first.parent:
        first = first.parent
    while not second.exists() and second != second.parent:
        second = second.parent
    try:
        return first.stat().st_dev == second.stat().st_dev
    except OSError:
        return bool(first.drive) and first.drive.casefold() == second.drive.casefold()


def _locked_storage_file(path: Path) -> FileLock | None:
    try:
        lock = FileLock(str(path), timeout=0)
        lock.acquire(timeout=0)
        return lock
    except (OSError, Timeout):
        return None


def _release_storage_lock(lock: FileLock) -> None:
    try:
        lock.release()
    except OSError:
        pass


@contextmanager
def _locked_session(message_path: Path) -> Iterator[FileLock | None]:
    transaction = _locked_storage_file(message_path.with_suffix(".lock"))
    if transaction is None:
        yield None
        return
    with ExitStack() as locks:
        locks.callback(_release_storage_lock, transaction)
        children = _storage_children(message_path.parent)
        if children is None:
            yield None
            return
        for child in children:
            if child.name.startswith(".use-") and child.name.endswith(".lock"):
                use_lock = (
                    _locked_storage_file(child)
                    if _safe_storage_child(message_path.parent, child)
                    else None
                )
                if use_lock is None:
                    yield None
                    return
                locks.callback(_release_storage_lock, use_lock)
        yield transaction


def _session_candidates(sessions: Path, current: Path, cutoff: float):
    candidates = []
    session_dirs = _storage_children(sessions)
    if session_dirs is None:
        return candidates
    for session_dir in session_dirs:
        if not _safe_storage_child(sessions, session_dir) or not session_dir.is_dir():
            continue
        message = session_dir / "model_messages.json"
        if not _safe_storage_child(session_dir, message) or not message.is_file():
            continue
        try:
            stale = message.stat().st_mtime < cutoff
            current_session = (
                message.resolve() == current
                or session_dir.resolve() == current.parent
            )
            if stale and not current_session:
                candidates.append((message.stat().st_mtime, session_dir, message))
        except OSError:
            continue
    return sorted(candidates, key=lambda row: row[0])


def _load_cleanup_session(
    session_dir: Path, message: Path, transaction: FileLock, project_id: str
):
    from redlotus.storage.session import SessionFile

    session = SessionFile.load(message, lock=transaction, recover=False)
    return (
        session
        if session.session_id == session_dir.name and session.project_id == project_id
        else None
    )


def _session_protected(
    session_dir: Path, message: Path, transaction: FileLock, project_id: str
) -> bool:
    try:
        session = _load_cleanup_session(session_dir, message, transaction, project_id)
        return (
            session is None
            or session.metadata.get("active_turn")
            or session.pending_jobs()
        )
    except (OSError, ValueError, KeyError, TypeError, Timeout):
        return True


def _delete_old_session(
    session_dir: Path, message: Path, cutoff: float, project_id: str
) -> int | None:
    with _locked_session(message) as transaction:
        if transaction is None or _session_protected(
            session_dir, message, transaction, project_id
        ):
            return None
        try:
            if message.stat().st_mtime >= cutoff or not _safe_storage_child(session_dir, message):
                return None
            released = message.stat().st_size
            message.unlink()
            return released
        except OSError:
            return None


def _active_cache_projects(sessions: Path, project_id: str) -> set[str] | None:
    active = set()
    session_dirs = _storage_children(sessions)
    if session_dirs is None:
        return None
    for session_dir in session_dirs:
        if not _safe_storage_child(sessions, session_dir) or not session_dir.is_dir():
            active.add(session_dir.name)
            continue
        message = session_dir / "model_messages.json"
        if (
            not _safe_storage_child(session_dir, message)
            or not message.is_file()
        ):
            active.add(session_dir.name)
            continue
        with _locked_session(message) as transaction:
            if transaction is None:
                active.add(session_dir.name)
                continue
            try:
                session = _load_cleanup_session(
                    session_dir, message, transaction, project_id
                )
            except (OSError, ValueError, KeyError, TypeError, Timeout):
                session = None
            if session is None:
                active.add(session_dir.name)
            elif session.metadata.get("active_turn") or session.pending_jobs():
                active.add(session.project_id)
    return active


def _delete_owned_cache(root: Path, target: Path) -> int | None:
    entries = []

    def collect(path: Path) -> bool:
        if not _safe_storage_child(path.parent, path):
            return False
        if path.is_dir():
            children = _storage_children(path)
            if children is None or not all(collect(child) for child in children):
                return False
        elif not path.is_file():
            return False
        entries.append(path)
        return True

    if not _safe_storage_child(root, target) or not collect(target):
        return None
    released = 0
    try:
        for path in entries:
            if not _safe_storage_child(path.parent, path):
                return None
            if path.is_file():
                released += path.stat().st_size
                path.unlink()
            else:
                path.rmdir()
    except OSError:
        return None
    return released


def _storage_cleanup_log(path: Path, released: int) -> None:
    try:
        _lg.info("Storage cleanup released {} bytes at {}", released, path)
    except OSError:
        pass


def _retry_after_cleanup(retry: Callable[[], None]) -> bool:
    try:
        retry()
    except OSError as error:
        if not _storage_full(error):
            raise
        return False
    return True


def retry_after_storage_cleanup(
    path: Path, error: OSError, retry: Callable[[], None]
) -> None:
    """Retry an ENOSPC write only after conservative cleanup of owned inactive data."""
    if not _storage_full(error):
        raise error
    storage = (settings().get("storage") or {})
    cleanup = storage.get("cleanup") or {}
    try:
        enabled = cleanup["enabled"]
        cache_enabled = cleanup["execution_cache"]
        cutoff = time.time() - float(cleanup["session_retention_days"]) * 86400
    except (KeyError, TypeError, ValueError):
        raise error
    if not enabled:
        raise error
    failed = Path(path).resolve()
    workspace = _storage_workspace()
    sessions = session_data_dir(workspace)
    if _same_storage_volume(sessions, failed):
        for _, session_dir, message in _session_candidates(sessions, failed, cutoff):
            released = _delete_old_session(
                session_dir, message, cutoff, workspace.project_id
            )
            if released is not None:
                _storage_cleanup_log(message, released)
                if _retry_after_cleanup(retry):
                    return
    if not cache_enabled:
        raise error
    from redlotus.execution.commands import execution_cache_dir

    cache_root = execution_cache_dir(workspace)
    environments = (Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve())
    if not _same_storage_volume(cache_root, failed):
        raise error
    current_project = workspace.project_id
    active = _active_cache_projects(sessions, workspace.project_id)
    if active is None:
        raise error
    protected_projects = active | ({current_project} if current_project else set())
    for cache in _storage_children(cache_root) or ():
        marker = cache / ".redlotus-cache"
        if not _safe_storage_child(cache_root, cache) or not cache.is_dir():
            continue
        try:
            in_environment_tree = any(
                cache.resolve().is_relative_to(environment)
                or environment.is_relative_to(cache.resolve())
                for environment in environments
            )
        except OSError:
            continue
        if (
            cache.name in protected_projects
            or in_environment_tree
            or not _safe_storage_child(cache, marker)
        ):
            continue
        try:
            if json.loads(marker.read_text(encoding="utf-8")) != {
                "project_id": cache.name
            }:
                continue
        except (OSError, ValueError, TypeError):
            continue
        released = _delete_owned_cache(cache_root, cache)
        if released is not None:
            _storage_cleanup_log(cache, released)
            if _retry_after_cleanup(retry):
                return
    raise error
