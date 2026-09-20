"""Runtime files responsibilities."""

from __future__ import annotations

import json
import os
import shutil
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from filelock import FileLock

APP_NAME = "RedLotus"


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """随包只读资源根（redlotus 包目录）。

    - 正常 / editable 安装：本文件位于 redlotus/runtime/files.py，上溯两级即包根。
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
    from redlotus.runtime import config as configuration
    # Config discovery uses user_config_dir(), so it never depends on these data paths.
    configured = configuration.settings()["storage"][name]
    return Path(configured).expanduser().resolve() if configured else default


def user_config_dir() -> Path:
    """用户配置根：config.json / .env / bot config.yaml。"""
    return Path(
        os.environ.get("REDLOTUS_CONFIG_DIR")
        or Path.home() / ".redlotus"
    ).expanduser().resolve()


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
    from redlotus.runtime.context import WorkspaceContext, current_workspace

    return WorkspaceContext.from_path(current_workspace())


def _project_storage_path(name: str, workspace=None) -> Path:
    from redlotus.runtime import config as configuration
    workspace = _storage_workspace(workspace)
    root = workspace.root.resolve()
    configured = configuration.settings()["storage"][name]
    if not configured:
        raise configuration.ConfigError(f"缺少配置 storage.{name}；检查来源: {config_source_summary()}")
    candidate = Path(configured).expanduser()
    path = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not path.is_relative_to(root):
        raise configuration.ConfigError(f"配置 storage.{name} 必须位于当前项目目录内")
    return path


def project_data_dir(workspace) -> Path:
    return _project_storage_path("project_dir", workspace)


def session_data_dir(workspace) -> Path:
    return _project_storage_path("sessions_dir", workspace)


def references_dir(workspace=None) -> Path:
    return _project_storage_path("references_dir", workspace)


def runtime_dir(workspace=None) -> Path:
    return _project_storage_path("runtime_dir", workspace)


def prompts_dir() -> Path:
    return resource_root() / "prompts"


def skills_dir() -> Path:
    """随包基线技能（只读）。"""
    return resource_root() / "tools" / "skills"


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
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
    atomic_write_bytes(Path(path), content.encode(encoding))


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
