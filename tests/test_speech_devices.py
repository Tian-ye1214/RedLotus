from redlotus.TTS import inference
"""Device preflight regressions; these tests never open native audio streams."""

import asyncio
import sys
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import numpy as np
import pytest

from redlotus.TTS import PCMChunk, SpeechUnavailable
import redlotus.TTS as asr
from redlotus.TTS import audio
from redlotus.TTS.audio import AudioCapture
from redlotus.TTS.audio import AudioIO, AudioPlayer


MICROPHONE_MESSAGE = (
    "暂时无法使用麦克风，请检查系统默认输入设备和权限。文字输入和语音回复仍可使用。"
)


@pytest.mark.asyncio
async def test_ffmpeg_uses_pipes_and_terminates_when_reader_closes(monkeypatch):
    fed = asyncio.Event()
    release = asyncio.Event()

    class Input:
        def write(self, data):
            fed.set()

        async def drain(self):
            pass

        def close(self):
            pass

    class Output:
        first = True

        async def read(self, size):
            if self.first:
                self.first = False
                return np.array([0, 16384], dtype="<i2").tobytes()
            await release.wait()
            return b""

    class Error:
        async def read(self, size):
            return b""

    class Process:
        def __init__(self):
            self.stdin, self.stdout, self.stderr = Input(), Output(), Error()
            self.returncode = None
            self.terminated = False
            self.waited = False

        def terminate(self):
            self.terminated = True

        async def wait(self):
            self.waited = True
            self.returncode = -1 if self.terminated else 0
            return self.returncode

    process = Process()

    async def spawn(*args, **kwargs):
        assert "pipe:0" in args and "pipe:1" in args
        assert kwargs["stdin"] == asyncio.subprocess.PIPE
        return process

    async def source():
        yield b"ID3xxxxxxxxxxxx"
        await release.wait()

    monkeypatch.setattr(audio.shutil, "which", lambda name: "ffmpeg")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    decoded = AudioIO.parse_input(source())
    first = await asyncio.wait_for(anext(decoded), 1)
    np.testing.assert_allclose(first.samples, [0, .5])
    await asyncio.wait_for(fed.wait(), 1)
    await decoded.aclose()
    assert process.terminated and process.waited


@pytest.mark.asyncio
async def test_capture_preflight_rechecks_current_default_without_opening_stream(monkeypatch):
    main_thread = threading.get_ident()
    selected = {"index": 7, "available": False}
    calls = []

    def query_devices(*, kind):
        calls.append(("query", threading.get_ident(), kind))
        if not selected["available"]:
            raise RuntimeError("default input unplugged")
        return {"index": selected["index"], "name": "Mic", "hostapi": 0, "max_input_channels": 1}

    def check_input_settings(**kwargs):
        calls.append(("format", threading.get_ident(), kwargs))

    def forbidden_stream(**kwargs):
        raise AssertionError("preflight opened a microphone stream")

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(
        query_devices=query_devices, query_hostapis=lambda index: {"name": "WASAPI"},
        check_input_settings=check_input_settings,
        InputStream=forbidden_stream,
    ))
    capture = AudioCapture()
    with pytest.raises(SpeechUnavailable, match=MICROPHONE_MESSAGE) as failed:
        await capture.check_available()
    assert isinstance(failed.value.__cause__, RuntimeError)
    assert str(failed.value.__cause__) == "default input unplugged"
    selected["available"] = True
    await capture.check_available()
    assert [call[0] for call in calls] == ["query", "query", "format"]
    assert all(call[1] != main_thread for call in calls)
    assert calls[-1][2] == {"device": 7, "channels": 1, "dtype": "float32", "samplerate": 16000}


@pytest.mark.asyncio
async def test_capture_preflight_rejects_unsupported_input_format(monkeypatch):
    native_error = RuntimeError("invalid sample rate")

    def check_input_settings(**kwargs):
        raise native_error

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(
        query_devices=lambda *, kind: {"index": 4, "name": "Mic", "hostapi": 0, "max_input_channels": 1},
        query_hostapis=lambda index: {"name": "WASAPI"},
        check_input_settings=check_input_settings,
        InputStream=lambda **kwargs: pytest.fail("preflight opened a stream"),
    ))
    with pytest.raises(SpeechUnavailable, match=MICROPHONE_MESSAGE) as failed:
        await AudioCapture().check_available()
    assert failed.value.__cause__ is native_error


@pytest.mark.asyncio
async def test_microphone_disappearing_after_preflight_reports_friendly_error(monkeypatch):
    native_error = RuntimeError("PortAudio input vanished")
    logged = []

    def unavailable_stream(**kwargs):
        raise native_error

    mic = {"index": 1, "name": "Mic", "hostapi": 0, "max_input_channels": 1}
    api = {"name": "WASAPI"}
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(
        InputStream=unavailable_stream,
        query_devices=lambda device=None, *, kind=None: mic if kind == "input" else [
            {"name": "Output", "hostapi": 0, "max_input_channels": 0}, mic,
        ],
        query_hostapis=lambda index=None: api if index is not None else [api],
        check_input_settings=lambda **kwargs: None,
    ))
    from redlotus.runtime import logging as logger
    monkeypatch.setattr(logger, "error", lambda *args, **kwargs: logged.append((args, kwargs)))
    capture = AudioCapture()
    with pytest.raises(SpeechUnavailable, match=MICROPHONE_MESSAGE) as failed:
        await capture.start()
    assert failed.value.__cause__ is native_error
    assert capture._stream is None
    assert logged == []


@pytest.mark.asyncio
async def test_record_audio_checks_device_before_recognition_or_evidence(tmp_path, monkeypatch):
    events = []
    native_error = RuntimeError("PortAudio: no default input")

    class Capture:
        async def check_available(self):
            events.append("check")
            raise SpeechUnavailable(MICROPHONE_MESSAGE) from native_error

        async def start(self):
            pytest.fail("recording started before device check")

        async def close(self):
            events.append("close")

    evidence = tmp_path / "evidence"
    with pytest.raises(SpeechUnavailable, match=MICROPHONE_MESSAGE) as failed:
        await asr.StreamingRecognizer(service=object()).record(Capture(), lambda _: None)
    assert failed.value.__cause__ is native_error
    assert events == ["check", "close"]
    assert not evidence.exists()


@pytest.mark.asyncio
async def test_cancelled_preflight_drains_native_query_without_starting(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def query_devices(*, kind):
        entered.set()
        release.wait(2)
        return {"index": 1, "max_input_channels": 1}

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(
        query_devices=query_devices,
        check_input_settings=lambda **kwargs: None,
        InputStream=lambda **kwargs: pytest.fail("cancelled preflight opened a stream"),
    ))
    task = asyncio.create_task(AudioCapture().check_available())
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 2)
    task.cancel()
    try:
        await asyncio.sleep(.02)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["open", "write"])
async def test_playback_native_failure_has_friendly_message_and_cause(monkeypatch, failure_point):
    native_error = RuntimeError("PortAudio output failure")
    streams = []

    class OutputStream:
        def __init__(self, **kwargs):
            if failure_point == "open":
                raise native_error
            streams.append(self)
            self.closed = False

        def start(self):
            pass

        def write(self, samples):
            raise native_error

        def abort(self):
            pass

        def close(self):
            self.closed = True

    async def pcm():
        import numpy as np
        from redlotus.TTS import PCMChunk
        yield PCMChunk(np.zeros(100, dtype=np.float32), 24000)

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(OutputStream=OutputStream))
    with pytest.raises(SpeechUnavailable, match="暂时无法使用扬声器") as failed:
        await AudioPlayer().play(pcm())
    assert failed.value.__cause__ is native_error
    assert all(stream.closed for stream in streams)


def test_asr_loader_announces_native_warmup_before_warming(tmp_path, monkeypatch):
    events = []
    for name in ("encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx", "tokens.txt"):
        (tmp_path / name).write_bytes(b"test")

    class Stream:
        def accept_waveform(self, sample_rate, samples):
            events.append("accept_waveform")

        def input_finished(self):
            events.append("input_finished")

    class Recognizer:
        @classmethod
        def from_transducer(cls, **kwargs):
            events.append("construct")
            return cls()

        def create_stream(self):
            events.append("create_stream")
            return Stream()

        def is_ready(self, stream):
            return False

        def get_result(self, stream):
            return ""

    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace(OnlineRecognizer=Recognizer))
    model = inference.XASRModel.load(tmp_path, 2)
    events.append("warming")
    model.warmup()
    assert isinstance(model._native, Recognizer)
    assert events == ["construct", "warming", "create_stream", "accept_waveform",
                      "accept_waveform", "input_finished"]


@pytest.mark.asyncio
async def test_asr_eof_tail_completes_last_word_once_after_endpoint():
    class Stream:
        pending = False
        finished = False
        silence_samples = 0
        text = ""
        endpoint = False

        def accept_waveform(self, rate, samples):
            if not np.any(samples):
                self.silence_samples += samples.size
            self.pending = True

        def input_finished(self):
            self.finished = True

    class Recognizer:
        def __init__(self):
            self.resets = 0

        def create_stream(self):
            self.stream = Stream()
            return self.stream

        def is_ready(self, stream):
            return stream.pending

        def decode_stream(self, stream):
            stream.pending = False
            if stream.finished and stream.silence_samples >= 19200:
                stream.text = "末字"
                stream.endpoint = True

        def is_endpoint(self, stream):
            return stream.endpoint

        def get_result(self, stream):
            return stream.text

        def reset(self, stream):
            self.resets += 1
            stream.text = ""
            stream.endpoint = False

    native = Recognizer()

    class Service:
        config = SimpleNamespace(pcm_seconds=2)
        engines = {"asr": SimpleNamespace(version="test-asr")}

        @asynccontextmanager
        async def acquire(self, kind):
            yield inference.XASRModel(native)

        async def run(self, kind, operation, *args):
            return operation(*args)

    async def source():
        from redlotus.TTS import PCMChunk
        yield PCMChunk(np.ones(160, dtype=np.float32), 16000)

    results = [item async for item in asr.StreamingRecognizer(service=Service()).recognize(source())]
    assert [(item.text, item.is_final) for item in results] == [("末字", True)]
    assert native.resets == 1


@pytest.mark.asyncio
async def test_player_stop_waits_for_native_write_before_device_close(monkeypatch):
    writing = threading.Event()
    release = threading.Event()
    devices = []

    class FakeOutput:
        def __init__(self, **kwargs):
            self.aborted = False
            self.closed = False
            devices.append(self)

        def start(self):
            pass

        def write(self, samples):
            assert samples.shape[0] <= 2400
            writing.set()
            release.wait(2)
            assert not self.closed

        def abort(self):
            self.aborted = True

        def close(self):
            self.closed = True

    async def source():
        yield PCMChunk(np.ones(2400, dtype=np.float32), 24000)

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(OutputStream=FakeOutput))
    player = AudioPlayer()
    play_task = asyncio.create_task(player.play(source()))
    assert await asyncio.wait_for(asyncio.to_thread(writing.wait, 1), 2)
    stop_task = asyncio.create_task(player.stop())
    try:
        await asyncio.sleep(.02)
        assert not stop_task.done()
        assert not devices[0].aborted
        assert not devices[0].closed
    finally:
        release.set()
    await stop_task
    with pytest.raises(asyncio.CancelledError):
        await play_task
    assert devices[0].aborted and devices[0].closed

@pytest.mark.asyncio
async def test_player_normal_completion_closes_device(monkeypatch):
    devices = []

    class FakeOutput:
        def __init__(self, **kwargs):
            self.closed = False
            devices.append(self)

        def start(self):
            pass

        def write(self, samples):
            pass

        def stop(self):
            pass

        def abort(self):
            pass

        def close(self):
            self.closed = True

    async def source():
        yield PCMChunk(np.ones(100, dtype=np.float32), 24000)

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(OutputStream=FakeOutput))
    player = AudioPlayer()
    await player.play(source())
    assert devices[0].closed
    assert player._stream is None
