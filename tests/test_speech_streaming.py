"""Native callback, bounded streaming, and per-response voice contracts."""
import asyncio
from types import SimpleNamespace

import pytest

from redlotus.TTS import SpeakerCondition, SynthesisRequest, VoiceProfile

class _SpeakerNative:
    sample_rate = 24000
    profile = VoiceProfile("speaker-test", "default", "speaker", 3, 0, True)

    def prepare_voice(self, profile):
        assert profile is self.profile
        return SpeakerCondition(profile.model_id, profile.default_sid, profile.latin_sid,
                                profile.english_number_words, profile.default_sid)

    def request_text(self, request):
        assert isinstance(request, SynthesisRequest)
        assert isinstance(request.voice, SpeakerCondition)
        assert request.voice.model_id == self.profile.model_id
        return request.text


async def test_native_rate_callback_streams_before_completion_and_flushes_resampler_once():
    import threading
    from contextlib import asynccontextmanager
    import numpy as np
    import soxr
    from redlotus.TTS import PCMChunk, SpeechSettings, StreamingSynthesizer
    from redlotus.runtime.resources import finish_io

    release, callback_returned = threading.Event(), threading.Event()
    source = np.sin(np.arange(32000, dtype=np.float32) * .047).astype(np.float32) * .3

    class Native(_SpeakerNative):
        sample_rate = 32000

        def generate(self, request, callback=None):
            callback(source[:16000], .5)
            callback_returned.set()
            assert release.wait(5)
            callback(source[16000:], 1)
            return PCMChunk(source, self.sample_rate)

    class Service:
        config = SpeechSettings(pcm_seconds=2)

        @asynccontextmanager
        async def acquire(self, _kind):
            yield Native()

        async def run(self, _kind, operation, *args):
            return await finish_io(asyncio.to_thread(operation, *args))

    stream = StreamingSynthesizer(Service()).synthesize("你好。")
    pending = asyncio.create_task(anext(stream))
    try:
        assert await asyncio.to_thread(callback_returned.wait, 1)
        await asyncio.sleep(.02)
        assert pending.done(), "native-rate PCM was held until whole-segment completion"
        first = pending.result()
        release.set()
        chunks = [first, *[chunk async for chunk in stream]]
        assert all(chunk.sample_rate == 24000 for chunk in chunks)
        np.testing.assert_allclose(np.concatenate([chunk.samples for chunk in chunks]),
                                   soxr.resample(source, 32000, 24000), atol=1e-6)
        assert [chunk.end_of_segment for chunk in chunks] == [False] * (len(chunks) - 1) + [True]
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()


@pytest.mark.parametrize("native_rate", [24000, 32000])
async def test_native_tts_callback_delivers_before_completion_and_cancels_full_queue(native_rate):
    import threading
    from contextlib import asynccontextmanager
    import numpy as np
    from redlotus.TTS import PCMChunk, SpeechSettings
    from redlotus.TTS import StreamingSynthesizer
    from redlotus.runtime.resources import finish_io
    callback_returned, release_native, completed = threading.Event(), threading.Event(), threading.Event()
    class Native(_SpeakerNative):
        sample_rate = native_rate

        def generate(self, request, callback=None):
            assert self.request_text(request) == "你好。"
            samples = np.ones(native_rate * 10, dtype=np.float32)
            try:
                if callback is not None:
                    callback(samples, 1.0)
                callback_returned.set()
                release_native.wait()
                return PCMChunk(samples, native_rate)
            finally:
                completed.set()
    class Service:
        config = SpeechSettings(pcm_seconds=.2)
        leased = False
        @asynccontextmanager
        async def acquire(self, _kind):
            self.leased = True
            try:
                yield Native()
            finally:
                self.leased = False
        async def run(self, _kind, operation, *args):
            return await finish_io(asyncio.to_thread(operation, *args))
    service = Service()
    stream = StreamingSynthesizer(service).synthesize("你好。")
    try:
        chunk = await asyncio.wait_for(anext(stream), 2)
        assert chunk.sample_rate == 24000
        assert len(chunk.samples) <= 4800
        assert not completed.is_set()
        assert service.leased
    finally:
        closing = asyncio.create_task(stream.aclose())
        try:
            assert await asyncio.to_thread(callback_returned.wait, 1)
            assert not closing.done()
            assert service.leased
        finally:
            release_native.set()
            await asyncio.wait_for(closing, 2)
    assert completed.is_set()
    assert not service.leased


async def test_native_tts_callback_continues_subsegments_without_replaying_returned_audio():
    from contextlib import asynccontextmanager
    import numpy as np
    from redlotus.TTS import PCMChunk, SpeechSettings
    from redlotus.TTS import StreamingSynthesizer
    from redlotus.runtime.resources import finish_io
    class Native(_SpeakerNative):
        def generate(self, request, callback=None):
            assert self.request_text(request) == "你好。"
            reusable = np.zeros(3200, dtype=np.float32)
            for value in (.1, .2, .3):
                reusable.fill(value)
                assert callback(reusable, value / .3) == 1
            reusable.fill(-1)
            return PCMChunk(np.zeros(9600, dtype=np.float32), 24000)
    class Service:
        config = SpeechSettings(pcm_seconds=.2)
        @asynccontextmanager
        async def acquire(self, _kind):
            yield Native()
        async def run(self, _kind, operation, *args):
            return await finish_io(asyncio.to_thread(operation, *args))
    chunks = [chunk async for chunk in StreamingSynthesizer(Service()).synthesize("你好。")]
    np.testing.assert_allclose(np.concatenate([chunk.samples for chunk in chunks]),
                               np.repeat(np.array([.1, .2, .3], dtype=np.float32), 3200))
    assert [chunk.end_of_segment for chunk in chunks] == [False] * (len(chunks) - 1) + [True]


async def test_tts_streams_returned_audio_when_model_does_not_call_back():
    from contextlib import asynccontextmanager
    import numpy as np
    from redlotus.TTS import PCMChunk, SpeechSettings
    from redlotus.TTS import StreamingSynthesizer
    from redlotus.runtime.resources import finish_io

    class Native(_SpeakerNative):
        def generate(self, request, callback=None):
            assert self.request_text(request) == "你好。"
            return PCMChunk(np.array([.1, .2, .3], dtype=np.float32), 24000)

    class Service:
        config = SpeechSettings(pcm_seconds=.2)
        @asynccontextmanager
        async def acquire(self, _kind):
            yield Native()
        async def run(self, _kind, operation, *args):
            return await finish_io(asyncio.to_thread(operation, *args))

    chunks = [chunk async for chunk in StreamingSynthesizer(Service()).synthesize("你好。")]
    np.testing.assert_allclose(np.concatenate([chunk.samples for chunk in chunks]), [.1, .2, .3])
    assert [chunk.end_of_segment for chunk in chunks] == [True]


async def test_tts_keeps_one_model_and_prepared_feature_voice_for_whole_response():
    from contextlib import asynccontextmanager
    import numpy as np
    from redlotus.TTS import PCMChunk, FeatureCondition, SpeechSettings
    from redlotus.TTS import StreamingSynthesizer
    from redlotus.runtime.resources import finish_io


    class Native:
        sample_rate = 24000
        def __init__(self, model_id):
            self.profile = VoiceProfile(model_id, "dedicated", "features")
            self.prepared = []
            self.requests = []
        def prepare_voice(self, profile):
            assert profile is self.profile
            voice = FeatureCondition(profile.model_id, profile.voice_id)
            self.prepared.append(voice)
            return voice
        def generate(self, request, callback=None):
            assert isinstance(request, SynthesisRequest)
            assert isinstance(request.voice, FeatureCondition)
            assert request.voice.model_id == self.profile.model_id
            self.requests.append(request)
            samples = np.ones(2400, dtype=np.float32)
            assert callback(samples, 1.0) == 1
            return PCMChunk(samples, 24000)

    class Service:
        config = SpeechSettings()
        def __init__(self):
            self.models = [Native("first-model"), Native("second-model")]
            self.acquisitions = 0
        @asynccontextmanager
        async def acquire(self, _kind):
            self.acquisitions += 1
            yield self.models[min(self.acquisitions - 1, 1)]
        async def run(self, _kind, operation, *args):
            return await finish_io(asyncio.to_thread(operation, *args))

    service = Service()
    chunks = [chunk async for chunk in StreamingSynthesizer(service).synthesize("第一句。第二句。")]
    first, second = service.models
    assert service.acquisitions == 1
    assert len(first.prepared) == 1
    assert [request.text for request in first.requests] == ["第一句。", "第二句。"]
    assert all(request.voice is first.prepared[0] for request in first.requests)
    assert first.prepared[0].voice_id == "dedicated"
    assert second.prepared == second.requests == []
    assert [chunk.end_of_segment for chunk in chunks] == [True, True]


async def test_tts_prepares_next_sentence_while_previous_audio_is_consumed():
    import threading
    from contextlib import asynccontextmanager
    import numpy as np
    from redlotus.TTS import PCMChunk, SpeechSettings
    from redlotus.TTS import StreamingSynthesizer
    from redlotus.runtime.resources import finish_io
    next_started = threading.Event()
    class Native(_SpeakerNative):
        def generate(self, request, callback=None):
            if self.request_text(request) == "第二句。":
                next_started.set()
            samples = np.ones(24000, dtype=np.float32)
            callback(samples, 1.0)
            return PCMChunk(samples, 24000)
    class Service:
        config = SpeechSettings(pcm_seconds=2)
        @asynccontextmanager
        async def acquire(self, _kind):
            yield Native()
        async def run(self, _kind, operation, *args):
            return await finish_io(asyncio.to_thread(operation, *args))
    stream = StreamingSynthesizer(Service()).synthesize("第一句。第二句。")
    try:
        await anext(stream)
        assert await asyncio.to_thread(next_started.wait, 1)
    finally:
        await stream.aclose()


async def test_tts_preserves_sentence_context_decimals_and_logical_clip_boundary():
    from contextlib import asynccontextmanager
    import numpy as np
    from redlotus.TTS import PCMChunk, SpeechSettings
    from redlotus.TTS import StreamingSynthesizer
    from redlotus.runtime.resources import finish_io
    calls = []
    class Native(_SpeakerNative):
        def generate(self, request, callback=None):
            calls.append(self.request_text(request))
            samples = np.ones(2400, dtype=np.float32)
            callback(samples, 1.0)
            return PCMChunk(samples, 24000)
    class Service:
        config = SpeechSettings()
        @asynccontextmanager
        async def acquire(self, _kind):
            yield Native()
        async def run(self, _kind, operation, *args):
            return await finish_io(asyncio.to_thread(operation, *args))
    text = "我们正在测试本地语音输出，数字3.14和Hello world都应该完整保留。"
    chunks = [chunk async for chunk in StreamingSynthesizer(Service()).synthesize(text)]
    assert calls == [text]
    assert "".join(calls).replace(" ", "") == text.replace(" ", "")
    assert any("3.14" in part for part in calls)
    assert any("Hello" in part for part in calls)
    assert any("world" in part for part in calls)
    assert [chunk.end_of_segment for chunk in chunks] == [False] * (len(chunks) - 1) + [True]


@pytest.mark.asyncio
async def test_tts_flushes_deferred_word_as_soon_as_boundary_arrives():
    from redlotus.TTS.tts import TextSegmenter
    boundary, ended = asyncio.Event(), asyncio.Event()

    async def deltas():
        yield "hel"
        await boundary.wait()
        yield "lo "
        await ended.wait()

    stream = TextSegmenter(20, 120).segments(deltas())
    next_segment = asyncio.create_task(anext(stream))
    try:
        await asyncio.sleep(.06)
        assert not next_segment.done()
        boundary.set()
        assert await asyncio.wait_for(next_segment, .5) == "hello"
    finally:
        ended.set()
        next_segment.cancel()
        await asyncio.gather(next_segment, return_exceptions=True)
        await stream.aclose()


async def test_cancelled_native_stream_releases_worker_references_without_gc(tmp_path, monkeypatch):
    import gc
    import threading
    import weakref
    from contextlib import aclosing
    import numpy as np
    from redlotus.TTS import ModelKind, PCMChunk, SpeechSettings, StreamingSynthesizer
    from redlotus.TTS.inference import _SegmentStream
    from redlotus.TTS.service import SpeechService
    from redlotus.TTS import inference

    refs = []
    class ObservedStream(_SegmentStream):
        def __init__(self, service):
            super().__init__(service)
            refs.append(weakref.ref(self))
    monkeypatch.setattr(inference, "_SegmentStream", ObservedStream)

    class Native(_SpeakerNative):
        def generate(self, request, callback=None):
            assert self.request_text(request) == "你好。"
            self.entered.set()
            assert self.release.wait(2)
            return PCMChunk(np.ones(2400, dtype=np.float32), 24000)
        def close(self):
            pass

    service = SpeechService(SpeechSettings(model_dir=tmp_path))
    native = Native()
    service.engines[ModelKind.TTS].native = native
    synthesizer = StreamingSynthesizer(service)

    async def cancelled_call():
        native.entered, native.release = threading.Event(), threading.Event()
        async def consume():
            async with aclosing(synthesizer.synthesize("你好。")) as stream:
                async for _ in stream:
                    pass
        task = asyncio.create_task(consume())
        try:
            assert await asyncio.to_thread(native.entered.wait, 1)
            task.cancel()
            await asyncio.sleep(.01)
            assert not task.done()  # Cancellation drains the running native call.
        finally:
            native.release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            pytest.fail("stream did not cancel")

    gc_enabled = gc.isenabled()
    gc.disable()
    try:
        for cycle in range(2):
            await cancelled_call()
            deadline = asyncio.get_running_loop().time() + .5
            while any(ref() is not None for ref in refs) and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(.005)
            assert len(refs) == cycle + 1
            assert all(ref() is None for ref in refs)
    finally:
        await service.close()
        if gc_enabled:
            gc.enable()

