"""Incremental Markdown parsing, sentence boundaries, and response speech control."""
from __future__ import annotations

import asyncio
import inspect
import re
from collections import deque
from contextlib import aclosing
from typing import AsyncIterable, Awaitable, Callable

from . import PCMChunk, SpeakerCondition, SpeechBusy, SpeechError, SpeechUnavailable, StreamingSynthesizer
from redlotus.runtime.resources import finish_io

_REPLIES: set[asyncio.Task] = set()

class SpeechTextParser:
    """A bounded Markdown-to-speech cursor; each committed span is emitted once."""

    def __init__(self, limit: int = 4096):
        from markdown_it import MarkdownIt
        self._markdown = MarkdownIt("commonmark").enable("strikethrough")
        self.limit = limit
        self.clear()

    @property
    def pending_chars(self) -> int:
        return len(self._pending)

    def clear(self) -> None:
        self._pending = ""
        self._muted = 0
        self._start = True
        self._fence = ""
        self._skip_line = False
        self._comment = False
        self._previous = ""
        self._heading = False
        self._checkbox = False

    def mute_pending(self) -> None:
        self._muted = len(self._pending)

    def _take(self, count: int) -> tuple[str, bool]:
        text = self._pending[:count]
        audible = self._muted == 0
        self._pending = self._pending[count:]
        self._muted = max(0, self._muted - count)
        return text, audible

    @staticmethod
    def _closing(text: str, start: int, opening: str, closing: str) -> int:
        depth, escaped = 0, False
        for index in range(start, len(text)):
            char = text[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == opening:
                depth += 1
            elif char == closing:
                depth -= 1
                if depth == 0:
                    return index + 1
        return 0

    def _unit(self, final: bool) -> int:
        text, size = self._pending, len(self._pending)
        first = text[0]
        if first == "\\":
            return min(2, size) if size > 1 or final else 0
        if first in "![]" and (first == "[" or text.startswith("![")):
            end = self._closing(text, int(first == "!"), "[", "]")
            if not end or end == size and not final:
                return size if final else 0
            if end < size and text[end] in "([":
                closing = ")" if text[end] == "(" else "]"
                return self._closing(text, end, text[end], closing) or (size if final else 0)
            return end
        if first in "!*_~`" and size == 1 and not final:
            return 0
        if first in "*_~`":
            run = len(text) - len(text.lstrip(first))
            if run == size and not final:
                return 0
            if first == "_" and self._previous.isalnum() or first != "`" and (run == size or text[run].isspace()):
                return run
            marker = first * run
            end = re.search(r"(?<![\\" + re.escape(first) + "])" + re.escape(marker) + "(?!" + re.escape(first) + ")", text[run:])
            count = run + end.end() if end else 0
            return count if count and (count < size or final) else (size if final else 0)
        if first == "<":
            end = text.find(">")
            return end + 1 if end >= 0 else (size if final else 0)
        if first == "&":
            match = re.match(r"&(?:#x?[0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);", text)
            if match:
                return match.end()
            return 1 if final or re.search(r"[\s&<>]", text[1:]) else 0
        url = re.match(r"(?:https?://|www\.)[^\s<>]+", text, re.I)
        if url:
            return url.end() if url.end() < size or final else 0
        special = re.search(r"[!\[<\\*_~`&\n|#]|https?://|www\.", text[1:], re.I)
        end = special.start() + 1 if special else size
        tail = re.search(r"[A-Za-z:/\.]+$", text[:end])
        if not final and end == size and tail and any(prefix.startswith(tail[0].lower()) for prefix in ("https://", "http://", "www.")):
            end = tail.start()
        return end

    def _render(self, source: str) -> str:
        if re.fullmatch(r"<(?:https?://|www\.)[^<>]+>", source, re.I) or re.match(r"</?[a-z][^>]*$", source, re.I):
            return ""
        if source.startswith(("[", "![")):
            start = int(source[0] == "!")
            end = self._closing(source, start, "[", "]")
            source = source[start + 1:end - 1 if end else None]
        tokens = list(self._markdown.parseInline(source)[0].children or [])
        result = []
        while tokens:
            token = tokens.pop(0)
            if token.type == "image":
                tokens[0:0] = token.children or []
            elif token.type in {"text", "code_inline"}:
                result.append(token.content)
            elif token.type in {"softbreak", "hardbreak"}:
                result.append(" ")
        return "".join(result)

    def _drain(self, final: bool) -> str:
        output = []
        while self._pending:
            text = self._pending
            newline = text.find("\n")
            if self._comment:
                end = text.find("-->")
                self._take(end + 3 if end >= 0 else max(0, len(text) - 2))
                self._comment = end < 0
                if self._comment:
                    break
                continue
            if self._skip_line:
                self._take(newline + 1 if newline >= 0 else len(text))
                if newline < 0:
                    break
                self._skip_line, self._start = False, True
                continue
            if self._fence and self._start:
                row = text[:newline] if newline >= 0 else text
                stripped = re.sub(r"^(?: {0,3}> ?)* {0,3}", "", row)
                if stripped and (stripped[0] != self._fence[0] or re.search(r"[^" + re.escape(self._fence[0]) + r"\s]", stripped)):
                    self._skip_line = True
                    continue
                if newline < 0 and not final:
                    break
                if re.fullmatch(re.escape(self._fence[0]) + "{" + str(len(self._fence)) + r",}\s*", stripped):
                    self._fence = ""
                self._take(newline + 1 if newline >= 0 else len(text))
                continue
            if self._start:
                row = text[:newline] if newline >= 0 else text
                if not final and newline < 0 and re.fullmatch(r"[\s>#*+\-~`_\d.)|:]*", row):
                    break
                prefix = re.match(r"(?: {0,3}> ?)*", text)[0]
                plain = text[len(prefix):]
                if not final and newline < 0 and re.fullmatch(r" {0,3}\[[^\]]*\]?", plain):
                    break
                if re.match(r" {0,3}\[[^\]]+\]:", plain):
                    self._skip_line = True
                    continue
                fence = re.match(r" {0,3}(`{3,}|~{3,})", plain)
                if fence or plain.startswith(("    ", "\t")):
                    self._fence = fence[1] if fence else ""
                    self._skip_line = True
                    continue
                if row.strip() and re.fullmatch(r"[\s*_:|\-]+", row) and sum(c in "*_-" for c in row) >= 3:
                    self._skip_line = True
                    continue
                marker = re.match(r" {0,3}(?:#{1,6}\s+|[-+*]\s+|\d+[.)]\s+)", plain)
                self._heading = bool(marker and marker[0].lstrip().startswith("#"))
                self._checkbox = bool(marker and not self._heading)
                self._take(len(prefix) + (marker.end() if marker else 0))
                self._start = False
                continue
            if text.startswith("<!--"):
                self._take(4)
                self._comment = True
                continue
            if text[0] == "\n":
                _, audible = self._take(1)
                self._start, self._previous = True, ""
                if audible:
                    output.append("\n")
                continue
            if text[0] == "|":
                _, audible = self._take(1)
                if audible and self._previous:
                    output.append(", ")
                continue
            if self._heading and text[0] == "#" and self._previous.isspace():
                if newline < 0 and not final:
                    break
                if re.fullmatch(r"#+\s*", text[:newline] if newline >= 0 else text):
                    self._take(newline if newline >= 0 else len(text))
                    continue
            count = self._unit(final)
            if not count:
                break
            raw, audible = self._take(count)
            self._previous = raw[-1]
            if self._checkbox and raw in {"[ ]", "[x]", "[X]"}:
                raw = ""
            self._checkbox = self._checkbox and bool(raw) and raw.isspace()
            if re.match(r"(?:https?://|www\.)", raw, re.I):
                raw = ""
            elif final and raw[:1] in "*_~`[" and self._render(raw) == raw:
                raw = raw.lstrip("*_~`[").rstrip("]")
            if audible:
                output.append(self._render(raw))
        return "".join(output)

    def feed(self, delta: str, *, audible: bool = True) -> str:
        output = []
        produced = 0
        for offset in range(0, len(delta), 64):
            self._pending += delta[offset:offset + 64].replace("\r", "")
            if not audible:
                self.mute_pending()
            value = self._drain(False)
            if audible:
                output.append(value)
                produced += len(value)
            if self.pending_chars + produced > self.limit:
                self.clear()
                raise SpeechBusy("语音文本缓冲区已满，文字回复继续")
        return "".join(output)

    def finish(self) -> str:
        try:
            return self._drain(True)
        finally:
            self.clear()


class SpeakerTextFrontend:
    """Choose the speaker and normalize English numbers for one response."""

    @staticmethod
    def spoken_segment(text: str, condition: SpeakerCondition) -> tuple[str, int]:
        if re.search(r"[\u3400-\u9fff]", text):
            return text, condition.default_sid
        if not re.search(r"[A-Za-z]", text) and condition.previous_sid != condition.latin_sid:
            return text, condition.default_sid
        if not condition.english_number_words or not re.search(r"[0-9]", text):
            return text, condition.latin_sid
        from num2words import num2words

        def number(match):
            sign, integer, decimals, ordinal, percent = match.groups()
            integer = integer.replace(",", "")
            if len(integer) > 1 and integer.startswith("0"):
                value = " ".join(num2words(int(digit), lang="en") for digit in integer)
            else:
                value = num2words(int(integer), lang="en", to="ordinal" if ordinal else "cardinal")
            for digits in decimals.split(".")[1:]:
                value += " point " + " ".join(num2words(int(digit), lang="en") for digit in digits)
            return ({"-": "minus ", "+": "plus "}.get(sign, "") + value
                    + (" percent" if percent else ""))

        text = re.sub(r"([+-]?)([0-9]+(?:,[0-9]{3})*)((?:\.[0-9]+)*)(st\b|nd\b|rd\b|th\b)?(%?)",
                      number, text)
        return text, condition.latin_sid


class TextSegmenter:
    def __init__(self, flush_ms: int, limit: int):
        self.flush_ms = flush_ms
        self.limit = limit

    @staticmethod
    def _sentence_end(text: str, limit: int) -> int | None:
        for index, char in enumerate(text[:limit]):
            if char in "。！？!?；;\n":
                end = index + 1
            elif char == ".":
                previous = text[index - 1] if index else ""
                following = text[index + 1] if index + 1 < len(text) else ""
                if previous.isdigit() and (following.isdigit() or not following):
                    continue
                end = index + 1
            else:
                continue
            while end < len(text) and end < limit and text[end] in '”’"\')]}':
                end += 1
            return end
        return None


    @staticmethod
    def _split_at(text: str, limit: int) -> int:
        for index in range(min(limit, len(text)) - 1, 0, -1):
            if text[index].isspace() or text[index] in "，、,":
                return index + 1
        return min(limit, len(text))


    @staticmethod
    def _pop_segment(text: str, limit: int, *, force: bool) -> tuple[str, str] | None:
        text = text.lstrip()
        if not text:
            return None
        end = TextSegmenter._sentence_end(text, limit)
        if end is None:
            if force and len(text) <= limit:
                end = len(text)
            elif len(text) < limit:
                return None
            else:
                end = TextSegmenter._split_at(text, limit)
        return text[:end].strip(), text[end:].lstrip()


    async def segments(self, text: str | AsyncIterable[str], on_consumed: Callable[[int], None] | None = None):
        if isinstance(text, str):
            pending = text
            while segment := self._pop_segment(pending, self.limit, force=True):
                value, pending = segment
                if value:
                    yield value
            return

        source = aiter(text)
        pending = ""
        pending_since = None
        awaiting_boundary = False
        next_delta = None
        loop = asyncio.get_running_loop()

        def trim_pending():
            nonlocal pending
            trimmed = pending.lstrip()
            removed = len(pending) - len(trimmed)
            pending = trimmed
            if removed and on_consumed:
                on_consumed(removed)

        async def emit(force):
            nonlocal pending
            while segment := self._pop_segment(pending, self.limit, force=force):
                removed = len(pending) - len(segment[1])
                value, pending = segment
                if value:
                    yield value
                if on_consumed:
                    on_consumed(removed)

        try:
            while True:
                trim_pending()
                if not pending:
                    pending_since = None
                async for value in emit(False):
                    yield value
                if not pending:
                    pending_since = None
                if next_delta is None:
                    next_delta = asyncio.create_task(anext(source))
                remaining = (None if pending_since is None or awaiting_boundary else
                             max(0.0, pending_since + self.flush_ms / 1000 - loop.time()))
                ready, _ = await asyncio.wait({next_delta}, timeout=remaining)
                if not ready:
                    suffix = re.search(r"[A-Za-z0-9_.+-]+$", pending)
                    tail = pending[suffix.start():] if suffix else ""
                    if suffix:
                        pending = pending[:suffix.start()]
                    trim_pending()
                    async for value in emit(True):
                        yield value
                    pending += tail
                    awaiting_boundary = bool(tail)
                    if not tail:
                        pending_since = None
                    continue
                try:
                    delta = next_delta.result()
                except StopAsyncIteration:
                    trim_pending()
                    async for value in emit(True):
                        yield value
                    return
                next_delta = None
                awaiting_boundary = False
                if not isinstance(delta, str):
                    raise TypeError("TTS 文本增量必须为 str")
                if delta:
                    if not pending:
                        pending_since = loop.time()
                    pending += delta
        finally:
            if next_delta is not None and not next_delta.done():
                next_delta.cancel()
                await asyncio.gather(next_delta, return_exceptions=True)
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()


class SpeechReply:
    """Buffer bounded text deltas and synthesize them in a background task."""

    def __init__(
        self,
        consumer: Callable[[AsyncIterable[PCMChunk]], Awaitable[None]],
        *,
        service=None,
        predecessor: Awaitable[object] | None = None,
        on_error: Callable[[Exception], object] | None = None,
        is_current: Callable[[], bool] | None = None,
    ):
        if service is None:
            from .service import SpeechService
            service = SpeechService.shared()
        if len(_REPLIES) >= service.config.queue_size + 1:
            raise SpeechBusy("语音回复队列已满，文字回复继续")
        self._pending: deque[str] = deque()
        self._pending_chars = 0
        self._event = asyncio.Event()
        self._finished = False
        self._failed = False
        self._reported = False
        self._consumer = consumer
        self._service = service
        self._predecessor = predecessor
        self._on_error = on_error
        self._is_current = is_current or (lambda: True)
        self._started = False
        self.task = asyncio.create_task(self._run())
        _REPLIES.add(self.task)
        self.task.add_done_callback(_REPLIES.discard)
        self.task.add_done_callback(self._clean_unstarted)

    @property
    def pending_chars(self) -> int:
        return self._pending_chars

    @property
    def text_capacity(self) -> int:
        return self._service.config.text_chars

    def _clean_unstarted(self, _task) -> None:
        if self._started:
            return
        self._finished = True
        self._pending.clear()
        self._pending_chars = 0
        if inspect.iscoroutine(self._predecessor):
            self._predecessor.close()
            self._predecessor = None

    async def _notify_error(self, error: Exception) -> None:
        if self._reported:
            return
        self._reported = True
        if self._on_error is None:
            raise error
        notified = self._on_error(error)
        if inspect.isawaitable(notified):
            await notified

    async def feed(self, delta: str) -> None:
        if not isinstance(delta, str):
            raise TypeError("TTS 文本增量必须为 str")
        if self._failed:
            return
        if self._finished or self.task.done():
            raise RuntimeError("语音回复已结束")
        if not self._is_current():
            self.cancel()
            return
        if delta:
            if self._pending_chars + len(delta) > self._service.config.text_chars:
                self._failed = True
                self.cancel()
                await self._notify_error(SpeechBusy("语音文本缓冲区已满，文字回复继续"))
                return
            self._pending.append(delta)
            self._pending_chars += len(delta)
            self._event.set()

    async def finish(self) -> None:
        """Signal EOF without waiting for queued native synthesis or playback."""
        if not self._finished:
            self._finished = True
            self._event.set()

    def cancel(self) -> None:
        if not self._finished:
            self._finished = True
            self._event.set()
        self._pending.clear()
        self._pending_chars = 0
        if not self._started:
            self._clean_unstarted(self.task)
        self.task.cancel()

    async def _read(self):
        while True:
            if self._pending:
                if not self._is_current():
                    return
                delta = self._pending.popleft()
                yield delta
                continue
            if self._finished:
                return
            self._event.clear()
            await self._event.wait()

    def _consume_text(self, count: int) -> None:
        self._pending_chars -= count

    async def _pcm(self):
        stream = StreamingSynthesizer(self._service)._synthesize(self._read(), self._consume_text)
        try:
            async for chunk in stream:
                if not self._is_current():
                    return
                yield chunk
        finally:
            await stream.aclose()

    async def _run(self) -> None:
        self._started = True
        try:
            if self._predecessor is not None:
                try:
                    await self._predecessor
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                except Exception:
                    pass
            if not self._is_current():
                return
            stream = self._pcm()
            try:
                await self._consumer(stream)
            finally:
                await stream.aclose()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failed = True
            await self._notify_error(exc)
        finally:
            self._finished = True
            self._pending.clear()
            self._pending_chars = 0
            self._event.set()
