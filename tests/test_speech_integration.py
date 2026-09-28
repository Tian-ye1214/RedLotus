import asyncio
from types import SimpleNamespace

import pytest


def delta(text, kind="text"):
    return SimpleNamespace(event_kind="part_delta", delta=SimpleNamespace(part_delta_kind=kind, content_delta=text))


@pytest.mark.parametrize("pieces", [["你好。<!--REDLOTUS_GOAL:DONE-->再见。"], ["你好。<", "!-", "- RED", "LOTUS_GOAL : CONT", "INUE -->", "再见。"]])
async def test_voice_receives_body_without_terminal_streaming_or_goal_markers(pieces):
    from redlotus.core.gateway import coordinator_stream_handler
    spoken, finished = [], []
    class Reply:
        async def feed(self, text):
            spoken.append(text)
        async def finish(self):
            finished.append(True)
    session = SimpleNamespace(generation=(0, 0), voice_enabled=True, begin_voice=lambda *args, **kwargs: Reply())
    presentation = SimpleNamespace(supports_model_stream=lambda: False, update_output=lambda *args: None)
    system = SimpleNamespace(session_key="one", _session=session, presentation=presentation, workspace=None)
    handler = coordinator_stream_handler(system)
    assert handler is not None
    async def events():
        yield delta("hidden", "thinking")
        for piece in pieces:
            yield delta(piece)
        yield delta("tool", "tool_call")
    await handler(None, events())
    assert "".join(spoken) == "你好。再见。"
    assert finished == [True]


async def test_voice_is_not_started_for_disabled_or_stale_session():
    from redlotus.core.gateway import coordinator_stream_handler
    def unexpected(*args, **kwargs):
        raise AssertionError("voice was not authorized for this response")
    session = SimpleNamespace(generation=(0, 0), voice_enabled=False, begin_voice=unexpected)
    ui = SimpleNamespace(supports_model_stream=lambda: False, update_output=lambda *args: None)
    system = SimpleNamespace(session_key="one", _session=session, presentation=ui, workspace=None)
    handler = coordinator_stream_handler(system)
    assert handler is not None
    session.generation = (0, 1)
    session.voice_enabled = True
    async def events():
        yield delta("must not be spoken")
    await handler(None, events())


async def test_reset_cancels_voice_work_and_discard_disables_voice():
    from redlotus.sessions.control import SessionController
    state = SessionController()
    state.voice_enabled = True
    task = asyncio.create_task(asyncio.Event().wait())
    state.voice_tasks.add(task)
    state.reset(discard=True)
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert not state.voice_enabled


async def test_bot_voice_command_controls_session_without_model_request(phone):
    from redlotus.sessions.context import UserMessage
    bot, state, send, replies, calls = phone
    await bot.dispatch_user_message("wx_owner", UserMessage("/voice on"), send)
    assert state.voice_enabled
    await bot.dispatch_user_message("wx_owner", UserMessage("/voice off"), send)
    assert not state.voice_enabled
    assert not calls
    assert len(replies) == 2


async def test_silent_channel_voice_reports_retry_without_agent_turn(phone, monkeypatch):
    from redlotus.TTS import NoSpeechDetected
    from redlotus.sessions.context import UserMessage

    bot, state, send, replies, calls = phone
    async def silent(*args, **kwargs):
        raise NoSpeechDetected("未识别到语音，请重试。")
    monkeypatch.setattr(state, "prepare_message", silent)
    bot._submit_turn("wx_owner", state, UserMessage("", voice=True), send)
    await state.queue.join()

    assert not calls
    assert replies == ["未识别到语音，请重试。"]


async def test_channel_voice_never_writes_audio_reference(phone, monkeypatch):
    from pydantic_ai import BinaryContent
    from redlotus.sessions.context import UserMessage
    from redlotus.TTS import Transcript
    from redlotus.TTS import asr, audio

    _, state, _, _, _ = phone
    raw = b"#!SILK_V3memory-only"
    seen = []
    async def forbidden(*args, **kwargs):
        pytest.fail("channel speech wrote an audio reference")
    class FakeAudioIO:
        @staticmethod
        async def parse_input(source, **kwargs):
            assert source == raw
            yield object()
    class FakeRecognizer:
        def __init__(self, service=None):
            pass
        async def recognize(self, pcm):
            async for _ in pcm:
                seen.append(True)
            yield Transcript("识别正文", True)
    monkeypatch.setattr(state.agent.toolkit._references, "capture_bytes", forbidden)
    monkeypatch.setattr(audio, "AudioIO", FakeAudioIO, raising=False)
    monkeypatch.setattr(asr, "StreamingRecognizer", FakeRecognizer)
    message = UserMessage("手写正文", attachments=[BinaryContent(raw, media_type="audio/silk")], voice=True)

    await state.prepare_message(state.agent, message)
    await state.prepare_message(state.agent, message)

    assert message.text == "手写正文\n识别正文"
    assert message.attachments == []
    assert message.references == []
    assert seen == [True]
    assert b"SILK" not in repr(message).encode()
    assert sum("识别正文" in part for part in message.to_prompt() if isinstance(part, str)) == 1


async def test_channel_fifo_request_saves_original_speech_body(phone):
    from redlotus.sessions.context import UserMessage
    bot, state, send, _, _ = phone
    state.queue.ready.clear()
    bot._submit_turn("wx_owner", state, UserMessage("typed\nspoken", speech_body="typed"), send)
    try:
        await asyncio.gather(*tuple(state._preparations))
        request = state.queue.pending[0][2]
        assert request["text"] == "typed\nspoken"
        assert request["speech_body"] == "typed"
    finally:
        state.queue.discard()
        await state.queue.cancel(discard=True)


async def test_resumed_voice_prompt_keeps_main_and_supplement_order_without_recognition(tmp_path, monkeypatch):
    from redlotus.sessions import control
    from redlotus.sessions.control import SessionController
    refs = {key: SimpleNamespace(id=key, transcript=transcript, to_prompt=lambda key=key: [key])
            for key, transcript in (("image-1", None), ("voice-1", "spoken-1"),
                                    ("image-2", None), ("voice-2", "spoken-2"))}
    saved = dict(request=dict(id="input", text="main\nspoken-1", speech_body="main",
                              reference_ids=["image-1", "voice-1"]), turn_id="turn",
                 reference_ids=list(refs), supplements=[dict(text="urgent\nspoken-2", speech_body="urgent",
                                                            reference_ids=["image-2", "voice-2"])],
                 queued=[], user_inputs=["main\nspoken-1"], submitted=False)
    state = SessionController()
    state.paused = saved
    async def load(text, *, workspace, captured):
        return [refs[key] for key in captured["reference_ids"]]
    async def save(_):
        pass
    async def durable(operation):
        operation()
    monkeypatch.setattr(control, "load_file_refs", load)
    monkeypatch.setattr(state, "save_pause", save)
    storage = SimpleNamespace(update=lambda **kwargs: None)
    system = SimpleNamespace(workspace=tmp_path, _session_file=storage, _durable_write=durable,
                             presentation=SimpleNamespace(update_output=lambda *args: None))
    messages = []
    async def execute(message, admission, request):
        messages.append(message)
    assert await state.resume(system, execute, lambda: None)
    await state.queue.join()
    assert len(messages) == 1
    message = messages[0]
    assert message.speech_body == "main"
    prompt = message.to_prompt()
    def at(marker):
        return next(index for index, part in enumerate(prompt) if isinstance(part, str) and marker in part)
    assert at("image-1") < at("spoken-1") < at("image-2") < at("spoken-2")
    assert [sum(marker in part for part in prompt if isinstance(part, str)) for marker in ("spoken-1", "spoken-2")] == [1, 1]
    saved["submitted"] = True
    resumed = message.to_prompt()
    assert all(not isinstance(part, str) or "spoken-1" not in part and "image-1" not in part for part in resumed)
    assert any(isinstance(part, str) and "spoken-2" in part for part in resumed)


def test_wechat_ignores_sdk_voice_transcription_but_keeps_actual_text():
    from functools import partial
    from redlotus.api.WeChat import WeChatAgentBot
    msg = SimpleNamespace(user_id="owner", text="实际文字\nSDK转写", voices=[object()], images=[], files=[], videos=[], raw={"item_list": [{"type": 1, "text_item": {"text": "实际文字"}}, {"type": 3, "voice_item": {"text": "SDK转写"}}]})
    sdk = SimpleNamespace(reply=lambda *args: None)
    adapted = WeChatAgentBot().adapt_message(sdk, msg)
    assert adapted[1].text == "实际文字"
    assert adapted[1].voice
    assert isinstance(adapted[2], partial)


async def test_recording_returns_transcript_without_writing_audio(tmp_path, monkeypatch):
    import numpy as np
    from redlotus.TTS import asr, PCMChunk, Transcript
    events = []
    class Capture:
        async def check_available(self):
            pass
        async def start(self):
            events.append("start")
        async def close(self):
            events.append("close")
        def __aiter__(self):
            async def chunks():
                yield PCMChunk(np.zeros(1600, dtype=np.float32), 16000)
            return chunks()
    async def recognize(self, pcm):
        async for _ in pcm:
            yield Transcript("preview", False)
        yield Transcript("final", True)
    monkeypatch.setattr(asr.StreamingRecognizer, "recognize", recognize)
    updates = []
    result = await asr.StreamingRecognizer(object()).record(Capture(), updates.append)
    assert result.text == "final"
    assert [item.text for item in updates] == ["preview", "final"]
    assert events == ["start", "close"]
    assert not list(tmp_path.iterdir())


async def test_failed_recording_closes_capture_without_audio_file(tmp_path, monkeypatch):
    from redlotus.TTS import asr
    class Capture:
        closed = False
        async def check_available(self):
            pass
        async def close(self):
            self.closed = True
    async def recognize(self, pcm):
        raise RuntimeError("device failed")
        yield
    monkeypatch.setattr(asr.StreamingRecognizer, "recognize", recognize)
    capture = Capture()
    with pytest.raises(RuntimeError, match="device failed"):
        await asr.StreamingRecognizer(object()).record(capture, lambda _: None)
    assert capture.closed
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("edit", [False, True])
async def test_record_control_release_outside_and_late_result_preserves_draft(tmp_path, monkeypatch, isolated_config, edit):
    from textual.app import App, ComposeResult
    from textual.widgets import Input
    from redlotus.sessions.control import SessionController
    from redlotus.ui.widgets import VoiceControls
    from redlotus.TTS import ModelKind, ModelStage, PreparationStatus, asr, audio, service as speech_service, Transcript
    started, released, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Capture:
        def __init__(self, **kwargs):
            pass
        async def stop(self):
            released.set()
    async def record(self, capture, on_result, *, on_started=None):
        on_started()
        started.set()
        on_result(Transcript("预览", False))
        await released.wait()
        await finish.wait()
        return Transcript("最终转写", True)
    async def input_devices(cls, **kwargs):
        return []
    monkeypatch.setattr(audio.AudioDevices, "inputs", classmethod(input_devices))
    monkeypatch.setattr(asr, "AudioCapture", Capture)
    monkeypatch.setattr(asr.StreamingRecognizer, "record", record)
    fake_service = SimpleNamespace(config=SimpleNamespace(pcm_seconds=2), status=lambda: {
        ModelKind.ASR: PreparationStatus(stage=ModelStage.READY),
        ModelKind.TTS: PreparationStatus(stage=ModelStage.MISSING),
    }, _closed=False)
    monkeypatch.setattr(speech_service.SpeechService, "_shared", fake_service)
    class TestApp(App):
        def __init__(self):
            super().__init__()
            self.system = SimpleNamespace(_session=SessionController(), workspace=None)
            self._ask_future = None
        def compose(self) -> ComposeResult:
            yield Input(id="input")
            yield VoiceControls()
    app = TestApp()
    async with app.run_test(size=(100, 24)) as pilot:
        controls = app.query_one(VoiceControls)
        await controls._devices_task
        await pilot.pause()
        await pilot.mouse_down("#voice-record")
        await asyncio.wait_for(started.wait(), 2)
        await pilot.mouse_up("#input")
        await asyncio.wait_for(released.wait(), 2)
        if edit:
            app.query_one(Input).value = "手工修改"
            controls.edited()
        finish.set()
        await asyncio.wait_for(controls.record_task, 2)
        assert app.query_one(Input).value == ("手工修改" if edit else "最终转写")
        assert not hasattr(app.system._session, "voice_drafts")
        assert not list(tmp_path.glob("*.wav"))
        assert app.mouse_captured is None


async def test_cli_submission_uses_edited_text_without_audio_reference(tmp_path, isolated_config):
    from redlotus.runtime.resources import WorkspaceContext
    from redlotus.sessions.control import SessionController
    from redlotus.sessions.context import UserMessage
    from redlotus.tools.references import ReferenceStore
    workspace = WorkspaceContext.from_path(tmp_path)
    system = SimpleNamespace(workspace=workspace, toolkit=SimpleNamespace(_references=ReferenceStore(workspace)))
    state = SessionController()
    assert not hasattr(state, "voice_drafts")
    captured = {"text": "用户修改后的正文"}
    refs = await state.prepare_cli_references(system, captured["text"], captured)
    message = UserMessage(captured["text"], references=refs)
    assert refs == []
    assert captured["reference_ids"] == []
    assert message.to_prompt()[0] == "用户修改后的正文"
    assert all("自动识别正文" not in x for x in message.to_prompt() if isinstance(x, str))


@pytest.mark.parametrize("invalidate", [False, True])
async def test_channel_output_keeps_original_destination_without_files(phone, isolated_config, monkeypatch, invalidate):
    from redlotus.TTS import PCMChunk, SpeechSettings
    from redlotus.TTS import service
    import numpy as np
    bot, state, reply, _, _ = phone
    isolated_config["storage"]["runtime_dir"] = "WorkDatabase/runtime"
    state.agent.workspace = state.agent.toolkit._references.workspace
    state.voice_enabled = True
    sent = []
    async def sender(segment):
        sent.append((segment.data, segment.duration))
        if invalidate:
            state.reset()
    reply.speech_sender, reply.speech_format = sender, "wav"
    monkeypatch.setattr(service.SpeechService, "_shared", SimpleNamespace(
        config=SpeechSettings(model_dir=state.agent.workspace.root / "model"), _closed=False))
    bot._bind_voice_output("wx_owner", state, reply, state.generation)
    async def pcm():
        for _ in range(2):
            yield PCMChunk(np.zeros(240, dtype=np.float32), 24000, True)
    await state.voice_output(pcm())
    assert len(sent) == (1 if invalidate else 2)
    assert all(data.startswith(b"RIFF") and duration == .01 for data, duration in sent)
    assert not list(state.agent.workspace.root.rglob("*.wav"))


async def test_native_tts_callback_delivers_before_completion_and_cancels_full_queue():
    import threading
    from contextlib import asynccontextmanager
    import numpy as np
    from redlotus.TTS import PCMChunk, SpeechSettings
    from redlotus.TTS.tts import StreamingSynthesizer
    from redlotus.runtime.resources import finish_io
    completed = threading.Event()
    class Native:
        sample_rate = 24000
        def generate(self, text, speaker, callback=None):
            samples = np.ones(240000, dtype=np.float32)
            if callback is not None:
                callback(samples, 1.0)
            completed.set()
            return PCMChunk(samples, 24000)
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
        await asyncio.wait_for(stream.aclose(), 2)
    assert completed.is_set()
    assert not service.leased


async def test_native_tts_callback_continues_subsegments_without_replaying_returned_audio():
    from contextlib import asynccontextmanager
    import numpy as np
    from redlotus.TTS import PCMChunk, SpeechSettings
    from redlotus.TTS.tts import StreamingSynthesizer
    from redlotus.runtime.resources import finish_io
    class Native:
        sample_rate = 24000
        def generate(self, text, speaker, callback):
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


async def test_tts_prepares_next_sentence_while_previous_audio_is_consumed():
    import threading
    from contextlib import asynccontextmanager
    import numpy as np
    from redlotus.TTS import PCMChunk, SpeechSettings
    from redlotus.TTS.tts import StreamingSynthesizer
    from redlotus.runtime.resources import finish_io
    next_started = threading.Event()
    class Native:
        sample_rate = 24000
        def generate(self, text, speaker, callback):
            if text == "第二句。":
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


@pytest.mark.asyncio
@pytest.mark.parametrize("last_delta", [False, True])
@pytest.mark.parametrize("initially_enabled", [False, True])
@pytest.mark.parametrize("prefix,suffix", [("<", "second"), ("<!--REDLOTUS_GOAL:", "DONE-->second")])
async def test_voice_off_then_on_same_response_only_speaks_new_deltas(last_delta, initially_enabled, prefix, suffix):
    from redlotus.core.gateway import coordinator_stream_handler
    from redlotus.sessions.control import SessionController
    session, replies, errors = SessionController(), [], []
    session.voice_enabled = initially_enabled
    session.voice_error = errors.append

    class Reply:
        def __init__(self):
            self.spoken = []
            self.task = asyncio.create_task(asyncio.Event().wait())
            session.voice_tasks.add(self.task)
            replies.append(self)
        async def feed(self, text):
            if self.task.cancelling() or self.task.done():
                raise RuntimeError("语音回复已结束")
            self.spoken.append(text)
        async def finish(self):
            self.task.cancel()
        def cancel(self):
            self.task.cancel()

    session.begin_voice = lambda *args, **kwargs: Reply()
    system = SimpleNamespace(session_key="one", _session=session, workspace=None,
                             presentation=SimpleNamespace(supports_model_stream=lambda: False))
    async def events():
        yield delta("first" + prefix)
        session.stop_voice(disable=True)
        session.voice_enabled = True
        if not last_delta:
            yield delta(suffix)
    try:
        await coordinator_stream_handler(system)(None, events())
        expected = ([["first"]] if initially_enabled else []) + ([] if last_delta else [["second"]])
        assert [reply.spoken for reply in replies] == expected
        assert not errors
    finally:
        await session.drain_voice()


async def test_tts_preserves_sentence_context_decimals_and_logical_clip_boundary():
    from contextlib import asynccontextmanager
    import numpy as np
    from redlotus.TTS import PCMChunk, SpeechSettings
    from redlotus.TTS.tts import StreamingSynthesizer
    from redlotus.runtime.resources import finish_io
    calls = []
    class Native:
        sample_rate = 24000
        def generate(self, text, speaker, callback):
            calls.append(text)
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
