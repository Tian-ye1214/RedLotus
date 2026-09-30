"""The production voice controls expose devices and keep failures out of the console."""
import asyncio
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, Select, Static

from redlotus.TTS import InputDevice, ModelKind, ModelStage, PCMChunk, PreparationStatus, SpeechUnavailable, Transcript
import redlotus.TTS as asr
from redlotus.TTS import audio, service as speech_service
from redlotus.runtime import logging as logger, resources
from redlotus.sessions.control import SessionController
from redlotus.ui.widgets import RecordButton, VoiceControls


class MicrophoneApp(App):
    def __init__(self, workspace):
        super().__init__()
        self.system = SimpleNamespace(_session=SessionController(), workspace=workspace)
        self._ask_future = None

    def compose(self) -> ComposeResult:
        yield VoiceControls()
        yield Input(id="input", value="已有草稿")


@pytest.fixture
def microphone_ui(tmp_path, isolated_config, monkeypatch):
    isolated_config["storage"].update(runtime_dir="runtime", project_logs_dir="logs")
    workspace = resources.WorkspaceContext.from_path(tmp_path)
    monkeypatch.setattr(logger, "_configured", False)
    monkeypatch.setattr(logger, "_configured_dir", None)
    monkeypatch.setattr(logger, "_task_log_paths", {})
    console = []
    monkeypatch.setattr(logger, "console_sink", console.append)
    service = SimpleNamespace(config=SimpleNamespace(pcm_seconds=2), status=lambda: {
        kind: PreparationStatus(stage=ModelStage.READY, total=0) for kind in ModelKind}, _closed=False)
    monkeypatch.setattr(speech_service.SpeechService, "_shared", service)
    devices = [InputDevice(index=1, name="Mainboard", hostapi="MME", is_default=True),
               InputDevice(index=2, name="EDIFIER", hostapi="MME", is_default=False)]
    refreshes = []

    async def enumerate_devices(cls, refresh=False):
        refreshes.append(refresh)
        return devices.copy()

    monkeypatch.setattr(audio.AudioDevices, "inputs", classmethod(enumerate_devices))
    return SimpleNamespace(app=MicrophoneApp(workspace), console=console, devices=devices,
                           refreshes=refreshes, root=tmp_path)


class SilentCapture:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.input_device = kwargs.get("device")
        self.closed = False
        self.instances.append(self)

    async def check_available(self):
        pass

    async def start(self):
        pass

    async def close(self):
        self.closed = True

    async def stop(self):
        pass

    async def __aiter__(self):
        yield PCMChunk(np.zeros(1600, dtype=np.float32), 16000)


@pytest.mark.asyncio
async def test_successful_recording_edits_draft_without_writing_wav(microphone_ui, monkeypatch):
    import tempfile
    import wave

    def forbid_audio_file(*args, **kwargs):
        pytest.fail("recording attempted to create an audio file")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", forbid_audio_file)
    monkeypatch.setattr(tempfile, "mkstemp", forbid_audio_file)
    monkeypatch.setattr(wave, "open", forbid_audio_file)

    class Capture(SilentCapture):
        recording_stats = {"seconds": 1.0, "level": 0.5, "device": "EDIFIER"}
    class Recognizer:
        async def record(self, capture, on_result, *, on_started=None):
            if on_started is not None:
                on_started()
            on_result(Transcript("识别正文", False))
            return Transcript("识别正文", True, "asr-version")
    monkeypatch.setattr(audio, "AudioCapture", Capture)
    monkeypatch.setattr(asr, "StreamingRecognizer", lambda service: Recognizer())

    async with microphone_ui.app.run_test() as pilot:
        await pilot.pause()
        controls = pilot.app.query_one(VoiceControls)
        controls.start_recording()
        await controls.record_task
        assert pilot.app.query_one("#input", Input).value == "已有草稿 识别正文"
        assert not hasattr(pilot.app.system._session, "voice_drafts")
        assert not list(microphone_ui.root.rglob("*.wav"))


@pytest.mark.asyncio
async def test_empty_recording_is_a_notice_not_a_console_error(microphone_ui, monkeypatch):
    monkeypatch.setattr(audio, "AudioCapture", SilentCapture)

    async def empty(self, pcm):
        async for _ in pcm:
            pass
        yield Transcript("", True)

    monkeypatch.setattr(asr.StreamingRecognizer, "recognize", empty)
    async with microphone_ui.app.run_test() as pilot:
        await pilot.pause()
        controls = pilot.app.query_one(VoiceControls)
        controls.start_recording()
        await controls.record_task
        await pilot.pause()
        assert str(controls.query_one("#voice-preview", Static).render()) == "未识别到语音，请重试。"
        assert pilot.app.query_one(Input).value == "已有草稿"
        assert not hasattr(pilot.app.system._session, "voice_drafts")
        assert not microphone_ui.console
        assert not list((microphone_ui.root / "runtime/speech").glob("*.wav"))
        assert SilentCapture.instances[-1].closed
    log = (microphone_ui.root / "logs/speech.log").read_text(encoding="utf-8")
    assert "采集统计" in log and "INFO" in log
    assert "ERROR" not in log and "Traceback" not in log
    assert "'frames': 1600" in log and "'seconds': 0.1" in log
    assert "'rms': 0.0" in log and "'peak': 0.0" in log


@pytest.mark.asyncio
async def test_native_failure_is_logged_once_without_console_traceback(microphone_ui, monkeypatch):
    class Unavailable(SilentCapture):
        async def check_available(self):
            raise SpeechUnavailable("暂时无法使用麦克风，请检查系统默认输入设备和权限。文字输入和语音回复仍可使用。") from OSError("native endpoint lost")

    monkeypatch.setattr(audio, "AudioCapture", Unavailable)
    writes = []
    original_sink = logger._session_sink

    def record_thread(message):
        writes.append(threading.get_ident())
        original_sink(message)

    monkeypatch.setattr(logger, "_session_sink", record_thread)
    async with microphone_ui.app.run_test() as pilot:
        await pilot.pause()
        controls = pilot.app.query_one(VoiceControls)
        controls.start_recording()
        await controls.record_task
        assert str(controls.query_one("#voice-preview", Static).render()) == "暂时无法使用麦克风，请检查系统默认输入设备和权限。文字输入和语音回复仍可使用。"
        assert pilot.app.query_one(Input).value == "已有草稿"
        assert not microphone_ui.console
    log = (microphone_ui.root / "logs/speech.log").read_text(encoding="utf-8")
    assert "native endpoint lost" in log and "Traceback" in log
    assert log.count("ERROR") == 1
    assert writes and all(identity != threading.get_ident() for identity in writes)


@pytest.mark.asyncio
async def test_selected_microphone_survives_session_and_matches_after_refresh(microphone_ui, monkeypatch):
    captured = []

    async def record(self, capture, *args, **kwargs):
        captured.append(capture.kwargs["device"])
        raise SpeechUnavailable("测试设备暂不可用")

    monkeypatch.setattr(audio, "AudioCapture", SilentCapture)
    monkeypatch.setattr(asr.StreamingRecognizer, "record", record)
    async with microphone_ui.app.run_test() as pilot:
        await pilot.pause()
        controls = pilot.app.query_one(VoiceControls)
        selector = controls.query_one("#voice-input-device", Select)
        assert selector.value == -1
        selector.value = 2
        await pilot.pause()
        pilot.app.system._session = SessionController()
        controls.sync()
        assert selector.value == 2
        microphone_ui.devices[1] = SimpleNamespace(index=7, name="EDIFIER", hostapi="MME", is_default=False)
        await pilot.click("#voice-input-refresh")
        await pilot.pause()
        assert selector.value == 7
        assert microphone_ui.refreshes[-1] is True
        controls.start_recording()
        await controls.record_task
        assert captured[-1].name == "EDIFIER" and captured[-1].index == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_missing_or_ambiguous_device_never_falls_back_to_default(microphone_ui, ambiguous):
    async with microphone_ui.app.run_test() as pilot:
        await pilot.pause()
        controls = pilot.app.query_one(VoiceControls)
        selector = controls.query_one("#voice-input-device", Select)
        selector.value = 2
        await pilot.pause()
        microphone_ui.devices[:] = microphone_ui.devices[:1]
        if ambiguous:
            microphone_ui.devices.extend(SimpleNamespace(index=index, name="EDIFIER", hostapi="MME", is_default=False)
                                         for index in (6, 7))
        await pilot.click("#voice-input-refresh")
        await pilot.pause()
        assert selector.value != -1
        assert controls.query_one(RecordButton).disabled
        assert "重新选择" in str(controls.query_one("#voice-preview", Static).render())
        selector.value = -1
        await pilot.pause()
        assert not controls.query_one(RecordButton).disabled


@pytest.mark.asyncio
async def test_device_switch_locked_until_recording_finishes(microphone_ui, monkeypatch):
    pending = asyncio.Event()

    async def record(self, capture, *args, **kwargs):
        await pending.wait()
        raise SpeechUnavailable("测试设备暂不可用")

    monkeypatch.setattr(asr.StreamingRecognizer, "record", record)
    monkeypatch.setattr(audio, "AudioCapture", SilentCapture)
    async with microphone_ui.app.run_test() as pilot:
        await pilot.pause()
        controls = pilot.app.query_one(VoiceControls)
        controls.start_recording()
        await pilot.pause()
        assert controls.query_one("#voice-input-device").disabled
        assert controls.query_one("#voice-input-refresh").disabled
        pending.set()
        await controls.record_task
        await pilot.pause()
        assert not controls.query_one("#voice-input-device").disabled


@pytest.mark.asyncio
async def test_model_exception_keeps_cause_in_file_and_draft_in_ui(microphone_ui, monkeypatch):
    monkeypatch.setattr(audio, "AudioCapture", SilentCapture)
    async def broken(self, pcm):
        async for _ in pcm:
            raise RuntimeError("native recognizer decode failed")
        yield
    monkeypatch.setattr(asr.StreamingRecognizer, "recognize", broken)
    async with microphone_ui.app.run_test(size=(48, 25)) as pilot:
        await pilot.pause()
        controls = pilot.app.query_one(VoiceControls)
        controls.start_recording()
        await controls.record_task
        await pilot.pause()
        preview = controls.query_one("#voice-preview", Static)
        assert "未识别到语音" not in str(preview.render())
        assert "模型" in str(preview.render()) and preview.size.height >= 2
        assert pilot.app.query_one(Input).value == "已有草稿"
        assert not hasattr(pilot.app.system._session, "voice_drafts")
        assert SilentCapture.instances[-1].closed
        assert not list((microphone_ui.root / "runtime/speech").glob("*.wav"))
        assert not microphone_ui.console
    log = (microphone_ui.root / "logs/speech.log").read_text(encoding="utf-8")
    assert "native recognizer decode failed" in log and log.count("ERROR") == 1


@pytest.mark.asyncio
async def test_cancel_and_stale_recording_never_overwrite_current_ui(microphone_ui, monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()
    async def delayed(self, capture, *args, **kwargs):
        started.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            await finish.wait()
        raise SpeechUnavailable("old session device lost")
    monkeypatch.setattr(audio, "AudioCapture", SilentCapture)
    monkeypatch.setattr(asr.StreamingRecognizer, "record", delayed)
    async with microphone_ui.app.run_test() as pilot:
        await pilot.pause()
        controls = pilot.app.query_one(VoiceControls)
        controls.start_recording()
        await started.wait()
        pilot.app.system._session = SessionController()
        controls.sync()
        controls.notice("当前会话提示")
        finish.set()
        await controls.record_task
        assert str(controls.query_one("#voice-preview", Static).render()) == "当前会话提示"
        assert pilot.app.query_one(Input).value == "已有草稿"
        assert not microphone_ui.console


@pytest.mark.asyncio
async def test_cancelled_recording_never_creates_audio_file(monkeypatch):
    import tempfile
    import wave

    started = asyncio.Event()

    def forbid_audio_file(*args, **kwargs):
        pytest.fail("recording attempted to create an audio file")

    async def stalled(self, pcm):
        async for _ in pcm:
            started.set()
            await asyncio.Event().wait()
            yield Transcript("", True)

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", forbid_audio_file)
    monkeypatch.setattr(tempfile, "mkstemp", forbid_audio_file)
    monkeypatch.setattr(wave, "open", forbid_audio_file)
    monkeypatch.setattr(asr.StreamingRecognizer, "recognize", stalled)
    capture = SilentCapture()
    task = asyncio.create_task(asr.StreamingRecognizer(service=object()).record(capture, lambda _: None))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert capture.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [70, 220])
async def test_microphone_selector_stays_compact(microphone_ui, width):
    async with microphone_ui.app.run_test(size=(width, 25)) as pilot:
        await pilot.pause()
        selector = pilot.app.query_one("#voice-input-device", Select)
        assert 10 <= selector.size.width <= 44
        refresh = pilot.app.query_one("#voice-input-refresh")
        assert refresh.region.x == selector.region.right
        assert pilot.app.query_one("#voice-enabled").region.right <= width


@pytest.mark.asyncio
async def test_repeated_status_failure_logs_once_until_recovery(microphone_ui, monkeypatch):
    service = speech_service.SpeechService._shared
    original = service.status
    def failed():
        raise RuntimeError("status currently unavailable")
    async with microphone_ui.app.run_test() as pilot:
        await pilot.pause()
        controls = pilot.app.query_one(VoiceControls)
        monkeypatch.setattr(service, "status", failed)
        for _ in range(5):
            controls.sync()
        await asyncio.gather(*tuple(pilot.app.system._session._preparations))
        log = (microphone_ui.root / "logs/speech.log").read_text(encoding="utf-8")
        assert log.count("ERROR") == 1
        monkeypatch.setattr(service, "status", original)
        controls.sync()
        monkeypatch.setattr(service, "status", failed)
        controls.sync()
        await asyncio.gather(*tuple(pilot.app.system._session._preparations))
        log = (microphone_ui.root / "logs/speech.log").read_text(encoding="utf-8")
        assert log.count("ERROR") == 2
        assert not microphone_ui.console


@pytest.mark.asyncio
async def test_each_recording_shows_started_before_any_recognized_text(microphone_ui, monkeypatch):
    class Capture(SilentCapture):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.released = asyncio.Event()
        async def __aiter__(self):
            await self.released.wait()
            yield PCMChunk(np.zeros(1600, dtype=np.float32), 16000)
        async def stop(self):
            self.released.set()
    async def empty(self, pcm):
        async for _ in pcm:
            pass
        yield Transcript("", True)
    monkeypatch.setattr(audio, "AudioCapture", Capture)
    monkeypatch.setattr(asr.StreamingRecognizer, "recognize", empty)
    async with microphone_ui.app.run_test() as pilot:
        await pilot.pause()
        controls = pilot.app.query_one(VoiceControls)
        for _ in range(3):
            controls.start_recording()
            async with asyncio.timeout(2):
                while not controls._started:
                    await asyncio.sleep(.01)
            assert "录音中" in str(controls.query_one("#voice-preview", Static).render())
            await controls.release_recording()
            await controls.record_task
            await pilot.pause()
            assert not list((microphone_ui.root / "runtime/speech").glob("*.wav"))
            assert not hasattr(pilot.app.system._session, "voice_drafts")
        assert not microphone_ui.console
