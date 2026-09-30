"""Bounded, disk-free speech reply behavior."""
import asyncio
import threading

import pytest

from redlotus.TTS import SpeechBusy, tts
from redlotus.TTS.tts import SpeechReply
from test_speech_inference import FakeService, FakeTts


async def _drain(chunks):
    async for _ in chunks:
        pass


async def _wait_for(event):
    await event.wait()


@pytest.mark.asyncio
async def test_reply_keeps_pending_text_in_memory_without_creating_spool(tmp_path):
    preceding = asyncio.Event()
    service = FakeService(tts=FakeTts(samples=1))
    reply = SpeechReply(_drain, service=service, predecessor=_wait_for(preceding))
    try:
        await reply.feed("Hello world.")
        await reply.finish()
        assert not list(tmp_path.iterdir())
        assert not hasattr(reply, "path")
        preceding.set()
        await asyncio.wait_for(reply.task, 1)
    finally:
        preceding.set()
        if not reply.task.done():
            reply.cancel()
            await asyncio.gather(reply.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_reply_text_overflow_stops_only_speech_and_notifies_once():
    preceding = asyncio.Event()
    reported = []
    service = FakeService(tts=FakeTts(samples=1))
    service.config.text_chars = 4
    reply = SpeechReply(_drain, service=service, predecessor=_wait_for(preceding), on_error=reported.append)
    try:
        await reply.feed("abcd")
        await reply.feed("e")
        await reply.feed("ignored after overflow")
        await reply.finish()
        preceding.set()
        await asyncio.gather(reply.task, return_exceptions=True)
        assert len(reported) == 1
        assert isinstance(reported[0], tts.SpeechBusy)
        assert not reply._pending
    finally:
        preceding.set()
        if not reply.task.done():
            reply.cancel()
            await asyncio.gather(reply.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_text_limit_counts_delta_held_by_native_synthesis():
    entered, release = threading.Event(), threading.Event()
    errors = []

    class SlowTts(FakeTts):
        def generate(self, text, config, callback=None):
            entered.set()
            assert release.wait(2)
            return super().generate(text, config, callback=callback)

    service = FakeService(tts=SlowTts(samples=1))
    service.config.text_chars = len("Hello world.")
    reply = SpeechReply(_drain, service=service, on_error=errors.append)
    try:
        await reply.feed("Hello world.")
        assert await asyncio.to_thread(entered.wait, 1)
        await reply.feed("!")
        assert len(errors) == 1 and isinstance(errors[0], SpeechBusy)
        await reply.feed("ignored")
        assert len(errors) == 1
    finally:
        release.set()
        reply.cancel()
        await asyncio.gather(reply.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_text_capacity_returns_after_segment_finishes():
    entered, release = threading.Event(), threading.Event()
    errors = []

    class SlowTts(FakeTts):
        def generate(self, text, config, callback=None):
            if not entered.is_set():
                entered.set()
                assert release.wait(2)
            return super().generate(text, config, callback=callback)

    native = SlowTts(samples=1)
    service = FakeService(tts=native)
    service.config.text_chars = len("Hello.")
    reply = SpeechReply(_drain, service=service, on_error=errors.append)
    try:
        await reply.feed("Hello.")
        assert await asyncio.to_thread(entered.wait, 1)
        assert reply._pending_chars == len("Hello.")
        release.set()

        async def wait_consumed():
            while reply._pending_chars:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_consumed(), 1)
        await reply.feed("Again.")
        await reply.finish()
        await asyncio.wait_for(reply.task, 1)
        assert [call[0] for call in native.calls] == ["Hello.", "Again."]
        assert errors == []
    finally:
        release.set()
        if not reply.task.done():
            reply.cancel()
            await asyncio.gather(reply.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stripped_whitespace_releases_its_original_buffer_length():
    native = FakeTts(samples=1)
    service = FakeService(tts=native)
    service.config.text_chars = len("   Hi.  ")
    errors = []
    reply = SpeechReply(_drain, service=service, on_error=errors.append)
    try:
        await reply.feed("   Hi.  ")

        async def wait_consumed():
            while reply._pending_chars:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_consumed(), 1)
        await reply.feed("Bye.")
        await reply.finish()
        await asyncio.wait_for(reply.task, 1)
        assert [call[0] for call in native.calls] == ["Hi.", "Bye."]
        assert errors == []
    finally:
        if not reply.task.done():
            reply.cancel()
            await asyncio.gather(reply.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_speech_reply_buffers_deltas_and_finishes_before_predecessor(tmp_path):
    preceding = asyncio.Event()
    observed = []
    native = FakeTts(samples=5)

    async def consume(chunks):
        async for chunk in chunks:
            observed.append(len(chunk.samples))

    reply = SpeechReply(consume, service=FakeService(tts=native), predecessor=_wait_for(preceding))
    await reply.feed("Hello")
    await reply.feed(" world.")
    await reply.finish()
    assert not reply.task.done()
    assert not list(tmp_path.iterdir())

    preceding.set()
    await asyncio.wait_for(reply.task, 1)
    assert observed == [5] * len(native.calls)
    assert " ".join(call[0] for call in native.calls) == "Hello world."
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_speech_reply_reader_accepts_later_deltas(tmp_path):
    first = asyncio.Event()
    native = FakeTts(samples=1)

    async def consume(chunks):
        async for _ in chunks:
            first.set()

    reply = SpeechReply(consume, service=FakeService(tts=native))
    await reply.feed("One.")
    await asyncio.wait_for(first.wait(), 1)
    await reply.feed("Two.")
    await reply.finish()
    await asyncio.wait_for(reply.task, 1)
    assert [call[0] for call in native.calls] == ["One.", "Two."]


@pytest.mark.asyncio
async def test_speech_reply_pending_text_is_bounded(tmp_path):
    preceding = asyncio.Event()
    errors = []
    reply = SpeechReply(_drain, service=FakeService(tts=FakeTts(samples=1)), on_error=errors.append,
                        predecessor=_wait_for(preceding))
    try:
        await reply.feed("文" * 10000)
        assert len(errors) == 1 and isinstance(errors[0], tts.SpeechBusy)
        assert reply._pending_chars == 0
        assert not list(tmp_path.iterdir())
    finally:
        reply.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reply.task


@pytest.mark.asyncio
async def test_speech_reply_cancel_preserves_other_files(tmp_path):
    entered = asyncio.Event()
    waiting = asyncio.Event()
    retained = tmp_path / "other.txt"
    retained.write_text("keep", encoding="utf-8")

    async def consume(chunks):
        async for _ in chunks:
            entered.set()
            await waiting.wait()

    reply = SpeechReply(consume, service=FakeService(tts=FakeTts(samples=5)))
    await reply.feed("Ready.")
    await asyncio.wait_for(entered.wait(), 1)
    reply.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reply.task
    assert retained.read_text(encoding="utf-8") == "keep"
    assert not list(tmp_path.glob("speech-*.jsonl"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["internal_prestart", "external_prestart", "external_reader_wait"])
async def test_speech_reply_cancel_at_start_or_reader_wait(tmp_path, mode):
    entered = asyncio.Event()

    async def consume(chunks):
        entered.set()
        await _drain(chunks)

    reply = SpeechReply(consume, service=FakeService(tts=FakeTts()))
    if mode == "external_reader_wait":
        await asyncio.wait_for(entered.wait(), 1)
    if mode == "internal_prestart":
        reply.cancel()
    else:
        reply.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reply.task
    await asyncio.sleep(0)
    assert not list(tmp_path.glob("speech-*.jsonl"))


@pytest.mark.asyncio
async def test_speech_reply_bounds_pending_responses_across_sessions(tmp_path):
    from redlotus.TTS import SpeechBusy
    service = FakeService(tts=FakeTts())
    service.config.queue_size = 1
    first = SpeechReply(_drain, service=service)
    second = SpeechReply(_drain, service=service, predecessor=first.task)
    try:
        with pytest.raises(SpeechBusy):
            SpeechReply(_drain, service=service)
    finally:
        first.cancel()
        second.cancel()
        await asyncio.gather(first.task, second.task, return_exceptions=True)
    assert not list(tmp_path.glob("speech-*.jsonl"))
