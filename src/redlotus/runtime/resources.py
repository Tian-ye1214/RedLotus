"""Runtime resources responsibilities."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from loguru import logger as _lg

from redlotus.runtime.files import logs_dir, safe_name

CONSOLE_FMT = "{time:HH:mm:ss} | {level: <8} | {message}"


FILE_FMT = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}"


_WARNING_NO = 30


console_log_sink = None


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
    if console_log_sink is None:
        print(str(message), end="", file=sys.stderr)
    else:
        console_log_sink(message)


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
    from redlotus.runtime.context import active_workspace

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
    from redlotus.runtime.context import active_workspace

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
    from redlotus.runtime.context import active_workspace

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
