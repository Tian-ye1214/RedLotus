"""Conflict-checked file review, reversible hunks, and shared line-diff calculation."""

from __future__ import annotations

import difflib
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Callable

from redlotus.runtime.files import atomic_write_text


def _opcodes(baseline: str, current: str, *, keepends=True):
    a = baseline.splitlines(keepends=keepends)
    b = current.splitlines(keepends=keepends)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    return a, b, sm.get_opcodes()


@dataclass(frozen=True)
class Hunk:
    """一处连续改动（baseline→current 中的一个非 equal 区块）。"""

    index: int
    old_start: int
    old_lines: list[str]
    new_start: int
    new_lines: list[str]

    @property
    def location(self) -> str:
        return f"L{self.new_start}" if self.new_lines else f"L{self.old_start}"


def compute_hunks(baseline: str, current: str) -> list[Hunk]:
    """把 baseline→current 的差异切成逐块 Hunk 列表（equal 区块跳过）。"""
    a, b, ops = _opcodes(baseline, current)
    hunks: list[Hunk] = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        hunks.append(Hunk(len(hunks), i1 + 1, a[i1:i2], j1 + 1, b[j1:j2]))
    return hunks


def reconstruct(baseline: str, current: str, rejected: set[int]) -> str:
    """按逐块决定重建文件内容：rejected 的块取 baseline 侧，其余取 current 侧。

    rejected 为空 → 完全等于 current；rejected 含全部块 → 完全等于 baseline。
    """
    a, b, ops = _opcodes(baseline, current)
    out: list[str] = []
    idx = 0
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            out.extend(b[j1:j2])
            continue
        out.extend(a[i1:i2] if idx in rejected else b[j1:j2])
        idx += 1
    return "".join(out)


@dataclass
class ReviewEntry:
    path: Path
    name: str
    baseline: str
    snapshot: str
    decisions: dict[int, bool] = field(default_factory=dict)
    existed: bool = True

    @property
    def hunks(self):
        return compute_hunks(self.baseline, self.snapshot)


class PendingReviewStore:
    """跨线程共享的待审查暂存区。复用 toolkit 的 file_lock，避免与 agent 写盘竞争。"""

    def __init__(self, file_lock: threading.Lock) -> None:
        self._lock = file_lock
        self._entries: dict[str, ReviewEntry] = {}
        self._on_change: Callable[[], None] | None = None

    def activate(self, on_change: Callable[[], None]) -> None:
        with self._lock:
            self._on_change = on_change

    def deactivate(self) -> None:
        with self._lock:
            self._on_change = None
            self._entries.clear()

    def clear(self) -> None:
        """Discard the previous project's reviews while retaining the UI subscription."""
        with self._lock:
            self._entries.clear()
            cb = self._on_change
        self._notify(cb)

    def _register_locked(self, path, name, baseline, snapshot):
        if baseline == snapshot or self._on_change is None:
            return
        old = self._entries.get(str(path))
        self._entries[str(path)] = ReviewEntry(
            path,
            name,
            old.baseline if old else baseline or "",
            snapshot,
            existed=old.existed if old else baseline is not None,
        )

    def register(self, path: Path, *, name: str, baseline: str, snapshot: str) -> None:
        with self._lock:
            self._register_locked(path, name, baseline, snapshot)
            callback = self._on_change
        self._notify(callback)

    def write(self, path: Path, name: str, update):
        """Publish file contents and their review snapshot as one locked operation."""
        with self._lock:
            previous = path.read_text(encoding="utf-8") if path.exists() else None
            if entry := self._entries.get(str(path)):
                self._check_current(entry, previous)
            content = update(previous)
            atomic_write_text(path, content)
            self._register_locked(path, name, previous, content)
            callback = self._on_change
        self._notify(callback)
        return previous or "", content

    def entries(self) -> list[ReviewEntry]:
        with self._lock:
            return list(self._entries.values())

    def get(self, key: str) -> ReviewEntry | None:
        with self._lock:
            return self._entries.get(key)

    @staticmethod
    def _check_current(entry, current):
        rejected = {key for key, value in entry.decisions.items() if value}
        expected = None if not entry.existed and rejected and len(rejected) == len(entry.hunks) else reconstruct(entry.baseline, entry.snapshot, rejected)
        if current != expected:
            raise ValueError("文件已在审查界面之外被修改；请完成当前审查后重试，实际文件已保留。")

    def decide(self, entry: ReviewEntry, index: int, reject: bool) -> bool:
        """Apply a decision only to the exact version displayed by the UI."""
        with self._lock:
            if self._entries.get(str(entry.path)) is not entry:
                return False
            self._check_current(entry, entry.path.read_text(encoding="utf-8") if entry.path.exists() else None)
            decisions = {**entry.decisions, index: reject}
            rejected = {key for key, value in decisions.items() if value}
            if not entry.existed and len(rejected) == len(entry.hunks):
                entry.path.unlink(missing_ok=True)
            else:
                atomic_write_text(entry.path,
                    reconstruct(entry.baseline, entry.snapshot, rejected),
                )
            entry.decisions = decisions
        return True

    def finish_decided(self):
        for entry in self.entries():
            if all(hunk.index in entry.decisions for hunk in entry.hunks):
                self.finish(str(entry.path))

    def finish(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)
            cb = self._on_change
        self._notify(cb)

    def _notify(self, cb: Callable[[], None] | None) -> None:
        if cb is not None:
            cb()


class DiffKind(StrEnum):
    ADD = "add"
    DEL = "del"
    MOD = "mod"
    CTX = "ctx"
    GAP = "gap"


@dataclass(frozen=True)
class DiffLine:
    kind: DiffKind
    old_no: int | None
    new_no: int | None
    text: str


def compute_line_diff(old: str, new: str, *, context: int = 3) -> list[DiffLine]:
    """按行对比 old→new，返回带类别和行号的 DiffLine 列表；长未改段折叠为 gap。"""
    old_lines, new_lines, ops = _opcodes(old, new, keepends=False)
    out: list[DiffLine] = []
    for idx, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag == "equal":
            out += _equal_block(old_lines, i1, i2, j1, context=context, first=idx == 0, last=idx == len(ops) - 1)
        elif tag == "insert":
            out += [DiffLine(DiffKind.ADD, None, j1 + k + 1, line) for k, line in enumerate(new_lines[j1:j2])]
        elif tag == "delete":
            out += [DiffLine(DiffKind.DEL, i1 + k + 1, None, line) for k, line in enumerate(old_lines[i1:i2])]
        elif tag == "replace":
            out += [DiffLine(DiffKind.MOD, i1 + k + 1, None, line) for k, line in enumerate(old_lines[i1:i2])]
            out += [DiffLine(DiffKind.MOD, None, j1 + k + 1, line) for k, line in enumerate(new_lines[j1:j2])]
    return out


def _equal_block(old_lines, i1, i2, j1, *, context, first, last) -> list[DiffLine]:
    n = i2 - i1

    def ctx(off: int) -> DiffLine:
        return DiffLine(DiffKind.CTX, i1 + off + 1, j1 + off + 1, old_lines[i1 + off])

    top = 0 if first else context
    bot = 0 if last else context
    if top + bot >= n:
        return [ctx(k) for k in range(n)]
    hidden = n - top - bot
    return (
        [ctx(k) for k in range(top)]
        + [DiffLine(DiffKind.GAP, None, None, f"⋯ {hidden} 行未改动 ⋯")]
        + [ctx(k) for k in range(n - bot, n)]
    )


def diff_stats(lines: list[DiffLine]) -> tuple[int, int, int]:
    """统计 (新增, 删除, 改动) 行数；改动按新侧计。"""
    add = sum(1 for ln in lines if ln.kind is DiffKind.ADD)
    dele = sum(1 for ln in lines if ln.kind is DiffKind.DEL)
    mod = sum(1 for ln in lines if ln.kind is DiffKind.MOD and ln.new_no is not None)
    return add, dele, mod


def _gutter_width(lines: list[DiffLine]) -> int:
    nums = [n for ln in lines for n in (ln.old_no, ln.new_no) if n is not None]
    return max((len(str(n)) for n in nums), default=1)


def _line_text(ln: DiffLine, width: int, signs: dict[DiffKind, str]) -> str:
    sign = signs[ln.kind]
    if ln.kind is DiffKind.GAP:
        return f"{sign} {'':>{width}}  {ln.text}"
    no = ln.new_no if ln.new_no is not None else ln.old_no
    return f"{sign} {no:>{width}}  {ln.text}"


def format_diff_text(
    lines: list[DiffLine], *, path: str, stats: tuple[int, int, int], signs=None
) -> str:
    """无样式纯文本版（写日志 / bot 用）。"""
    signs = signs or {DiffKind.ADD: "+", DiffKind.DEL: "-", DiffKind.MOD: "~", DiffKind.CTX: " ", DiffKind.GAP: " "}
    add, dele, mod = stats
    width = _gutter_width(lines)
    return "\n".join([f"{path}  +{add} -{dele} ~{mod}", *(_line_text(ln, width, signs) for ln in lines)])
