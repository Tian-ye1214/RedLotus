"""Bounded audio conversion and local sound-device I/O for speech calls."""

from __future__ import annotations

import asyncio
import io
import queue
import shutil
import struct
import threading
import wave
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import aclosing
from pathlib import Path

import numpy as np

from redlotus.runtime.resources import finish_io
from . import AudioFormat, AudioSegment, InputDevice, PCMChunk, SpeechBusy, SpeechError, SpeechUnavailable
from .asr import PCMDecoder

_READ_SIZE = 65536
_INPUT_RATE = 16000
_OUTPUT_RATE = 24000
_SILK_HEADERS = (b"\x02#!SILK_V3", b"#!SILK_V3")
_FORMAT_ALIASES = {"wave": "wav", "mpeg": "mp3", "pcm": "pcm_s16le", "s16le": "pcm_s16le",
                   "f32le": "pcm_f32le", "mp4": "m4a", "opus+ogg": "opus"}
_MAGIC_FORMATS = {b"fLaC": "flac", b"OggS": "ogg", b"ID3": "mp3"}
_FFMPEG_FORMATS = {"mp3", "ogg", "flac", "m4a", "aac", "opus"}
_MICROPHONE_UNAVAILABLE = "暂时无法使用麦克风，请检查系统默认输入设备和权限。文字输入和语音回复仍可使用。"
_SELECTED_MICROPHONE_UNAVAILABLE = "所选麦克风不可用，请重新选择。"
_SPEAKER_UNAVAILABLE = "暂时无法使用扬声器，请检查系统默认输出设备和权限。文字输入和语音输入仍可使用。"
_REFRESH_BUSY = "请先结束录音或播报，再刷新设备。"
class AudioDevices:
    """Serialize native device lifetime, selection, and snapshot refresh."""

    _lock = threading.RLock()
    _active: set[object] = set()
    _needs_initialize = False
    _generation = 0

    @classmethod
    def input_error(cls, selected: bool, exc: Exception) -> None:
        raise SpeechUnavailable(_SELECTED_MICROPHONE_UNAVAILABLE if selected else _MICROPHONE_UNAVAILABLE) from exc

    @classmethod
    def _enumerate(cls, sd) -> list[InputDevice]:
        hostapis, entries = sd.query_hostapis(), sd.query_devices()
        try:
            default = sd.query_devices(kind="input")["index"]
        except Exception:
            default = None
        return [InputDevice(index, entry["name"], hostapis[entry["hostapi"]]["name"],
                            index == default, cls._generation)
                for index, entry in enumerate(entries) if entry["max_input_channels"] > 0]

    @classmethod
    def _resolve(cls, sd, selected: InputDevice | None, generation: int | None = None) -> InputDevice:
        if selected is None:
            current = sd.query_devices(kind="input")
            hostapi = sd.query_hostapis(current["hostapi"])
            return InputDevice(current["index"], current["name"], hostapi["name"], True, cls._generation)
        if generation == cls._generation or selected._generation == cls._generation:
            entries = sd.query_devices()
            if 0 <= selected.index < len(entries):
                entry = entries[selected.index]
                api = sd.query_hostapis(entry["hostapi"])["name"]
                if entry["max_input_channels"] > 0 and (entry["name"], api) == (selected.name, selected.hostapi):
                    return selected
        matches = [device for device in cls._enumerate(sd)
                   if (device.name, device.hostapi) == (selected.name, selected.hostapi)]
        if len(matches) != 1:
            raise SpeechUnavailable(_SELECTED_MICROPHONE_UNAVAILABLE)
        return matches[0]

    @classmethod
    async def inputs(cls, refresh: bool = False) -> list[InputDevice]:
        def enumerate_devices():
            import sounddevice as sd
            with cls._lock:
                if refresh:
                    if cls._active:
                        raise SpeechBusy(_REFRESH_BUSY)
                    if not cls._needs_initialize:
                        sd._terminate()
                        cls._needs_initialize = True
                    sd._initialize()
                    cls._needs_initialize = False
                    cls._generation += 1
                return cls._enumerate(sd)

        try:
            return await finish_io(asyncio.to_thread(enumerate_devices))
        except ImportError as exc:
            raise SpeechUnavailable("sounddevice is required for microphone capture") from exc

    @classmethod
    async def check_input(cls, selected: InputDevice | None, sample_rate: int) -> tuple[InputDevice, int]:
        def probe():
            import sounddevice as sd
            with cls._lock:
                device = cls._resolve(sd, selected)
                sd.check_input_settings(device=device.index, channels=1, dtype="float32", samplerate=sample_rate)
                return device, cls._generation

        try:
            return await finish_io(asyncio.to_thread(probe))
        except SpeechUnavailable:
            raise
        except Exception as exc:
            cls.input_error(selected is not None, exc)

    @classmethod
    def reserve(cls, owner) -> None:
        with cls._lock:
            cls._active.add(owner)

    @classmethod
    async def start(cls, owner, factory, *, selected: InputDevice | None = None,
                    generation: int | None = None, **kwargs) -> InputDevice | None:
        def open_stream():
            with cls._lock:
                device = None
                if selected is not None:
                    device = cls._resolve(__import__("sounddevice"), selected, generation)
                    kwargs["device"] = device.index
                owner._stream = factory(**kwargs)
                cls._active.add(owner)
                owner._stream.start()
                return device

        return await finish_io(asyncio.to_thread(open_stream))

    @classmethod
    async def close(cls, owner, method: str) -> None:
        def close_stream():
            with cls._lock:
                stream = owner._stream
                if stream is None:
                    cls._active.discard(owner)
                    return
                try:
                    getattr(stream, method)()
                finally:
                    stream.close()
                    owner._stream = None
                    cls._active.discard(owner)

        await finish_io(asyncio.to_thread(close_stream))


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
