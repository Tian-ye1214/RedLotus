"""Application startup, configuration, global paths, logging, and owned I/O resources."""

from __future__ import annotations

import sys
import os
import errno
import platformdirs
import json
import time
import threading
import asyncio
import httpx
import hashlib
import signal
from pathlib import Path
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
from typing import Any, Iterator, TYPE_CHECKING
from filelock import FileLock, Timeout
from loguru import logger as _lg
from collections.abc import Callable
from urllib.parse import urlsplit
from copy import deepcopy
from dotenv import dotenv_values
from pydantic_ai.usage import UsageLimits


APP_NAME = "RedLotus"


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """随包只读资源根（redlotus 包目录）。

    - 正常 / editable 安装：本文件位于 redlotus/infra/paths.py，上溯两级即包根。
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
    return _storage_path(
        "state_dir", Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))
    )


def _storage_path(name: str, default: Path) -> Path:
    # Config discovery uses user_config_dir(), so it never depends on these data paths.
    configured = settings()["storage"][name]
    return Path(configured).expanduser().resolve() if configured else default


def user_config_dir() -> Path:
    """用户配置根：config.json / .env / bot config.yaml。"""
    return Path(
        os.environ.get("REDLOTUS_CONFIG_DIR")
        or platformdirs.user_config_dir(APP_NAME, appauthor=False)
    )


# ---- 工作产物：跟随当前工作目录 ----
# ---- 配置 / 密钥：用户配置目录 ----
def config_file() -> Path:
    if path := os.environ.get("REDLOTUS_CONFIG_FILE"):
        return Path(path).resolve()
    return user_config_dir().resolve() / "config.json"


def default_config_file() -> Path:
    """随包默认 config.json（首次运行 seed 用）。"""
    return resource_root() / "core" / "config.json"


def dotenv_file() -> Path:
    if path := os.environ.get("REDLOTUS_DOTENV_FILE"):
        return Path(path).resolve()
    return user_config_dir() / ".env"


def project_data_dir(workspace) -> Path:
    """Project identity is independent of the interpreter or installation directory."""
    return user_data_dir() / "projects" / workspace.project_id


def session_data_dir(workspace) -> Path:
    return (
        _storage_path("sessions_dir", user_data_dir() / "projects")
        / workspace.project_id
    )


def references_dir() -> Path:
    return _storage_path("references_dir", user_data_dir() / "references")


def runtime_dir() -> Path:
    return _storage_path("runtime_dir", user_data_dir())


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
    projects = _storage_children(sessions)
    if projects is None:
        return candidates
    for project in projects:
        if not _safe_storage_child(sessions, project) or not project.is_dir():
            continue
        for session_dir in _storage_children(project) or ():
            if not _safe_storage_child(project, session_dir) or not session_dir.is_dir():
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
                    candidates.append(
                        (message.stat().st_mtime, project, session_dir, message)
                    )
            except OSError:
                continue
    return sorted(candidates, key=lambda row: row[0])


def _session_protected(
    project: Path, session_dir: Path, message: Path, transaction: FileLock
) -> bool:
    try:
        from redlotus.core.session import SessionFile

        session = SessionFile.load(message, lock=transaction, recover=False)
        return (
            session.project_id != project.name
            or session.session_id != session_dir.name
            or session.metadata.get("active_turn")
            or session.pending_jobs()
        )
    except (OSError, ValueError, KeyError, TypeError, Timeout):
        return True


def _delete_old_session(
    project: Path, session_dir: Path, message: Path, cutoff: float
) -> int | None:
    with _locked_session(message) as transaction:
        if transaction is None or _session_protected(project, session_dir, message, transaction):
            return None
        try:
            if message.stat().st_mtime >= cutoff or not _safe_storage_child(session_dir, message):
                return None
            released = message.stat().st_size
            message.unlink()
            return released
        except OSError:
            return None


def _active_cache_projects(sessions: Path) -> set[str] | None:
    active = set()
    projects = _storage_children(sessions)
    if projects is None:
        return None
    for project in projects:
        if not _safe_storage_child(sessions, project) or not project.is_dir():
            active.add(project.name)
            continue
        session_dirs = _storage_children(project)
        if session_dirs is None:
            active.add(project.name)
            continue
        for session_dir in session_dirs:
            message = session_dir / "model_messages.json"
            invalid = (
                not _safe_storage_child(project, session_dir)
                or not session_dir.is_dir()
                or not _safe_storage_child(session_dir, message)
                or not message.is_file()
            )
            if invalid:
                active.add(project.name)
                break
            with _locked_session(message) as transaction:
                if transaction is None or _session_protected(project, session_dir, message, transaction):
                    active.add(project.name)
                    break
    return active


def _execution_storage_root(template, runtime: Path) -> Path | None:
    if not template:
        return None
    value = str(template).replace("{runtime}", str(runtime)).replace("{project_id}", "")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else runtime / path).resolve()


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
    sessions = _storage_path("sessions_dir", user_data_dir() / "projects")
    if _same_storage_volume(sessions, failed):
        for _, project, session_dir, message in _session_candidates(sessions, failed, cutoff):
            released = _delete_old_session(project, session_dir, message, cutoff)
            if released is not None:
                _storage_cleanup_log(message, released)
                if _retry_after_cleanup(retry):
                    return
    if not cache_enabled:
        raise error
    runtime = runtime_dir()
    execution = settings().get("execution") or {}
    cache_root = _execution_storage_root(execution.get("cache_dir"), runtime)
    environments = _execution_storage_root(execution.get("environment_dir"), runtime)
    if cache_root is None or not _same_storage_volume(cache_root, failed):
        raise error
    try:
        relative = failed.relative_to(sessions.resolve())
        current_project = relative.parts[0] if len(relative.parts) > 1 else None
    except ValueError:
        current_project = None
    active = _active_cache_projects(sessions)
    if active is None:
        raise error
    protected_projects = active | ({current_project} if current_project else set())
    for cache in _storage_children(cache_root) or ():
        marker = cache / ".redlotus-cache"
        if not _safe_storage_child(cache_root, cache) or not cache.is_dir():
            continue
        try:
            in_environment_tree = environments is not None and (
                cache.resolve().is_relative_to(environments)
                or environments.is_relative_to(cache.resolve())
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
def logs_dir() -> Path:
    return user_data_dir() / "logs"


def memory_dir() -> Path:
    """个人全局 MEMORY.md 及旧 SOUL/USER 文档的迁移备份目录。"""
    return user_data_dir() / "LongTermMemory"


def user_skills_dir() -> Path:
    """运行时安装的技能 overlay（可写）；与随包基线技能合并加载。"""
    return runtime_dir() / "skills"


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


def safe_segment(s: str, max_len: int = 80) -> str:
    s = (s or "").strip().replace("\n", " ")
    return safe_name(s, extra="_-.", max_len=max_len, fallback="default")


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
_task_sink_id: int | None = None
SESSION_LOG_MAX_BYTES = 10 * 1024 * 1024  # 单会话日志上限，超出滚动保留一个 .log.1
LOG_RETENTION_DAYS = 14.0  # logs 根目录 *.log 保留天数；<=0 关闭清理


def get_log_dir() -> Path:
    return _configured_dir or logs_dir()


def ensure_configured() -> None:
    global _configured_dir
    with _configuration_lock:
        if _configured_dir is not None:
            return
        _configured_dir = logs_dir()
        _configured_dir.mkdir(parents=True, exist_ok=True)
        _lg.remove()
        _lg.add(
            _console_sink, level="DEBUG", format=CONSOLE_FMT, filter=_console_filter
        )
        _lg.add(_session_sink, level="DEBUG", format=FILE_FMT, filter=_session_filter)
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


def _session_sink(message: Any) -> None:
    path = get_log_dir() / f"{message.record['extra']['session']}.log"
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
    target = _lg.bind(file_only=True) if file_only else _lg
    target.opt(exception=exc_info).log(level, text)


def debug(msg: object, *args: Any, exc_info: bool = False) -> None:
    _emit("DEBUG", msg, args, exc_info=exc_info)


def info(msg: object, *args: Any, exc_info: bool = False) -> None:
    _emit("INFO", msg, args, exc_info=exc_info)


def warning(msg: object, *args: Any, exc_info: bool = False) -> None:
    _emit("WARNING", msg, args, exc_info=exc_info)


def error(msg: object, *args: Any, exc_info: bool = False) -> None:
    _emit("ERROR", msg, args, exc_info=exc_info)


def info_file_only(msg: object, *args: Any) -> None:
    """只写文件、不上控制台（避免与 Rich 输出重复）。"""
    _emit("INFO", msg, args, file_only=True)


def setup_task_logger(task_name: str = "task") -> None:
    """为本次任务追加一个文件 sink（重复调用会替换上一个）。"""
    global _task_sink_id
    ensure_configured()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = (
        get_log_dir() / f"{safe_name(task_name, max_len=50, fallback='task')}_{ts}.log"
    )
    if _task_sink_id is not None:
        _lg.remove(_task_sink_id)
    _task_sink_id = _lg.add(path, level="DEBUG", format=FILE_FMT, encoding="utf-8")
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
    with _lg.contextualize(
        session=safe_name(session_name, max_len=50, fallback="task")
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
_DOTENV_CACHE: dict[tuple, dict[str, str]] | None = None
_API_CONFIG_KEYS = {"BASE_URL", "API_KEY", "SILICONFLOW_BASE", "SILICONFLOW_KEY"}


def _copy_missing_defaults(config, defaults, section=""):
    for key, value in defaults.items():
        if key not in config:
            config[key] = deepcopy(value)
        elif (
            isinstance(value, dict)
            and isinstance(config[key], dict)
            and section != "models"
            and key not in ("gateways", "model_presets")
        ):
            if key == "context":
                contexts = config[key]
                roles = set(defaults["models"]) & set(value)
                if (
                    not roles.intersection(contexts)
                    and "default_context_tokens" in contexts
                ):
                    _copy_missing_defaults(contexts, value["coordinator"])
                    _copy_missing_defaults(
                        contexts, {"compression": value["compression"]}
                    )
                    continue
                value = deepcopy(value)
                for role in roles:
                    value[role] = {
                        k: v
                        for k, v in value[role].items()
                        if k not in contexts.get("defaults", {})
                    }
            _copy_missing_defaults(config[key], value, key)


def initialize_config() -> None:
    path = config_file()
    with file_lock(path):
        defaults = json.loads(default_config_file().read_text(encoding="utf-8"))
        if not path.exists():
            atomic_write_json(path, defaults)
            return
        raw = path.read_bytes()
        current = json.loads(raw)
        updated = deepcopy(current)
        _copy_missing_defaults(updated, defaults)
        updated["agent_run_policy"].pop("max_worker_concurrent", None)
        updated["memory_perception"].pop("max_concurrent", None)
        execution = updated["execution"]
        if "blocked_code_patterns" in execution["permissions"]:
            execution["permissions"].pop("blocked_code_patterns")
            execution["inherit_env"] = list(
                dict.fromkeys(
                    [
                        *execution["inherit_env"],
                        *defaults["execution"]["inherit_env"],
                    ]
                )
            )
        if updated != current:
            backup = (
                path.parent
                / "config-backups"
                / (hashlib.sha256(raw).hexdigest() + ".json")
            )
            if not backup.exists():
                atomic_write_json(backup, current)
            atomic_write_json(path, updated)


def load_config() -> dict[str, Any]:
    global _CONFIG
    initialize_config()
    path = config_file()
    with file_lock(path):
        raw = path.read_bytes()
        value = json.loads(raw)
        version = (path, raw)
    _CONFIG = (version, value)
    return deepcopy(value)


def reload_config() -> dict[str, Any]:
    global _CONFIG, _DOTENV_CACHE
    _CONFIG = None
    _DOTENV_CACHE = None
    return load_config()


def settings() -> dict[str, Any]:
    path = config_file()
    with file_lock(path):
        version = (path, path.read_bytes()) if path.exists() else None
    cached = _CONFIG
    if cached is None or version != cached[0]:
        return load_config()
    return deepcopy(cached[1])


def _dotenv_values() -> dict[str, str]:
    """Read only the selected credential file, never an arbitrary project's .env."""
    global _DOTENV_CACHE
    files = [dotenv_file()]
    key = tuple(
        (str(path), path.stat().st_mtime_ns if path.is_file() else None)
        for path in files
    )
    if _DOTENV_CACHE is None:
        _DOTENV_CACHE = {}
    if key not in _DOTENV_CACHE:
        values = {}
        for path in files:
            if path.is_file():
                values.update(
                    {
                        str(k): str(v).strip()
                        for k, v in dotenv_values(path).items()
                        if v and str(v).strip()
                    }
                )
        _DOTENV_CACHE[key] = values
    return _DOTENV_CACHE[key]


def _config_scalar(key: str, cfg=None) -> str:
    raw = (settings() if cfg is None else cfg).get(key)
    if raw is not None and not isinstance(raw, (dict, list)):
        return raw.strip() if isinstance(raw, str) else str(raw).strip()
    return ""


def get_env(key: str, *, warn: bool = True, default: str = "", cfg=None) -> str:
    """配置读取唯一入口；/api 管理的 key 让 config.json 优先于 .env。"""
    if env_val := (os.environ.get(key) or "").strip():
        return env_val
    configured, dotenv = _config_scalar(key, cfg), _dotenv_values().get(key, "")
    value = (
        (configured or dotenv) if key in _API_CONFIG_KEYS else (dotenv or configured)
    )
    if not value and warn and not default:
        warning("未配置 %r，请在 .env 或 config.json 根中填写。", key)
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


def save_config(cfg: dict[str, Any] | None = None) -> None:
    cfg = settings() if cfg is None else cfg
    save_locked_json(config_file(), cfg)
    reload_config()


def update_config(change) -> None:
    """Apply a local edit to the latest document under the cross-process lock."""
    path = config_file()
    initialize_config()
    with file_lock(path):
        config = json.loads(path.read_text(encoding="utf-8"))
        change(config)
        atomic_write_json(path, config)
    reload_config()


def get_agent_usage_limits() -> "_UsageLimits":
    """单次 Agent 运行对模型请求次数上限"""
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
    if preset := raw.pop("preset", None):
        base = deepcopy(cfg["model_presets"][preset])
        raw = {**base.pop("settings", {}), **base, **raw.pop("settings", {}), **raw}
    name = str(raw.pop("name")).strip()
    raw = {**raw.pop("settings", {}), **raw}
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


def set_api(
    base_url: str | None = None,
    api_key: str | None = None,
    *,
    embedding_url: str | None = None,
    embedding_key: str | None = None,
) -> None:
    values = {
        "BASE_URL": base_url,
        "API_KEY": api_key,
        "SILICONFLOW_BASE": embedding_url,
        "SILICONFLOW_KEY": embedding_key,
    }
    if all(v is None for v in values.values()):
        return
    update_config(
        lambda cfg: cfg.update(
            {k: v.strip() for k, v in values.items() if v is not None}
        )
    )


def get_agent_roles(*, cfg=None) -> tuple[str, ...]:
    return tuple((settings() if cfg is None else cfg)["models"])


def get_context_profile_roles() -> tuple[str, ...]:
    roles = get_agent_roles()
    return tuple(role for role in roles if role in settings()["context"]) or roles


def get_context_config(role: str, *, cfg=None) -> dict[str, Any]:
    cfg = settings() if cfg is None else cfg
    raw = cfg.get("context", {})
    roles = get_agent_roles(cfg=cfg)
    if role not in roles:
        raise ValueError(f"Unknown Agent role: {role}")
    if not any(key in raw for key in roles):
        return dict(raw)  # Legacy shared context configuration.
    return {**raw.get("defaults", {}), **raw.get(role, {})}


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
        initialize_config()
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
    from redlotus.core.system import AgentSystem

    initialize_config()
    deadline = ExitDeadline(settings()["lifecycle"]["shutdown_grace_seconds"])
    system = AgentSystem(exit_deadline=deadline)
    try:
        asyncio.run(run_cli(system))
    finally:
        deadline.close()
