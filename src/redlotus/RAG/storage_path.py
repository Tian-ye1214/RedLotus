from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from redlotus.infra import logger
from redlotus.infra.paths import APP_NAME, user_data_dir


def _fit_windows_path(path: Path, table_name: str) -> str:
    # Lance adds table, data/index directories and generated filenames. Its Windows
    # writer currently drops the extended-path prefix before committing a file.
    if (
        not table_name
        or sys.platform != "win32"
        or len(str(path)) + len(table_name) + 110 < 248
    ):
        return str(path)
    digest = hashlib.sha256(str(path).casefold().encode()).hexdigest()[:16]
    local_root = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    fallback = local_root / APP_NAME / "rag_lancedb" / digest
    logger.info(
        "LanceDB: 路径过深，向量索引改存 %s；原始记忆与配置保持原位。", fallback
    )
    return str(fallback)


def resolve_lancedb_dir(configured_path: str, *, table_name: str = "") -> str:
    """Use the configured database path, with an explicit process override for tests."""
    p = Path(os.environ.get("RAG_DB_PATH") or configured_path).expanduser()
    if not p.is_absolute():
        p = (user_data_dir() / p).resolve()
    else:
        p = p.resolve()

    return _fit_windows_path(p, table_name)
