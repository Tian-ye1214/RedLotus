"""Bounded audio conversion and local sound-device I/O for speech calls."""

from __future__ import annotations

import asyncio
import io
import queue
import shutil
import struct
import threading
import wave
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import aclosing
from pathlib import Path

import numpy as np

from redlotus.runtime.resources import finish_io
from . import AudioFormat, AudioSegment, InputDevice, PCMChunk, SpeechBusy, SpeechError, SpeechUnavailable
from .inference import PCMDecoder
from . import AudioDevices, _SPEAKER_UNAVAILABLE

_READ_SIZE = 65536
_INPUT_RATE = 16000
_OUTPUT_RATE = 24000
_SILK_HEADERS = (b"\x02#!SILK_V3", b"#!SILK_V3")
_FORMAT_ALIASES = {"wave": "wav", "mpeg": "mp3", "pcm": "pcm_s16le", "s16le": "pcm_s16le",
                   "f32le": "pcm_f32le", "mp4": "m4a", "opus+ogg": "opus"}
_MAGIC_FORMATS = {b"fLaC": "flac", b"OggS": "ogg", b"ID3": "mp3"}
_FFMPEG_FORMATS = {"mp3", "ogg", "flac", "m4a", "aac", "opus"}


class _SilkSink:
    """Keep a native decoder's synchronous writes behind two PCM blocks."""

    def __init__(self):
        self.blocks: queue.Queue[bytes | None] = queue.Queue(maxsize=2)
        self.stopped = threading.Event()

    def read(self, size: int = -1) -> bytes:
        raise io.UnsupportedOperation("SILK output cannot be read")

    def write(self, data: bytes) -> int:
        for start in range(0, len(data), _READ_SIZE):
            while not self.stopped.is_set():
                try:
                    self.blocks.put(data[start:start + _READ_SIZE], timeout=.05)
                    break
                except queue.Full:
                    pass
            else:
                raise SpeechError("SILK decode stopped")
        return len(data)

    def finish(self) -> None:
        while not self.stopped.is_set():
            try:
                self.blocks.put(None, timeout=.05)
                return
            except queue.Full:
                pass


class AudioIO:
    """Stream input audio to mono 16 kHz PCM and segment output in memory."""

    @staticmethod
    async def _source_bytes(source: bytes | Path | AsyncIterable[bytes]) -> AsyncIterator[bytes]:
        if isinstance(source, bytes):
            for start in range(0, len(source), _READ_SIZE):
                yield source[start:start + _READ_SIZE]
        elif isinstance(source, Path):
            with source.open("rb") as handle:
                while block := await finish_io(asyncio.to_thread(handle.read, _READ_SIZE)):
                    yield block
        else:
            async for block in source:
                if not isinstance(block, bytes):
                    raise SpeechError("audio source must yield bytes")
                for start in range(0, len(block), _READ_SIZE):
                    yield block[start:start + _READ_SIZE]

    @staticmethod
    async def _raw_chunks(parts: AsyncIterable[bytes], *, rate: int | None, channels: int, encoding: str) -> AsyncIterator[PCMChunk]:
        if rate is None:
            raise SpeechError("sample_rate is required for raw PCM")
        decoder = PCMDecoder(rate, channels, 2 if encoding == "pcm_s16le" else 4,
                              "int" if encoding == "pcm_s16le" else "float")
        async for block in parts:
            chunk = decoder.feed(block)
            if chunk is not None:
                yield chunk
        if tail := decoder.finish():
            yield tail


    @staticmethod
    async def _wav_chunks(parts: AsyncIterable[bytes]) -> AsyncIterator[PCMChunk]:
        buffer = bytearray()
        state, remaining, decoder = "riff", 0, None
        saw_data, data_padding = False, False
        async for block in parts:
            buffer.extend(block)
            while True:
                if state == "riff":
                    if len(buffer) < 12:
                        break
                    del buffer[:12]
                    state = "chunk"
                elif state == "chunk":
                    if len(buffer) < 8:
                        break
                    marker, remaining = struct.unpack_from("<4sI", buffer)
                    del buffer[:8]
                    if marker == b"fmt ":
                        if remaining < 16:
                            raise SpeechError("truncated WAV format chunk")
                        state = "fmt"
                    elif marker == b"data":
                        if decoder is None:
                            raise SpeechError("WAV data precedes format")
                        if remaining % decoder.frame_bytes:
                            raise SpeechError("incomplete WAV audio frame")
                        saw_data, data_padding, state = True, bool(remaining % 2), "data"
                    else:
                        remaining += remaining % 2
                        state = "skip"
                elif state == "fmt":
                    if len(buffer) < 16:
                        break
                    encoding, channels, rate, _, alignment, bits = struct.unpack_from("<HHIIHH", buffer)
                    if bits % 8 or alignment != channels * (bits // 8):
                        raise SpeechError("invalid WAV frame alignment")
                    if encoding not in {1, 3}:
                        raise SpeechUnavailable(f"unsupported WAV encoding {encoding}")
                    decoder = PCMDecoder(rate, channels, bits // 8, "float" if encoding == 3 else "int")
                    del buffer[:16]
                    remaining, state = remaining - 16 + remaining % 2, "skip"
                elif state == "skip":
                    take = min(remaining, len(buffer))
                    del buffer[:take]
                    remaining -= take
                    if remaining:
                        break
                    state = "chunk"
                elif state == "data":
                    if remaining == 0:
                        remaining, state = int(data_padding), "skip" if data_padding else "chunk"
                        continue
                    take = min(remaining, len(buffer))
                    if not take:
                        break
                    chunk = decoder.feed(bytes(buffer[:take]))
                    del buffer[:take]
                    remaining -= take
                    if chunk is not None:
                        yield chunk
        if not saw_data or state in {"riff", "fmt", "data", "skip"} and remaining:
            raise SpeechError("truncated WAV audio")
        if tail := decoder.finish():
            yield tail



    @classmethod
    async def _silk_chunks(cls, parts: AsyncIterable[bytes]) -> AsyncIterator[PCMChunk]:
        try:
            import pysilk
        except ImportError as exc:
            raise SpeechUnavailable("silk-python is required for SILK audio") from exc
        compressed = bytearray()
        async for block in parts:
            if len(compressed) + len(block) > 8 * 1024 * 1024:
                raise SpeechError("SILK input exceeds the memory limit")
            compressed.extend(block)
        sink = _SilkSink()

        def decode():
            try:
                pysilk.decode(io.BytesIO(compressed), sink, _INPUT_RATE)
            except Exception as exc:
                raise SpeechError(f"SILK decode failed: {exc}") from exc
            finally:
                sink.finish()

        worker = asyncio.create_task(asyncio.to_thread(decode))

        async def decoded():
            while (block := await finish_io(asyncio.to_thread(sink.blocks.get))) is not None:
                yield block
            await worker

        try:
            async with aclosing(decoded()) as blocks, aclosing(
                cls._raw_chunks(blocks, rate=_INPUT_RATE, channels=1, encoding="pcm_s16le")
            ) as pcm:
                async for chunk in pcm:
                    yield chunk
        finally:
            sink.stopped.set()
            await finish_io(asyncio.wait({worker}))
            if worker.done() and not worker.cancelled():
                worker.exception()

    @classmethod
    async def _ffmpeg_chunks(cls, parts: AsyncIterable[bytes]) -> AsyncIterator[PCMChunk]:
        executable = shutil.which("ffmpeg")
        if executable is None:
            raise SpeechUnavailable("FFmpeg is required for compressed audio input")
        process = await asyncio.create_subprocess_exec(
            executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le",
            "-acodec", "pcm_s16le", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def feed():
            try:
                async for block in parts:
                    process.stdin.write(block)
                    await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        async def errors():
            tail = bytearray()
            while block := await process.stderr.read(_READ_SIZE):
                tail.extend(block)
                if len(tail) > 4096:
                    del tail[:-4096]
            return tail.decode(errors="replace")

        writer = asyncio.create_task(feed())
        stderr = asyncio.create_task(errors())

        async def output():
            while block := await process.stdout.read(_READ_SIZE):
                yield block
            await writer
            returncode = await process.wait()
            detail = await stderr
            if returncode:
                raise SpeechError(f"FFmpeg decode failed: {detail or returncode}")

        try:
            async with aclosing(output()) as blocks, aclosing(
                cls._raw_chunks(blocks, rate=_INPUT_RATE, channels=1, encoding="pcm_s16le")
            ) as pcm:
                async for chunk in pcm:
                    yield chunk
        finally:
            if process.returncode is None:
                process.terminate()
            if not writer.done():
                writer.cancel()
            await finish_io(asyncio.wait({writer, stderr}))
            await finish_io(process.wait())

    @classmethod
    async def parse_input(
        cls, source: bytes | Path | AsyncIterable[bytes], *, format: str | None = None,
        sample_rate: int | None = None, channels: int = 1,
    ) -> AsyncIterator[PCMChunk]:
        """Decode a real audio header or declared format to 16 kHz mono float32."""
        async with aclosing(cls._source_bytes(source)) as parts:
            head = bytearray()
            while len(head) < 12 and (block := await anext(parts, None)) is not None:
                head.extend(block)
            if not head:
                raise SpeechError("empty audio input")
            if head[:4] == b"RIFF":
                if len(head) < 12 or head[8:12] != b"WAVE":
                    raise SpeechError("invalid WAV header")
                kind = "wav"
            elif any(head.startswith(header) for header in _SILK_HEADERS):
                kind = "silk"
            else:
                declared = (format or (source.suffix.lstrip(".") if isinstance(source, Path) else "")).lower().split(";", 1)[0].strip()
                declared = declared.removeprefix("audio/").removeprefix("x-")
                fallback = "m4a" if head[4:8] == b"ftyp" else _FORMAT_ALIASES.get(declared, declared or None)
                kind = next((name for magic, name in _MAGIC_FORMATS.items() if head.startswith(magic)), fallback)

            async def joined():
                yield bytes(head)
                async for block in parts:
                    yield block

            async with aclosing(joined()) as stream:
                if kind == "wav":
                    decoded = cls._wav_chunks(stream)
                elif kind in {"pcm_s16le", "pcm_f32le"}:
                    decoded = cls._raw_chunks(stream, rate=sample_rate, channels=channels, encoding=kind)
                elif kind == "silk":
                    decoded = cls._silk_chunks(stream)
                elif kind in _FFMPEG_FORMATS:
                    decoded = cls._ffmpeg_chunks(stream)
                else:
                    raise SpeechUnavailable(f"unsupported audio format: {kind or 'unknown'}")
                async with aclosing(decoded):
                    async for chunk in decoded:
                        yield chunk

    @staticmethod
    def _output_samples(chunk: PCMChunk) -> np.ndarray:
        if chunk.sample_rate != _OUTPUT_RATE:
            raise SpeechError("output PCM must be 24000 Hz")
        samples = chunk.samples
        if not isinstance(samples, np.ndarray) or samples.dtype != np.float32 or samples.ndim != 1 or not np.isfinite(samples).all():
            raise SpeechError("output PCM must be finite mono float32")
        return samples

    @classmethod
    async def parse_output(
        cls, pcm: AsyncIterable[PCMChunk], *, target: str = "cli", max_seconds: float = 55,
    ) -> AsyncIterator[PCMChunk | AudioSegment]:
        """Pass CLI PCM through or yield duration-bounded encoded bytes."""
        if target not in {"cli", "wav", "silk"}:
            raise SpeechError(f"unsupported audio target: {target}")
        if target == "cli":
            async for chunk in pcm:
                cls._output_samples(chunk)
                yield chunk
            return
        if not 0 < max_seconds <= 55:
            raise SpeechError("max_seconds must be greater than zero and at most 55")
        max_frames = int(max_seconds * _OUTPUT_RATE)
        if max_frames < 1:
            raise SpeechError("max_seconds is shorter than one sample")
        buffer = None
        writer = None
        frames = 0

        async def finish_segment():
            nonlocal buffer, writer, frames
            if writer is not None:
                writer.close()
            if target == "silk":
                try:
                    import pysilk
                except ImportError as exc:
                    raise SpeechUnavailable("silk-python is required for SILK audio") from exc
                encoded = io.BytesIO()
                buffer.seek(0)
                try:
                    await finish_io(asyncio.to_thread(pysilk.encode, buffer, encoded, _OUTPUT_RATE, 24000))
                except Exception as exc:
                    raise SpeechError(f"SILK encode failed: {exc}") from exc
                data = encoded.getvalue()
            else:
                data = buffer.getvalue()
            segment = AudioSegment(data, AudioFormat(target), frames / _OUTPUT_RATE, _OUTPUT_RATE)
            buffer = writer = None
            frames = 0
            return segment

        try:
            async for chunk in pcm:
                samples = cls._output_samples(chunk)
                offset = 0
                while offset < samples.size:
                    if buffer is None:
                        buffer = io.BytesIO()
                        if target == "wav":
                            writer = wave.open(buffer, "wb")
                            writer.setnchannels(1)
                            writer.setsampwidth(2)
                            writer.setframerate(_OUTPUT_RATE)
                    take = min(max_frames - frames, 4096, samples.size - offset)
                    block = samples[offset:offset + take]
                    data = np.rint(np.clip(block, -1, 1) * 32767).astype("<i2").tobytes()
                    writer.writeframesraw(data) if writer is not None else buffer.write(data)
                    frames += take
                    offset += take
                    if frames == max_frames or (chunk.end_of_segment and offset == samples.size):
                        yield await finish_segment()
                if chunk.end_of_segment and not samples.size and frames:
                    yield await finish_segment()
            if frames:
                yield await finish_segment()
        finally:
            if writer is not None:
                writer.close()

class AudioPlayer:
    """Play PCM in short native writes, so cancellation can abort the stream."""

    def __init__(self, sample_rate: int = 24000, pcm_seconds: float = 2):
        if sample_rate <= 0 or not 0 < pcm_seconds <= 2:
            raise SpeechError("player requires positive sample_rate and pcm_seconds at most two")
        self.sample_rate = sample_rate
        self._write_frames = max(1, min(int(sample_rate / 10), int(sample_rate * pcm_seconds)))
        self._stream = None
        self._play_task: asyncio.Task | None = None
        self._stopped = False

    async def play(self, pcm: AsyncIterable[PCMChunk]) -> None:
        if self._play_task is not None:
            raise SpeechBusy("audio playback is already active")
        try:
            sd = await finish_io(asyncio.to_thread(__import__, "sounddevice"))
        except ImportError as exc:
            raise SpeechUnavailable("sounddevice is required for audio playback") from exc
        self._play_task = asyncio.current_task()
        self._stopped = False
        failed = False
        try:
            await finish_io(asyncio.to_thread(AudioDevices.reserve, self))
            if self._stream is None:
                try:
                    await AudioDevices.start(self, sd.OutputStream, samplerate=self.sample_rate,
                                             channels=1, dtype="float32")
                except Exception as exc:
                    raise SpeechUnavailable(_SPEAKER_UNAVAILABLE) from exc
            async for chunk in pcm:
                if self._stopped:
                    break
                if chunk.sample_rate != self.sample_rate:
                    raise SpeechError(f"playback PCM must be {self.sample_rate} Hz")
                samples = np.asarray(chunk.samples, dtype=np.float32)
                if samples.ndim != 1:
                    raise SpeechError("playback PCM must be one-dimensional")
                for start in range(0, samples.size, self._write_frames):
                    if self._stopped:
                        break
                    try:
                        await finish_io(asyncio.to_thread(
                            self._stream.write, samples[start:start + self._write_frames].reshape(-1, 1),
                        ))
                    except Exception as exc:
                        raise SpeechUnavailable(_SPEAKER_UNAVAILABLE) from exc
        except BaseException:
            failed = True
            raise
        finally:
            try:
                await AudioDevices.close(self, "abort" if failed or self._stopped else "stop")
            finally:
                self._play_task = None

    async def stop(self) -> None:
        self._stopped = True
        task = self._play_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await finish_io(asyncio.wait({task}))
        elif task is None:
            await AudioDevices.close(self, "abort")

    async def close(self) -> None:
        await self.stop()


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
