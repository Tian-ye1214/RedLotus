"""Local speech contracts; importing these types never loads a speech runtime."""
from __future__ import annotations

import atexit
import asyncio
from contextlib import aclosing
from typing import AsyncIterable, AsyncIterator
from redlotus.runtime.resources import FileFingerprint, finish_io
import threading

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
import math
import os
import re
import importlib
import importlib.util
from pathlib import Path
from typing import Literal, Self, TypedDict

import numpy as np
from numpy.typing import NDArray

FloatSamples = NDArray[np.float32]


class ModelKind(StrEnum):
    ASR = "asr"
    TTS = "tts"


class TTSBackend(StrEnum):
    SHERPA = "sherpa-onnx"
    MAMBO = "mambo-onnx"


class ModelStage(StrEnum):
    MISSING = "missing"
    WAITING = "waiting"
    CHECKING = "checking"
    DOWNLOADING = "downloading"
    VERIFYING = "verifying"
    INSTALLING = "installing"
    EXTRACTING = "extracting"
    LOADING = "loading"
    WARMING = "warming"
    INSTALLED = "installed"
    READY = "ready"
    FAILED = "failed"


class AudioFormat(StrEnum):
    CLI = "cli"
    PCM_S16LE = "pcm_s16le"
    PCM_F32LE = "pcm_f32le"
    WAV = "wav"
    SILK = "silk"
    MP3 = "mp3"
    OGG = "ogg"
    FLAC = "flac"
    M4A = "m4a"
    AAC = "aac"
    OPUS = "opus"


class SpeechError(RuntimeError):
    """A speech operation failed without invalidating the text conversation."""


class SpeechUnavailable(SpeechError):
    """A required local runtime, device or model is unavailable."""


class SpeechBusy(SpeechError):
    """The bounded speech admission queue has no free place."""


class NoSpeechDetected(SpeechError):
    """The capture completed without a usable spoken transcript."""




@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    hostapi: str
    is_default: bool = False
    _generation: int | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class PCMChunk:
    samples: FloatSamples
    sample_rate: int
    end_of_segment: bool = False

    @classmethod
    def from_bytes(cls, data: bytes, sample_rate: int) -> Self:
        """Decode one owned little-endian float32 mono block."""
        if len(data) % 4:
            raise SpeechError("音频分块包含不完整的 float32 样本")
        samples = np.frombuffer(data, dtype="<f4")
        if not np.isfinite(samples).all():
            raise SpeechError("音频分块包含非有限样本")
        return cls(samples, sample_rate)


@dataclass(frozen=True)
class VoiceProfile:
    model_id: str
    voice_id: str
    mode: Literal["speaker", "features"]
    default_sid: int | None = None
    latin_sid: int | None = None
    english_number_words: bool = False


@dataclass
class SpeakerCondition:
    model_id: str
    default_sid: int
    latin_sid: int
    english_number_words: bool
    previous_sid: int


@dataclass(frozen=True)
class FeatureCondition:
    model_id: str
    voice_id: str


VoiceCondition = SpeakerCondition | FeatureCondition


@dataclass(frozen=True)
class SynthesisRequest:
    text: str
    voice: VoiceCondition


@dataclass(frozen=True)
class Transcript:
    text: str
    is_final: bool
    model_id: str | None = None


@dataclass(frozen=True)
class AudioSegment:
    data: bytes
    format: AudioFormat
    duration: float
    sample_rate: int

    def __post_init__(self):
        if not isinstance(self.data, bytes):
            raise TypeError("encoded audio must contain bytes, not a file path")
        object.__setattr__(self, "format", AudioFormat(self.format))
        if self.format not in (AudioFormat.WAV, AudioFormat.SILK):
            raise ValueError("encoded output must be WAV or SILK")
        if not math.isfinite(self.duration) or self.duration < 0 or self.sample_rate != 24000:
            raise ValueError("encoded output must have a valid duration and 24 kHz sample rate")


@dataclass(frozen=True)
class ModelSpec:
    kind: ModelKind
    archive: str
    url: str
    sha256: str
    root: str
    required: tuple[str, ...]
    archive_limit: int
    unpack_limit: int
    backend: TTSBackend | None = None

    @property
    def version(self) -> str:
        return f"{self.root}-{self.sha256[:12]}"

    @property
    def identity(self) -> dict[str, str]:
        return {"sha256": self.sha256, "url": self.url}

    @classmethod
    def from_dict(cls, kind: ModelKind, row: Mapping[str, object]) -> Self:
        names = ("archive", "url", "sha256", "root")
        if (not all(isinstance(row.get(name), str) and row[name] for name in names)
                or not isinstance(row.get("required"), list) or not row["required"]
                or not all(isinstance(name, str) for name in row["required"])
                or any(type(row.get(name)) is not int or row[name] <= 0 for name in ("archive_limit", "unpack_limit"))):
            raise ValueError("invalid model catalog entry")
        try:
            backend = TTSBackend(row["runtime"]["runtime"]) if kind == ModelKind.TTS else None
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid TTS catalog runtime") from exc
        return cls(ModelKind(kind), *(row[name] for name in names), tuple(row["required"]), row["archive_limit"], row["unpack_limit"], backend)


    def resources_present(self, files: Mapping[str, FileFingerprint]) -> bool:
        return all(any(path.startswith(name) and detail.size > 0 for path, detail in files.items())
                   if name.endswith("/") else name in files and files[name].size > 0 for name in self.required)






@dataclass(frozen=True)
class InstalledModel:
    path: str
    archive_sha256: str
    files: dict[str, FileFingerprint]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, row: Mapping[str, object]) -> Self:
        if (not isinstance(row, dict) or not isinstance(row.get("path"), str)
                or not isinstance(row.get("archive_sha256"), str) or not isinstance(row.get("files"), dict)):
            raise ValueError("invalid installed model record")
        try:
            files = {name: FileFingerprint(**value) for name, value in row["files"].items() if isinstance(name, str)}
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid installed model fingerprint") from exc
        if len(files) != len(row["files"]):
            raise ValueError("invalid installed model resource name")
        return cls(row["path"], row["archive_sha256"], files)


@dataclass
class InstalledState:
    versions: dict[ModelKind, dict[str, InstalledModel]] = field(default_factory=lambda: {kind: {} for kind in ModelKind})
    active: dict[ModelKind, str] = field(default_factory=dict)
    previous: dict[ModelKind, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {"schema": 1, "versions": {kind.value: {key: row.to_dict() for key, row in versions.items()}
                for kind, versions in self.versions.items()}, "active": dict(self.active), "previous": dict(self.previous)}

    @classmethod
    def from_dict(cls, row: Mapping[str, object]) -> Self:
        if (not isinstance(row, dict) or row.get("schema") != 1
                or any(not isinstance(row.get(key), dict) for key in ("versions", "active", "previous"))
                or any(not isinstance(row["versions"].get(kind), dict) for kind in ModelKind)):
            raise ValueError("unknown model installation schema")
        try:
            versions = {kind: {version: InstalledModel.from_dict(value) for version, value in row["versions"][kind].items()}
                        for kind in ModelKind}
            pins = [{ModelKind(kind): version for kind, version in row[key].items()} for key in ("active", "previous")]
            if not all(isinstance(version, str) for pin in pins for version in pin.values()):
                raise ValueError("invalid model version pin")
        except (TypeError, KeyError) as exc:
            raise ValueError("invalid model installation state") from exc
        return cls(versions, *pins)


@dataclass
class PreparationStatus:
    stage: ModelStage = ModelStage.MISSING
    bytes: int = 0
    total: int | None = None
    active: str | None = None
    loaded: str | None = None
    previous: str | None = None
    target: str | None = None
    error: str | None = None


class PreparationUpdate(TypedDict, total=False):
    stage: ModelStage
    bytes: int
    total: int | None
    active: str | None
    loaded: str | None
    previous: str | None
    target: str | None
    error: str | None


class RecognitionSession(ABC):
    @abstractmethod
    def accept(self, samples: FloatSamples) -> str | None:
        """Consume standard PCM and return a changed complete hypothesis."""

    @abstractmethod
    def finish(self) -> str:
        """Flush trailing tokens and return one final transcript."""

    @abstractmethod
    def close(self) -> None:
        """Release this recognition stream."""


class SpeechModel(ABC):
    @classmethod
    def retained_root(cls) -> Path | None:
        """Installed resources referenced by process-global native state."""
        return None

    @classmethod
    @abstractmethod
    def load(cls, root: Path, threads: int) -> Self:
        """Construct a model from installed resources without downloading."""

    @abstractmethod
    def warmup(self) -> None:
        """Exercise native inference before publishing readiness."""

    @abstractmethod
    def close(self) -> None:
        """Release native model resources after outstanding calls finish."""


class ASRModel(SpeechModel):
    @abstractmethod
    def create_session(self) -> RecognitionSession:
        """Create an independent streaming recognition state."""


class TTSModel(SpeechModel):
    @property
    @abstractmethod
    def sample_rate(self) -> int:
        """Return the synthesis model output rate."""

    @abstractmethod
    def prepare_voice(self, profile: VoiceProfile) -> VoiceCondition:
        """Prepare a voice for one synthesis response on the model worker."""

    @abstractmethod
    def generate(self, request: SynthesisRequest, callback: Callable[[FloatSamples, float], int] | None = None) -> PCMChunk | None:
        """Return segment PCM, or None after delivering PCM through the callback."""


@dataclass(frozen=True)
class SpeechSettings:
    """Published speech defaults, overridden through existing config sources."""

    model_dir: Path | None = None
    tts_package: Path | None = None
    tts_backend: TTSBackend = TTSBackend.MAMBO
    asr_threads: int = 2
    tts_threads: int = 8
    queue_size: int = 8
    pcm_seconds: float = 2
    flush_ms: int = 600
    segment_chars: int = 120
    clip_seconds: float = 55
    text_chars: int = 4096

    def __post_init__(self):
        from redlotus.runtime.config import user_config_dir
        object.__setattr__(self, "model_dir", Path(self.model_dir if self.model_dir is not None else
                                                    user_config_dir() / "model").expanduser().resolve())
        if self.tts_package is not None:
            if not isinstance(self.tts_package, (str, Path)) or not str(self.tts_package).strip():
                raise ValueError("speech.tts_package 必须是模型包目录")
            object.__setattr__(self, "tts_package", Path(self.tts_package).expanduser().absolute())
        object.__setattr__(self, "tts_backend", TTSBackend(self.tts_backend))
        for item in fields(self):
            if item.name not in {"model_dir", "tts_package", "tts_backend"}:
                value = getattr(self, item.name)
                kind = (float, int) if item.name in {"pcm_seconds", "clip_seconds"} else int
                if isinstance(value, bool) or not isinstance(value, kind) or not math.isfinite(value) or value <= 0:
                    raise ValueError(f"speech.{item.name} 必须为有限正数")
        for name, limit in (("pcm_seconds", 2), ("clip_seconds", 55), ("queue_size", 8),
                            ("segment_chars", 120), ("text_chars", 4096)):
            if getattr(self, name) > limit:
                raise ValueError(f"speech.{name} 不得超过 {limit}")

    @classmethod
    def read(cls, values=None):
        from redlotus.runtime.config import config_value, settings
        values = settings() if values is None else values
        defaults = cls()
        selected = {}
        for item in fields(cls):
            kind = ((str, type(None)) if item.name == "tts_package" else str if item.name in {"model_dir", "tts_backend"}
                    else (int, float) if item.name in {"pcm_seconds", "clip_seconds"} else int)
            default = str(defaults.model_dir) if item.name == "model_dir" else getattr(defaults, item.name)
            selected[item.name] = config_value(values, ("speech", item.name), default, kind=kind)
        return cls(**selected)


class ModelFactory:
    @classmethod
    def available(cls, backend: TTSBackend | None = None) -> bool:
        runtime = importlib.util.find_spec("sherpa_onnx")
        return runtime is not None and (backend != TTSBackend.MAMBO or bool(
            os.name == "nt" and runtime.origin
            and (Path(__file__).with_name("native") / "redlotus_mambo.exe").is_file()
            and (Path(runtime.origin).parent / "lib" / "onnxruntime.dll").is_file()))

    @classmethod
    def require_runtime(cls) -> None:
        try:
            importlib.import_module("sherpa_onnx")
        except (ImportError, OSError) as exc:
            raise SpeechUnavailable(f"本地语音运行库不可用；安装 RedLotus[speech]: {exc}") from exc

    @classmethod
    def implementation(cls, kind: ModelKind, backend: TTSBackend = TTSBackend.SHERPA) -> type[SpeechModel]:
        from .inference import XASRModel, KokoroModel, MamboTTSModel
        return (XASRModel if ModelKind(kind) == ModelKind.ASR else
                {TTSBackend.SHERPA: KokoroModel, TTSBackend.MAMBO: MamboTTSModel}[TTSBackend(backend)])

    @classmethod
    def create(cls, kind: ModelKind, root: Path, threads: int) -> SpeechModel:
        backend = TTSBundle.read(root).backend if ModelKind(kind) == ModelKind.TTS else TTSBackend.SHERPA
        return cls.implementation(kind, backend).load(root, threads)


class ModelLease:
    """Guard installed resources while a model or native global state uses them."""

    _retained: dict[Path, ModelLease] = {}
    _guard = threading.Lock()

    def __init__(self, root: Path, path: Path):
        from filelock import FileLock
        self.root, self.path = root.resolve(), path
        self.lock = FileLock(str(path), thread_local=False)
        self.lock.acquire()

    def close(self, *, retain: bool = False) -> None:
        with self._guard:
            if retain and self.root not in self._retained:
                if not self._retained:
                    atexit.register(type(self).release_retained)
                self._retained[self.root] = self
            if self._retained.get(self.root) is self:
                return
        self.lock.release()
        self.path.unlink(missing_ok=True)

    @classmethod
    def release_retained(cls) -> None:
        with cls._guard:
            retained = tuple(cls._retained.values())
            cls._retained.clear()
        for lease in retained:
            lease.close()

_INPUT_RATE = 16000
_SELECTED_MICROPHONE_UNAVAILABLE = "所选麦克风不可用，请重新选择。"
_SPEAKER_UNAVAILABLE = "暂时无法使用扬声器，请检查系统默认输出设备和权限。文字输入和语音输入仍可使用。"

class AudioDevices:
    """Serialize native device lifetime, selection, and snapshot refresh."""

    _lock = threading.RLock()
    _active: set[object] = set()
    _needs_initialize = False
    _generation = 0

    @classmethod
    def input_error(cls, selected: bool, exc: Exception) -> None:
        raise SpeechUnavailable(_SELECTED_MICROPHONE_UNAVAILABLE if selected else
                                "暂时无法使用麦克风，请检查系统默认输入设备和权限。文字输入和语音回复仍可使用。") from exc

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
                        raise SpeechBusy("请先结束录音或播报，再刷新设备。")
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

class _SpeechStream:
    """Bind a public streaming operation to an injected or process-shared service."""

    def __init__(self, service=None):
        if service is None:
            from .service import SpeechService
            service = SpeechService.shared()
        self._service = service


class StreamingRecognizer(_SpeechStream):
    @staticmethod
    def _check_chunk(chunk: PCMChunk) -> FloatSamples:
        samples = chunk.samples
        if (chunk.sample_rate != _INPUT_RATE or not isinstance(samples, np.ndarray)
                or samples.ndim != 1 or samples.dtype != np.float32 or not np.all(np.isfinite(samples))):
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
                    max_samples = max(1, int(service.config.pcm_seconds * _INPUT_RATE))
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
                stats["seconds"] = round(stats["frames"] / _INPUT_RATE, 3)
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

class StreamingSynthesizer(_SpeechStream):
    async def synthesize(self, text: str | AsyncIterable[str]) -> AsyncIterator[PCMChunk]:
        """Synthesize each new segment once and emit bounded PCM windows."""
        async with aclosing(self._synthesize(text)) as stream:
            async for chunk in stream:
                yield chunk

    async def _synthesize(self, text, on_consumed=None):
        from .inference import _SegmentStream
        from .tts import TextSegmenter
        config = self._service.config
        segments = TextSegmenter(config.flush_ms, config.segment_chars).segments(text, on_consumed)
        try:
            async with aclosing(_SegmentStream(self._service).stream(segments)) as stream:
                async for chunk in stream:
                    yield chunk
        finally:
            await segments.aclose()

from .inference import TTSBundle
from .tts import SpeechTextParser

__all__ = ["InputDevice", "PCMChunk", "Transcript", "AudioSegment", "SpeechSettings", "SpeechError", "SpeechUnavailable",
           "SpeechBusy", "NoSpeechDetected", "ModelFactory", "ModelKind", "ModelStage", "AudioFormat", "ModelSpec", "InstalledModel",
           "InstalledState", "PreparationStatus", "SpeechModel", "ASRModel", "TTSModel", "RecognitionSession", "SpeechTextParser",
           "AudioDevices", "StreamingRecognizer", "StreamingSynthesizer", "TTSBundle", "TTSBackend", "VoiceProfile", "VoiceCondition", "SpeakerCondition", "FeatureCondition", "SynthesisRequest"]
