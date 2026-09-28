"""Incremental sentence buffering and bounded Kokoro synthesis."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import aclosing
import inspect
import hashlib
import os
import queue
import re
from pathlib import Path
import threading
from typing import AsyncIterable, Awaitable, Callable

import numpy as np

from . import FloatSamples, ModelKind, PCMChunk, SpeechBusy, SpeechError, SpeechUnavailable, TTSModel
from ..runtime.resources import finish_io


_SAMPLE_RATE = 24000
_CHINESE_SPEAKER = 3  # zf_001 in the official Kokoro v1.1 speaker list.
_ENGLISH_SPEAKER = 0  # af_maple; shares the same Kokoro model and voices.bin.
_REPLIES: set[asyncio.Task] = set()


class KokoroModel(TTSModel):
    """The installed bilingual Kokoro 82M v1.1 model."""

    _phonemizer_source: tuple[str, Path] | None = None
    _phonemizer_lock = threading.Lock()

    def __init__(self, native):
        self._native = native

    @classmethod
    def retained_root(cls) -> Path | None:
        return cls._phonemizer_source[1] if cls._phonemizer_source is not None else None

    @property
    def sample_rate(self) -> int:
        return self._native.sample_rate

    @staticmethod
    def _ascii_absolute(path: Path) -> str | None:
        spelling = str(path.resolve())
        if spelling.isascii():
            return spelling
        if os.name != "nt":
            return None
        import ctypes

        get_short = ctypes.windll.kernel32.GetShortPathNameW
        get_short.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
        get_short.restype = ctypes.c_uint
        size = get_short(spelling, None, 0)
        if size:
            buffer = ctypes.create_unicode_buffer(size)
            used = get_short(spelling, buffer, size)
            if 0 < used < size and buffer.value.isascii():
                return buffer.value
        return None

    @classmethod
    def load(cls, root: Path, threads: int) -> KokoroModel:
        with cls._phonemizer_lock:
            return cls._load_locked(root, threads)

    @classmethod
    def _load_locked(cls, root: Path, threads: int) -> KokoroModel:
        model_name = "model.onnx" if (root / "model.onnx").is_file() else "model.int8.onnx"
        files = (
            model_name, "voices.bin", "tokens.txt", "lexicon-us-en.txt", "lexicon-zh.txt",
            "phone-zh.fst", "date-zh.fst", "number-zh.fst",
        )
        missing = [name for name in files if not (root / name).is_file()]
        if not (root / "espeak-ng-data").is_dir():
            missing.append("espeak-ng-data")
        if missing:
            raise SpeechUnavailable(f"Kokoro 资源缺失: {', '.join(missing)}")
        try:
            import sherpa_onnx
            import num2words  # Validate the English normalizer before model construction.
        except ImportError as exc:
            raise SpeechUnavailable("请安装 RedLotus[speech] 以使用本地语音合成") from exc

        native = {name: cls._ascii_absolute(root / name) for name in (*files, "espeak-ng-data")}
        if any(value is None for value in native.values()):
            raise SpeechUnavailable("Kokoro 资源需要英文绝对路径；请将 model_dir 设为英文路径并重启应用")

        # espeak-ng retains its first dictionary path in process-global state.
        # Installed resources therefore remain pinned until process exit.
        dictionary = ("lexicon-us-en.txt", "lexicon-zh.txt", "tokens.txt",
                      "phone-zh.fst", "date-zh.fst", "number-zh.fst")
        digest = hashlib.sha256()
        sources = [*(root / name for name in dictionary), *(root / "espeak-ng-data").rglob("*")]
        for source in sorted(sources, key=lambda path: path.relative_to(root).as_posix()):
            if not source.is_file():
                continue
            digest.update(source.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            with source.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        fingerprint = digest.hexdigest()
        pinned = cls._phonemizer_source
        if pinned is not None and (fingerprint, root.resolve()) != pinned:
            raise SpeechUnavailable("Kokoro 词典已变更；请重启应用后更新模型")

        kokoro = sherpa_onnx.OfflineTtsKokoroModelConfig(
            model=native[model_name],
            voices=native["voices.bin"],
            tokens=native["tokens.txt"],
            data_dir=native["espeak-ng-data"],
            lexicon=",".join(native[name] for name in ("lexicon-us-en.txt", "lexicon-zh.txt")),
        )
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(kokoro=kokoro, num_threads=threads, provider="cpu"),
            max_num_sentences=1,
            rule_fsts=",".join(native[name] for name in ("phone-zh.fst", "date-zh.fst", "number-zh.fst")),
        )
        if not config.validate():
            raise SpeechUnavailable("Kokoro 模型配置无效")
        cls._phonemizer_source = (fingerprint, root.resolve())
        return cls(sherpa_onnx.OfflineTts(config))

    def warmup(self) -> None:
        for text in ("你好。", "Hello, version 3.14 is ready."):
            spoken, speaker = TextSegmenter.spoken_segment(text)
            warm = self.generate(spoken, speaker)
            if warm.sample_rate != _SAMPLE_RATE or not len(warm.samples):
                raise SpeechUnavailable("Kokoro 预热未产生 24 kHz 音频")

    def generate(self, text: str, speaker: int,
                 callback: Callable[[FloatSamples, float], int] | None = None) -> PCMChunk:
        audio = self._native.generate(text, sid=speaker, speed=1.0, callback=callback)
        return PCMChunk(np.asarray(audio.samples, dtype=np.float32), audio.sample_rate)

    def close(self) -> None:
        self._native = None


class TextSegmenter:
    def __init__(self, flush_ms: int, limit: int):
        self.flush_ms = flush_ms
        self.limit = limit

    @staticmethod
    def spoken_segment(text: str, previous_speaker: int = _CHINESE_SPEAKER) -> tuple[str, int]:
        """Keep English numbers out of the native Chinese FST normalizers."""
        if re.search(r"[\u3400-\u9fff]", text):
            return text, _CHINESE_SPEAKER
        if not re.search(r"[A-Za-z]", text) and previous_speaker != _ENGLISH_SPEAKER:
            return text, _CHINESE_SPEAKER
        if not re.search(r"[0-9]", text):
            return text, _ENGLISH_SPEAKER
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
        return text, _ENGLISH_SPEAKER


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

        try:
            while True:
                trim_pending()
                if not pending:
                    pending_since = None
                while segment := self._pop_segment(pending, self.limit, force=False):
                    removed = len(pending) - len(segment[1])
                    value, pending = segment
                    if value:
                        yield value
                    if on_consumed:
                        on_consumed(removed)
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
                    while segment := self._pop_segment(pending, self.limit, force=True):
                        removed = len(pending) - len(segment[1])
                        value, pending = segment
                        if value:
                            yield value
                        if on_consumed:
                            on_consumed(removed)
                    pending += tail
                    awaiting_boundary = bool(tail)
                    if not tail:
                        pending_since = None
                    continue
                try:
                    delta = next_delta.result()
                except StopAsyncIteration:
                    trim_pending()
                    while segment := self._pop_segment(pending, self.limit, force=True):
                        removed = len(pending) - len(segment[1])
                        value, pending = segment
                        if value:
                            yield value
                        if on_consumed:
                            on_consumed(removed)
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


class _SegmentStream:
    def __init__(self, service):
        self.service = service
        self.window = max(1, min(2400, int(service.config.pcm_seconds * _SAMPLE_RATE / 2)))
        capacity = max(1, int(service.config.pcm_seconds * _SAMPLE_RATE) // self.window - 1)
        self.pending_pcm: queue.Queue[PCMChunk] = queue.Queue(maxsize=capacity)
        self.stopped = threading.Event()
        self.ready = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        self.speaker = _CHINESE_SPEAKER

    def _generate(self, model: TTSModel, segment: str) -> None:
        if model.sample_rate != _SAMPLE_RATE:
            raise SpeechError(f"Kokoro 输出采样率错误: {model.sample_rate}")
        received = False
        previous = None

        def put(samples, final=False):
            while not self.stopped.is_set():
                try:
                    self.pending_pcm.put(PCMChunk(samples, _SAMPLE_RATE, final), timeout=.05)
                    self.loop.call_soon_threadsafe(self.ready.set)
                    return True
                except queue.Full:
                    continue
            return False

        def callback(samples, _progress):
            nonlocal received, previous
            samples = np.asarray(samples, dtype=np.float32)
            if samples.ndim != 1 or not np.all(np.isfinite(samples)):
                raise SpeechError("Kokoro 未产生有效单声道音频")
            received |= bool(len(samples))
            for start in range(0, len(samples), self.window):
                if self.stopped.is_set() or previous is not None and not put(previous):
                    return 0
                previous = samples[start:start + self.window].copy()
            return 1  # sherpa-onnx: 1 continues; 0 stops native generation.

        if self.stopped.is_set():
            return
        spoken, self.speaker = TextSegmenter.spoken_segment(segment, self.speaker)
        audio = model.generate(spoken, self.speaker, callback=callback)
        if not self.stopped.is_set() and (not received or audio.sample_rate != _SAMPLE_RATE):
            raise SpeechError("Kokoro 未产生有效的 24 kHz 音频")
        if previous is not None:
            put(previous, True)

    async def _produce(self, segments):
        async for segment in segments:
            async with self.service.acquire(ModelKind.TTS) as model:
                try:
                    await self.service.run(ModelKind.TTS, self._generate, model, segment)
                finally:
                    del model

    async def stream(self, segments) -> AsyncIterator[PCMChunk]:
        worker = asyncio.create_task(self._produce(segments))
        worker.add_done_callback(lambda _: self.ready.set())
        try:
            while True:
                try:
                    block = self.pending_pcm.get_nowait()
                except queue.Empty:
                    if worker.done():
                        break
                    self.ready.clear()
                    if self.pending_pcm.empty() and not worker.done():
                        await self.ready.wait()
                    continue
                yield block
            await worker
        finally:
            self.stopped.set()
            if not worker.done():
                worker.cancel()
            try:
                await finish_io(asyncio.wait({worker}))
            finally:
                if worker.done() and not worker.cancelled():
                    worker.exception()


class StreamingSynthesizer:
    def __init__(self, service=None):
        if service is None:
            from .service import SpeechService
            service = SpeechService.shared()
        self._service = service

    async def synthesize(self, text: str | AsyncIterable[str]) -> AsyncIterator[PCMChunk]:
        """Synthesize each new segment once and emit bounded PCM windows."""
        async with aclosing(self._synthesize(text)) as stream:
            async for chunk in stream:
                yield chunk

    async def _synthesize(self, text, on_consumed=None):
        config = self._service.config
        segments = TextSegmenter(config.flush_ms, config.segment_chars).segments(text, on_consumed)
        try:
            async with aclosing(_SegmentStream(self._service).stream(segments)) as stream:
                async for chunk in stream:
                    yield chunk
        finally:
            await segments.aclose()


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
