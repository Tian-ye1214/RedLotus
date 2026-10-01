"""Native speech models, verified package resources, and bounded PCM inference."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import queue
import re
import threading
from contextlib import suppress
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import AsyncIterator, Callable, Literal, Self

import numpy as np

from redlotus.runtime.resources import finish_io, file_sha256, FramedProcess
from . import (ASRModel, FileFingerprint, FloatSamples, ModelKind, PCMChunk, RecognitionSession,
               FeatureCondition, SpeakerCondition, SpeechError, SpeechUnavailable, SynthesisRequest,
               TTSBackend, TTSModel, VoiceCondition, VoiceProfile)

_INPUT_RATE = 16000
_OUTPUT_RATE = 24000
_TAIL_SAMPLES = 19200
_NATIVE_PATH_ROLES = frozenset({"model", "voices", "tokens", "data_dir", "lexicon", "gpt_encoder", "gpt_step", "sovits", "bert", "config"})

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
        if rate != _INPUT_RATE:
            try:
                import soxr
            except ImportError as exc:
                raise SpeechUnavailable("soxr is required for audio resampling") from exc
            self.resampler = soxr.ResampleStream(rate, _INPUT_RATE, 1, dtype="float32")

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
            return PCMChunk(np.asarray(samples, dtype=np.float32), _INPUT_RATE)

    def finish(self) -> PCMChunk | None:
        if self.pending:
            raise SpeechError("truncated PCM frame")
        if self.frames == 0:
            raise SpeechError("empty audio input")
        if self.resampler is not None:
            tail = self.resampler.resample_chunk(np.empty(0, dtype=np.float32), last=True)
            if tail.size:
                return PCMChunk(np.asarray(tail, dtype=np.float32), _INPUT_RATE)


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
            num_threads=threads, sample_rate=_INPUT_RATE, feature_dim=80,
            model_type="zipformer2", provider="cpu", enable_endpoint_detection=True,
        )
        return cls(native)

    def warmup(self) -> None:
        session = self.create_session()
        try:
            session.accept(np.zeros(_INPUT_RATE, dtype=np.float32))
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
        for part in filter(None, map(str.strip, (*self._segments, current))):
            if (output and output[-1].isascii() and part[0].isascii()
                    and (output[-1].isalnum() or output[-1] in ".?!") and part[0].isalnum()):
                output += " "
            output += part
        return output

    def _decode(self) -> str:
        while self._native.is_ready(self._stream):
            self._native.decode_stream(self._stream)
            if self._native.is_endpoint(self._stream):
                if segment := self._result_text():
                    self._segments.append(segment)
                self._native.reset(self._stream)
        return self._join(self._result_text())

    def accept(self, samples: FloatSamples) -> str | None:
        self._stream.accept_waveform(_INPUT_RATE, samples)
        full = self._decode()
        if full != self._preview:
            self._preview = full
            return full
        return None

    def finish(self) -> str:
        self._stream.accept_waveform(_INPUT_RATE, np.zeros(_TAIL_SAMPLES, dtype=np.float32))
        self._stream.input_finished()
        return self._decode()

    def close(self) -> None:
        self._stream = None
        self._native = None


@dataclass(frozen=True)
class TTSBundle:
    root: Path
    version: str
    backend: TTSBackend
    family: str
    base_model_id: str
    sample_rate: int
    native_model_files: dict[str, tuple[str, ...]]
    rule_fsts: tuple[str, ...]
    profile: VoiceProfile
    resources: dict[str, FileFingerprint] | None

    @staticmethod
    def _path(root: Path, name: str) -> Path:
        from redlotus.runtime.resources import owned_path

        if not isinstance(name, str) or not name or "\\" in name or ":" in name or name.startswith("/"):
            raise SpeechUnavailable("模型资源路径无效")
        try:
            return owned_path(root, name)
        except ValueError as exc:
            raise SpeechUnavailable("模型资源路径越界") from exc

    @classmethod
    def read(cls, root: Path) -> Self:
        root = Path(root)
        if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
            raise SpeechUnavailable("模型目录不能是链接")
        root = root.resolve()
        manifest = root / "model.json"
        if manifest.is_file():
            if manifest.is_symlink() or manifest.stat().st_size > 1024 * 1024:
                raise SpeechUnavailable("模型清单不是普通小文件")
            raw = manifest.read_bytes()
            try:
                row = json.loads(raw)
            except (UnicodeDecodeError, ValueError) as exc:
                raise SpeechUnavailable("模型清单无效") from exc
            version = hashlib.sha256(raw).hexdigest()
            fingerprints = row.get("resources") if isinstance(row, dict) else None
            if not isinstance(fingerprints, dict) or not fingerprints:
                raise SpeechUnavailable("模型清单缺少资源校验值")
        else:
            catalog = json.loads(Path(__file__).with_name("catalog.json").read_text(encoding="utf-8"))["models"]["tts"]
            rows = (catalog, *catalog.get("compatible", []))
            match = next((item for item in rows if root.name == f"{item['root']}-{item['sha256'][:12]}"), None)
            if match is None or not isinstance(match.get("runtime"), dict):
                raise SpeechUnavailable("模型目录缺少经过验证的运行清单")
            row = match["runtime"]
            version = root.name
            fingerprints = None
        if not isinstance(row, dict) or row.get("schema") != 1 or row.get("kind") != "tts" or row.get("runtime") not in TTSBackend:
            raise SpeechUnavailable("模型清单 schema 或运行时无效")
        backend = TTSBackend(row["runtime"])
        family = row.get("native_family")
        base_model_id = row.get("base_model_id")
        if (not isinstance(family, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", family)
                or not isinstance(base_model_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", base_model_id)
                or row.get("sample_rate") != (32000 if backend == TTSBackend.MAMBO else 24000)):
            raise SpeechUnavailable("模型身份或采样率无效")
        files = row.get("native_model_files")
        if not isinstance(files, dict) or not files or any(role not in _NATIVE_PATH_ROLES for role in files):
            raise SpeechUnavailable("模型原生资源角色无效")
        native_files = {}
        for role, value in files.items():
            names = (value,) if isinstance(value, str) else tuple(value) if isinstance(value, list) and value else ()
            if not names or any(not isinstance(name, str) for name in names):
                raise SpeechUnavailable("模型原生资源字段无效")
            native_files[role] = names
        rules = row.get("rule_fsts", [])
        if not isinstance(rules, list) or any(not isinstance(name, str) for name in rules):
            raise SpeechUnavailable("模型规则文件无效")
        generation, voice = row.get("generation"), row.get("voice")
        if not isinstance(generation, dict) or not isinstance(voice, dict):
            raise SpeechUnavailable("模型音色定义无效")
        mode, voice_id = generation.get("mode"), voice.get("id")
        if mode not in {"speaker", "features"} or not isinstance(voice_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", voice_id):
            raise SpeechUnavailable("模型音色模式无效")
        if mode == "speaker":
            default_sid, latin_sid = voice.get("default_sid"), voice.get("latin_sid")
            words = voice.get("english_number_words", False)
            if type(default_sid) is not int or default_sid < 0 or type(latin_sid) is not int or latin_sid < 0 or type(words) is not bool:
                raise SpeechUnavailable("模型说话人配置无效")
            profile = VoiceProfile(base_model_id, voice_id, mode, default_sid, latin_sid, words)
        else:
            profile = VoiceProfile(base_model_id, voice_id, mode)
        if backend == TTSBackend.MAMBO:
            layout = {key: (name,) for key, name in {
                "gpt_encoder": "gpt_encoder.onnx", "gpt_step": "gpt_step.onnx", "sovits": "sovits.onnx",
                "bert": "bert.onnx", "config": "config.json", "voices": "mambo.gsppkg", "data_dir": "frontend"}.items()}
            if native_files != layout or family != "gpt_sovits_v2" or mode != "features" or rules or row.get("cache_layout") != "growing":
                raise SpeechUnavailable("曼波模型包接口或资源布局不兼容")
        elif family != "kokoro" or mode != "speaker":
            raise SpeechUnavailable("当前运行库不支持此模型；已退役的模型包请更换后重启")
        required = [name for group in native_files.values() for name in group] + list(rules)
        for name in required:
            cls._path(root, name)
        resources = None
        if fingerprints is not None:
            resources = {}
            for name, item in fingerprints.items():
                cls._path(root, name)
                if (not isinstance(item, dict) or type(item.get("size")) is not int or item["size"] <= 0
                        or not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
                    raise SpeechUnavailable("模型资源校验值无效")
                resources[name] = FileFingerprint(item["size"], item["sha256"])
            if any(not (any(item.startswith(name + "/") for item in resources) if name in native_files.get("data_dir", ())
                        else name in resources) for name in required):
                raise SpeechUnavailable("模型原生资源未列入校验清单")
        return cls(root, version, backend, family, base_model_id, row["sample_rate"], native_files, tuple(rules), profile, resources)

    def verify(self) -> Self:
        data_dirs = tuple(self._path(self.root, name) for name in self.native_model_files.get("data_dir", ()))
        for path in chain((self.root,), self.root.rglob("*")):
            if path.is_symlink() or path.is_junction() or path.suffix.lower() in {
                    ".exe", ".dll", ".pyd", ".so", ".dylib", ".py", ".pyc", ".ps1", ".bat", ".cmd", ".sh"}:
                raise SpeechUnavailable("模型资源不能是链接或可执行文件")
            if (self.resources is not None and path.is_file() and path.relative_to(self.root).as_posix() not in self.resources
                    and (path.suffix.lower() == ".onnx" or any(path.is_relative_to(directory) for directory in data_dirs))):
                raise SpeechUnavailable("模型含未列入校验清单的资源")
        required = [(name, role == "data_dir") for role, names in self.native_model_files.items() for name in names]
        required.extend((name, False) for name in self.rule_fsts)
        for name, directory in required:
            path = self._path(self.root, name)
            if not (path.is_dir() if directory else path.is_file()):
                raise SpeechUnavailable(f"模型资源缺失或不是普通文件: {name}")
        for name, fingerprint in (self.resources or {}).items():
            path = self._path(self.root, name)
            if not path.is_file() or path.stat().st_size != fingerprint.size or file_sha256(path) != fingerprint.sha256:
                raise SpeechUnavailable(f"模型资源校验失败: {name}")
        return self


class KokoroModel(TTSModel):
    """One sherpa-onnx TTS adapter configured by a passive model bundle."""

    _phonemizer_source: tuple[str, Path] | None = None
    _phonemizer_lock = threading.Lock()

    def __init__(self, native, bundle: TTSBundle, cwd_anchor: Path | None = None):
        self._native = native
        self.bundle = bundle
        self.profile = bundle.profile
        self._cwd_anchor = cwd_anchor

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
        buffer = ctypes.create_unicode_buffer(32768)
        used = get_short(spelling, buffer, len(buffer))
        if 0 < used < len(buffer) and buffer.value.isascii():
            return buffer.value
        return None

    @staticmethod
    def _check_cwd(anchor: Path | None) -> None:
        if anchor is not None and Path.cwd().resolve() != anchor:
            raise SpeechUnavailable("语音模型相对资源路径的工作目录已变化；请重启应用")

    @classmethod
    def load(cls, root: Path, threads: int) -> KokoroModel:
        with cls._phonemizer_lock:
            return cls._load_locked(root, threads)

    @classmethod
    def _load_locked(cls, root: Path, threads: int) -> KokoroModel:
        bundle = TTSBundle.read(root)
        cwd_anchor = Path.cwd().resolve()
        relative_paths = False

        def native_path(name: str, *, directory: bool = False) -> str:
            nonlocal relative_paths
            path = bundle._path(bundle.root, name)
            if not (path.is_dir() if directory else path.is_file()):
                raise SpeechUnavailable(f"模型原生资源缺失: {name}")
            absolute = cls._ascii_absolute(path)
            if absolute is not None and "," not in absolute:
                return absolute
            try:
                relative = path.resolve().relative_to(cwd_anchor).as_posix()
            except ValueError:
                relative = ""
            if not relative or not relative.isascii() or "," in relative:
                raise SpeechUnavailable("模型资源需要当前工作目录下的英文路径；请更换模型包目录并重启应用")
            relative_paths = True
            return relative

        try:
            import sherpa_onnx
            if bundle.profile.english_number_words:
                import num2words  # Validate the normalizer before native construction.
        except ImportError as exc:
            raise SpeechUnavailable("请安装 RedLotus[speech] 以使用本地语音合成") from exc
        paths = {role: ",".join(native_path(name, directory=role == "data_dir") for name in names)
                 for role, names in bundle.native_model_files.items()}
        model = sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(**paths), num_threads=threads, provider="cpu")
        rules = [native_path(name) for name in bundle.rule_fsts]
        config = sherpa_onnx.OfflineTtsConfig(model=model, max_num_sentences=1, rule_fsts=",".join(rules))
        if not config.validate():
            raise SpeechUnavailable("语音模型配置无效")

        frontend = [name for role in ("tokens", "lexicon", "data_dir")
                    for name in bundle.native_model_files.get(role, ())] + list(bundle.rule_fsts)
        paths = [bundle._path(bundle.root, name) for name in frontend]
        sources = [child for path in paths for child in (path.rglob("*") if path.is_dir() else (path,))]
        entries = (f"{source.relative_to(bundle.root).as_posix()}\0{file_sha256(source)}"
                   for source in sorted(sources, key=lambda path: path.relative_to(bundle.root).as_posix())
                   if source.is_file())
        fingerprint = hashlib.sha256("".join(entries).encode("utf-8")).hexdigest()
        pinned = cls._phonemizer_source
        if pinned is not None and (fingerprint, bundle.root) != pinned:
            raise SpeechUnavailable("语音词典已变更；请重启应用后更新模型")
        cls._check_cwd(cwd_anchor if relative_paths else None)
        cls._phonemizer_source = (fingerprint, bundle.root)
        native = sherpa_onnx.OfflineTts(config)
        cls._check_cwd(cwd_anchor if relative_paths else None)
        return cls(native, bundle, cwd_anchor if relative_paths else None)

    def warmup(self) -> None:
        condition = self.prepare_voice(self.profile)
        for text in ("你好。", "Hello, version 3.14 is ready."):
            warm = self.generate(SynthesisRequest(text, condition))
            if (warm.sample_rate != _OUTPUT_RATE or warm.samples.ndim != 1
                    or not len(warm.samples) or not np.isfinite(warm.samples).all()):
                raise SpeechUnavailable("语音模型预热未产生 24 kHz 音频")

    def prepare_voice(self, profile: VoiceProfile) -> VoiceCondition:
        if profile.model_id != self.bundle.base_model_id or profile.mode != "speaker":
            raise SpeechUnavailable("音色与基础模型不兼容")
        return SpeakerCondition(profile.model_id, profile.default_sid, profile.latin_sid,
                                profile.english_number_words, profile.default_sid)

    def generate(self, request: SynthesisRequest,
                 callback: Callable[[FloatSamples, float], int] | None = None) -> PCMChunk:
        self._check_cwd(self._cwd_anchor)
        import sherpa_onnx

        condition = request.voice
        if not isinstance(condition, SpeakerCondition) or condition.model_id != self.bundle.base_model_id:
            raise SpeechUnavailable("音色与基础模型不兼容")
        config = sherpa_onnx.GenerationConfig()
        from .tts import SpeakerTextFrontend
        text, config.sid = SpeakerTextFrontend.spoken_segment(request.text, condition)
        condition.previous_sid = config.sid
        audio = self._native.generate(text, config, callback=callback)
        return PCMChunk(np.asarray(audio.samples, dtype=np.float32), audio.sample_rate)

    def close(self) -> None:
        self._native = None


class MamboTTSModel(TTSModel):
    """One persistent native CPU worker; its frontend dies with the model."""

    sample_rate = 32000

    def __init__(self, worker, bundle: TTSBundle):
        self._worker = worker
        self.profile = bundle.profile

    @classmethod
    def load(cls, root: Path, threads: int) -> MamboTTSModel:
        import sherpa_onnx
        executable = Path(__file__).with_name("native") / "redlotus_mambo.exe"
        if os.name != "nt" or not executable.is_file():
            raise SpeechUnavailable("当前安装未包含曼波 CPU 运行组件；请安装支持曼波的 Windows 版本")
        runtime = Path(sherpa_onnx.__file__).parent / "lib" / "onnxruntime.dll"
        environment = {**os.environ, "REDLOTUS_MAMBO_ORT": str(runtime.resolve()), "REDLOTUS_MAMBO_ROOT": str(root.resolve())}
        bundle = TTSBundle.read(root)
        model = cls(FramedProcess([str(executable), str(threads)], executable.parent, environment), bundle)
        try:
            ready = model._worker.receive_header(8192)
            if ready.get("status") == "error":
                raise SpeechError(f"曼波合成失败: {ready.get('message', '未知错误')}")
            if (ready.get("status") != "ready" or ready.get("protocol") != 2 or ready.get("sample_rate") != 32000
                    or not Path(ready.get("runtime", "")).samefile(runtime)):
                raise SpeechUnavailable("曼波运行组件协议或 ONNX Runtime 不兼容")
            return model
        except BaseException:
            model.close()
            raise

    def warmup(self) -> None:
        self.generate(SynthesisRequest("你好。Hello.", self.prepare_voice(self.profile)))

    def prepare_voice(self, profile: VoiceProfile) -> FeatureCondition:
        if profile != self.profile or profile.mode != "features":
            raise SpeechUnavailable("音色与基础模型不兼容")
        return FeatureCondition(profile.model_id, profile.voice_id)

    def generate(self, request: SynthesisRequest, callback=None) -> PCMChunk | None:
        if request.voice != self.prepare_voice(self.profile) or not 0 < len(request.text) <= 120 or "\0" in request.text:
            raise SpeechError("曼波音色条件或文本片段无效")
        self._worker.send(request.text.encode("utf-8"))
        collected = []
        try:
            with self._worker.responses(32000 * 2 * 4, 32000 * 55 * 4) as blocks:
                for block in blocks:
                    if not block:
                        return PCMChunk(np.empty(0, dtype=np.float32), self.sample_rate, True)
                    chunk = PCMChunk.from_bytes(block, self.sample_rate)
                    if callback is None:
                        collected.append(chunk.samples)
                    elif not callback(chunk.samples, 0.0):
                        break
        except (RuntimeError, ValueError) as exc:
            raise SpeechError(f"曼波合成失败: {exc}") from exc
        return PCMChunk(np.concatenate(collected), self.sample_rate) if collected else None

    def close(self) -> None:
        self._worker.close()


class _SegmentStream:
    def __init__(self, service):
        self.service = service
        self.window = max(1, min(2400, int(service.config.pcm_seconds * _OUTPUT_RATE / 2)))
        capacity = max(1, int(service.config.pcm_seconds * _OUTPUT_RATE) // self.window - 1)
        self.pending_pcm: queue.Queue[PCMChunk] = queue.Queue(maxsize=capacity)
        self.stopped = threading.Event()
        self.ready = asyncio.Event()
        self.loop = asyncio.get_running_loop()

    def _generate(self, model: TTSModel, condition: VoiceCondition, segment: str) -> None:
        native_rate = model.sample_rate
        received = False
        previous = None
        resampler = None
        if native_rate != _OUTPUT_RATE:
            import soxr
            resampler = soxr.ResampleStream(native_rate, _OUTPUT_RATE, 1, dtype="float32")

        def put(samples, final=False):
            while not self.stopped.is_set():
                try:
                    self.pending_pcm.put(PCMChunk(samples, _OUTPUT_RATE, final), timeout=.05)
                    self.loop.call_soon_threadsafe(self.ready.set)
                    return True
                except queue.Full:
                    continue
            return False

        def queue_windows(samples):
            nonlocal previous
            for start in range(0, len(samples), self.window):
                if self.stopped.is_set() or previous is not None and not put(previous):
                    return False
                previous = samples[start:start + self.window].copy()
            return True

        def callback(samples, _progress):
            nonlocal received
            samples = np.asarray(samples, dtype=np.float32)
            if samples.ndim != 1 or not np.all(np.isfinite(samples)):
                raise SpeechError("语音模型未产生有效单声道音频")
            received |= bool(len(samples))
            return int(queue_windows(resampler.resample_chunk(samples) if resampler else samples))

        if self.stopped.is_set():
            return
        audio = model.generate(SynthesisRequest(segment, condition), callback=callback)
        if self.stopped.is_set():
            return
        if not received:
            if audio is not None and audio.sample_rate == native_rate and audio.end_of_segment and not len(audio.samples):
                return
            if audio is None or audio.sample_rate != native_rate or not len(audio.samples):
                raise SpeechError("语音模型未产生有效单声道音频")
            callback(audio.samples, 1.0)
        if resampler is not None:
            queue_windows(resampler.resample_chunk(np.empty(0, dtype=np.float32), last=True))
        if previous is not None:
            put(previous, True)

    async def _produce(self, segments):
        async with self.service.acquire(ModelKind.TTS) as model:
            condition = await self.service.run(ModelKind.TTS, model.prepare_voice, model.profile)
            async for segment in segments:
                await self.service.run(ModelKind.TTS, self._generate, model, condition, segment)

    async def stream(self, segments) -> AsyncIterator[PCMChunk]:
        worker = asyncio.create_task(self._produce(segments))
        worker.add_done_callback(lambda _: self.ready.set())
        try:
            while not worker.done() or not self.pending_pcm.empty():
                try:
                    yield self.pending_pcm.get_nowait()
                except queue.Empty:
                    self.ready.clear()
                    if not worker.done() and self.pending_pcm.empty():
                        await self.ready.wait()
            await worker
        finally:
            self.stopped.set()
            if not worker.done():
                worker.cancel()
            try:
                await finish_io(asyncio.wait({worker}))
            finally:
                if worker.done():
                    with suppress(asyncio.CancelledError):
                        worker.exception()
