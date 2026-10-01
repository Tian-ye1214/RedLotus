"""Keep linguistic context intact before handing a sentence to the TTS frontend."""
import asyncio

import pytest

from redlotus.TTS import StreamingSynthesizer
from test_speech_inference import FakeService, FakeTts


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "这是红莲的语音输出测试。",
    "请检查这一段有没有重复播放。",
    "任务已经完成百分之八十。",
    "圆周率约等于三点一四。",
    "The quick brown fox jumps over the lazy dog.",
])
async def test_native_frontend_receives_sentence_context_for_words_and_numbers(text):
    native = FakeTts(samples=1)
    async for _ in StreamingSynthesizer(FakeService(tts=native)).synthesize(text):
        pass
    assert [call[0] for call in native.calls] == [text]


@pytest.mark.asyncio
@pytest.mark.parametrize("text,spoken,speaker", [
    ("Version 3.14 is ready for testing.", "Version three point one four is ready for testing.", 0),
    ("We finished 80% of 120 jobs.", "We finished eighty percent of one hundred and twenty jobs.", 0),
    ("The value is -23.50.", "The value is minus twenty-three point five zero.", 0),
    ("The 21st test uses 1,234 files.", "The twenty-first test uses one thousand, two hundred and thirty-four files.", 0),
    ("Hello, this is a voice test.", "Hello, this is a voice test.", 0),
    ("现在的温度是23.5摄氏度。", "现在的温度是23.5摄氏度。", 3),
])
async def test_language_specific_numbers_and_voice_preserve_a_complete_sentence(text, spoken, speaker):
    native = FakeTts(samples=1)
    async for _ in StreamingSynthesizer(FakeService(tts=native)).synthesize(text):
        pass
    assert native.calls == [(spoken, speaker, 1.0)]


@pytest.mark.asyncio
async def test_split_english_decimal_is_normalized_once_after_reassembly():
    async def deltas():
        for delta in ("Version 3", ".", "1", "4 is ready."):
            yield delta

    native = FakeTts(samples=1)
    async for _ in StreamingSynthesizer(FakeService(tts=native)).synthesize(deltas()):
        pass
    assert native.calls == [("Version three point one four is ready.", 0, 1.0)]


@pytest.mark.asyncio
async def test_deferred_number_keeps_english_language_after_timer_flush():
    release = asyncio.Event()
    async def deltas():
        yield "The value is 3."
        await release.wait()
        yield "14."

    native = FakeTts(samples=1)
    stream = StreamingSynthesizer(FakeService(tts=native, flush_ms=10)).synthesize(deltas())
    try:
        await asyncio.wait_for(anext(stream), 1)
        release.set()
        async for _ in stream:
            pass
    finally:
        release.set()
        await stream.aclose()
    assert native.calls == [("The value is", 0, 1.0), ("three point one four.", 0, 1.0)]
