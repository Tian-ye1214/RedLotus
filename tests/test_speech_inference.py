import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import gc
from pathlib import Path
import shutil
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from redlotus.TTS import PCMChunk
from redlotus.TTS import asr, tts
from redlotus.TTS.asr import StreamingRecognizer, XASRModel
from redlotus.TTS.tts import KokoroModel, SpeechReply, StreamingSynthesizer
from redlotus.core.gateway import GoalTextFilter, coordinator_stream_handler
from redlotus.core.tasks import GOAL_MARKER_RE


class FakeService:
    def __init__(self, *, asr=None, tts=None, flush_ms=600, segment_chars=120, pcm_seconds=2):
        self.models = {"asr": XASRModel(asr) if asr is not None else None,
                       "tts": KokoroModel(tts) if tts is not None else None}
        self.engines = {"asr": SimpleNamespace(version="test-asr"), "tts": SimpleNamespace(version="test-tts")}
        self.config = SimpleNamespace(
            flush_ms=flush_ms, segment_chars=segment_chars, pcm_seconds=pcm_seconds,
            queue_size=8, text_chars=4096,
        )
        self.acquisitions = []

    @asynccontextmanager
    async def acquire(self, kind):
        self.acquisitions.append(kind)
        yield self.models[kind]

    async def run(self, kind, operation, *args, **kwargs):
        assert self.models[kind] is not None
        from redlotus.runtime.resources import finish_io
        return await finish_io(asyncio.to_thread(operation, *args, **kwargs))


class FakeStream:
    def __init__(self):
        self.pending = False
        self.marker = None
        self.text = ""
        self.endpoint = False
        self.finished = False
        self.tail_samples = 0

    def accept_waveform(self, sample_rate, samples):
        assert sample_rate == 16000
        if np.any(samples):
            self.marker = round(float(samples[0]), 1)
        else:
            self.tail_samples += len(samples)
        self.pending = True

    def input_finished(self):
        self.finished = True


class FakeRecognizer:
    def __init__(self):
        self.stream = None
        self.resets = 0

    def create_stream(self):
        self.stream = FakeStream()
        return self.stream

    def is_ready(self, stream):
        return stream.pending

    def decode_stream(self, stream):
        stream.pending = False
        if stream.marker is not None:
            stream.text, stream.endpoint = {
                0.1: ("你", False),
                0.2: ("你好", False),
                0.3: ("你好。", True),
                0.4: ("world", False),
            }[stream.marker]
            stream.marker = None

    def get_result(self, stream):
        return SimpleNamespace(text=stream.text)

    def is_endpoint(self, stream):
        return stream.endpoint

    def reset(self, stream):
        self.resets += 1
        stream.text = ""
        stream.endpoint = False


async def pcm(*values):
    for value in values:
        yield PCMChunk(np.full(160, value, dtype=np.float32), 16000)


@pytest.mark.asyncio
async def test_record_returns_final_transcript_without_writing_audio(tmp_path, monkeypatch):
    class Capture:
        input_device = None
        recording_stats = None

        def __init__(self):
            self.closed = False

        async def check_available(self):
            pass

        async def start(self):
            pass

        async def close(self):
            self.closed = True

        async def __aiter__(self):
            async for chunk in pcm(0.1, 0.2):
                yield chunk

    capture = Capture()
    observed = []
    recognizer = asr.StreamingRecognizer(FakeService(asr=FakeRecognizer()))
    final = await recognizer.record(capture, observed.append)
    assert final.is_final and final.text == "你好"
    assert capture.closed
    assert capture.recording_stats["frames"] == 320
    assert not list(tmp_path.iterdir())


def test_asr_model_exposes_typed_recognition_session():
    model = asr.XASRModel(FakeRecognizer())
    session = model.create_session()
    assert session.accept(np.full(160, .1, dtype=np.float32)) == "你"
    assert session.finish() == "你"
    session.close()


@pytest.mark.asyncio
async def test_recognize_replaces_preview_and_only_finalizes_at_input_end():
    native = FakeRecognizer()
    result = [item async for item in StreamingRecognizer(FakeService(asr=native)).recognize(pcm(0.1, 0.2, 0.3, 0.4))]

    assert [item.text for item in result] == ["你", "你好", "你好。", "你好。world", "你好。world"]
    assert [item.is_final for item in result] == [False, False, False, False, True]
    assert native.resets == 1
    assert native.stream.finished
    assert native.stream.tail_samples >= 10560


@pytest.mark.asyncio
async def test_recognize_tail_is_decoded_and_silence_has_one_empty_final():
    class TailRecognizer(FakeRecognizer):
        def decode_stream(self, stream):
            stream.pending = False
            if stream.finished and stream.tail_samples >= 10560:
                stream.text = "尾音"

    native = TailRecognizer()
    result = [item async for item in StreamingRecognizer(FakeService(asr=native)).recognize(pcm(0.1))]
    assert result[-1].text == "尾音"
    assert sum(item.is_final for item in result) == 1

    silent = FakeRecognizer()
    result = [item async for item in StreamingRecognizer(FakeService(asr=silent)).recognize(pcm(0.0))]
    assert [(item.text, item.is_final) for item in result] == [("", True)]


@pytest.mark.asyncio
async def test_recognize_accepts_sherpa_string_results():
    class StringRecognizer(FakeRecognizer):
        def get_result(self, stream):
            return stream.text

    result = [item async for item in StreamingRecognizer(FakeService(asr=StringRecognizer())).recognize(pcm(0.1))]
    assert result[-1].text == "你"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "samples,rate",
    [(np.zeros(2, dtype=np.int16), 16000), (np.zeros((2, 1), dtype=np.float32), 16000),
     (np.zeros(2, dtype=np.float32), 24000)],
)
async def test_recognize_rejects_nonstandard_pcm(samples, rate):
    async def source():
        yield PCMChunk(samples, rate)

    with pytest.raises(ValueError):
        _ = [item async for item in StreamingRecognizer(FakeService(asr=FakeRecognizer())).recognize(source())]


class FakeTts:
    sample_rate = 24000

    def __init__(self, samples=50000):
        self.calls = []
        self.samples = samples

    def generate(self, text, *, sid, speed, callback=None):
        self.calls.append((text, sid, speed))
        samples = np.ones(self.samples, dtype=np.float32)
        if callback is not None:
            callback(samples, 1.0)
        return SimpleNamespace(samples=samples, sample_rate=24000)


@pytest.mark.asyncio
async def test_synthesize_segments_arbitrary_deltas_without_splitting_decimal():
    async def deltas():
        for delta in ("Price is 3", ".14. Next", " sentence!"):
            yield delta

    native = FakeTts()
    output = [chunk async for chunk in StreamingSynthesizer(FakeService(tts=native)).synthesize(deltas())]
    spoken = [call[0] for call in native.calls]
    assert "".join(spoken).replace(" ", "") == "Priceisthreepointonefour.Nextsentence!"
    assert any("three point one four" in part for part in spoken)
    assert all(call[1:] == (0, 1.0) for call in native.calls)
    assert all(chunk.sample_rate == 24000 and chunk.samples.dtype == np.float32 for chunk in output)
    assert max(len(chunk.samples) for chunk in output) <= 48000
    assert sum(len(chunk.samples) for chunk in output) == len(spoken) * 50000


@pytest.mark.asyncio
async def test_synthesize_flushes_pending_text_before_source_finishes():
    release = asyncio.Event()

    async def deltas():
        yield "Hello "
        await release.wait()

    native = FakeTts(samples=10)
    stream = StreamingSynthesizer(FakeService(tts=native, flush_ms=10)).synthesize(deltas())
    try:
        first = await asyncio.wait_for(anext(stream), 1)
        assert len(first.samples) == 10
        assert [call[0] for call in native.calls] == ["Hello"]
    finally:
        release.set()
        await stream.aclose()


@pytest.mark.asyncio
async def test_synthesize_flush_deadline_starts_with_first_pending_delta():
    release = asyncio.Event()

    async def deltas():
        yield "One"
        await asyncio.sleep(0.15)
        yield " two "
        await release.wait()

    native = FakeTts(samples=1)
    stream = StreamingSynthesizer(FakeService(tts=native, flush_ms=200)).synthesize(deltas())
    start = asyncio.get_running_loop().time()
    try:
        await asyncio.wait_for(anext(stream), 0.31)
        assert asyncio.get_running_loop().time() - start < 0.31
        assert [call[0] for call in native.calls] == ["One two"]
    finally:
        release.set()
        await stream.aclose()


@pytest.mark.asyncio
async def test_synthesize_caps_long_text_on_unicode_and_word_boundaries():
    native = FakeTts(samples=1)
    _ = [chunk async for chunk in StreamingSynthesizer(FakeService(tts=native, segment_chars=8)).synthesize("hello world 中文😀测试")]
    segments = [call[0] for call in native.calls]
    assert all(len(segment) <= 8 for segment in segments)
    assert "".join(segments).replace(" ", "") == "helloworld中文😀测试"
    assert segments[0] == "hello"


@pytest.mark.asyncio
async def test_timeout_keeps_an_incomplete_decimal_or_english_word():
    for head, tail, first, last in (("Price is 3.", "14.", "Price is", "3.14."),
                                    ("Say hel", "lo!", "Say", "hello!")):
        release = asyncio.Event()
        async def source():
            yield head
            await release.wait()
            yield tail
        stream = tts.TextSegmenter(10, 120).segments(source())
        try:
            assert await asyncio.wait_for(anext(stream), 1) == first
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(.03)
            assert not pending.done()
            release.set()
            assert await asyncio.wait_for(pending, 1) == last
        finally:
            release.set()
            await stream.aclose()


def test_asr_loader_uses_zipformer2_int8_and_prewarms(tmp_path, monkeypatch):
    for name in ("encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx", "tokens.txt"):
        (tmp_path / name).write_bytes(b"test")
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return FakeRecognizer()

    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace(
        OnlineRecognizer=SimpleNamespace(from_transducer=factory)
    ))
    model = asr.XASRModel.load(tmp_path, 2)
    model.warmup()
    assert captured["model_type"] == "zipformer2"
    assert captured["enable_endpoint_detection"] is True
    assert captured["num_threads"] == 2
    assert captured["encoder"].endswith("encoder.int8.onnx")
    assert captured["joiner"].endswith("joiner.int8.onnx")
    assert model._native.stream.finished


def _fake_tts_loader(tmp_path, monkeypatch, *, keep_fp32=True, fail_first=False):
    monkeypatch.setattr(tts.KokoroModel, "_phonemizer_source", None)
    monkeypatch.setattr(tts.KokoroModel, "_ascii_absolute", staticmethod(lambda path: str(path.resolve())))
    files = ("model.int8.onnx", "voices.bin", "tokens.txt", "lexicon-us-en.txt",
             "lexicon-zh.txt", "phone-zh.fst", "date-zh.fst", "number-zh.fst")
    for name in (*files, *(("model.onnx",) if keep_fp32 else ())):
        (tmp_path / name).write_bytes(b"test")
    (tmp_path / "espeak-ng-data").mkdir()
    captured = {"attempts": []}

    class Config:
        def __init__(self, **kwargs):
            self.options = kwargs

        def validate(self):
            return True

    class Native:
        sample_rate = 24000

        def __init__(self, config):
            captured["config"] = config
            captured["attempts"].append(config)
            if fail_first and len(captured["attempts"]) == 1:
                raise RuntimeError("native setup failed after phonemizer init")

        def generate(self, text, *, sid, speed, callback=None):
            captured.setdefault("warm", []).append((text, sid, speed))
            return SimpleNamespace(samples=np.ones(1, dtype=np.float32), sample_rate=24000)

    monkeypatch.setitem(sys.modules, "sherpa_onnx", SimpleNamespace(
        OfflineTtsKokoroModelConfig=Config, OfflineTtsModelConfig=Config,
        OfflineTtsConfig=Config, OfflineTts=Native,
    ))
    return captured


@pytest.mark.parametrize("keep_fp32", [False, True])
def test_tts_loader_uses_bilingual_assets_and_chinese_speaker(tmp_path, monkeypatch, keep_fp32):
    captured = _fake_tts_loader(tmp_path, monkeypatch, keep_fp32=keep_fp32)
    model = tts.KokoroModel.load(tmp_path, 2)
    model.warmup()
    options = captured["config"].options
    assert options["model"].options["num_threads"] == 2
    kokoro_options = options["model"].options["kokoro"].options
    expected_model = "model.onnx" if keep_fp32 else "model.int8.onnx"
    assert kokoro_options["model"].endswith(expected_model)
    assert Path(kokoro_options["data_dir"]).is_absolute()
    assert Path(kokoro_options["data_dir"]).is_dir()
    assert all(Path(name).is_absolute() for name in options["rule_fsts"].split(","))
    assert "lexicon-us-en.txt" in options["model"].options["kokoro"].options["lexicon"]
    assert "lexicon-zh.txt" in options["model"].options["kokoro"].options["lexicon"]
    assert "number-zh.fst" in options["rule_fsts"]
    assert captured["warm"] == [("你好。", 3, 1.0), ("Hello, version three point one four is ready.", 0, 1.0)]
    data_path = Path(kokoro_options["data_dir"])
    del model
    gc.collect()
    assert data_path.resolve() == (tmp_path / "espeak-ng-data").resolve()
    assert tts.KokoroModel._phonemizer_source[1] == tmp_path.resolve()
    (tmp_path / "lexicon-zh.txt").write_bytes(b"changed")
    with pytest.raises(tts.SpeechUnavailable, match="重启"):
        tts.KokoroModel.load(tmp_path, 2)


def test_tts_loader_pins_espeak_after_native_constructor_failure(tmp_path, monkeypatch):
    captured = _fake_tts_loader(tmp_path, monkeypatch, fail_first=True)
    with pytest.raises(RuntimeError, match="native setup failed"):
        tts.KokoroModel.load(tmp_path, 2)
    assert tts.KokoroModel._phonemizer_source[1] == tmp_path.resolve()
    tts.KokoroModel.load(tmp_path, 2)
    assert len(captured["attempts"]) == 2
    assert (tmp_path / "espeak-ng-data").is_dir()


def test_tts_loader_serializes_first_dictionary_selection(tmp_path, monkeypatch):
    first_root, second_root = tmp_path / "first", tmp_path / "second"
    first_root.mkdir()
    _fake_tts_loader(first_root, monkeypatch)
    shutil.copytree(first_root, second_root)
    native_module = sys.modules["sherpa_onnx"]
    config_class = native_module.OfflineTtsConfig
    entered, release = threading.Event(), threading.Event()

    class WaitingConfig(config_class):
        def validate(self):
            data_dir = self.options["model"].options["kokoro"].options["data_dir"]
            if first_root.name in data_dir:
                entered.set()
                assert release.wait(2)
            return True

    monkeypatch.setattr(native_module, "OfflineTtsConfig", WaitingConfig)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(tts.KokoroModel.load, first_root, 2)
        assert entered.wait(2)
        second = executor.submit(tts.KokoroModel.load, second_root, 2)
        time.sleep(.05)
        release.set()
        assert isinstance(first.result(timeout=2), tts.KokoroModel)
        with pytest.raises(tts.SpeechUnavailable, match="重启"):
            second.result(timeout=2)


def test_goal_filter_removes_split_markers_and_keeps_prose_comments():
    body = (
        "前<!-- ReDlOtUs_GoAl : CONTINUE -->中"
        "<!-- ordinary comment -->后<!--REDLOTUS_GOAL:DONE-->尾"
    )
    expected = GOAL_MARKER_RE.sub("", body)
    for split in range(len(body) + 1):
        filtered = GoalTextFilter()
        actual = filtered.feed(body[:split]) + filtered.feed(body[split:])
        actual += filtered.feed("", final=True)
        assert actual == expected, split
    filtered = GoalTextFilter()
    actual = "".join(filtered.feed(char) for char in body)
    assert actual + filtered.feed("", final=True) == expected


def test_goal_filter_preserves_incomplete_literal_tail():
    for body in ("hello<", "hello<!", "hello<!-", "hello<!-- ordinary"):
        filtered = GoalTextFilter()
        actual = filtered.feed(body) + filtered.feed("", final=True)
        assert actual == body
    for body in ("hello<!--", "hello<!-- REDLOTUS_G", "hello<!--REDLOTUS_GOAL:DONE"):
        filtered = GoalTextFilter()
        assert filtered.feed(body) + filtered.feed("", final=True) == "hello"


def test_goal_filter_preserves_ordinary_comment_and_filters_nested_marker():
    body = "正文<!-- ordinary <!--REDLOTUS_GOAL:DONE-->尾巴"
    filtered = GoalTextFilter()
    actual = "".join(filtered.feed(body[i:i + 5]) for i in range(0, len(body), 5))
    assert actual + filtered.feed("", final=True) == GOAL_MARKER_RE.sub("", body)


def test_goal_filter_bounds_unclosed_marker_buffer():
    filtered = GoalTextFilter()
    body = "before<!-- REDLOTUS_GOAL: " + " " * 3000 + "DONE -->after"
    output = []
    for offset in range(0, len(body), 37):
        output.append(filtered.feed(body[offset:offset + 37]))
        assert len(filtered.pending) <= 512
    output.append(filtered.feed("", final=True))
    assert "".join(output) == "beforeafter"


def _speech_stream_delta(text, kind="text"):
    return SimpleNamespace(event_kind="part_delta", delta=SimpleNamespace(
        part_delta_kind=kind, content_delta=text))


def _speech_system(begin_voice, voice_error, *, supports=False, displayed=None):
    session = SimpleNamespace(generation=0, voice_enabled=True,
                              begin_voice=begin_voice, voice_error=voice_error)
    presentation = SimpleNamespace(supports_model_stream=lambda: supports,
                                   update_output=lambda *args: displayed.append(args) if displayed is not None else None)
    return SimpleNamespace(session_key="one", _session=session,
                           presentation=presentation, workspace=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_method", ["feed", "finish"])
@pytest.mark.parametrize("report_fails", [False, True])
async def test_coordinator_speech_failure_keeps_agent_text_stream(failed_method, report_fails):
    failure = OSError("speech disk unavailable")
    displayed, spoken, reported, replies = [], [], [], []

    class Reply:
        def __init__(self):
            self.cancelled = 0
            self.finished = 0

        async def feed(self, text):
            if failed_method == "feed":
                raise failure
            spoken.append(text)

        async def finish(self):
            self.finished += 1
            if failed_method == "finish":
                raise failure

        def cancel(self):
            self.cancelled += 1

    def begin_voice(*_args, **_kwargs):
        reply = Reply()
        replies.append(reply)
        return reply

    async def report(error):
        reported.append(error)
        if report_fails:
            raise RuntimeError("voice error reporter unavailable")

    system = _speech_system(begin_voice, report, supports=True, displayed=displayed)

    async def events():
        yield _speech_stream_delta("first")
        yield _speech_stream_delta("thinking", "thinking")
        yield _speech_stream_delta("second")

    await coordinator_stream_handler(system)(None, events())
    assert [call[1] for call in displayed if call[0] == "append_model_stream_delta"] == [
        "first", "thinking", "second"]
    assert len(replies) == 1
    assert replies[0].cancelled == 1
    assert reported == [failure]
    assert spoken == ([] if failed_method == "feed" else ["first", "second"])


@pytest.mark.asyncio
async def test_coordinator_preserves_agent_cancellation_and_cancels_voice():
    reported, cancelled = [], []

    class Reply:
        async def feed(self, _text):
            pass

        async def finish(self):
            raise AssertionError("cancelled stream cannot finish speech")

        def cancel(self):
            cancelled.append(True)

    system = _speech_system(lambda *_args, **_kwargs: Reply(), reported.append)

    async def events():
        yield _speech_stream_delta("first")
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await coordinator_stream_handler(system)(None, events())
    assert cancelled == [True]
    assert reported == []


@pytest.mark.asyncio
async def test_coordinator_reports_final_tail_feed_failure_once():
    failure = OSError("speech spool closed")
    reported, fed, cancelled = [], [], []

    class Reply:
        async def feed(self, text):
            fed.append(text)
            if text == "<":
                raise failure

        async def finish(self):
            raise AssertionError("failed speech must not finish")

        def cancel(self):
            cancelled.append(True)

    system = _speech_system(lambda *_args, **_kwargs: Reply(), reported.append)

    async def events():
        yield _speech_stream_delta("hello<")

    await coordinator_stream_handler(system)(None, events())
    assert fed == ["hello", "<"]
    assert cancelled == [True]
    assert reported == [failure]
