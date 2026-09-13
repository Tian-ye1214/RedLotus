from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from filelock import FileLock


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
