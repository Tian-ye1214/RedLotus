"""Local speech contracts; importing these types never loads a speech runtime."""
from __future__ import annotations

import atexit
import threading

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
import math
import importlib
import importlib.util
from pathlib import Path
from typing import Self, TypedDict

import numpy as np
from numpy.typing import NDArray

FloatSamples = NDArray[np.float32]


class ModelKind(StrEnum):
    ASR = "asr"
    TTS = "tts"


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

    @property
    def version(self) -> str:
        return f"{self.root}-{self.sha256[:12]}"

    @property
    def identity(self) -> dict[str, str]:
        return {"sha256": self.sha256, "url": self.url}

    @classmethod
    def from_dict(cls, kind: ModelKind, row: Mapping[str, object]) -> Self:
        names = ("archive", "url", "sha256", "root")
        if not all(isinstance(row.get(name), str) and row[name] for name in names):
            raise ValueError("invalid model catalog strings")
        required = row.get("required")
        if not isinstance(required, list) or not required or not all(isinstance(name, str) for name in required):
            raise ValueError("invalid required model resources")
        if any(type(row.get(name)) is not int or row[name] <= 0 for name in ("archive_limit", "unpack_limit")):
            raise ValueError("invalid model resource limits")
        return cls(ModelKind(kind), *(row[name] for name in names), tuple(required), row["archive_limit"], row["unpack_limit"])


    def resources_present(self, files: Mapping[str, FileFingerprint]) -> bool:
        return all(any(path.startswith(name) and detail.size > 0 for path, detail in files.items())
                   if name.endswith("/") else name in files and files[name].size > 0 for name in self.required)


@dataclass(frozen=True)
class FileFingerprint:
    size: int
    sha256: str

    def __post_init__(self):
        if type(self.size) is not int or self.size < 0 or not isinstance(self.sha256, str):
            raise ValueError("invalid model file fingerprint")


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
    def generate(self, text: str, speaker: int, callback: Callable[[FloatSamples, float], int] | None = None) -> PCMChunk:
        """Generate one bounded segment, optionally publishing native chunks."""


@dataclass(frozen=True)
class SpeechSettings:
    """Published speech defaults, overridden through existing config sources."""

    model_dir: Path | None = None
    asr_threads: int = 2
    tts_threads: int = 4
    queue_size: int = 8
    pcm_seconds: float = 2
    flush_ms: int = 600
    segment_chars: int = 120
    clip_seconds: float = 55
    text_chars: int = 4096

    def __post_init__(self):
        from redlotus.runtime.config import user_config_dir
        root = self.model_dir if self.model_dir is not None else user_config_dir() / "model"
        object.__setattr__(self, "model_dir", Path(root).expanduser().resolve())
        for item in fields(self):
            if item.name != "model_dir":
                value = getattr(self, item.name)
                kind = (float, int) if item.name in {"pcm_seconds", "clip_seconds"} else int
                if isinstance(value, bool) or not isinstance(value, kind) or not math.isfinite(value) or value <= 0:
                    raise ValueError(f"speech.{item.name} 必须为有限正数")
        if self.pcm_seconds > 2 or self.clip_seconds > 55:
            raise ValueError("speech.pcm_seconds 不得超过 2，speech.clip_seconds 不得超过 55")
        if self.queue_size > 8 or self.segment_chars > 120:
            raise ValueError("speech.queue_size 不得超过 8，speech.segment_chars 不得超过 120")
        if self.text_chars > 4096:
            raise ValueError("speech.text_chars 不得超过 4096")

    @classmethod
    def read(cls, values=None):
        from redlotus.runtime.config import config_value, settings
        values = settings() if values is None else values
        defaults = cls()
        selected = {}
        for item in fields(cls):
            kind = str if item.name == "model_dir" else (int, float) if item.name in {"pcm_seconds", "clip_seconds"} else int
            default = str(defaults.model_dir) if item.name == "model_dir" else getattr(defaults, item.name)
            selected[item.name] = config_value(values, ("speech", item.name), default, kind=kind)
        return cls(**selected)


class ModelFactory:
    @classmethod
    def available(cls) -> bool:
        return importlib.util.find_spec("sherpa_onnx") is not None

    @classmethod
    def require_runtime(cls) -> None:
        try:
            importlib.import_module("sherpa_onnx")
        except (ImportError, OSError) as exc:
            raise SpeechUnavailable(f"本地语音运行库不可用；安装 RedLotus[speech]: {exc}") from exc

    @classmethod
    def implementation(cls, kind: ModelKind) -> type[SpeechModel]:
        if kind == ModelKind.ASR:
            from .asr import XASRModel
            return XASRModel
        if kind == ModelKind.TTS:
            from .tts import KokoroModel
            return KokoroModel
        raise ValueError("unknown speech model kind")

    @classmethod
    def create(cls, kind: ModelKind, root: Path, threads: int) -> SpeechModel:
        return cls.implementation(kind).load(root, threads)


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



__all__ = ["InputDevice", "PCMChunk", "Transcript", "AudioSegment", "SpeechSettings", "SpeechError", "SpeechUnavailable",
           "SpeechBusy", "NoSpeechDetected", "ModelFactory", "ModelKind", "ModelStage", "AudioFormat", "ModelSpec", "InstalledModel",
           "InstalledState", "PreparationStatus", "SpeechModel", "ASRModel", "TTSModel", "RecognitionSession"]
