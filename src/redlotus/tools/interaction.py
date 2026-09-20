"""User input references and file review decisions."""

from __future__ import annotations

import re
import unicodedata
import asyncio
import os
import difflib
import threading
from dataclasses import dataclass, field
from redlotus.tools.references import ReferenceFile, ReferenceStore
from redlotus.prompts.prompt import with_runtime_context
from collections.abc import Iterator
from pathlib import Path
from redlotus.core.gateway import ModelInputPolicy
from redlotus.core.agents import WorkspaceContext
from redlotus.core.session import current_workspace
from typing import Callable


@dataclass
class UserMessage:
    """User prompt text plus optional pydantic-AI multimodal content."""

    text: str
    attachments: list = field(default_factory=list)
    original_text: str | None = None
    references: list[ReferenceFile] = field(default_factory=list)

    def to_prompt(self):
        """Pass original requirements and explicitly labelled reference data together."""
        parts = [self.text]
        for reference in self.references:
            parts.extend(reference.to_prompt())
        parts.extend(self.attachments)
        return with_runtime_context(parts)


def user_message_from_cli_input(raw_input: str) -> UserMessage:
    """Reference resolution happens once in the input controller, never in file contents."""
    return UserMessage(text=raw_input, original_text=raw_input)


# A period can end the preceding sentence; normal email local parts cannot end in one.
_AT_START = re.compile(r"(?<![A-Za-z0-9_%+-])@")
_WORD = re.compile(r"\S*")
_CLOSERS = {'"': '"', "'": "'", "{": "}"}


@dataclass(frozen=True)
class ReferenceSpan:
    start: int
    end: int
    value: str
    opener: str = ""
    closed: bool = True
    alternatives: tuple[str, ...] = ()


def resolve_ref_path(value: str, root: Path) -> Path:
    return (root / Path(value).expanduser()).resolve()


def _existing_path(value: str, root: Path, *, file_only=False) -> bool:
    if not value:
        return False
    try:
        path = resolve_ref_path(value, root)
        return path.is_file() if file_only else path.exists()
    except (OSError, RuntimeError, ValueError):
        return False


def _punctuation(text: str) -> bool:
    return bool(text) and all(
        unicodedata.category(char).startswith("P") for char in text
    )


def _inline_suffix(suffix: str) -> bool:
    if suffix[0] == ".":
        return _punctuation(suffix)  # A sentence ending, never a missing .backup file.
    return suffix[0] not in "-_/\\" and bool(
        re.match(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", suffix)
        or unicodedata.category(suffix[0]).startswith("P")
    )


def _existing_prefix(value: str, root: Path) -> str | None:
    if _existing_path(value, root):
        return value
    for length in range(len(value) - 1, 0, -1):
        if _inline_suffix(value[length:]) and _existing_path(
            value[:length], root, file_only=True
        ):
            return value[:length]
    return None


def _spaced_prefix(text: str, start: int, word_end: int, root: Path) -> str | None:
    """Extend an unquoted path across spaces only when a real file matches."""
    tail = text[word_end:].split("\n", 1)[0].split("\r", 1)[0]
    for match in re.finditer(r"\s+\S+", tail):
        end = word_end + match.end()
        candidate = _existing_prefix(text[start:end], root)
        if candidate is not None and len(candidate.rstrip()) > word_end - start:
            return candidate.rstrip()
        if "@" in match.group():
            break
    return None


def iter_reference_spans(text: str, *, root: Path) -> Iterator[ReferenceSpan]:
    cursor, adjacent = 0, False
    while cursor < len(text):
        match = _AT_START.search(text, cursor)
        if adjacent and text[cursor] == "@":
            start = cursor
        elif match is not None:
            start = match.start()
        else:
            return
        value_start = start + 1
        opener = text[value_start : value_start + 1]
        if opener in _CLOSERS:
            close = text.find(_CLOSERS[opener], value_start + 1)
            end = len(text) if close == -1 else close + 1
            yield ReferenceSpan(
                start,
                end,
                text[value_start + 1 : end if close == -1 else close],
                opener,
                close != -1,
            )
            cursor, adjacent = end, True
            continue

        word_end = _WORD.match(text, value_start).end()
        value = text[value_start:word_end]
        prefix = _existing_prefix(value, root)
        spaced = _spaced_prefix(text, value_start, word_end, root)
        alternatives = ()
        if spaced and spaced != prefix:
            if prefix and _existing_path(prefix, root, file_only=True):
                alternatives = (prefix, spaced)
            prefix = spaced
        if prefix is not None:
            # A filename can contain @; prefer the longest existing path before
            # interpreting any remaining markers as separate references.
            consumed = value_start + len(prefix)
            yield ReferenceSpan(start, consumed, prefix, alternatives=alternatives)
            cursor, adjacent = consumed, True
            continue

        separator = re.search(r"[@，。、；！？,;]", value)
        end = word_end if separator is None else value_start + separator.start()
        yield ReferenceSpan(start, end, text[value_start:end])
        cursor, adjacent = end, True


def quote_reference_path(value: str, *, opener: str = "", directory=False) -> str:
    ambiguous = any(char.isspace() or char in "@{}\"',;，；" for char in value)
    if not opener and not ambiguous:
        return value
    for quote in dict.fromkeys([opener, '"', "'", "{"]):
        if quote and _CLOSERS[quote] not in value:
            return quote + value + ("" if directory else _CLOSERS[quote])
    raise ValueError("文件名包含无法用引号或花括号包裹的分隔符。")


def parse_file_paths(text: str, *, root: Path | None = None) -> list[Path]:
    root = root or current_workspace()
    candidates = []
    remaining = list(text)
    for reference in iter_reference_spans(text, root=root):
        if reference.alternatives:
            raise ValueError(
                "引用路径存在歧义，请用引号或花括号指定："
                + "、".join(reference.alternatives)
            )
        if not reference.closed:
            raise ValueError(f"引用路径未闭合：{text[reference.start :]}")
        if value := reference.value.strip():
            if value.lower().startswith(("http://", "https://")):
                raise ValueError(f"引用仅支持本地文件：{value}")
            candidates.append((reference.start, resolve_ref_path(value, root)))
        remaining[reference.start : reference.end] = " " * (
            reference.end - reference.start
        )
    media = {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".bmp",
        ".mp4",
        ".mov",
        ".mkv",
        ".avi",
        ".webm",
    }
    for match in re.finditer(r'"([^"\n]+)"|\'([^\'\n]+)\'|(\S+)', "".join(remaining)):
        value = next(v for v in match.groups() if v is not None)
        if Path(value).suffix.lower() in media:
            path = resolve_ref_path(value, root)
            if path.is_file():
                candidates.append((match.start(), path))
    unique = {}
    for _, path in sorted(candidates, key=lambda item: item[0]):
        unique.setdefault(os.path.normcase(str(path)), path)
    return list(unique.values())


async def load_file_refs(
    text: str, *, role: str = "coordinator", workspace: WorkspaceContext | None = None
) -> list[ReferenceFile]:
    workspace = workspace or WorkspaceContext.from_path(current_workspace())
    paths = parse_file_paths(text, root=workspace.root)
    policy = ModelInputPolicy.for_role(role)
    if len(paths) > policy.max_files:
        raise ValueError(
            f"最多引用 {policy.max_files} 个文件，本次引用 {len(paths)} 个。"
        )
    errors, sizes = [], []
    for path in paths:
        if not path.is_file():
            errors.append(f"{path}: 文件不存在或不是普通文件")
            continue
        size = path.stat().st_size
        sizes.append(size)
        if size > policy.max_file_bytes:
            errors.append(
                f"{path}: {size:,} 字节，超过单文件限额 {policy.max_file_bytes:,} 字节"
            )
    if errors:
        raise ValueError("引用文件失败：\n" + "\n".join(errors))
    policy.check(sizes)
    store = ReferenceStore(workspace)
    captured = await asyncio.gather(
        *(store.capture_file(path, policy=policy) for path in paths)
    )
    slots = asyncio.Semaphore(4)

    async def read(reference):
        async with slots:
            try:
                return await store.parse(reference)
            except Exception as exc:
                raise ValueError(f"引用文件 {reference.name} 解析失败：{exc}") from exc

    return list(await asyncio.gather(*(read(reference) for reference in captured)))


def _opcodes(baseline: str, current: str):
    a = baseline.splitlines(keepends=True)
    b = current.splitlines(keepends=True)
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
            content = update(previous)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
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

    def decide(self, entry: ReviewEntry, index: int, reject: bool) -> bool:
        """Apply a decision only to the exact version displayed by the UI."""
        with self._lock:
            if self._entries.get(str(entry.path)) is not entry:
                return False
            previous_rejections = {
                key for key, value in entry.decisions.items() if value
            }
            expected = reconstruct(entry.baseline, entry.snapshot, previous_rejections)
            current = (
                entry.path.read_text(encoding="utf-8") if entry.path.exists() else ""
            )
            if current != expected:
                raise ValueError(
                    "文件已在审查界面之外被修改；为保留这些改动，本次决定未应用。"
                )
            decisions = {**entry.decisions, index: reject}
            rejected = {key for key, value in decisions.items() if value}
            if not entry.existed and len(rejected) == len(entry.hunks):
                entry.path.unlink(missing_ok=True)
            else:
                entry.path.write_text(
                    reconstruct(entry.baseline, entry.snapshot, rejected),
                    encoding="utf-8",
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
