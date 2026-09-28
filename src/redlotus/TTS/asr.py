"""Streaming X-ASR inference over the package's standard PCM stream."""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from contextlib import aclosing
from pathlib import Path
from typing import AsyncIterable, AsyncIterator, Literal

import numpy as np

from redlotus.runtime.resources import finish_io

from . import (ASRModel, FloatSamples, InputDevice, ModelKind, NoSpeechDetected,
               PCMChunk, RecognitionSession, SpeechBusy, SpeechError, SpeechUnavailable, Transcript)


_SAMPLE_RATE = 16000
_TAIL_SAMPLES = 19200  # 1.2 seconds flushes trailing X-ASR tokens on real sentence WAVs.


class PCMDecoder:
    """Incrementally convert input frames to the recognizer's mono PCM format."""

    def __init__(self, rate: int, channels: int, width: int, encoding: Literal["int", "float"]):
        if not 8000 <= rate <= 192000 or not 1 <= channels <= 8 or width not in {1, 2, 3, 4}:
            raise SpeechError("invalid audio sample rate, channels, or sample width")
        if encoding == "float" and width != 4:
            raise SpeechUnavailable("only float32 WAV samples are supported")
        self.channels = channels
        self.width = width
        self.encoding = encoding
        self.frame_bytes = channels * width
        self.frames = 0
        self.pending = bytearray()
        self.resampler = None
        if rate != _SAMPLE_RATE:
            try:
                import soxr
            except ImportError as exc:
                raise SpeechUnavailable("soxr is required for audio resampling") from exc
            self.resampler = soxr.ResampleStream(rate, _SAMPLE_RATE, 1, dtype="float32")

    def feed(self, block: bytes) -> PCMChunk | None:
        self.pending.extend(block)
        size = len(self.pending) - len(self.pending) % self.frame_bytes
        if not size:
            return None
        block = bytes(self.pending[:size])
        del self.pending[:size]
        if self.encoding == "float":
            samples = np.frombuffer(block, dtype="<f4").astype(np.float32)
            if not np.isfinite(samples).all():
                raise SpeechError("non-finite audio sample")
            samples = np.clip(samples, -1, 1)
        elif self.width == 3:
            octets = np.frombuffer(block, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            values = octets[:, 0] | (octets[:, 1] << 8) | (octets[:, 2] << 16)
            samples = ((values ^ 0x800000) - 0x800000).astype(np.float32) / 8388608
        else:
            dtype = {1: np.uint8, 2: "<i2", 4: "<i4"}[self.width]
            samples = np.frombuffer(block, dtype=dtype).astype(np.float32)
            samples = (samples - (128 if self.width == 1 else 0)) / (2 ** (self.width * 8 - 1))
        self.frames += samples.size // self.channels
        if self.channels != 1:
            samples = samples.reshape(-1, self.channels).mean(axis=1, dtype=np.float32)
        if self.resampler is not None:
            samples = self.resampler.resample_chunk(samples)
        if samples.size:
            return PCMChunk(np.asarray(samples, dtype=np.float32), _SAMPLE_RATE)
        return None

    def finish(self) -> PCMChunk | None:
        if self.pending:
            raise SpeechError("truncated PCM frame")
        if self.frames == 0:
            raise SpeechError("empty audio input")
        if self.resampler is not None:
            tail = self.resampler.resample_chunk(np.empty(0, dtype=np.float32), last=True)
            if tail.size:
                return PCMChunk(np.asarray(tail, dtype=np.float32), _SAMPLE_RATE)
        return None


class XASRModel(ASRModel):
    """The fixed streaming X-ASR model and its native recognizer."""

    def __init__(self, native):
        self._native = native

    @classmethod
    def load(cls, root: Path, threads: int) -> XASRModel:
        files = ("encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx", "tokens.txt")
        missing = [name for name in files if not (root / name).is_file()]
        if missing:
            raise SpeechUnavailable(f"X-ASR 资源缺失: {', '.join(missing)}")
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise SpeechUnavailable("请安装 RedLotus[speech] 以使用本地语音识别") from exc
        native = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(root / "tokens.txt"), encoder=str(root / "encoder.int8.onnx"),
            decoder=str(root / "decoder.onnx"), joiner=str(root / "joiner.int8.onnx"),
            num_threads=threads, sample_rate=_SAMPLE_RATE, feature_dim=80,
            model_type="zipformer2", provider="cpu", enable_endpoint_detection=True,
        )
        return cls(native)

    def warmup(self) -> None:
        session = self.create_session()
        try:
            session.accept(np.zeros(_SAMPLE_RATE, dtype=np.float32))
            session.finish()
        finally:
            session.close()

    def create_session(self) -> RecognitionSession:
        return XASRSession(self._native)

    def close(self) -> None:
        self._native = None


class XASRSession(RecognitionSession):
    def __init__(self, native):
        self._native = native
        self._stream = native.create_stream()
        self._segments: list[str] = []
        self._preview = ""

    def _result_text(self) -> str:
        result = self._native.get_result(self._stream)
        return (result if isinstance(result, str) else result.text).strip()

    def _join(self, current: str) -> str:
        output = ""
        for part in (*self._segments, current):
            part = part.strip()
            if not part:
                continue
            if output and output[-1].isascii() and part[0].isascii():
                if (output[-1].isalnum() or output[-1] in ".?!") and part[0].isalnum():
                    output += " "
            output += part
        return output

    def _decode(self) -> str:
        while self._native.is_ready(self._stream):
            self._native.decode_stream(self._stream)
            if self._native.is_endpoint(self._stream):
                segment = self._result_text()
                if segment:
                    self._segments.append(segment)
                self._native.reset(self._stream)
        return self._join(self._result_text())

    def accept(self, samples: FloatSamples) -> str | None:
        self._stream.accept_waveform(_SAMPLE_RATE, samples)
        full = self._decode()
        if full != self._preview:
            self._preview = full
            return full
        return None

    def finish(self) -> str:
        self._stream.accept_waveform(_SAMPLE_RATE, np.zeros(_TAIL_SAMPLES, dtype=np.float32))
        self._stream.input_finished()
        return self._decode()

    def close(self) -> None:
        self._stream = None
        self._native = None


class StreamingRecognizer:
    def __init__(self, service=None):
        if service is None:
            from .service import SpeechService
            service = SpeechService.shared()
        self._service = service

    @staticmethod
    def _check_chunk(chunk: PCMChunk) -> FloatSamples:
        samples = chunk.samples
        if chunk.sample_rate != _SAMPLE_RATE or not isinstance(samples, np.ndarray):
            raise ValueError("ASR 仅接收 16 kHz 单声道 float32 PCM")
        if samples.ndim != 1 or samples.dtype != np.float32 or not np.all(np.isfinite(samples)):
            raise ValueError("ASR 仅接收 16 kHz 单声道 float32 PCM")
        return np.ascontiguousarray(samples)

    async def recognize(self, pcm: AsyncIterable[PCMChunk]) -> AsyncIterator[Transcript]:
        """Yield full replacement previews and exactly one final transcript at EOF."""
        service = self._service
        async with service.acquire(ModelKind.ASR) as model:
            model_id = service.engines[ModelKind.ASR].version
            session = await service.run(ModelKind.ASR, model.create_session)
            try:
                async for chunk in pcm:
                    samples = self._check_chunk(chunk)
                    max_samples = max(1, int(service.config.pcm_seconds * _SAMPLE_RATE))
                    for start in range(0, len(samples), max_samples):
                        preview = await service.run(ModelKind.ASR, session.accept, samples[start:start + max_samples])
                        if preview is not None:
                            yield Transcript(preview, False, model_id)
                final = await service.run(ModelKind.ASR, session.finish)
            finally:
                await service.run(ModelKind.ASR, session.close)
        yield Transcript(final, True, model_id)

    async def record(self, capture, on_result, *, on_started=None) -> Transcript:
        """Stream microphone PCM to recognition and collect recording statistics."""
        capture.recording_stats = {"frames": 0, "seconds": 0.0, "peak": 0.0, "rms": 0.0}
        energy = 0.0

        async def chunks():
            nonlocal energy
            await capture.start()
            device = getattr(capture, "input_device", None)
            capture.recording_stats["device"] = (
                f"{device.name} · {device.hostapi} [{device.index}]" if device else None
            )
            if on_started:
                on_started()
            async for chunk in capture:
                samples = self._check_chunk(chunk)
                stats = capture.recording_stats
                stats["frames"] += len(samples)
                energy += float(np.dot(samples.astype(np.float64), samples))
                stats["seconds"] = round(stats["frames"] / _SAMPLE_RATE, 3)
                stats["peak"] = max(stats["peak"], float(np.max(np.abs(samples), initial=0)))
                stats["rms"] = round((energy / max(stats["frames"], 1)) ** .5, 6)
                yield chunk

        final = None
        try:
            await capture.check_available()
            async with aclosing(chunks()) as pcm, aclosing(self.recognize(pcm)) as results:
                async for result in results:
                    on_result(result)
                    if result.is_final:
                        final = result
            if final is None or not final.text.strip():
                raise NoSpeechDetected("未识别到语音，请重试。")
            return final
        finally:
            await capture.close()


class AudioCapture:
    """One microphone session with a two-second callback-to-async queue."""

    def __init__(self, sample_rate: int = 16000, blocksize: int = 1600,
                 pcm_seconds: float = 2, device: InputDevice | None = None):
        if sample_rate <= 0 or blocksize <= 0 or not 0 < pcm_seconds <= 2:
            raise SpeechError("capture requires positive sample_rate, blocksize, and pcm_seconds at most two")
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self.device = device
        self.input_device: InputDevice | None = None
        self._checked_generation: int | None = None
        self._max_frames = max(1, int(sample_rate * pcm_seconds))
        self._lock = threading.Lock()
        self._queue: deque[np.ndarray] = deque()
        self._queued_frames = 0
        self._event = asyncio.Event()
        self._io_lock = asyncio.Lock()
        self._loop = None
        self._stream = None
        self._opening = False
        self._stopped = True
        self._error: Exception | None = None
        self._callback_abort = None

    async def check_available(self) -> None:
        """Resolve and validate the selected microphone without opening it."""
        from .audio import AudioDevices

        self.input_device = None
        self.input_device, self._checked_generation = await AudioDevices.check_input(self.device, self.sample_rate)

    def _callback(self, samples, frames, time_info, status):
        error = SpeechError(f"microphone error: {status}") if status else None
        block = np.asarray(samples[:, 0], dtype=np.float32).copy()
        with self._lock:
            if self._stopped:
                return
            if self._queued_frames + frames > self._max_frames:
                error = SpeechBusy("microphone PCM buffer exceeded its configured limit")
            if error is None:
                self._queue.append(block)
                self._queued_frames += frames
            else:
                self._error = error
                self._stopped = True
        self._loop.call_soon_threadsafe(self._event.set)
        if error is not None and self._callback_abort is not None:
            raise self._callback_abort

    async def start(self) -> None:
        from .audio import AudioDevices

        if self._stream is not None or self._opening:
            raise SpeechBusy("microphone capture is already active")
        self._opening = True
        try:
            await finish_io(asyncio.to_thread(AudioDevices.reserve, self))
            async with self._io_lock:
                if self.input_device is None:
                    await self.check_available()
                sd = await finish_io(asyncio.to_thread(__import__, "sounddevice"))
                self._callback_abort = getattr(sd, "CallbackAbort", None)
                self._loop = asyncio.get_running_loop()
                self._event.clear()
                self._error = None
                with self._lock:
                    self._queue.clear()
                    self._queued_frames = 0
                    self._stopped = False
                current = await finish_io(AudioDevices.start(
                    self, sd.InputStream, selected=self.input_device,
                    generation=self._checked_generation, samplerate=self.sample_rate,
                    channels=1, dtype="float32", blocksize=self.blocksize, callback=self._callback,
                ))
                self.input_device = current
        except BaseException as exc:
            await finish_io(self.close())
            if isinstance(exc, (asyncio.CancelledError, SpeechUnavailable)):
                raise
            AudioDevices.input_error(self.device is not None, exc)
        finally:
            self._opening = False

    async def __aiter__(self) -> AsyncIterator[PCMChunk]:
        while True:
            with self._lock:
                if self._queue:
                    block = self._queue.popleft()
                    self._queued_frames -= block.size
                elif self._error is not None:
                    raise self._error
                elif self._stopped:
                    return
                else:
                    block = None
                    self._event.clear()
            if block is None:
                await self._event.wait()
            else:
                yield PCMChunk(block, self.sample_rate)

    async def _halt(self, *, close: bool) -> None:
        from .audio import AudioDevices

        with self._lock:
            self._stopped = True
        self._event.set()

        async def halt_stream() -> None:
            async with self._io_lock:
                if close:
                    await AudioDevices.close(self, "abort")
                elif self._stream is not None:
                    await asyncio.to_thread(self._stream.abort)

        await finish_io(halt_stream())

    async def stop(self) -> None:
        await self._halt(close=False)

    async def close(self) -> None:
        await self._halt(close=True)
