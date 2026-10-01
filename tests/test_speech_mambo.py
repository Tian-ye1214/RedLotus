"""Mambo's passive bundle, bounded pipe protocol, and model lifetime."""
import io
import asyncio
import importlib.util
import json
import struct
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, get_type_hints

import numpy as np
import pytest

from redlotus.TTS import ModelKind, SpeechError, SpeechSettings, SpeechUnavailable, StreamingSynthesizer, SynthesisRequest, TTSModel, VoiceProfile
from redlotus.TTS.inference import MamboTTSModel
from redlotus.runtime.resources import FramedProcess, finish_io


class WorkerStub:
    def __init__(self, output):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(output)
        self.waited = 0

    def poll(self):
        return None

    def wait(self, **kwargs):
        self.waited += 1
        return 0


def packet(metadata, pcm=b""):
    header = json.dumps(metadata).encode()
    return struct.pack("<I", len(header)) + header + pcm


def model_for(output):
    profile = VoiceProfile("mambo-v1", "mambo", "features")
    bundle = SimpleNamespace(profile=profile, base_model_id=profile.model_id)
    channel = FramedProcess.__new__(FramedProcess)
    channel.process = WorkerStub(output)
    return MamboTTSModel(channel, bundle)


def test_framed_reply_drains_after_early_consumer_stop():
    worker = model_for(packet({"status": "data", "size": 3}, b"one") +
                       packet({"status": "data", "size": 3}, b"two") +
                       packet({"status": "done", "size": 6}) +
                       packet({"status": "data", "size": 4}, b"next") +
                       packet({"status": "done", "size": 4}))._worker
    with worker.responses(4, 8) as blocks:
        assert next(blocks) == b"one"
    with worker.responses(4, 8) as blocks:
        assert list(blocks) == [b"next"]


def test_framed_reply_drains_after_callback_failure():
    worker = model_for(packet({"status": "data", "size": 3}, b"one") +
                       packet({"status": "data", "size": 3}, b"two") +
                       packet({"status": "done", "size": 6}) +
                       packet({"status": "data", "size": 4}, b"next") +
                       packet({"status": "done", "size": 4}))._worker
    with pytest.raises(RuntimeError, match="consumer failed"):
        with worker.responses(4, 8) as blocks:
            assert next(blocks) == b"one"
            raise RuntimeError("consumer failed")
    with worker.responses(4, 8) as blocks:
        assert list(blocks) == [b"next"]


@pytest.mark.parametrize("text", ["😊", "…"])
def test_nonphonetic_reply_is_empty_and_next_request_uses_same_worker(text):
    pcm = np.array([0.125, -0.375], dtype="<f4")
    model = model_for(packet({"status": "skipped", "size": 0}) +
                      packet({"status": "data", "size": pcm.nbytes}, pcm.tobytes()) +
                      packet({"status": "done", "size": pcm.nbytes}))
    request = SynthesisRequest(text, model.prepare_voice(model.profile))
    skipped = model.generate(request, callback=lambda *_: pytest.fail("empty text emitted PCM"))
    assert skipped.sample_rate == 32000 and skipped.end_of_segment and len(skipped.samples) == 0
    assert model._worker.process is not None
    next_audio = model.generate(SynthesisRequest("你好。", request.voice))
    np.testing.assert_array_equal(next_audio.samples, pcm)


async def test_nonphonetic_segment_does_not_abort_later_chinese_audio():
    pcm = np.full(3200, 0.25, dtype="<f4")
    model = model_for(packet({"status": "skipped", "size": 0}) +
                      packet({"status": "skipped", "size": 0}) +
                      packet({"status": "data", "size": pcm.nbytes}, pcm.tobytes()) +
                      packet({"status": "done", "size": pcm.nbytes}))

    class Service:
        config = SpeechSettings(pcm_seconds=.2, flush_ms=1)

        @asynccontextmanager
        async def acquire(self, kind):
            assert kind is ModelKind.TTS
            yield model

        async def run(self, kind, operation, *args):
            assert kind is ModelKind.TTS
            return await finish_io(asyncio.to_thread(operation, *args))

    async def deltas():
        yield "😊"
        await asyncio.sleep(.05)
        yield "…"
        await asyncio.sleep(.05)
        yield "你好。"

    chunks = [chunk async for chunk in StreamingSynthesizer(Service()).synthesize(deltas())]
    assert chunks and chunks[-1].end_of_segment
    assert all(np.all(np.isfinite(chunk.samples)) for chunk in chunks)
    assert sum(len(chunk.samples) for chunk in chunks) > 0
    assert model._worker.process is not None
    wire = model._worker.process.stdin.getvalue()
    requests = []
    while wire:
        size, = struct.unpack("<I", wire[:4])
        requests.append(wire[4:4 + size].decode("utf-8"))
        wire = wire[4 + size:]
    assert requests == ["😊", "…", "你好。"]


@pytest.mark.parametrize("output", [
    packet({"status": "data", "size": 9}),
    packet({"status": "data", "size": 4}, b"data") + packet({"status": "done", "size": 3}),
    packet({"status": "data", "size": 4}, b"data") * 3,
    packet({"status": "done", "size": 0}),
    packet({"status": "skipped", "size": 4}),
    packet({"status": "data", "size": 4}, b"data") + packet({"status": "skipped", "size": 0}),
    packet({"status": "data", "size": True}, b"x"),
])
def test_framed_reply_rejects_invalid_bounds_and_reaps_worker(output):
    channel = model_for(output)._worker
    process = channel.process
    with pytest.raises(ValueError):
        with channel.responses(4, 8) as blocks:
            list(blocks)
    assert process.waited == 1
    assert channel.process is None


def test_mambo_implements_tts_contract_and_returns_owned_pcm():
    assert get_type_hints(VoiceProfile)["mode"] == Literal["speaker", "features"]
    pcm = np.array([0.1, -0.1, 0.25], dtype="<f4")
    model = model_for(packet({"status": "data", "size": pcm.nbytes}, pcm.tobytes()) +
                      packet({"status": "done", "size": pcm.nbytes}))
    assert isinstance(model, TTSModel)
    voice = model.prepare_voice(model.profile)
    result = model.generate(SynthesisRequest("你好。", voice))
    assert result.sample_rate == 32000
    np.testing.assert_array_equal(result.samples, pcm)
    wire = model._worker.process.stdin.getvalue()
    assert wire[4:] == "你好。".encode()


@pytest.mark.parametrize("output", [b"\x01", struct.pack("<I", 8193), packet([]),
    packet({"status": "data", "size": 32000 * 2 * 4 + 1}),
    packet({"status": "data", "size": 8}, b"abcd"),
    packet({"status": "data", "size": 3}, b"abc") + packet({"status": "done", "size": 3}),
    packet({"status": "data", "size": 4}, struct.pack("<f", float("nan"))) + packet({"status": "done", "size": 4})])
def test_mambo_rejects_invalid_reply_without_unbounded_allocation(output):
    model = model_for(output)
    with pytest.raises((SpeechError, ValueError)):
        model.generate(SynthesisRequest("test", model.prepare_voice(model.profile)))


def test_mambo_rejects_wrong_voice_and_unbounded_text_before_pipe_write():
    model = model_for(b"")
    with pytest.raises(SpeechUnavailable):
        model.prepare_voice(VoiceProfile("other", "mambo", "features"))
    voice = model.prepare_voice(model.profile)
    for text in ("x" * 121, "invalid\0text"):
        with pytest.raises(SpeechError):
            model.generate(SynthesisRequest(text, voice))
    assert not model._worker.process.stdin.getvalue()


def test_mambo_close_waits_for_worker_and_is_idempotent():
    model = model_for(b"")
    worker = model._worker.process
    model.close()
    model.close()
    assert worker.waited == 1
    assert worker.stdin.closed and worker.stdout.closed


def test_worker_error_message_is_preserved():
    model = model_for(packet({"status": "error", "message": "invalid model interface"}))
    with pytest.raises(SpeechError, match="invalid model interface"):
        model.generate(SynthesisRequest("test", model.prepare_voice(model.profile)))


def test_native_artifact_allowlist_excludes_models_and_foreign_executables():
    path = Path(__file__).resolve().parents[1] / "scripts/verify_wheel.py"
    spec = importlib.util.spec_from_file_location("speech_wheel_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("redlotus_mambo.exe", "NOTICE.md", "UPSTREAM-LICENSE.txt"):
        assert not module.forbidden_asset("redlotus/TTS/native/" + name)
    for name in ("unknown.dll", "other.exe", "bert.onnx", "refer.wav", "model.json", "audio.mp3"):
        assert module.forbidden_asset("redlotus/TTS/native/" + name)


def test_mambo_rejects_incompatible_decoder_layout(tmp_path):
    from test_speech_types import _external_bundle
    from redlotus.TTS import TTSBundle

    manifest = _external_bundle(tmp_path)
    manifest["cache_layout"] = "fixed"
    (tmp_path / "model.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SpeechUnavailable, match="不兼容"):
        TTSBundle.read(tmp_path)


def test_mambo_pcm_survives_next_request_and_worker_close():
    first = np.array([0.125, -0.375], dtype="<f4")
    second = np.array([0.25, -0.75], dtype="<f4")
    header, done = {"status": "data", "size": 8}, {"status": "done", "size": 8}
    model = model_for(packet(header, first.tobytes()) + packet(done) + packet(header, second.tobytes()) + packet(done))
    request = SynthesisRequest("test", model.prepare_voice(model.profile))
    output = model.generate(request)
    next_output = model.generate(request)
    model.close()
    np.testing.assert_array_equal(output.samples, first)
    np.testing.assert_array_equal(next_output.samples, second)


def test_mambo_callbacks_release_chunks_and_cancel_drains_before_next_request():
    chunks = [np.full(16, value, dtype="<f4") for value in (.1, .2, .3)]
    messages = b"".join(packet({"status": "data", "size": part.nbytes}, part.tobytes()) for part in chunks)
    reply = messages + packet({"status": "done", "size": 192})
    model = model_for(reply + reply)
    request = SynthesisRequest("test", model.prepare_voice(model.profile))
    received = []

    def stop_after_first(samples, _progress):
        received.append(samples)
        assert model._worker.process.stdout.tell() < len(reply)
        return 0

    assert model.generate(request, callback=stop_after_first) is None
    assert len(received) == 1
    np.testing.assert_array_equal(received[0], chunks[0])
    np.testing.assert_array_equal(model.generate(request).samples, np.concatenate(chunks))


@pytest.mark.parametrize("name", ["onnxruntime.dll", "prepare.py", "frontend/plugin.pyd"])
def test_model_bundle_rejects_executable_files_even_if_declared(tmp_path, name):
    import hashlib
    from test_speech_types import _external_bundle
    from redlotus.TTS import TTSBundle

    manifest = _external_bundle(tmp_path)
    content = b"not executable test data"
    (tmp_path / name).write_bytes(content)
    manifest["resources"][name] = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    (tmp_path / "model.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SpeechUnavailable, match="可执行文件"):
        TTSBundle.read(tmp_path).verify()
