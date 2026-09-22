"""Conversation identity, input ordering, incremental recovery, and saved-session discovery."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path

from filelock import FileLock, Timeout

from redlotus.runtime.config import settings
from redlotus.runtime.resources import (
    _delete_owned_cache,
    _locked_storage_file,
    _retry_after_cleanup,
    _safe_storage_child,
    _same_storage_volume,
    _storage_children,
    _storage_cleanup_log,
    _storage_full,
    _storage_workspace,
    session_data_dir,
    workspace_context,
)


def _write_with_cleanup(path, operation, *, workspace=None):
    """Retry one unchanged file transaction only after approved space reclamation."""
    context = workspace_context(workspace) if workspace is not None else nullcontext()
    with context:
        try:
            operation()
        except OSError as exc:
            retry_after_storage_cleanup(path, exc, operation)


@contextmanager
def _locked_session(message_path: Path) -> Iterator[FileLock | None]:
    transaction = _locked_storage_file(message_path.with_suffix(".lock"))
    if transaction is None:
        yield None
        return
    with ExitStack() as locks:
        locks.callback(transaction.release)
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
                locks.callback(use_lock.release)
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
    from redlotus.sessions.storage import SessionFile

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
    from redlotus.tools.execution import execution_cache_dir

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
