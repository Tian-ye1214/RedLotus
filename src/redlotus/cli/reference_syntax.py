"""Shared reference spans for submission parsing and cursor-prefix completion."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

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
