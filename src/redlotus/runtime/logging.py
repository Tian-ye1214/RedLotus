"""Project-aware log routing with an injected terminal renderer."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from loguru import logger as _lg
from rich.console import Console
from rich.text import Text

from redlotus.runtime.config import settings, config_value
from redlotus.runtime.resources import active_workspace, logs_dir, safe_name

console_sink = Console().print
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
_configured = False
_configuration_lock = threading.Lock()
_SESSION_LOG_DIR = "session_log_dir"
_LOG_DIR = "log_dir"
_TASK_LOG_PATH = "task_log_path"
_task_log_path: ContextVar[Path | None] = ContextVar("task_log_path", default=None)
_task_log_paths: dict[Path, Path] = {}

def get_log_dir() -> Path:
    return _configured_dir or logs_dir()

def _install_log_sinks(log_dir: Path | None) -> None:
    global _configured_dir, _configured
    _configured_dir, _configured = log_dir, True
    _lg.remove()
    _lg.add(
        _console_sink, level="DEBUG", format=CONSOLE_FMT, filter=_console_filter
    )
    _lg.add(_session_sink, level="DEBUG", format=FILE_FMT, filter=_session_filter)
    _lg.add(_task_sink, level="DEBUG", format=FILE_FMT)

def ensure_configured() -> None:
    with _configuration_lock:
        if not _configured:
            _install_log_sinks(None)

def prepare_log_dir(workspace) -> Path | None:
    """Validate the target project's log directory before discarding a session."""
    if config_value(settings(), ("storage", "project_logs_dir"), kind=str) is None:
        return None
    log_dir = logs_dir(workspace)
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir

def activate_log_dir(log_dir: Path | None) -> None:
    """Route future logs to a directory prepared before the workspace switch."""
    global _configured_dir
    target = Path(log_dir).resolve() if log_dir is not None else None
    with _configuration_lock:
        if not _configured:
            _install_log_sinks(target)
        else:
            _configured_dir = target

def _console_filter(record: dict) -> bool:
    # Index details stay in the file log; production progress and all failures remain visible.
    return not record["extra"].get("file_only") and not (
        record["level"].no < _WARNING_NO and record["message"].startswith("RAG ")
    )

def _console_sink(message: Any) -> None:
    level = message.record["level"].name
    console_sink(Text(str(message).rstrip("\n"), style=_STYLES.get(level, "")))

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
    with path.open("a", encoding="utf-8") as f:
        f.write(str(message))

def _task_path(record: dict) -> Path | None:
    log_dir = _record_log_dir(record)
    if log_dir is None:
        return _task_log_paths.get(_configured_dir)
    if path := record["extra"].get(_TASK_LOG_PATH):
        task_path = Path(path)
        if task_path.parent == log_dir:
            return task_path
    return _task_log_paths.get(log_dir)

def _task_sink(message: Any) -> None:
    path = _task_path(message.record)
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(str(message))

def _emit(
    level: str,
    msg: object,
    *args: Any,
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
    if (workspace := active_workspace()) and config_value(settings(), ("storage", "project_logs_dir"), kind=str) is not None:
        extra[_LOG_DIR] = str(logs_dir(workspace))
    target = _lg.bind(**extra) if extra else _lg
    target.opt(exception=exc_info).log(level, text)

def setup_task_logger(task_name: str = "task") -> None:
    """为本次任务设置日志文件；同一工作区的新任务替换默认路由。"""
    ensure_configured()
    if config_value(settings(), ("storage", "project_logs_dir"), kind=str) is None:
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    workspace = active_workspace()
    log_dir = logs_dir(workspace) if workspace is not None else get_log_dir()
    with _configuration_lock:
        log_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{safe_name(task_name, fallback='task')}_{ts}.log"
        path = log_dir / filename
        _task_log_paths[log_dir] = path
        _task_log_path.set(path)
    info("日志文件已创建: %s", path)

def prune_old_logs() -> None:
    """会话入口按配置的保留期清理日志根目录的普通 .log 文件。"""
    config = settings()
    days = config_value(config, ('storage', 'cleanup', 'log_retention_days'), kind=(int, float))
    if days is None or days <= 0:
        return
    if config_value(config, ("storage", "project_logs_dir"), kind=str) is None:
        return
    cutoff = time.time() - days * 86400.0
    with suppress(OSError):
        candidates = list(get_log_dir().glob("*.log"))
        for p in candidates:
            with suppress(OSError):
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)

@contextmanager
def session_log_context(session_name: str):
    """把本上下文内的日志额外落到 {session}.log。"""
    log_dir = prepare_log_dir(active_workspace())
    extra = {"session": safe_name(session_name, fallback="task"), _SESSION_LOG_DIR: str(log_dir)} if log_dir is not None else {}
    with _lg.contextualize(**extra):
        yield


debug = partial(_emit, "DEBUG")
info = partial(_emit, "INFO")
warning = partial(_emit, "WARNING")
error = partial(_emit, "ERROR")
info_file_only = partial(_emit, "INFO", file_only=True)
