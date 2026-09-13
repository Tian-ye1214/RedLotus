from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from redlotus.infra.persist_utils import file_lock, safe_segment

MODEL_MESSAGES_SUFFIX = "_ModelMessages.json"
MODEL_MESSAGES_GLOB = f"*{MODEL_MESSAGES_SUFFIX}"
LOADABLE_ROLES = frozenset({"coordinator", "manager"})

_workspace: Path | None = None


def current_workspace() -> Path:
    from redlotus.runtime.context import active_workspace

    active = active_workspace()
    if active is not None:
        return active.root
    if _workspace is not None:
        return _workspace
    return Path.cwd().resolve()


def set_workspace(path: Path | str) -> Path:
    global _workspace
    resolved = Path(path).expanduser().resolve()
    _workspace = resolved
    return resolved


def conversations_root() -> Path:
    from redlotus.infra.paths import project_data_dir
    from redlotus.runtime.context import WorkspaceContext

    return project_data_dir(WorkspaceContext.from_path(current_workspace()))


def snapshot_basename(
    role: str,
    date: str,
    topic: str,
    *,
    sub_id: str | None = None,
) -> str:
    # Full titles belong in metadata; short filenames also work in deep Windows projects.
    identity = hashlib.sha256(
        f"{role}\0{date}\0{topic}\0{sub_id or ''}".encode()
    ).hexdigest()[:16]
    return f"{safe_segment(role, 12)}_{safe_segment(date, 8)}_{identity}"


def snapshot_base_from_loadable(path: Path) -> Path:
    name = Path(path).name
    if not name.endswith(MODEL_MESSAGES_SUFFIX):
        raise ValueError(f"not a loadable snapshot: {path}")
    stem = name[: -len(MODEL_MESSAGES_SUFFIX)]
    return Path(path).parent / stem


def _parse_saved_at(value: Any, *, fallback_path: Path) -> datetime:
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            pass
    try:
        ts = fallback_path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except OSError:
        return datetime.min.replace(tzinfo=timezone.utc)


def read_snapshot_meta(path: Path) -> dict[str, Any]:
    path = Path(path)
    with file_lock(path):
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    meta_raw = data.get("meta")
    meta: dict[str, Any] = dict(meta_raw) if isinstance(meta_raw, dict) else {}
    if data.get("saved_at") is not None:
        meta["saved_at"] = data.get("saved_at")
    raw_messages = data.get("model_messages")
    if isinstance(raw_messages, list):
        meta["message_count"] = len(raw_messages)
    return meta


@dataclass(frozen=True)
class WorkspaceSnapshot:
    path: Path
    meta: dict[str, Any]
    saved_at: datetime
    agent: str
    date: str
    topic: str
    message_count: int

    @property
    def label(self) -> str:
        saved = self.saved_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        session_id = str(self.meta.get("session_id") or self.meta.get("sub_id") or "")
        sid = f" #{session_id}" if session_id else ""
        return (
            f"{saved} | {self.agent} | {self.date}/{self.topic}{sid} "
            f"| {self.message_count} msgs"
        )


def _snapshot_from_path(path: Path) -> WorkspaceSnapshot | None:
    path = Path(path)
    if not path.is_file() or not path.name.endswith(MODEL_MESSAGES_SUFFIX):
        return None
    try:
        meta = read_snapshot_meta(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    agent = str(meta.get("agent") or "").strip().lower()
    if agent not in LOADABLE_ROLES:
        return None
    saved_at = _parse_saved_at(meta.get("saved_at"), fallback_path=path)
    return WorkspaceSnapshot(
        path=path,
        meta=meta,
        saved_at=saved_at,
        agent=agent,
        date=str(meta.get("date") or ""),
        topic=str(meta.get("topic") or ""),
        message_count=int(meta.get("message_count") or 0),
    )


def list_workspace_snapshots(*, root: Path | None = None) -> list[WorkspaceSnapshot]:
    conv_root = root or conversations_root()
    if not conv_root.is_dir():
        return []
    snapshots: list[WorkspaceSnapshot] = []
    for fp in conv_root.glob(MODEL_MESSAGES_GLOB):
        item = _snapshot_from_path(fp)
        if item is not None:
            snapshots.append(item)
    snapshots.sort(key=lambda s: (s.saved_at, str(s.path)), reverse=True)
    return snapshots
