from __future__ import annotations

import sys
import os
from pathlib import Path

import platformdirs

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
    """全局可写状态根：日志 / 向量库 / 长期记忆 / 运行时技能 overlay。"""
    return Path(
        os.environ.get("REDLOTUS_DATA_DIR")
        or platformdirs.user_data_dir(APP_NAME, appauthor=False)
    )


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
    return resource_root() / "config.default.json"


def dotenv_file() -> Path:
    if path := os.environ.get("REDLOTUS_DOTENV_FILE"):
        return Path(path).resolve()
    return user_config_dir() / ".env"


def project_data_dir(workspace) -> Path:
    """Project identity is independent of the interpreter or installation directory."""
    return user_data_dir() / "projects" / workspace.project_id


def migrate_project_data(workspace) -> None:
    """Copy legacy state once, preserving both the source and any newer destination."""
    import shutil
    from redlotus.infra.persist_utils import atomic_write_json, file_lock

    source = workspace.root / ".redlotus"
    target = project_data_dir(workspace)
    marker = target / "legacy_import.json"
    if not source.is_dir() or marker.exists():
        return
    with file_lock(marker):
        if marker.exists():
            return
        copied = []
        for path in source.rglob("*"):
            if not path.is_file() or path.suffix in (".lock", ".tmp"):
                continue
            relative = path.relative_to(source)
            destination = target / relative
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix(destination.suffix + ".tmp")
                with file_lock(path):
                    shutil.copy2(path, temporary)
                os.replace(temporary, destination)
                copied.append(str(relative))
        atomic_write_json(marker, {"source": str(source), "files": copied})


# ---- 只读随包资源 ----
def prompts_dir() -> Path:
    return resource_root() / "prompts"


def skills_dir() -> Path:
    """随包基线技能（只读）。"""
    return resource_root() / "skills"


# ---- 全局可写状态 ----
def logs_dir() -> Path:
    return user_data_dir() / "logs"


def memory_dir() -> Path:
    """个人全局 MEMORY.md 及旧 SOUL/USER 文档的迁移备份目录。"""
    return user_data_dir() / "LongTermMemory"


def user_skills_dir() -> Path:
    """运行时安装的技能 overlay（可写）；与随包基线技能合并加载。"""
    return user_data_dir() / "skills"
