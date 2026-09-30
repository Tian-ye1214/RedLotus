from test_speech_service import CatalogFixture, NativeFixture
import asyncio
import builtins
import hashlib
import importlib
import io
import json
import tempfile
import struct
import subprocess
import sys
import tarfile
from types import SimpleNamespace
from pathlib import Path

import numpy as np

import pytest

from redlotus.TTS import SpeechSettings, SpeechUnavailable, service
from redlotus.runtime import resources


@pytest.fixture
def forbid_audio_writes(monkeypatch):
    """Install after any explicit input fixture is created."""
    def install():
        original_path_open = Path.open
        original_open = builtins.open

        def path_open(path, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                pytest.fail("audio conversion attempted a disk write")
            return original_path_open(path, mode, *args, **kwargs)

        def mkstemp(*args, **kwargs):
            pytest.fail("audio conversion attempted a temporary file")

        def file_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                pytest.fail("audio conversion attempted a disk write")
            return original_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", path_open)
        monkeypatch.setattr(tempfile, "mkstemp", mkstemp)
        monkeypatch.setattr(builtins, "open", file_open)

    return install


class PipeFFmpeg:
    def __init__(self, pcm=b"", *, error=b"", code=0, blocked=False):
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        if not blocked:
            self.stdout.feed_data(pcm)
            self.stdout.feed_eof()
        self.stderr.feed_data(error)
        self.stderr.feed_eof()
        self.stdin = self.Input(self)
        self.code, self.returncode, self.terminated = code, None, False
        self.exit = asyncio.Event()
        self.waited = False

    class Input:
        def __init__(self, process):
            self.process, self.data = process, bytearray()

        def write(self, data):
            self.data.extend(data)

        async def drain(self):
            await asyncio.sleep(0)

        def close(self):
            self.process.exit.set()

    def terminate(self):
        self.terminated = True
        self.stdout.feed_eof()
        self.exit.set()

    async def wait(self):
        await self.exit.wait()
        self.waited = True
        self.returncode = -15 if self.terminated else self.code
        return self.returncode


def test_speech_defaults_are_public_and_do_not_write_configuration(tmp_path, monkeypatch):
    speech = importlib.import_module("redlotus.TTS")
    monkeypatch.setenv("REDLOTUS_CONFIG_DIR", str(tmp_path / "global"))
    values = {}
    options = speech.SpeechSettings.read(values)
    assert options.model_dir == (tmp_path / "global" / "model").resolve()
    assert (options.asr_threads, options.tts_threads, options.queue_size) == (2, 8, 8)
    assert (options.pcm_seconds, options.flush_ms, options.segment_chars, options.clip_seconds) == (2, 600, 120, 55)
    assert values == {}
    assert not options.model_dir.exists()


def test_speech_config_preserves_layered_field_priority(tmp_path, monkeypatch):
    from redlotus.runtime.config import ConfigValues
    from redlotus.TTS import SpeechSettings
    monkeypatch.setenv("REDLOTUS_CONFIG_DIR", str(tmp_path / "global"))
    values = ConfigValues(sources=({"speech": {"asr_threads": 3}}, {"speech": {"tts_threads": 4}}, {}))
    options = SpeechSettings.read(values)
    assert (options.asr_threads, options.tts_threads, options.queue_size) == (3, 4, 8)


@pytest.mark.parametrize("key,value", [("queue_size", 0), ("pcm_seconds", float("inf")), ("asr_threads", True), ("flush_ms", -1), ("pcm_seconds", 3), ("clip_seconds", 60), ("queue_size", 9), ("segment_chars", 121)])
def test_invalid_explicit_speech_options_do_not_silently_fall_back(key, value):
    from redlotus.TTS import SpeechSettings
    with pytest.raises((ValueError, RuntimeError)):
        SpeechSettings.read({"speech": {key: value}})


def test_import_speech_does_not_load_optional_runtimes_or_models():
    result = subprocess.run([sys.executable, "-c", "import sys; import redlotus.TTS; assert not any(n in sys.modules for n in ('sherpa_onnx', 'sounddevice', 'pysilk', 'soxr'))"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_ffmpeg_header_overrides_mime_and_streams_before_input_eof(monkeypatch, forbid_audio_writes):
    from redlotus.TTS.audio import AudioIO
    process = PipeFFmpeg(np.array([0, 16384], dtype="<i2").tobytes())
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def spawn(*args, **kwargs):
        calls.append(args)
        started.set()
        assert kwargs["stdin"] == asyncio.subprocess.PIPE
        return process

    async def source():
        yield b"fLaCexample!"
        await release.wait()

    monkeypatch.setattr("shutil.which", lambda name: "ffmpeg")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    forbid_audio_writes()
    decoded = AudioIO.parse_input(source(), format="audio/wav")
    first = await asyncio.wait_for(anext(decoded), 1)
    assert started.is_set() and not release.is_set()
    np.testing.assert_allclose(first.samples, [0, .5])
    release.set()
    assert [chunk async for chunk in decoded] == []
    assert process.stdin.data == b"fLaCexample!"
    assert calls[0][calls[0].index("-i") + 1] == "pipe:0"
    assert calls[0][-1] == "pipe:1"
    assert calls[0][calls[0].index("-f") + 1] == "s16le"


@pytest.mark.asyncio
async def test_ffmpeg_missing_or_failed_reports_bounded_error(monkeypatch, forbid_audio_writes):
    from redlotus.TTS import SpeechError
    from redlotus.TTS.audio import AudioIO
    monkeypatch.setattr("shutil.which", lambda name: None)
    forbid_audio_writes()
    with pytest.raises(SpeechUnavailable, match="FFmpeg"):
        _ = [chunk async for chunk in AudioIO.parse_input(b"ID3example")]

    process = PipeFFmpeg(error=b"old failure" * 1000 + b"invalid compressed audio", code=1)
    monkeypatch.setattr("shutil.which", lambda name: "ffmpeg")

    async def spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(SpeechError, match="invalid compressed audio") as failed:
        _ = [chunk async for chunk in AudioIO.parse_input(b"unknown", format="audio/ogg")]
    assert len(str(failed.value)) < 4200
    assert process.waited


@pytest.mark.asyncio
async def test_ffmpeg_uses_supported_path_suffix_only(tmp_path, monkeypatch, forbid_audio_writes):
    from redlotus.TTS.audio import AudioIO
    source = tmp_path / "voice.opus"
    source.write_bytes(b"compressed-data")
    process = PipeFFmpeg(np.array([8192], dtype="<i2").tobytes())
    calls = []

    async def spawn(*args, **kwargs):
        calls.append(args)
        return process

    monkeypatch.setattr("shutil.which", lambda name: "ffmpeg")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    forbid_audio_writes()
    chunks = [chunk async for chunk in AudioIO.parse_input(source)]
    np.testing.assert_allclose(chunks[0].samples, [.25])
    assert process.stdin.data == b"compressed-data"
    assert calls[0][calls[0].index("-i") + 1] == "pipe:0"
    with pytest.raises(SpeechUnavailable, match="unsupported audio format"):
        _ = [chunk async for chunk in AudioIO.parse_input(b"unknown", format="audio/webm")]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_ffmpeg_cancel_terminates_and_waits(monkeypatch, forbid_audio_writes):
    from redlotus.TTS.audio import AudioIO
    process = PipeFFmpeg(blocked=True)
    started = asyncio.Event()

    async def spawn(*args, **kwargs):
        started.set()
        return process

    monkeypatch.setattr("shutil.which", lambda name: "ffmpeg")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    forbid_audio_writes()

    async def consume():
        return [chunk async for chunk in AudioIO.parse_input(b"ID3example", format="audio/opus")]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.terminated and process.waited


@pytest.mark.asyncio
@pytest.mark.parametrize("rate", [1, 192001])
async def test_wav_rejects_unreasonable_sample_rate_before_resampling(rate, monkeypatch):
    from redlotus.TTS import SpeechError
    from redlotus.TTS.audio import AudioIO

    def unexpected_resampler(*args, **kwargs):
        pytest.fail("untrusted WAV header reached the resampler")

    monkeypatch.setitem(sys.modules, "soxr", SimpleNamespace(ResampleStream=unexpected_resampler))
    fmt = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)
    wav = b"RIFF" + struct.pack("<I", 38) + b"WAVEfmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", 2) + b"\0\0"
    with pytest.raises(SpeechError, match="sample rate"):
        _ = [chunk async for chunk in AudioIO.parse_input(wav)]


@pytest.mark.asyncio
async def test_bare_audio_l16_is_not_decoded_as_little_endian_pcm():
    from redlotus.TTS import SpeechUnavailable
    from redlotus.TTS.audio import AudioIO

    with pytest.raises(SpeechUnavailable, match="unsupported audio format"):
        _ = [chunk async for chunk in AudioIO.parse_input(b"\x40\x00", format="audio/L16", sample_rate=16000)]
    fmt = struct.pack("<HHIIHH", 1, 1, 16000, 32000, 2, 16)
    wav = b"RIFF" + struct.pack("<I", 38) + b"WAVEfmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", 2) + b"\0\x40"
    chunks = [chunk async for chunk in AudioIO.parse_input(wav, format="audio/L16")]
    np.testing.assert_allclose(np.concatenate([chunk.samples for chunk in chunks]), [.5])


@pytest.mark.asyncio
async def test_new_voice_output_waits_for_previous_device_stop(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from redlotus.sessions.control import SessionController
    from redlotus.TTS import service
    from redlotus.runtime import resources
    stop_started, stopped, playing, end_play = (asyncio.Event() for _ in range(4))
    state = SessionController()
    async def stop():
        stop_started.set()
        await stopped.wait()
    async def play(_pcm):
        playing.set()
        await end_play.wait()
    monkeypatch.setattr(resources, "runtime_dir", lambda *_: tmp_path)
    monkeypatch.setattr(service.SpeechService, "shared", lambda: SimpleNamespace(config=SimpleNamespace(queue_size=8)))
    state.voice_stop, state.voice_output, state.voice_enabled = stop, play, True
    state.stop_voice()
    reply = state.begin_voice(None, is_current=lambda: True)
    try:
        await asyncio.wait_for(stop_started.wait(), 1)
        await asyncio.sleep(.02)
        assert not playing.is_set()
        stopped.set()
        await asyncio.wait_for(playing.wait(), 1)
    finally:
        stopped.set()
        end_play.set()
        await state.drain_voice()
        assert reply.task.done()


def recovery_archive(tmp_path, monkeypatch, root="tiny-asr"):
    source = tmp_path / f"{root}.tar.bz2"
    with tarfile.open(source, "w:bz2") as tar:
        for name in ("model.bin", "tokens.txt"):
            data = name.encode()
            member = tarfile.TarInfo(f"{root}/{name}")
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    spec = {"archive": source.name, "url": f"https://example.test/{source.name}",
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "root": root,
            "required": ["model.bin", "tokens.txt"], "archive_limit": 100000, "unpack_limit": 100000}
    CatalogFixture.install(monkeypatch, spec)
    return source, spec


@pytest.fixture
def recovery_speech(tmp_path, monkeypatch):
    monkeypatch.setattr(service.ModelFactory, "require_runtime", lambda: None)
    return service.SpeechService(SpeechSettings(model_dir=tmp_path / "model"))


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [None, "foreign"])
async def test_unknown_partial_is_preserved_and_prepare_reports_conflict(tmp_path, monkeypatch, recovery_speech, metadata):
    _, spec = recovery_archive(tmp_path, monkeypatch)
    part, meta = recovery_speech._download_paths(service.ModelSpec.from_dict("asr", spec))
    part.parent.mkdir(parents=True)
    part.write_bytes(b"foreign bytes")
    if metadata:
        meta.write_text(json.dumps({"sha256": "foreign", "url": spec["url"]}))
    monkeypatch.setattr(service.httpx, "Client", lambda **kwargs: pytest.fail("foreign partial must not start network"))
    with pytest.raises(SpeechUnavailable, match="冲突"):
        await recovery_speech.prepare("asr")
    await recovery_speech.clean()
    assert part.read_bytes() == b"foreign bytes"
    assert meta.exists() is bool(metadata)
    await recovery_speech.close()


@pytest.mark.asyncio
async def test_clean_reclaims_owned_compatible_partial(tmp_path, monkeypatch, recovery_speech):
    _, old = recovery_archive(tmp_path, monkeypatch, root="compatible-old")
    _, current = recovery_archive(tmp_path, monkeypatch, root="recommended-new")
    current["compatible"] = [old]
    part, meta = recovery_speech._download_paths(service.ModelSpec.from_dict("asr", old))
    part.parent.mkdir(parents=True)
    part.write_bytes(b"partial")
    meta.write_text(json.dumps({"sha256": old["sha256"], "url": old["url"]}))
    await recovery_speech.clean()
    assert not part.exists() and not meta.exists()
    await recovery_speech.close()


@pytest.mark.asyncio
async def test_clean_recovers_stage_after_installed_marker_write(tmp_path, monkeypatch, recovery_speech):
    source, spec = recovery_archive(tmp_path, monkeypatch)
    actual_replace = resources.replace_retry
    def stop_before_publish(first, second):
        if service.Path(first).is_dir():
            raise RuntimeError("simulated process crash before publish")
        return actual_replace(first, second)
    with monkeypatch.context() as patch:
        patch.setattr(resources, "replace_retry", stop_before_publish)
        patch.setattr(service.shutil, "rmtree", lambda path: None)
        with pytest.raises(RuntimeError, match="simulated process crash"):
            await recovery_speech.prepare("asr", source)
    stage = next((recovery_speech.root / ".staging").iterdir())
    assert (stage / spec["root"] / ".redlotus-installed.json").is_file()
    await recovery_speech.clean()
    assert not stage.exists()
    await recovery_speech.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [".staging", ".downloads", ".locks"])
async def test_clean_rejects_reparse_managed_directory(tmp_path, monkeypatch, recovery_speech, name):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "private.txt"
    sentinel.write_text("keep")
    link = recovery_speech.root / name
    link.mkdir(parents=True)
    ordinary = resources._ordinary_storage_path
    monkeypatch.setattr(resources, "_ordinary_storage_path", lambda path: False if path == link else ordinary(path))
    try:
        with pytest.raises(SpeechUnavailable, match="普通目录"):
            await recovery_speech.clean()
        assert sentinel.read_text() == "keep"
    finally:
        await recovery_speech.close()


@pytest.mark.asyncio
async def test_clean_does_not_read_reparse_stage_marker(tmp_path, monkeypatch, recovery_speech):
    stage = recovery_speech.root / ".staging" / "prepare-foreign"
    stage.mkdir(parents=True)
    marker = stage / ".redlotus-stage.json"
    marker.write_text('{"schema": 1}')
    ordinary, read_text = resources._ordinary_storage_path, service.Path.read_text
    monkeypatch.setattr(resources, "_ordinary_storage_path", lambda path: False if path == stage else ordinary(path))
    def guarded_read(path, *args, **kwargs):
        if path == marker:
            pytest.fail("reparse stage marker was read")
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(service.Path, "read_text", guarded_read)
    await recovery_speech.clean()
    assert stage.exists()
    await recovery_speech.close()
