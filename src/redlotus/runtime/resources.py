"""Workspace identity, owned paths, file locks and atomic filesystem operations."""
from __future__ import annotations

import asyncio
import errno
import functools
import hashlib
import inspect
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from typing import Any, Iterator

from filelock import FileLock, Timeout
from loguru import logger as _lg

from redlotus.runtime.config import (
    config_value,
    ConfigError,
    _frozen,
    settings,
)

_REPARSE_POINT = 0x400
_workspace_context: ContextVar[WorkspaceContext | None] = ContextVar('workspace_context', default=None)
_workspace = None


@dataclass(frozen=True)
class FileFingerprint:
    size: int
    sha256: str

    def __post_init__(self):
        if type(self.size) is not int or self.size < 0 or not isinstance(self.sha256, str):
            raise ValueError("invalid model file fingerprint")


class FramedProcess:
    """A native worker's bounded binary channel and owned process lifetime."""

    def __init__(self, command: list[str], cwd: Path, environment: dict[str, str]):
        self.process = subprocess.Popen(command, cwd=cwd, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def read_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            block = self.process.stdout.read(size - len(data))
            if not block:
                raise ValueError("原生运行组件已退出或返回了不完整数据")
            data.extend(block)
        return bytes(data)

    def receive_header(self, limit: int) -> dict:
        size, = struct.unpack("<I", self.read_exact(4))
        if not 0 < size <= limit:
            raise ValueError("原生运行组件返回了无效消息长度")
        row = json.loads(self.read_exact(size))
        if not isinstance(row, dict):
            raise ValueError("原生运行组件返回了无效消息")
        return row

    def send(self, data: bytes) -> None:
        self.process.stdin.write(struct.pack("<I", len(data)) + data)
        self.process.stdin.flush()

    @contextmanager
    def responses(self, block_limit: int, total_limit: int) -> Iterator[Iterator[bytes]]:
        """Drain one bounded reply before allowing the next request on this pipe."""
        blocks = self._response_blocks(block_limit, total_limit)
        try:
            yield blocks
        finally:
            for _ in blocks:
                pass

    def _response_blocks(self, block_limit: int, total_limit: int) -> Iterator[bytes]:
        total = 0
        try:
            while True:
                row = self.receive_header(8192)
                if row.get("status") == "error":
                    raise RuntimeError(str(row.get("message", "原生运行组件失败")))
                size = row.get("size")
                if type(size) is not int:
                    raise ValueError("原生运行组件返回了无效分块长度")
                if row.get("status") == "done":
                    if not total or size != total:
                        raise ValueError("原生运行组件返回了不匹配的完成消息")
                    return
                if row.get("status") == "skipped" and not total and size == 0:
                    yield b""
                    return
                if row.get("status") != "data" or not 0 < size <= block_limit or total + size > total_limit:
                    raise ValueError("原生运行组件返回了超限或无效的分块")
                total += size
                yield self.read_exact(size)
        except (OSError, ValueError):
            self.close()
            raise

    def close(self) -> None:
        if self.process is None:
            return
        process, self.process = self.process, None
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        finally:
            process.stdout.close()


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
    configured = config_value(settings(), ("storage", "state_dir"), purpose="应用状态和全局数据目录；空值使用用户目录", kind=(str, type(None)))
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".redlotus"

def _storage_workspace(workspace=None):
    return workspace if workspace is not None else WorkspaceContext.from_path(current_workspace())

def _project_storage_path(name: str, workspace=None, *, required=True) -> Path | None:
    root = _storage_workspace(workspace).root.resolve()
    configured = config_value(settings(), ("storage", name), kind=str)
    if configured is None:
        if not required:
            return None
        raise ConfigError(
            f'缺少配置 storage.{name}；作用：当前项目的数据目录；runtime_dir 用于命令、技能安装和 Office 临时文件；'
            '示例：{"storage": {"runtime_dir": "WorkDatabase/runtime"}}', path=("storage", name), missing=True,
        )
    path = (root / Path(configured).expanduser()).resolve()
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

_storage_cleanup_log = functools.partial(_lg.info, "Storage cleanup released {1} bytes at {0}")

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

def user_skills_dir(workspace=None, *, required=True) -> Path | None:
    """运行时安装的技能 overlay（可写）；与随包基线技能合并加载。"""
    root = runtime_dir(workspace, required=required)
    return root / "skills" if root is not None else None

async def finish_io(operation, *, on_cancel=None):
    """Drain an I/O operation before cancellation releases its owner."""
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        drain = asyncio.gather(task, return_exceptions=True)
        while not drain.done():
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError:
                pass
        raise

_THREAD_CANCEL = ContextVar("resource_thread_cancel", default=None)


def check_thread_cancel():
    if (signal := _THREAD_CANCEL.get()) is not None and signal.is_set():
        raise asyncio.CancelledError()


async def thread_work(operation, *args, **kwargs):
    """Run blocking resource work off-loop; signal cancellation and drain its thread."""
    signal = threading.Event()
    def run():
        token = _THREAD_CANCEL.set(signal)
        try:
            check_thread_cancel()
            return operation(*args, **kwargs)
        finally:
            _THREAD_CANCEL.reset(token)
    return await finish_io(asyncio.to_thread(run), on_cancel=signal.set)


@contextmanager
def cancellable_lock(path: Path, *, wait=True):
    """Acquire a worker-owned lock without hiding cancellation behind a long wait."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path), thread_local=False)
    while True:
        check_thread_cancel()
        try:
            lock.acquire(timeout=0)
            break
        except Timeout:
            if not wait:
                raise
            time.sleep(0.05)
    try:
        yield lock
    finally:
        lock.release()


@asynccontextmanager
async def threaded_context(factory, *args, **kwargs):
    """Enter/leave a blocking resource context off-loop, including cancelled entry."""
    entered = []
    def enter():
        manager = factory(*args, **kwargs)
        value = manager.__enter__()
        entered.append(manager)
        return value
    try:
        yield await thread_work(enter)
    finally:
        if entered:
            await finish_io(asyncio.to_thread(entered[0].__exit__, *sys.exc_info()))


def atomic_write(path: Path, content: str | bytes, *, encoding: str = "utf-8") -> None:
    """Replace a complete file using native text encoding or unchanged bytes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        dir=path.parent, mode="w" if isinstance(content, str) else "wb",
        encoding=encoding if isinstance(content, str) else None, delete_on_close=False,
    ) as temporary:
        temporary.write(content)
        temporary.close()
        replace_retry(temporary.name, path)

@contextmanager
def file_lock(path: Path, *, timeout: float | None = None) -> Iterator[None]:
    """跨进程文件锁；锁文件与目标同目录。"""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock_path), is_singleton=True).acquire(timeout=timeout):
        yield

def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            check_thread_cancel()
            digest.update(chunk)
    return digest.hexdigest()

def owned_path(root: Path, value: str) -> Path:
    path = Path(value)
    target = root / path
    if path.is_absolute() or ".." in path.parts or not path.parts or not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("path escapes its owner directory")
    return target

def managed_subdir(root: Path, name: str) -> Path:
    """Reject a replaced storage subdirectory before creating or deleting children."""
    if Path(name).parts != (name,):
        raise ValueError("invalid managed directory name")
    path = root / name
    try:
        path.lstat()
    except FileNotFoundError:
        return path
    if not ordinary_owned_directory(root, path):
        raise ValueError("managed directory is a link or escapes its root")
    return path

def ordinary_owned_directory(root: Path, path: Path) -> bool:
    return (_ordinary_storage_path(path) and path.is_dir() and
            path.resolve().is_relative_to(root.resolve()))

def verified_partial(part: Path, metadata: Path, identity: dict, limit: int) -> tuple[dict, int]:
    """A resumable partial belongs to its recorded source or is a conflict."""
    try:
        metadata.lstat()
    except FileNotFoundError:
        if part.exists() or part.is_symlink():
            raise ValueError("partial archive has no owner metadata")
        return {}, 0
    if not _ordinary_storage_path(metadata) or not metadata.is_file():
        raise ValueError("partial metadata is not a plain file")
    try:
        prior = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("partial metadata is unreadable") from exc
    if not isinstance(prior, dict) or any(prior.get(key) != value for key, value in identity.items()):
        raise ValueError("partial archive identity differs")
    try:
        part.lstat()
    except FileNotFoundError:
        return prior, 0
    if not _ordinary_storage_path(part) or not part.is_file() or part.stat().st_size > limit:
        raise ValueError("partial archive is not a compatible plain file")
    return prior, part.stat().st_size

def discard_verified_partial(part: Path, metadata: Path, identity: dict, limit: int) -> bool:
    try:
        prior, _ = verified_partial(part, metadata, identity, limit)
    except ValueError:
        return False
    if not prior:
        return False
    part.unlink(missing_ok=True)
    metadata.unlink()
    return True

def discard_completed_partial(part: Path, metadata: Path, identity: dict, limit: int) -> bool:
    """Remove an owned archive only when its complete content matches the expected hash."""
    try:
        _, size = verified_partial(part, metadata, identity, limit)
    except ValueError:
        return False
    if not size:
        return False
    with part.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != identity["sha256"]:
            return False
    return discard_verified_partial(part, metadata, identity, limit)

def replace_retry(source: Path, destination: Path) -> None:
    """Retry transient Windows sharing failures without moving the original first."""
    for attempt in range(5):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32) or attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))

def safe_tar_name(name: str, root: str) -> bool:
    path = PurePosixPath(name)
    return (bool(path.parts) and name in (path.as_posix(), path.as_posix() + "/") and not name.startswith("/") and
            "\\" not in name and ":" not in name and ".." not in path.parts and path.parts[0] == root)

def checked_tar_members(tar, root: str, size_limit: int, *, reserved=()) -> list[str]:
    members = tar.getmembers()
    if len(members) > 100000:
        raise ValueError("归档成员数量超过上限")
    names, unpacked = set(), 0
    for member in members:
        check_thread_cancel()
        name = PurePosixPath(member.name).as_posix()
        if not safe_tar_name(member.name, root) or not (member.isfile() or member.isdir()) or name in names:
            raise ValueError("归档包含越界或不受支持的成员")
        names.add(name)
        if member.isfile():
            unpacked += member.size
            if unpacked > size_limit:
                raise ValueError("归档解压量超过上限")
    if names.intersection(reserved):
        raise ValueError("归档包含管理器保留文件")
    return sorted(names.union(reserved))

def remove_recorded_tree(directory: Path, marker_name: str, expected: dict,
                         names, *, complete: bool = False) -> bool:
    """Remove only a plain tree whose marker and entries match a recorded manifest."""
    marker = directory / marker_name
    try:
        if (not _ordinary_storage_path(directory) or not directory.is_dir() or
                not _ordinary_storage_path(marker) or json.loads(marker.read_text(encoding="utf-8")) != expected):
            return False
        allowed = set(names) | {marker_name}
        allowed |= {parent.as_posix() for name in tuple(allowed) for parent in PurePosixPath(name).parents if str(parent) != "."}
        entries = []
        resolved_root = directory.resolve()
        def collect(parent):
            for path in parent.iterdir():
                if (not _ordinary_storage_path(path) or not path.resolve().is_relative_to(resolved_root) or
                        not (path.is_file() or path.is_dir())):
                    return False
                entries.append(path)
                if path.is_dir() and not collect(path):
                    return False
            return True
        if not collect(directory):
            return False
        actual = {path.relative_to(directory).as_posix() for path in entries}
        if (complete and actual != allowed) or (not complete and not actual <= allowed):
            return False
        shutil.rmtree(directory)
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False

def locks_in_use(paths) -> bool:
    """Under a caller's gate lock, reap stale use records and detect live leases."""
    for path in paths:
        try:
            lock = FileLock(str(path))
            lock.acquire(timeout=0)
        except Timeout:
            return True
        lock.release()
        path.unlink(missing_ok=True)
    return False

def read_locked_json(path: Path):
    """Read mutable JSON under the writer's lock, including on Windows."""
    with file_lock(path):
        return json.loads(path.read_text(encoding="utf-8"))

def atomic_write_json(path: Path, data: Any, *, indent: int = 2) -> None:
    atomic_write(
        path,
        json.dumps(data, ensure_ascii=False, indent=indent) + "\n",
    )

def save_locked_json(path: Path, data: Any) -> None:
    """在跨进程文件锁下原子写 JSON。"""
    with file_lock(path):
        atomic_write_json(path, data)

def safe_name(
    text: str, *, extra: str = "_-", fallback: str = "default"
) -> str:
    """把任意字符串清洗成文件名/键安全形式：非字母数字且不在 extra 内的字符替换为 _。"""
    cleaned = "".join(c if c.isalnum() or c in extra else "_" for c in text)
    return cleaned.strip() or fallback

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
