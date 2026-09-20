"""Storage snapshots responsibilities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from redlotus.runtime.context import conversations_root
from redlotus.storage.session import SessionFile


@dataclass(frozen=True)
class WorkspaceSnapshot:
    path: Path
    meta: dict
    saved_at: datetime
    agent: str
    date: str
    topic: str
    message_count: int
    error: str = ""

    @property
    def is_loadable(self) -> bool:
        return not self.error

    @property
    def title(self) -> str:
        return self.topic.strip() if isinstance(self.topic, str) and self.topic.strip() else "未命名会话"

    @property
    def session_id(self) -> str:
        return str(self.meta.get("session_id") or self.path.parent.name)

    @property
    def input_summary(self) -> str:
        value = self.meta.get("user_input_count")
        return f"用户输入次数：{value if value is not None else '未知（记录不完整）'}"

    @property
    def status(self) -> str:
        if not self.is_loadable:
            return "损坏"
        return {
            "active": "进行中", "interrupted": "上次已中断", "completed": "已完成",
            "cancelled": "已取消", "failed": "失败", "new": "未开始",
        }.get(self.meta.get("status"), "未知")

    @property
    def local_activity_time(self) -> str:
        return self.saved_at.astimezone().strftime("%Y-%m-%d %H:%M")

    @property
    def error_summary(self) -> str:
        return " ".join(self.error.split())[:120] or "会话文件不可读取"

    @property
    def label(self):
        if not self.is_loadable:
            return (
                f"标题：无法加载 · 本地活动：{self.local_activity_time} · "
                f"状态：损坏 · 原因：{self.error_summary}"
            )
        return (
            f"标题：{self.title} · 本地活动：{self.local_activity_time} · "
            f"{self.input_summary} · 状态：{self.status} · 会话：{self.session_id}"
        )


def list_workspace_snapshots(*, root=None, include_unloadable=False):
    snapshots = []
    for entry in SessionFile.scan_info(root or conversations_root()):
        path, meta = entry.path, entry.info
        if not entry.error:
            snapshots.append(WorkspaceSnapshot(
                path, meta, datetime.fromisoformat(meta["saved_at"]), "coordinator",
                meta["saved_at"][:10], meta["title"], 0
            ))
            continue
        else:
            from redlotus.presentation.output import print_warning
            print_warning(f"会话无法加载: {path}: {entry.error}")
            if include_unloadable:
                try:
                    saved_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                except OSError:
                    saved_at = datetime.fromtimestamp(0, timezone.utc)
                snapshots.append(WorkspaceSnapshot(
                    path, {}, saved_at, "coordinator", "", path.parent.name, 0, entry.error
                ))
    return sorted(snapshots, key=lambda row: (row.saved_at, str(row.path)), reverse=True)
