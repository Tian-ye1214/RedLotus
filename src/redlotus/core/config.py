"""Application startup, configuration, global paths, logging, and owned I/O resources."""

from __future__ import annotations

import sys
import os
import shutil
import errno
import json
import time
import threading
import asyncio
import httpx
import hashlib
import signal
from pathlib import Path
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Iterator, TYPE_CHECKING
from filelock import FileLock, Timeout
from loguru import logger as _lg
from collections.abc import Callable
from urllib.parse import urlsplit
from copy import deepcopy
from io import StringIO
from dotenv.parser import parse_stream


APP_NAME = "RedLotus"


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """随包只读资源根（redlotus 包目录）。

    - 正常 / editable 安装：本文件位于 redlotus/core/config.py，上溯两级即包根。
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
    return _storage_path("state_dir", Path.home() / ".redlotus")


def _storage_path(name: str, default: Path) -> Path:
    # Config discovery uses user_config_dir(), so it never depends on these data paths.
    configured = settings()["storage"][name]
    return Path(configured).expanduser().resolve() if configured else default


def user_config_dir() -> Path:
    """用户配置根：config.json / .env / bot config.yaml。"""
    return Path(
        os.environ.get("REDLOTUS_CONFIG_DIR")
        or Path.home() / ".redlotus"
    ).expanduser().resolve()


# ---- 工作产物：跟随当前工作目录 ----
# ---- 配置 / 密钥：用户配置目录 ----
def local_config_root() -> Path:
    """开发覆盖只搜索一个目录；冻结程序使用 EXE 目录。"""
    return Path(sys.executable).resolve().parent if _frozen() else Path.cwd()


def config_sources() -> tuple[Path, Path, Path]:
    """配置优先级：本地 JSON、本地 .env、全局 JSON。"""
    local = os.environ.get("REDLOTUS_CONFIG_FILE")
    return (
        Path(local).expanduser().resolve() if local else local_config_root() / "src/redlotus/config.json",
        dotenv_file(),
        user_config_dir() / "config.json",
    )


def config_source_summary() -> str:
    """缺项提示使用可直接定位的三层文件路径。"""
    return " → ".join(map(str, config_sources()))


def config_file() -> Path:
    """配置命令只修改已有本地 JSON，否则修改全局 JSON。"""
    local, _, global_file = config_sources()
    return local if local.is_file() else global_file


def dotenv_file() -> Path:
    if path := os.environ.get("REDLOTUS_DOTENV_FILE"):
        return Path(path).expanduser().resolve()
    return local_config_root() / ".env"


def _storage_workspace(workspace=None):
    if workspace is not None:
        return workspace
    from redlotus.core.agents import WorkspaceContext, current_workspace

    return WorkspaceContext.from_path(current_workspace())


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


def project_data_dir(workspace) -> Path:
    return _project_storage_path("project_dir", workspace)


def session_data_dir(workspace) -> Path:
    return _project_storage_path("sessions_dir", workspace)


def references_dir(workspace=None) -> Path:
    return _project_storage_path("references_dir", workspace)


def runtime_dir(workspace=None) -> Path:
    return _project_storage_path("runtime_dir", workspace)


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
    from redlotus.core.session import SessionFile

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


# ---- 只读随包资源 ----
def prompts_dir() -> Path:
    return resource_root() / "prompts"


def skills_dir() -> Path:
    """随包基线技能（只读）。"""
    return resource_root() / "tools" / "skills"


# ---- 全局可写状态 ----
def logs_dir(workspace=None) -> Path:
    return _project_storage_path("project_logs_dir", workspace)


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


CONSOLE_FMT = "{time:HH:mm:ss} | {level: <8} | {message}"
FILE_FMT = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}"
_WARNING_NO = 30
_STYLES = {
    "DEBUG": "dim cyan",
    "INFO": "green",
    "WARNING": "yellow",
    "ERROR": "bold red",
    "CRITICAL": "bold white on red",
}
_configured_dir: Path | None = None
_configuration_lock = threading.Lock()
_SESSION_LOG_DIR = "session_log_dir"
_LOG_DIR = "log_dir"
_TASK_LOG_PATH = "task_log_path"
_task_log_path: ContextVar[Path | None] = ContextVar("task_log_path", default=None)
_task_log_paths: dict[Path, Path] = {}
_workspace_log_dirs: dict[Path, Path] = {}
SESSION_LOG_MAX_BYTES = 10 * 1024 * 1024  # 单会话日志上限，超出滚动保留一个 .log.1
LOG_RETENTION_DAYS = 14.0  # logs 根目录 *.log 保留天数；<=0 关闭清理


def get_log_dir() -> Path:
    return _configured_dir or logs_dir()


def _workspace_log_dir(workspace) -> Path:
    root = workspace.root
    if log_dir := _workspace_log_dirs.get(root):
        return log_dir
    log_dir = logs_dir(workspace)
    _workspace_log_dirs[root] = log_dir
    return log_dir


def _install_log_sinks(log_dir: Path) -> None:
    global _configured_dir
    _configured_dir = log_dir
    _lg.remove()
    _lg.add(
        _console_sink, level="DEBUG", format=CONSOLE_FMT, filter=_console_filter
    )
    _lg.add(_session_sink, level="DEBUG", format=FILE_FMT, filter=_session_filter)
    _lg.add(_task_sink, level="DEBUG", format=FILE_FMT, filter=_task_filter)


def ensure_configured() -> None:
    with _configuration_lock:
        if _configured_dir is not None:
            return
        log_dir = logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        _install_log_sinks(log_dir)
        prune_old_logs()


def prepare_log_dir(workspace) -> Path:
    """Validate the target project's log directory before discarding a session."""
    log_dir = _workspace_log_dir(workspace)
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def activate_log_dir(log_dir: Path) -> None:
    """Route future logs to a directory prepared before the workspace switch."""
    global _configured_dir
    target = Path(log_dir).resolve()
    with _configuration_lock:
        if _configured_dir == target:
            return
        if _configured_dir is None:
            _install_log_sinks(target)
        else:
            _configured_dir = target
        prune_old_logs()


def _console_filter(record: dict) -> bool:
    # Index details stay in the file log; production progress and all failures remain visible.
    return not record["extra"].get("file_only") and not (
        record["level"].no < _WARNING_NO and record["message"].startswith("RAG ")
    )


def _console_sink(message: Any) -> None:
    # 延迟导入：控制台日志走 CLI/TUI 的 Rich sink，而非 stderr。
    from rich.text import Text

    from redlotus.core.presentation import emit_renderable

    level = message.record["level"].name
    emit_renderable(Text(str(message).rstrip("\n"), style=_STYLES.get(level, "")))


def _session_filter(record: dict) -> bool:
    return bool(record["extra"].get("session"))


def _record_log_dir(record: dict) -> Path | None:
    value = record["extra"].get(_LOG_DIR) or record["extra"].get(
        _SESSION_LOG_DIR
    )
    return Path(value) if value else None


def _session_sink(message: Any) -> None:
    log_dir = _record_log_dir(message.record) or get_log_dir()
    path = log_dir / f"{message.record['extra']['session']}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() and path.stat().st_size >= SESSION_LOG_MAX_BYTES:
            backup = path.with_name(path.name + ".1")
            backup.unlink(missing_ok=True)
            path.rename(backup)
    except OSError:
        pass
    with path.open("a", encoding="utf-8") as f:
        f.write(str(message))


def _task_path(record: dict) -> Path | None:
    log_dir = _record_log_dir(record)
    if log_dir is None:
        return _task_log_paths.get(get_log_dir())
    if path := record["extra"].get(_TASK_LOG_PATH):
        task_path = Path(path)
        if task_path.parent == log_dir:
            return task_path
    return _task_log_paths.get(log_dir)


def _task_filter(record: dict) -> bool:
    return _task_path(record) is not None


def _task_sink(message: Any) -> None:
    path = _task_path(message.record)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(str(message))


def _emit(
    level: str,
    msg: object,
    args: tuple[Any, ...],
    *,
    exc_info: bool = False,
    file_only: bool = False,
) -> None:
    # loguru 用 {}-style；这里沿用项目的 %-style 先自行格式化，再把成品串原样交给 loguru
    # （不传 args 时 loguru 不会再 .format()，故含 { } / JSON 的文本也安全）。
    ensure_configured()
    text = (str(msg) % args) if args else str(msg)
    extra = {"file_only": True} if file_only else {}
    if path := _task_log_path.get():
        extra[_TASK_LOG_PATH] = str(path)
    from redlotus.core.agents import active_workspace

    if workspace := active_workspace():
        extra[_LOG_DIR] = str(_workspace_log_dir(workspace))
    target = _lg.bind(**extra) if extra else _lg
    target.opt(exception=exc_info).log(level, text)


def debug(msg: object, *args: Any, exc_info: bool = False) -> None:
    _emit("DEBUG", msg, args, exc_info=exc_info)


def info(msg: object, *args: Any, exc_info: bool = False) -> None:
    _emit("INFO", msg, args, exc_info=exc_info)


def warning(msg: object, *args: Any, exc_info: bool = False) -> None:
    _emit("WARNING", msg, args, exc_info=exc_info)


def error(msg: object, *args: Any, exc_info: bool = False, file_only: bool = False) -> None:
    _emit("ERROR", msg, args, exc_info=exc_info, file_only=file_only)


def info_file_only(msg: object, *args: Any) -> None:
    """只写文件、不上控制台（避免与 Rich 输出重复）。"""
    _emit("INFO", msg, args, file_only=True)


def setup_task_logger(task_name: str = "task") -> None:
    """为本次任务设置日志文件；同一工作区的新任务替换默认路由。"""
    ensure_configured()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    from redlotus.core.agents import active_workspace

    workspace = active_workspace()
    log_dir = _workspace_log_dir(workspace) if workspace is not None else get_log_dir()
    with _configuration_lock:
        log_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{safe_name(task_name, max_len=50, fallback='task')}_{ts}.log"
        path = log_dir / filename
        _task_log_paths[log_dir] = path
        _task_log_path.set(path)
    info("日志文件已创建: %s", path)


def prune_old_logs(max_age_days: float | None = None) -> None:
    """删除 logs 根目录下超过保留期的 *.log / *.log.1（不递归，不动 conversations 等子目录）。"""
    days = LOG_RETENTION_DAYS if max_age_days is None else max_age_days
    if days <= 0:
        return
    cutoff = time.time() - days * 86400.0
    try:
        candidates = list(get_log_dir().glob("*.log")) + list(
            get_log_dir().glob("*.log.1")
        )
    except OSError:
        return
    for p in candidates:
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def session_log_context(session_name: str):
    """把本上下文内的日志额外落到 {session}.log。"""
    from redlotus.core.agents import active_workspace

    workspace = active_workspace()
    log_dir = _workspace_log_dir(workspace) if workspace is not None else get_log_dir()
    with _lg.contextualize(
        session=safe_name(session_name, max_len=50, fallback="task"),
        **{_SESSION_LOG_DIR: str(log_dir)},
    ):
        yield


_local = threading.local()


def openai_base_url(value: str | None) -> str | None:
    """Append /v1 for a bare host; preserve provider-specific API prefixes."""
    if not value:
        return None
    base = value.rstrip("/")
    return base if urlsplit(base).path else base + "/v1"


def _clients() -> dict[str, httpx.AsyncClient]:
    """A connection pool belongs to one event loop in one thread."""
    loop = asyncio.get_running_loop()
    pools = getattr(_local, "pools", None)
    if pools is None:
        pools = _local.pools = {}
    return pools.setdefault(loop, {})


def get_client(
    key: str,
    factory: Callable[[], httpx.AsyncClient],
) -> httpx.AsyncClient:
    """Return a named AsyncClient owned by the current event loop."""
    clients = _clients()
    client = clients.get(key)
    if client is None or client.is_closed:
        client = factory()
        clients[key] = client
    return client


async def close_client(key: str) -> None:
    client = _clients().pop(key, None)
    if client is not None and not client.is_closed:
        try:
            await client.aclose()
        except Exception as e:
            debug("关闭 HTTP 客户端 %r 时忽略异常: %s", key, e)


async def close_all_clients() -> None:
    """Close this loop's clients without touching other Agent threads."""
    keys = list(_clients())
    for key in keys:
        await close_client(key)
    _local.pools.pop(asyncio.get_running_loop(), None)


if TYPE_CHECKING:
    from pydantic_ai.usage import UsageLimits as _UsageLimits
    from redlotus.core.agents import AgentRunPolicy








_CONFIG: tuple[tuple, dict[str, Any]] | None = None
class ConfigError(ValueError):
    """配置错误只包含字段和来源，不包含可能敏感的值。"""


class ConfigValues(dict):
    """保持字典接口，同时为必填项和凭据保留路径及来源。"""

    def __init__(self, values=(), *, path=(), origins=None):
        self.path, self.origins = path, origins if origins is not None else {}
        super().__init__()
        for key, value in dict(values).items():
            self[key] = (
                ConfigValues(value, path=(*path, key), origins=self.origins)
                if isinstance(value, dict) else value
            )

    def __getitem__(self, key):
        if key not in self:
            raise ConfigError(f"缺少配置 {'.'.join((*self.path, key))}；检查来源: {config_source_summary()}")
        return super().__getitem__(key)


def _connection_field(key: str) -> bool:
    lowered = key.lower()
    return lowered in {"api_key", "base_url", "siliconflow_base", "siliconflow_key"} or lowered.endswith(("_api_key", "_base_url"))


def _model_selection_field(path: tuple[str, ...]) -> bool:
    return bool(path) and (
        path[0] == "RAG_models" or
        path[0] in {"models", "model_presets"} and path[-1] == "name"
    )


def _selected_model_name_path(
    role: str,
    cfg: dict[str, Any],
    schema: dict[str, Any] | None = None,
) -> tuple[str, ...]:
    """Locate the field that owns a selected role's model name."""
    selected = (cfg.get("models") or {}).get(role)
    if selected is None and schema is not None:
        selected = (schema.get("models") or {}).get(role)
    preset = selected if isinstance(selected, str) else selected.get("preset")
    presets = (cfg.get("model_presets") or {}).copy()
    if schema is not None:
        presets = {**(schema.get("model_presets") or {}), **presets}
    if isinstance(preset, str) and preset.strip() and preset in presets:
        return "model_presets", preset, "name"
    return "models", role, "name"


def _model_roles(schema: dict[str, Any], values: dict[str, Any]) -> list[str]:
    """List roles declared by the shipped schema and any configured extensions."""
    return list(
        dict.fromkeys(
            [*(schema.get("models") or {}), *(values.get("models") or {})]
        )
    )


def _validate_config(value, source: Path, path=()) -> None:
    """校验结构和服务字段；不提供任何模型或策略默认值。"""
    objects = {"models", "gateways", "model_presets", "RAG_models", "context", "storage",
               "bot", "lifecycle", "memory_perception", "agent_run_policy",
               "short_term_memory", "long_term_memory", "model_metadata",
               "rag_service", "input_limits", "task_title", "conversation_log"}
    key = path[-1] if path else ""
    expected = None
    if not path or len(path) == 1 and key in objects:
        expected = dict
    elif len(path) == 2 and path[0] in {"gateways", "model_presets"}:
        expected = dict
    elif len(path) == 2 and path[0] == "models":
        expected = (dict, str)
    elif _connection_field(key) or key == "api_key_env" or (
        len(path) == 2 and path[0] == "RAG_models"
        or path and path[0] == "storage" and key.endswith("_dir")
    ):
        expected = str
    elif path and path[0] == "gateways" and key in {"timeout", "connect_timeout"}:
        expected = (int, float)
    elif path and path[0] in {"models", "model_presets"} and key in {"max_tokens", "temperature", "top_p"}:
        expected = int if key == "max_tokens" else (int, float)
    elif path == ("MODEL_HTTP_TIMEOUT",):
        expected = (int, float)
    if expected and (value is not None or expected is dict) and (not isinstance(value, expected) or isinstance(value, bool) and expected != bool):
        raise ConfigError(f"配置 {source}: 字段 {'.'.join(path) or '<root>'} 类型错误")
    if expected is list and isinstance(value, list):
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise ConfigError(f"配置 {source}: 字段 {'.'.join(path)}.{index} 类型错误")
    if isinstance(value, dict):
        for name, child in value.items():
            _validate_config(child, source, (*path, name))


def _parse_config(path: Path, raw: bytes | None, *, dotenv=False) -> dict:
    """解析一个来源；.env 不做环境变量展开，嵌套键用双下划线。"""
    if raw is None:
        return {}
    try:
        if not dotenv:
            result = json.loads(raw)
        else:
            result = {}
            for binding in parse_stream(StringIO(raw.decode("utf-8-sig"))):
                if binding.error:
                    raise ConfigError(f"配置 {path}: 第 {binding.original.line} 行语法错误")
                if binding.key is None or binding.value is None:
                    continue
                try:
                    value = json.loads(binding.value)
                except json.JSONDecodeError:
                    value = binding.value
                parts = binding.key.split("__")
                node = result
                for key in parts[:-1]:
                    node = node.setdefault(key, {})
                    if not isinstance(node, dict):
                        raise ConfigError(f"配置 {path}: 字段 {binding.key} 结构冲突")
                if parts[-1] in node or not all(parts):
                    raise ConfigError(f"配置 {path}: 字段 {binding.key} 重复或结构冲突")
                node[parts[-1]] = value
        _validate_config(result, path)
        if not isinstance(result, dict):
            raise ConfigError(f"配置 {path}: 根节点必须是对象")
        return result
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"配置 {path}: 无效编码或 JSON，无法读取") from exc


def _merge_config(target, incoming, origins, source, path=()):
    """较高层逐字段覆盖；空白连接配置才继续使用低层值。"""
    for key, value in incoming.items():
        field = (*path, key)
        if _connection_field(key) and (value is None or isinstance(value, str) and not value.strip()):
            continue
        if _model_selection_field(field) and key in target and (
            value is None or isinstance(value, str) and not value.strip()
        ):
            continue
        if isinstance(value, dict):
            if not isinstance(target.get(key), dict):
                target[key] = {}
            _merge_config(target[key], value, origins, source, field)
        else:
            target[key] = deepcopy(value)
            origins[field] = source


@contextmanager
def _config_locks():
    """Acquire all configuration writer locks in a stable order."""
    paths = set(path for path in config_sources() if path.exists())
    paths.add(config_file())
    with ExitStack() as locks:
        for path in sorted(paths):
            locks.enter_context(file_lock(path))
        yield


def _config_version():
    return tuple((path, path.read_bytes() if path.is_file() else None) for path in config_sources())


def settings() -> dict[str, Any]:
    """从各配置来源的一次完整字节快照解析，并返回独立副本。"""
    global _CONFIG
    version = _config_version()
    cached = _CONFIG
    if cached is None or version != cached[0]:
        value, origins = {}, {}
        for index in reversed(range(len(version))):
            path, raw = version[index]
            _merge_config(value, _parse_config(path, raw, dotenv=index == 1), origins, index)
        cached = version, ConfigValues(value, origins=origins)
        _CONFIG = cached
    return deepcopy(cached[1])


def load_config() -> dict[str, Any]:
    global _CONFIG
    _CONFIG = None
    _workspace_log_dirs.clear()
    return settings()


def reload_config() -> dict[str, Any]:
    return load_config()


def initialize_user_configuration() -> Path:
    """Validate user-selected sources without copying or creating configuration files."""
    path = config_file()
    values = load_config()
    if not values:
        raise ConfigError(f"缺少配置 models；请填写 {path}。检查来源: {config_source_summary()}")
    return path


async def ask_configuration(question: str, *, secret=False):
    """Read a startup answer with the same hidden-key contract as the TUI dialog."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings

    bindings = KeyBindings()

    @bindings.add("escape", eager=True)
    def cancel(event):
        event.app.exit(result=None)

    return await PromptSession(key_bindings=bindings).prompt_async(question, is_password=secret)


def configuration_prompt_available() -> bool:
    """Return whether the default first-use dialog can safely open a terminal prompt."""
    return bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stdout, "isatty", lambda: False)()
    )


class ConfigurationSetup:
    """Collect explicit configuration edits and commit them as a single atomic change."""

    def __init__(self, ask=None, emit=print):
        self.values = settings()
        self.changes = {}
        self.ask, self.emit = ask or ask_configuration, emit

    def value(self, path):
        node = self.values
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        return node

    @staticmethod
    def assign(values, path, value):
        node = values
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = deepcopy(value)

    def connection_paths(self, role="coordinator"):
        """Use the selected role's credential reference, not an unrelated global API key."""
        _, parameters = get_model_and_params(role, cfg=self.values)
        gateway = parameters.get("gateway")
        if not gateway:
            return [("BASE_URL",), ("API_KEY",)]
        selected = self.values["gateways"][gateway]
        # Edit an explicit gateway key when present; otherwise retain its named reference.
        key = (selected["api_key_env"],) if selected.get("api_key_env") and not selected.get("api_key") else ("gateways", gateway, "api_key")
        return [("gateways", gateway, "base_url"), key]

    async def fill(self, path):
        """Validate one field, retaining previous input until the whole dialog succeeds."""
        key = ".".join(path)
        current = self.value(path)
        secret = "key" in path[-1].lower() or "token" in path[-1].lower()
        shown = ("已填写" if current else "空") if secret else str(current if current is not None else "空")
        hint = "回车保留"
        if _model_selection_field(path) and path[0] == "models":
            hint += "；输入 =角色名 可明确复用其模型"
        while True:
            answer = await self.ask(f"{key}（当前 {shown}；{hint}；Esc 取消）：", secret=secret)
            if answer is None or answer == "\x1b":
                return False
            text = answer.strip()
            if not text and current not in (None, ""):
                return True
            try:
                if not text:
                    raise ValueError("不能为空")
                value = get_model_and_params(text[1:], cfg=self.values)[0] if text.startswith("=") and path[0] == "models" else text
                if "url" in path[-1].lower() or path[-1] == "SILICONFLOW_BASE":
                    parsed = urlsplit(value)
                    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                        raise ValueError("请输入完整的 http/https 服务地址")
                candidate = deepcopy(self.values)
                self.assign(candidate, path, value)
                _validate_config(candidate, config_file())
            except (ValueError, KeyError, TypeError):
                self.emit(f"{key} 无效：请按字段类型填写；模型复用需指定已配置的角色。")
                continue
            self.values = candidate
            self.changes[path] = value
            return True

    def commit(self):
        """Only changed fields are applied to the latest locked configuration."""
        if self.changes:
            def apply(values):
                for path, value in self.changes.items():
                    self.assign(values, path, value)
            update_config(apply)


def python_tool_startup_notice(cfg: dict[str, Any]) -> str | None:
    """Explain an optional frozen-build Python requirement without blocking chat."""
    if not _frozen():
        return None
    if any(shutil.which(name) for name in ("python", "python3", "py")):
        return None
    return (
        "未发现外部 Python：纯聊天仍可使用；Python/pip 工具暂不可用。"
        "请将现有 Python 加入 PATH 后重启。"
    )


def rag_configuration_paths():
    """Connection and model fields exposed by the existing RAG configuration dialog."""
    return [("SILICONFLOW_BASE",), ("SILICONFLOW_KEY",), ("RAG_models", "embedding"), ("RAG_models", "reranker")]


async def prepare_startup_configuration(*, ask=None, emit=print) -> bool:
    """Complete first-use configuration before constructing Agents or starting clients."""
    initialize_user_configuration()
    setup = ConfigurationSetup(ask, emit)
    schema = setup.values
    try:
        missing_model_names = []
        roles = _model_roles(schema, setup.values)
        if "coordinator" not in roles:
            raise ConfigError(f"缺少配置 models.coordinator；检查来源: {config_source_summary()}")
        roles.sort(key=lambda role: role != "coordinator")
        for role in roles:
            try:
                get_model_and_params(role, cfg=setup.values)
            except ConfigError as exc:
                path = _selected_model_name_path(role, setup.values, schema)
                missing_role = str(exc).startswith(
                    f"缺少配置 models.{role}；"
                ) or str(exc).startswith("缺少配置 models；")
                if ".".join(path) not in str(exc) and not missing_role:
                    raise
                missing_model_names.append(path)
        interactive = ask is not None or configuration_prompt_available()
        if not interactive:
            missing = list(missing_model_names)
            if not missing:
                paths = dict.fromkeys(path for role in roles for path in setup.connection_paths(role))
                missing.extend(path for path in paths if not str(setup.value(path) or "").strip())
            if missing:
                fields = "、".join(".".join(path) for path in missing)
                raise ConfigError(f"非交互启动缺少必填配置 {fields}；请编辑 {config_file()} 后重试")
            missing_rag = [path for path in rag_configuration_paths() if not str(setup.value(path) or "").strip()]
            if missing_rag:
                emit("RAG 尚未配置；可先聊天，使用 /api embedding 补齐向量检索配置。")
            if notice := python_tool_startup_notice(setup.values):
                emit(notice)
            return True
        emit(f"配置修改目标: {config_file()}\n读取来源: {config_source_summary()}")
        for path in dict.fromkeys(missing_model_names):
            if not await setup.fill(path):
                return False
        paths = dict.fromkeys(path for role in roles for path in setup.connection_paths(role))
        for path in paths:
            if not str(setup.value(path) or "").strip() and not await setup.fill(path):
                return False
        missing_rag = [path for path in rag_configuration_paths() if not str(setup.value(path) or "").strip()]
        if missing_rag:
            choice = await setup.ask("现在配置 RAG 向量检索吗？y 配置 / n 稍后（回车稍后；Esc 取消）：")
            if choice is None or choice == "\x1b":
                return False
            if choice.strip().lower() in {"y", "yes", "是"}:
                for path in missing_rag:
                    if not await setup.fill(path):
                        return False
        setup.commit()
        if notice := python_tool_startup_notice(setup.values):
            emit(notice)
        return True
    except (KeyboardInterrupt, EOFError, asyncio.CancelledError):
        emit("配置已取消，未保存本次填写内容。")
        return False


async def configure_api(*, embedding=False, ask=None, emit=print) -> bool:
    """Edit service fields atomically for both plain CLI and TUI callers."""
    setup = ConfigurationSetup(ask, emit)
    paths = rag_configuration_paths() if embedding else setup.connection_paths()
    try:
        for path in paths:
            if not await setup.fill(path):
                return False
        setup.commit()
        return True
    except (KeyboardInterrupt, EOFError, asyncio.CancelledError):
        return False


def credential_value(gateway_name: str, cfg: dict) -> str:
    """命名网关的直接值和命名引用按各自来源排序，同层优先直接值。"""
    gateway = cfg["gateways"][gateway_name]
    candidates = [(gateway.get("api_key"), ("gateways", gateway_name, "api_key"))]
    if reference := gateway.get("api_key_env"):
        candidates.append((get_env(reference, warn=False, cfg=cfg), (reference,)))
    origins = getattr(cfg, "origins", {})
    candidates.sort(key=lambda item: origins.get(item[1], 0))
    return next((str(value).strip() for value, _ in candidates if value is not None and str(value).strip()), "")


def get_env(key: str, *, warn: bool = True, default: str = "", cfg=None) -> str:
    """旧标量读取接口共享配置快照；不从宿主环境变量读取业务值。"""
    configuration = settings() if cfg is None else cfg
    raw = configuration.get(key)
    if isinstance(raw, (dict, list)):
        raise ConfigError(f"配置字段 {key} 必须是标量；检查来源: {config_source_summary()}")
    value = str(raw).strip() if raw is not None else ""
    if not value and warn and not default:
        raise ConfigError(f"缺少配置 {key}；检查来源: {config_source_summary()}")
    return value or default


def _missing_keys(keys: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(key for key in keys if not get_env(key, warn=False).strip())


def missing_main_api_keys() -> tuple[str, ...]:
    _, params = get_model_and_params("coordinator")
    if params.get("gateway"):
        from redlotus.core.gateway import ModelTarget

        target = ModelTarget.for_role("coordinator")
        return () if target.api_key else (f"gateways.{params['gateway']}.api_key",)
    return _missing_keys(("BASE_URL", "API_KEY"))


def missing_rag_api_keys() -> tuple[str, ...]:
    return _missing_keys(("SILICONFLOW_BASE", "SILICONFLOW_KEY"))


def _persist_changes(target, before, after):
    """只把用户改动落到写入层，不复制回退层的其他字段。"""
    for key in before.keys() - after.keys():
        target.pop(key, None)
    for key, value in after.items():
        if key in before and before[key] == value:
            continue
        if isinstance(value, dict) and isinstance(before.get(key), dict):
            child = target.setdefault(key, {})
            _persist_changes(child, before[key], value)
        else:
            target[key] = deepcopy(value)


def update_config(change) -> None:
    """锁内读取最新有效值，只保存本次编辑产生的差异。"""
    path = config_file()
    try:
        with _config_locks():
            path = config_file()
            raw = _parse_config(path, path.read_bytes() if path.is_file() else None)
            before = settings()
            after = deepcopy(before)
            change(after)
            _persist_changes(raw, before, after)
            _validate_config(raw, path)
            atomic_write_json(path, raw)
    except OSError as exc:
        raise ConfigError(f"无法写入配置 {path}: {exc}") from None
    reload_config()


def get_agent_usage_limits() -> "_UsageLimits":
    """单次 Agent 运行对模型请求次数上限"""
    from pydantic_ai.usage import UsageLimits

    cfg = settings()
    raw = cfg["request_limit"]
    if raw is None or str(raw).strip().lower() in ("none", "unlimited", "null", ""):
        return UsageLimits(request_limit=None)
    return UsageLimits(request_limit=int(raw))


def get_agent_run_policy() -> AgentRunPolicy:
    """Construct the execution policy only when configuration is requested."""
    from redlotus.core.agents import AgentRunPolicy

    return AgentRunPolicy.from_config(settings())


def supported_thinking_efforts(model_name: str | None) -> tuple[str, ...]:
    from redlotus.core.history import _lookup_openrouter_meta

    meta = _lookup_openrouter_meta(model_name) if model_name else None
    configured = settings()["model_metadata"]["supported_thinking_efforts"]
    available = (meta or {}).get("supported_efforts") or configured
    return tuple(value for value in configured if value in available)


def role_supported_thinking_efforts(role: str) -> tuple[str, ...]:
    return supported_thinking_efforts(get_model_and_params(role)[0])


def apply_thinking_config(model_params, *, model_name=None):
    """Translate config thinking fields to Pydantic AI's common model settings."""
    params = deepcopy(model_params)
    thinking = str(params.pop("thinking", "")).strip().lower()
    effort = str(params.pop("reasoning_effort", "")).strip().lower()
    if thinking in ("disabled", "off", "false"):
        params["thinking"] = False
        if model_name and "deepseek" in model_name.lower():
            params["extra_body"] = {
                **params.get("extra_body", {}),
                "thinking": {"type": "disabled"},
            }
    elif thinking == "enabled":
        params["thinking"] = "xhigh" if effort == "max" else effort or True
    return params


def get_model_and_params(role: str, *, cfg=None) -> tuple[str, dict[str, Any]]:
    cfg = settings() if cfg is None else cfg
    raw = deepcopy(cfg["models"][role])
    if isinstance(raw, str):
        raw = {"preset": raw}
    preset = raw.pop("preset", None)
    if preset is not None and str(preset).strip():
        if not isinstance(preset, str) or preset not in cfg.get("model_presets", {}):
            raise ConfigError(
                f"配置 models.{role}.preset 引用了不存在的 model_presets.{preset}；"
                f"检查来源: {config_source_summary()}"
            )
        base = deepcopy(cfg["model_presets"][preset])
        origins = getattr(cfg, "origins", {})
        selection = origins.get(("models", role, "preset"), 0)
        raw = {key: value for key, value in raw.items()
               if min((rank for path, rank in origins.items()
                       if path[:3] == ("models", role, key)), default=0) <= selection}
        raw = {**base.pop("settings", {}), **base, **raw.pop("settings", {}), **raw}
    raw = {**raw.pop("settings", {}), **raw}
    gateway = raw.get("gateway")
    if gateway is not None and str(gateway).strip():
        if not isinstance(gateway, str) or gateway not in cfg.get("gateways", {}):
            raise ConfigError(
                f"配置 models.{role}.gateway 引用了不存在的 gateways.{gateway}；"
                f"检查来源: {config_source_summary()}"
            )
        raw["gateway"] = gateway
    name = raw.pop("name", None)
    if not isinstance(name, str) or not name.strip():
        field = ".".join(_selected_model_name_path(role, cfg))
        raise ConfigError(f"缺少有效配置 {field}；检查来源: {config_source_summary()}")
    name = name.strip()
    return name, raw


def set_model_name(role: str, model_name: str) -> None:
    selection = model_name.strip()

    def change(cfg):
        if selection in cfg.get("model_presets", {}):
            cfg["models"][role] = {"preset": selection}
        else:
            _, parameters = get_model_and_params(role, cfg=cfg)
            cfg["models"][role] = {"name": selection, **parameters}

    update_config(change)




def get_agent_roles(*, cfg=None) -> tuple[str, ...]:
    return tuple((settings() if cfg is None else cfg)["models"])


def get_context_profile_roles() -> tuple[str, ...]:
    cfg = settings()
    return tuple(
        role for role in get_agent_roles(cfg=cfg)
        if "auto_compress_ratio" in get_context_config(role, cfg=cfg)
    )


def get_context_config(role: str, *, cfg=None) -> dict[str, Any]:
    cfg = settings() if cfg is None else cfg
    _, parameters = get_model_and_params(role, cfg=cfg)
    fields = ("max_context_windows", "auto_compress_ratio", "compress_head_turns", "compress_tail_turns")
    result = {key: deepcopy(parameters[key]) for key in fields if key in parameters}
    for key, value in result.items():
        if key == "max_context_windows" and value is None:
            continue
        valid = (
            isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 1
            if key == "auto_compress_ratio"
            else isinstance(value, int) and not isinstance(value, bool)
            and (value > 0 if key == "max_context_windows" else value >= 0)
        )
        if not valid:
            raise ConfigError(f"无效配置 models.{role}.{key}；检查来源: {config_source_summary()}")
    return result


class ExitDeadline:
    """Bound process exit even when a native call ignores task cancellation."""

    def __init__(self, seconds: float):
        self._timer = threading.Timer(seconds, os._exit, args=(0,))
        self._timer.daemon = True

    def start(self):
        self._timer.start()

    def close(self):
        self._timer.cancel()


def install_stop_handlers(stop_event: asyncio.Event) -> None:
    """Map process signals to the interactive runner's stop event."""
    loop = asyncio.get_running_loop()

    def request_stop(*_args: object) -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except (NotImplementedError, ValueError):
            signal.signal(sig, request_stop)


async def run_cli(system=None):
    """Run the interactive RedLotus CLI/TUI."""
    from redlotus.core.system import AgentSystem

    if system is None:
        load_config()
        system = AgentSystem()
    stop_event = asyncio.Event()
    install_stop_handlers(stop_event)
    try:
        await system.run_interactive(stop_event=stop_event)
    finally:
        await system.shutdown()
        await close_all_clients()
    return system


def main() -> None:
    """CLI entrypoint used by root ``main.py``."""
    try:
        load_config()
        if not asyncio.run(prepare_startup_configuration()):
            return
        from redlotus.core.system import AgentSystem

        deadline = ExitDeadline(settings()["lifecycle"]["shutdown_grace_seconds"])
        try:
            system = AgentSystem(exit_deadline=deadline)
            asyncio.run(run_cli(system))
        finally:
            deadline.close()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None
