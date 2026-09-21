"""Workspace identity, owned paths, file locks and atomic filesystem operations."""
from __future__ import annotations

import asyncio
import errno
import functools
import hashlib
import inspect
import json
import os
import sys
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from filelock import FileLock, Timeout
from loguru import logger as _lg

from redlotus.runtime.config import (
    ConfigError,
    _frozen,
    config_source_summary,
    settings,
)

_REPARSE_POINT = 0x400
_workspace_context: ContextVar[WorkspaceContext | None] = ContextVar('workspace_context', default=None)
_workspace = None

@dataclass(frozen=True)
class WorkspaceContext:
    """Immutable project identity carried into every run and child thread."""

    root: Path
    project_id: str

    @classmethod
    def from_path(cls, path: Path | str) -> WorkspaceContext:
        root = Path(path).expanduser().resolve()
        identity = os.path.normcase(str(root)).encode("utf-8")
        return cls(root, hashlib.sha256(identity).hexdigest()[:24])

@contextmanager
def bind_context(variable, value, *, expose=False):
    """Set one execution context value and restore its parent on every exit path."""
    token = variable.set(value)
    try:
        yield value if expose else None
    finally:
        variable.reset(token)

active_workspace = _workspace_context.get
workspace_context = functools.partial(bind_context, _workspace_context, expose=True)

def current_workspace():
    active = active_workspace()
    return active.root if active else _workspace or Path.cwd().resolve()

def set_workspace(path):
    global _workspace
    _workspace = Path(path).expanduser().resolve()
    return _workspace

def resource_root() -> Path:
    """随包只读资源根（redlotus 包目录）。

    - 正常 / editable 安装：本文件位于 redlotus/runtime/resources.py，上溯两级即包根。
    - PyInstaller 冻结：优先 _MEIPASS/redlotus，回退 exe 目录。
    """
    if _frozen():
        base = getattr(sys, "_MEIPASS", None)
        root = Path(base) if base else Path(sys.executable).resolve().parent
        pkg = root / "redlotus"
        return pkg if pkg.is_dir() else root
    return Path(__file__).resolve().parent.parent

def user_data_dir() -> Path:
    """Persistent memory state; the explicit environment override remains supported."""
    if override := os.environ.get("REDLOTUS_DATA_DIR"):
        return Path(override)
    # Config discovery uses user_config_dir(), so it never depends on these data paths.
    configured = settings()["storage"]["state_dir"]
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".redlotus"

def _storage_workspace(workspace=None):
    return workspace if workspace is not None else WorkspaceContext.from_path(current_workspace())

def _project_storage_path(name: str, workspace=None) -> Path:
    workspace = _storage_workspace(workspace)
    root = workspace.root.resolve()
    configured = settings()["storage"][name]
    if not configured:
        raise ConfigError(f"缺少配置 storage.{name}；检查来源: {config_source_summary()}")
    candidate = Path(configured).expanduser()
    path = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not path.is_relative_to(root):
        raise ConfigError(f"配置 storage.{name} 必须位于当前项目目录内")
    return path

project_data_dir = functools.partial(_project_storage_path, "project_dir")
session_data_dir = functools.partial(_project_storage_path, "sessions_dir")
conversations_root = session_data_dir
references_dir = functools.partial(_project_storage_path, "references_dir")
runtime_dir = functools.partial(_project_storage_path, "runtime_dir")

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

def prompts_dir() -> Path:
    return resource_root() / "prompts"

def skills_dir() -> Path:
    """随包基线技能（只读）。"""
    return resource_root() / "tools" / "skills"

logs_dir = functools.partial(_project_storage_path, "project_logs_dir")

def memory_dir() -> Path:
    """个人全局 MEMORY.md 及旧 SOUL/USER 文档的迁移备份目录。"""
    return user_data_dir() / "LongTermMemory"

def user_skills_dir(workspace=None) -> Path:
    """运行时安装的技能 overlay（可写）；与随包基线技能合并加载。"""
    return runtime_dir(workspace) / "skills"

async def finish_file_io(operation):
    """Drain a file operation before cancellation releases its owning lock."""
    import asyncio

    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise

def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)

@contextmanager
def file_lock(path: Path, *, timeout: float = 30.0) -> Iterator[None]:
    """跨进程文件锁；锁文件与目标同目录。"""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock_path), timeout=timeout, is_singleton=True):
        yield

def read_locked_json(path: Path):
    """Read mutable JSON under the writer's lock, including on Windows."""
    with file_lock(path):
        return json.loads(path.read_text(encoding="utf-8"))

def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding=encoding)
    os.replace(tmp, path)

def atomic_write_json(path: Path, data: Any, *, indent: int = 2) -> None:
    atomic_write_text(
        path,
        json.dumps(data, ensure_ascii=False, indent=indent) + "\n",
    )

def save_locked_json(path: Path, data: Any) -> None:
    """在跨进程文件锁下原子写 JSON。"""
    with file_lock(path):
        atomic_write_json(path, data)

def safe_name(
    text: str, *, extra: str = "_-", max_len: int = 50, fallback: str = "default"
) -> str:
    """把任意字符串清洗成文件名/键安全形式：非字母数字且不在 extra 内的字符替换为 _。"""
    cleaned = "".join(c if c.isalnum() or c in extra else "_" for c in text)
    return cleaned[:max_len] or fallback

def iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def bind_to_loop(function, loop):
    """Expose an owner-loop service to child tools without sharing loop-bound resources."""
    @functools.wraps(function)
    async def call(*args, **kwargs):
        async def invoke():
            result = function(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result

        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(invoke(), loop))

    return call
