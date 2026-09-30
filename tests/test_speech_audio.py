import asyncio
import io
import struct
import sys
import tempfile
import threading
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from redlotus.TTS import PCMChunk, SpeechBusy, SpeechError, SpeechUnavailable
from redlotus.TTS.audio import AudioCapture
from redlotus.TTS.audio import AudioIO, AudioPlayer


@pytest.mark.asyncio
async def test_silk_input_never_creates_spool_files(monkeypatch):
    def no_tempfile(*args, **kwargs):
        pytest.fail("audio conversion attempted to create a spool file")

    def decode(source, output, sample_rate):
        assert source.read(10) == b"\x02#!SILK_V3"
        output.write(np.array([0, 16384], dtype="<i2").tobytes())

    monkeypatch.setattr(tempfile, "mkstemp", no_tempfile)
    monkeypatch.setitem(sys.modules, "pysilk", SimpleNamespace(decode=decode))
    chunks = [chunk async for chunk in AudioIO.parse_input(b"\x02#!SILK_V3encoded")]
    np.testing.assert_allclose(np.concatenate([c.samples for c in chunks]), [0, .5])


@pytest.mark.asyncio
async def test_silk_decoder_error_is_not_mistaken_for_empty_audio(monkeypatch):
    def decode(source, output, sample_rate):
        raise ValueError("invalid SILK packet")

    monkeypatch.setitem(sys.modules, "pysilk", SimpleNamespace(decode=decode))
    with pytest.raises(SpeechError, match="SILK decode failed") as failed:
        _ = [chunk async for chunk in AudioIO.parse_input(b"#!SILK_V3bad")]
    assert isinstance(failed.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_wav_output_is_owned_bytes_without_spool_files(monkeypatch):
    def no_tempfile(*args, **kwargs):
        pytest.fail("audio conversion attempted to create a spool file")

    monkeypatch.setattr(tempfile, "mkstemp", no_tempfile)
    segments = [segment async for segment in AudioIO.parse_output(
        pcm_stream(np.ones(2400)), target="wav"
    )]
    assert len(segments) == 1
    assert isinstance(segments[0].data, bytes)
    with wave.open(io.BytesIO(segments[0].data), "rb") as reader:
        assert reader.getnframes() == 2400


async def byte_stream(*parts):
    for part in parts:
        yield part


async def pcm_stream(*parts):
    for part in parts:
        yield PCMChunk(np.asarray(part, dtype=np.float32), 24000)


def fake_input_backend(stream, **extras):
    mic = {"index": 1, "name": "Mic", "hostapi": 0, "max_input_channels": 1}
    api = {"name": "WASAPI"}
    return SimpleNamespace(
        InputStream=stream,
        query_devices=lambda device=None, *, kind=None: mic if kind == "input" else [
            {"name": "Output", "hostapi": 0, "max_input_channels": 0}, mic,
        ],
        query_hostapis=lambda index=None: api if index is not None else [api],
        check_input_settings=lambda **kwargs: None,
        **extras,
    )


def wav_bytes(samples, *, rate=16000, channels=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return output.getvalue()


@pytest.mark.asyncio
async def test_wav_header_overrides_mislabeled_mime_and_streams_before_eof():
    frames = np.tile(np.array([16384, -16384], dtype=np.int16), (2048, 1))
    payload = wav_bytes(frames, channels=2)
    gate = asyncio.Event()

    async def source():
        yield payload[:2500]
        await gate.wait()
        yield payload[2500:]

    decoded = AudioIO.parse_input(source(), format="audio/mpeg")
    first = await asyncio.wait_for(anext(decoded), 1)
    assert first.sample_rate == 16000
    assert first.samples.dtype == np.float32
    assert first.samples.ndim == 1
    np.testing.assert_allclose(first.samples, 0, atol=1 / 32768)
    gate.set()
    remainder = [chunk async for chunk in decoded]
    assert first.samples.size + sum(chunk.samples.size for chunk in remainder) == 2048


@pytest.mark.asyncio
async def test_raw_pcm_preserves_partial_frames_and_rejects_truncation():
    raw = np.array([0, 16384, -16384, 32767], dtype="<i2").tobytes()
    chunks = [chunk async for chunk in AudioIO.parse_input(byte_stream(raw[:3], raw[3:5], raw[5:]), format="pcm_s16le", sample_rate=16000)]
    np.testing.assert_allclose(np.concatenate([c.samples for c in chunks]), [0, .5, -.5, 32767 / 32768])
    with pytest.raises(SpeechError, match="truncat|incomplete"):
        _ = [chunk async for chunk in AudioIO.parse_input(raw[:-1], format="pcm_s16le", sample_rate=16000)]


@pytest.mark.asyncio
async def test_resampler_reuses_state_and_flushes_tail(monkeypatch):
    instances = []

    class FakeResampler:
        def __init__(self, input_rate, output_rate, channels, dtype):
            assert (input_rate, output_rate, channels, dtype) == (8000, 16000, 1, "float32")
            self.calls = []
            instances.append(self)

        def resample_chunk(self, samples, last=False):
            self.calls.append(last)
            return np.array([.75], dtype=np.float32) if last else np.repeat(samples, 2)

    monkeypatch.setitem(sys.modules, "soxr", SimpleNamespace(ResampleStream=FakeResampler))
    samples = np.array([16384, -16384] * 4, dtype="<i2")
    raw = samples.tobytes()
    chunks = [chunk async for chunk in AudioIO.parse_input(byte_stream(raw[:12], raw[12:]), format="pcm_s16le", sample_rate=8000)]
    np.testing.assert_allclose(np.concatenate([c.samples for c in chunks]),
                               [value for sample in samples / 32768 for value in (sample, sample)] + [.75])
    assert len(instances) == 1
    assert instances[0].calls == [False, False, True]


@pytest.mark.asyncio
async def test_silk_header_overrides_mime_and_decodes_with_file_objects(monkeypatch):
    calls = []

    def decode(source, output, sample_rate):
        calls.append((source.read(10), sample_rate))
        output.write(np.array([0, 16384], dtype="<i2").tobytes())

    monkeypatch.setitem(sys.modules, "pysilk", SimpleNamespace(decode=decode))
    chunks = [chunk async for chunk in AudioIO.parse_input(b"\x02#!SILK_V3encoded", format="audio/mpeg")]
    np.testing.assert_allclose(np.concatenate([c.samples for c in chunks]), [0, .5])
    assert calls == [(b"\x02#!SILK_V3", 16000)]


@pytest.mark.asyncio
async def test_input_errors_are_specific_and_text_path_needs_no_codec(tmp_path, monkeypatch):
    with pytest.raises(SpeechError, match="WAV|truncat"):
        _ = [chunk async for chunk in AudioIO.parse_input(b"RIFF\x00\x00\x00\x00WAVEbad")]
    monkeypatch.setitem(sys.modules, "soxr", None)
    with pytest.raises(SpeechUnavailable, match="soxr"):
        _ = [chunk async for chunk in AudioIO.parse_input(b"\0\0", format="pcm_s16le", sample_rate=44100)]
    with pytest.raises(SpeechUnavailable, match="unsupported audio format"):
        _ = [chunk async for chunk in AudioIO.parse_input(b"unknown")]
    path = tmp_path / "input.wav"
    path.write_bytes(wav_bytes([1, 2]))
    assert sum(c.samples.size for c in [chunk async for chunk in AudioIO.parse_input(path)]) == 2


@pytest.mark.asyncio
async def test_wav_byte_padding_keeps_next_chunk_aligned():
    fmt = struct.pack("<HHIIHH", 1, 1, 16000, 16000, 1, 8)
    payload = b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt
    payload += b"JUNK" + struct.pack("<I", 1) + b"x\0"
    payload += b"data" + struct.pack("<I", 3) + bytes([128, 255, 0]) + b"\0"
    payload += b"JUNK" + struct.pack("<I", 2) + b"xy"
    audio = b"RIFF" + struct.pack("<I", len(payload)) + payload
    chunks = [chunk async for chunk in AudioIO.parse_input(audio)]
    np.testing.assert_allclose(np.concatenate([c.samples for c in chunks]), [0, 127 / 128, -1])


@pytest.mark.asyncio
async def test_wav_output_segments_by_actual_duration():
    output = AudioIO.parse_output(pcm_stream(np.zeros(30000), np.ones(30000)), target="wav", max_seconds=1)
    segments = []
    async for segment in output:
        assert isinstance(segment.data, bytes)
        assert segment.format == "wav"
        with wave.open(io.BytesIO(segment.data), "rb") as reader:
            assert reader.getnframes() == int(segment.duration * 24000)
            assert reader.getframerate() == 24000
        segments.append(segment)
    assert [segment.duration for segment in segments] == [1, 1, .5]


@pytest.mark.asyncio
async def test_output_flushes_sentence_before_next_pcm_arrives():
    next_sentence = asyncio.Event()

    async def source():
        yield PCMChunk(np.ones(4800, dtype=np.float32), 24000)
        yield PCMChunk(np.empty(0, dtype=np.float32), 24000, end_of_segment=True)
        await next_sentence.wait()
        yield PCMChunk(np.zeros(2400, dtype=np.float32), 24000, end_of_segment=True)

    output = AudioIO.parse_output(source(), target="wav")
    try:
        first = await asyncio.wait_for(anext(output), 1)
        assert first.duration == .2
        assert first.data.startswith(b"RIFF")
        next_sentence.set()
        second = await asyncio.wait_for(anext(output), 1)
        assert second.duration == .1
        with pytest.raises(StopAsyncIteration):
            await anext(output)
    finally:
        next_sentence.set()
        await output.aclose()


@pytest.mark.asyncio
async def test_output_sentence_at_hard_limit_has_no_empty_segment():
    async def source():
        yield PCMChunk(np.ones(24000, dtype=np.float32), 24000, end_of_segment=True)

    segments = [segment async for segment in AudioIO.parse_output(source(), target="wav",
                                                           max_seconds=1)]
    assert [segment.duration for segment in segments] == [1]


@pytest.mark.asyncio
async def test_output_close_preserves_owned_bytes():
    output = AudioIO.parse_output(pcm_stream(np.ones(24000)), target="wav")
    segment = await anext(output)
    assert segment.data.startswith(b"RIFF")
    await output.aclose()
    assert segment.data.startswith(b"RIFF")


@pytest.mark.asyncio
async def test_silk_output_uses_file_codec_in_memory(monkeypatch):
    seen = []

    def encode(source, output, sample_rate, bit_rate):
        data = source.read()
        seen.append((len(data), sample_rate, bit_rate))
        output.write(b"\x02#!SILK_V3" + data)

    monkeypatch.setitem(sys.modules, "pysilk", SimpleNamespace(encode=encode))
    output = AudioIO.parse_output(pcm_stream(np.ones(2400)), target="silk")
    segment = await anext(output)
    assert segment.data.startswith(b"\x02#!SILK_V3")
    assert seen == [(4800, 24000, 24000)]
    await output.aclose()


@pytest.mark.asyncio
async def test_cli_output_keeps_pcm_chunks_and_rejects_other_rates():
    source = PCMChunk(np.ones(4, dtype=np.float32), 24000)
    chunks = [chunk async for chunk in AudioIO.parse_output(pcm_stream(source.samples), target="cli")]
    np.testing.assert_array_equal(chunks[0].samples, source.samples)
    async def wrong_rate():
        yield PCMChunk(source.samples, 16000)
    with pytest.raises(SpeechError, match="24000"):
        _ = [chunk async for chunk in AudioIO.parse_output(wrong_rate())]


@pytest.mark.asyncio
@pytest.mark.parametrize("samples", [
    np.ones(2, dtype=np.float64), np.array([np.nan], dtype=np.float32),
    np.ones((2, 1), dtype=np.float32),
])
async def test_cli_output_requires_finite_mono_float32(samples):
    async def source():
        yield PCMChunk(samples, 24000)

    with pytest.raises(SpeechError, match="float32"):
        _ = [chunk async for chunk in AudioIO.parse_output(source())]


@pytest.mark.asyncio
async def test_capture_reports_buffer_overflow_and_closes_device(monkeypatch):
    devices = []

    class CallbackAbort(Exception):
        pass

    class FakeInput:
        def __init__(self, **kwargs):
            self.callback = kwargs["callback"]
            self.closed = False
            devices.append(self)

        def start(self):
            pass

        def abort(self):
            pass

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "sounddevice", fake_input_backend(FakeInput, CallbackAbort=CallbackAbort))
    capture = AudioCapture(sample_rate=100, blocksize=10, pcm_seconds=.1)
    await capture.start()
    with pytest.raises(CallbackAbort):
        devices[0].callback(np.ones((11, 1), dtype=np.float32), 11, None, None)
    with pytest.raises(SpeechBusy, match="two seconds|buffer"):
        await anext(aiter(capture))
    await capture.close()
    assert devices[0].closed


@pytest.mark.asyncio
async def test_player_stop_aborts_device_while_source_waits(monkeypatch):
    devices = []
    written = asyncio.Event()

    class FakeOutput:
        def __init__(self, **kwargs):
            self.aborted = False
            self.closed = False
            devices.append(self)

        def start(self):
            pass

        def write(self, samples):
            assert samples.shape[0] <= 2400
            written.set()

        def abort(self):
            self.aborted = True

        def close(self):
            self.closed = True

    async def source():
        yield PCMChunk(np.ones(2400, dtype=np.float32), 24000)
        await asyncio.Event().wait()

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(OutputStream=FakeOutput))
    player = AudioPlayer(pcm_seconds=2)
    task = asyncio.create_task(player.play(source()))
    await asyncio.wait_for(written.wait(), 1)
    await player.stop()
    assert devices[0].aborted and devices[0].closed
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_player_error_closes_device(monkeypatch):
    devices = []

    class FakeOutput:
        def __init__(self, **kwargs):
            self.aborted = False
            self.closed = False
            devices.append(self)

        def start(self):
            pass

        def abort(self):
            self.aborted = True

        def close(self):
            self.closed = True

    async def wrong_rate():
        yield PCMChunk(np.ones(8, dtype=np.float32), 16000)

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(OutputStream=FakeOutput))
    player = AudioPlayer()
    with pytest.raises(SpeechError, match="24000"):
        await player.play(wrong_rate())
    assert devices[0].aborted and devices[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_at", ["construct", "start"])
async def test_capture_cancel_drains_open_before_closing(monkeypatch, blocked_at):
    entered = threading.Event()
    release = threading.Event()
    devices = []

    class FakeInput:
        def __init__(self, **kwargs):
            self.closed = False
            self.starting = False
            devices.append(self)
            if blocked_at == "construct":
                entered.set()
                release.wait(2)

        def start(self):
            self.starting = True
            if blocked_at == "start":
                entered.set()
                release.wait(2)
            self.starting = False

        def abort(self):
            assert not self.starting

        def close(self):
            assert not self.starting
            self.closed = True

    monkeypatch.setitem(sys.modules, "sounddevice", fake_input_backend(FakeInput))
    capture = AudioCapture()
    task = asyncio.create_task(capture.start())
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 2)
    task.cancel()
    try:
        await asyncio.sleep(.02)
        assert not task.done()
        assert not devices[0].closed
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert devices[0].closed
    assert capture._stream is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["stop", "close"])
async def test_capture_cancel_shutdown_while_opening_still_halts_device(monkeypatch, action):
    opening = threading.Event()
    release = threading.Event()
    devices = []

    class FakeInput:
        def __init__(self, **kwargs):
            self.aborted = False
            self.closed = False
            devices.append(self)

        def start(self):
            opening.set()
            release.wait(2)

        def abort(self):
            self.aborted = True

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "sounddevice", fake_input_backend(FakeInput))
    capture = AudioCapture()
    start_task = asyncio.create_task(capture.start())
    assert await asyncio.wait_for(asyncio.to_thread(opening.wait, 1), 2)
    shutdown_task = asyncio.create_task(getattr(capture, action)())
    await asyncio.sleep(.02)
    shutdown_task.cancel()
    try:
        await asyncio.sleep(.02)
        assert not shutdown_task.done()
    finally:
        release.set()
    await start_task
    with pytest.raises(asyncio.CancelledError):
        await shutdown_task
    assert devices[0].aborted
    assert devices[0].closed == (action == "close")
    await capture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_at", ["construct", "start"])
async def test_player_cancel_drains_open_before_closing(monkeypatch, blocked_at):
    entered = threading.Event()
    release = threading.Event()
    devices = []

    class FakeOutput:
        def __init__(self, **kwargs):
            self.closed = False
            self.starting = False
            devices.append(self)
            if blocked_at == "construct":
                entered.set()
                release.wait(2)

        def start(self):
            self.starting = True
            if blocked_at == "start":
                entered.set()
                release.wait(2)
            self.starting = False

        def abort(self):
            assert not self.starting

        def close(self):
            assert not self.starting
            self.closed = True

    async def source():
        yield PCMChunk(np.ones(1, dtype=np.float32), 24000)

    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(OutputStream=FakeOutput))
    player = AudioPlayer()
    task = asyncio.create_task(player.play(source()))
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 2)
    task.cancel()
    try:
        await asyncio.sleep(.02)
        assert not task.done()
        assert not devices[0].closed
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert devices[0].closed
    assert player._stream is None






@pytest.mark.asyncio
async def test_native_soxr_stream_conserves_duration_and_tail():
    pytest.importorskip("soxr")
    source = np.full(44100, 8192, dtype="<i2").tobytes()
    chunks = [chunk async for chunk in AudioIO.parse_input(byte_stream(source[:12345], source[12345:]),
                                                 format="pcm_s16le", sample_rate=44100)]
    samples = np.concatenate([chunk.samples for chunk in chunks])
    assert abs(samples.size - 16000) <= 1
    assert .24 < np.mean(samples) < .26


@pytest.mark.asyncio
async def test_native_silk_roundtrip_uses_actual_codec():
    pytest.importorskip("pysilk")
    time = np.arange(12000, dtype=np.float32) / 24000
    tone = .2 * np.sin(2 * np.pi * 440 * time)
    output = AudioIO.parse_output(pcm_stream(tone), target="silk")
    segment = await anext(output)
    assert segment.duration == .5
    chunks = [chunk async for chunk in AudioIO.parse_input(segment.data)]
    decoded = np.concatenate([chunk.samples for chunk in chunks])
    assert abs(decoded.size - 8000) < 500
    assert np.sqrt(np.mean(decoded * decoded)) > .05
    await output.aclose()


@pytest.mark.asyncio
async def test_silk_input_close_stops_bounded_bridge(monkeypatch):
    def decode(source, output, sample_rate):
        output.write(np.ones(40000, dtype="<i2").tobytes())

    monkeypatch.setitem(sys.modules, "pysilk", SimpleNamespace(decode=decode))
    input_audio = AudioIO.parse_input(b"#!SILK_V3example")
    assert (await anext(input_audio)).samples.size > 0
    await input_audio.aclose()


@pytest.mark.asyncio
async def test_silk_encode_failure_is_reported(monkeypatch):
    def encode(source, output, sample_rate, bit_rate):
        raise ValueError("broken codec")

    monkeypatch.setitem(sys.modules, "pysilk", SimpleNamespace(encode=encode))
    with pytest.raises(SpeechError, match="SILK encode failed"):
        _ = [part async for part in AudioIO.parse_output(pcm_stream(np.ones(2400)), target="silk")]


@pytest.mark.asyncio
async def test_cancel_during_native_decode_waits_for_codec(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def decode(source, output, sample_rate):
        started.set()
        release.wait(2)
        output.write(np.ones(100, dtype="<i2").tobytes())

    monkeypatch.setitem(sys.modules, "pysilk", SimpleNamespace(decode=decode))

    async def consume():
        return [chunk async for chunk in AudioIO.parse_input(b"#!SILK_V3example")]

    task = asyncio.create_task(consume())
    assert await asyncio.wait_for(asyncio.to_thread(started.wait), 1)
    task.cancel()
    try:
        await asyncio.sleep(.02)
        assert not task.done()
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
