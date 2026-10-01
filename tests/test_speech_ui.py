"""Speech controls keep model preparation separate from conversation activity."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import numpy as np
import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, Static, Switch

from redlotus.TTS import ModelKind, ModelStage, PreparationStatus
from redlotus.sessions.control import SessionController
from redlotus.ui.widgets import RecordButton, VoiceControls


@pytest.fixture(autouse=True)
def devices_without_hardware(monkeypatch):
    from redlotus.TTS import audio
    async def inputs(cls, refresh=False):
        return []
    monkeypatch.setattr(audio.AudioDevices, "inputs", classmethod(inputs))


def model_rows(*, asr="preparing", tts="preparing", count=1024):
    return {
        ModelKind(kind): PreparationStatus(stage=ModelStage.WAITING if stage == "preparing" else ModelStage(stage),
                                           bytes=count, total=4096, target="model")
        for kind, stage in (("asr", asr), ("tts", tts))
    }


class SpeechApp(App):
    def __init__(self, service):
        super().__init__()
        self.system = SimpleNamespace(_session=SessionController(), workspace=object())
        self._ask_future = None
        self.service = service

    def compose(self) -> ComposeResult:
        yield VoiceControls()
        yield Input(id="input")


@pytest.mark.asyncio
async def test_model_progress_survives_record_preview_and_finished_task(isolated_config, monkeypatch):
    from redlotus.TTS import service as speech_service

    rows = model_rows(asr="downloading", tts="preparing")
    service = SimpleNamespace(status=lambda: rows, config=SimpleNamespace(pcm_seconds=2))
    monkeypatch.setattr(speech_service.SpeechService, "_shared", service)

    async with SpeechApp(service).run_test() as pilot:
        await pilot.pause()
        controls = pilot.app.query_one(VoiceControls)
        controls.notice("正在完成转写…")
        controls.record_task = asyncio.create_task(asyncio.sleep(0))
        await controls.record_task
        rows[ModelKind.ASR].bytes = 2048
        controls.sync()
        assert "转写" in str(controls.query_one("#voice-preview", Static).render())
        assert "2048" in str(controls.query_one("#voice-model-status", Static).render())


@pytest.mark.asyncio
async def test_record_and_voice_on_gate_independently_and_voice_can_turn_off(isolated_config, monkeypatch):
    from redlotus.TTS import service as speech_service

    rows = model_rows(asr="ready", tts="downloading")
    service = SimpleNamespace(status=lambda: rows, config=SimpleNamespace(pcm_seconds=2))
    monkeypatch.setattr(speech_service.SpeechService, "_shared", service)

    async with SpeechApp(service).run_test() as pilot:
        await pilot.pause()
        controls = pilot.app.query_one(VoiceControls)
        assert not controls.query_one(RecordButton).disabled
        assert controls.query_one(Switch).disabled
        rows[ModelKind.ASR].stage = ModelStage.WAITING
        rows[ModelKind.TTS].stage = ModelStage.READY
        controls.sync()
        assert controls.query_one(RecordButton).disabled
        controls.start_recording()
        assert controls.record_task is None
        assert not controls.query_one(Switch).disabled
        rows[ModelKind.TTS].stage = ModelStage.LOADING
        controls.query_one(Switch).value = True
        await pilot.pause()
        assert not pilot.app.system._session.voice_enabled
        assert not controls.query_one(Switch).value
        rows[ModelKind.TTS].stage = ModelStage.READY
        pilot.app.system._session.voice_enabled = True
        controls.sync()
        assert controls.query_one(Switch).value
        assert not controls.query_one(Switch).disabled
        rows[ModelKind.TTS].stage = ModelStage.FAILED
        controls.sync()
        assert not controls.query_one(Switch).disabled
        assert "/voice prepare tts" in str(controls.query_one("#voice-model-status", Static).render())
        controls.query_one(Switch).value = False
        await pilot.pause()
        assert not pilot.app.system._session.voice_enabled


@pytest.mark.asyncio
async def test_voice_on_during_preparation_does_not_enable_or_construct_player(isolated_config, monkeypatch):
    from redlotus.TTS import audio, service as speech_service
    from redlotus.ui import cli_commands

    service = SimpleNamespace(status=lambda: model_rows(asr="ready", tts="preparing"))
    messages = []
    monkeypatch.setattr(speech_service.SpeechService, "_shared", service)
    monkeypatch.setattr(audio, "AudioPlayer", lambda **_: pytest.fail("player created before TTS readiness"))
    monkeypatch.setattr(cli_commands, "print_warning", messages.append)
    state = SessionController()
    state.voice_enabled = True
    queued = asyncio.create_task(asyncio.Event().wait())
    state.voice_tasks.add(queued)
    await cli_commands.SlashCommands(SimpleNamespace(system=object()), state, "/voice on").voice()
    await asyncio.gather(queued, return_exceptions=True)
    await cli_commands.SlashCommands(SimpleNamespace(system=object()), state, "/voice test").voice()
    assert queued.cancelled()
    assert not state.voice_enabled
    assert state.voice_output is None
    assert any("准备" in message for message in messages)


@pytest.mark.asyncio
async def test_voice_command_waits_for_async_startup_only_when_needed(isolated_config, monkeypatch):
    from redlotus.TTS import service as speech_service
    from redlotus.api import base
    from redlotus.ui import cli_commands

    service = SimpleNamespace(status=lambda: model_rows(asr="ready", tts="ready"))
    started, panels = [], []

    async def start():
        started.append(True)
        return service

    monkeypatch.setattr(speech_service.SpeechService, "_shared", None)
    monkeypatch.setattr(base, "start_speech", start)
    monkeypatch.setattr(cli_commands, "print_panel", lambda body, **kwargs: panels.append(body))
    state = SessionController()
    await cli_commands.SlashCommands(SimpleNamespace(system=object()), state, "/voice off").voice()
    assert not started
    await cli_commands.SlashCommands(SimpleNamespace(system=object()), state, "/voice status").voice()
    assert started == [True] and panels


@pytest.mark.asyncio
async def test_widget_status_does_not_construct_service_on_event_loop(isolated_config, monkeypatch):
    from redlotus.TTS import service as speech_service

    monkeypatch.setattr(speech_service.SpeechService, "_shared", None)
    monkeypatch.setattr(speech_service.SpeechService, "shared", classmethod(lambda cls: pytest.fail("constructed on event loop")))
    async with SpeechApp(None).run_test() as pilot:
        await pilot.pause()
        status = pilot.app.query_one("#voice-model-status", Static)
        assert "初始化" in str(status.render())


@pytest.mark.asyncio
async def test_record_preflight_failure_keeps_friendly_microphone_message(
    isolated_config, tmp_path, monkeypatch
):
    import redlotus.TTS as asr
    from redlotus.TTS import SpeechSettings, SpeechUnavailable, audio, service as speech_service
    from redlotus.ui import widgets

    rows = model_rows(asr="ready", tts="ready")
    service = SimpleNamespace(status=lambda: rows, config=SimpleNamespace(pcm_seconds=2))
    monkeypatch.setattr(speech_service.SpeechService, "_shared", service)
    monkeypatch.setattr(SpeechSettings, "read", classmethod(lambda cls: pytest.fail("configuration reread")))
    logs = []
    async def log(workspace, message, error=None):
        logs.append((message, error))
    monkeypatch.setattr(widgets.logger, "speech_log", log)

    class Capture:
        def __init__(self, **kwargs):
            assert kwargs == {"pcm_seconds": 2, "device": None}

        async def check_available(self):
                raise SpeechUnavailable("暂时无法使用麦克风，请检查系统默认输入设备和权限。文字输入和语音回复仍可使用。") from OSError("native details")

        async def close(self):
            pass

    monkeypatch.setattr(audio, "AudioCapture", Capture)
    async with SpeechApp(service).run_test() as pilot:
        await pilot.pause()
        controls = pilot.app.query_one(VoiceControls)
        controls.start_recording()
        await controls.record_task
        preview = str(controls.query_one("#voice-preview", Static).render())
        assert preview == "暂时无法使用麦克风，请检查系统默认输入设备和权限。文字输入和语音回复仍可使用。"
        assert "native details" not in preview
        assert logs and isinstance(logs[0][1].__cause__, OSError)


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_enabled", [False, True])
async def test_voice_test_uses_real_synthesis_without_enabling_replies_and_off_cancels_it(
    isolated_config, monkeypatch, initially_enabled
):
    from redlotus.TTS import SpeechSettings, audio, service as speech_service
    from redlotus.runtime.resources import finish_io
    from redlotus.ui import cli_commands

    started = asyncio.Event()
    played = []

    class Native:
        sample_rate = 24000
        profile = None

        def prepare_voice(self, profile):
            from redlotus.TTS import SpeakerCondition
            return SpeakerCondition("synthetic", 3, 0, True, 3)

        def generate(self, request, *, callback):
            played.append(request.text)
            samples = np.ones(2400, dtype=np.float32)
            callback(samples, 1.0)
            return SimpleNamespace(samples=samples, sample_rate=24000)

    class Service:
        config = SpeechSettings()
        _closed = False

        def status(self):
            return model_rows(asr="preparing", tts="ready")

        @asynccontextmanager
        async def acquire(self, kind):
            assert kind == "tts"
            yield Native()

        async def run(self, kind, operation, *args):
            return await finish_io(asyncio.to_thread(operation, *args))

    class Player:
        def __init__(self, **_):
            pass

        async def play(self, pcm):
            async for chunk in pcm:
                assert chunk.sample_rate == 24000
                if "Hello" in "".join(played):
                    started.set()
                    await asyncio.Event().wait()

        async def stop(self):
            pass

    service = Service()
    monkeypatch.setattr(speech_service.SpeechService, "_shared", service)
    monkeypatch.setattr(audio, "AudioPlayer", Player)
    monkeypatch.setattr(SpeechSettings, "read", classmethod(lambda cls: pytest.fail("configuration reread")))
    state = SessionController()
    state.voice_enabled = initially_enabled
    command = lambda value: cli_commands.SlashCommands(SimpleNamespace(system=object()), state, value).voice()
    await command("/voice test")
    await asyncio.wait_for(started.wait(), 2)
    assert state.voice_enabled == initially_enabled
    assert played and "你好" in "".join(played) and "Hello" in "".join(played)
    assert state.voice_tasks
    await command("/voice off")
    await state.drain_voice()
    assert not state.voice_tasks
    started.clear()
    await command("/voice test")
    await asyncio.wait_for(started.wait(), 2)
    state.reset(discard=True)
    await state.drain_voice()
    assert not state.voice_tasks


@pytest.mark.asyncio
async def test_voice_test_failure_is_logged_even_without_error_callback(monkeypatch):
    from redlotus.TTS import tts
    from redlotus.sessions import control

    records = []

    async def fail(self, _text):
        raise RuntimeError("native failure")
        yield

    async def consume(pcm):
        async for _ in pcm:
            pass

    monkeypatch.setattr(tts.StreamingSynthesizer, "synthesize", fail)
    monkeypatch.setattr(control.logger, "error", lambda *args, **kwargs: records.append((args, kwargs)))
    state = SessionController()
    state.voice_output = consume
    await state.start_voice_test("测试")
    assert records and records[0][1]["exc_info"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,kinds,archive", [
    ("/voice prepare", ("asr", "tts"), None),
    ('/voice prepare asr "archive file"', ("asr",), "archive file"),
])
async def test_voice_prepare_requests_warm_engine_without_second_acquire(
    isolated_config, tmp_path, monkeypatch, raw, kinds, archive
):
    from redlotus.TTS import service as speech_service
    from redlotus.runtime.resources import WorkspaceContext
    from redlotus.ui import cli_commands

    rows = model_rows(asr="missing", tts="missing")
    prepared, panels = [], []

    class Service:
        def status(self):
            return rows

        async def prepare(self, kind, path, *, warm, report_failure):
            assert callable(report_failure) if kind is None else report_failure is None
            prepared.append((kind, path, warm))
            for chosen in (kind,) if kind else ("asr", "tts"):
                rows[chosen].stage = ModelStage.READY

        @asynccontextmanager
        async def acquire(self, kind):
            pytest.fail("prepare used a second acquire")
            yield object()

    service = Service()
    monkeypatch.setattr(speech_service.SpeechService, "_shared", service)
    monkeypatch.setattr(cli_commands, "print_panel", lambda body, **kwargs: panels.append(body))
    state = SessionController()
    controller = SimpleNamespace(system=SimpleNamespace(workspace=WorkspaceContext.from_path(tmp_path)))
    await cli_commands.SlashCommands(controller, state, raw).voice()
    assert prepared == [(kinds[0] if len(kinds) == 1 else None, archive, True)]
    assert all(rows[kind].stage == ModelStage.READY for kind in kinds)
    assert panels


@pytest.mark.asyncio
async def test_voice_prepare_reports_failed_engine_and_still_shows_other_ready(isolated_config, tmp_path, monkeypatch):
    from redlotus.TTS import service as speech_service
    from redlotus.runtime.resources import WorkspaceContext
    from redlotus.ui import cli_commands

    rows = model_rows(asr="missing", tts="missing")
    warnings, panels, logs = [], [], []

    class Service:
        def status(self):
            return rows

        async def prepare(self, kind, path, *, warm, report_failure):
            assert kind is None and path is None and warm
            assert callable(report_failure)
            rows[ModelKind.ASR].stage = ModelStage.FAILED
            rows[ModelKind.ASR].error = "native details"
            rows[ModelKind.TTS].stage = ModelStage.READY

        @asynccontextmanager
        async def acquire(self, kind):
            pytest.fail("prepare used a second acquire")
            yield object()

    service = Service()
    monkeypatch.setattr(speech_service.SpeechService, "_shared", service)
    monkeypatch.setattr(cli_commands, "print_warning", warnings.append)
    monkeypatch.setattr(cli_commands, "print_panel", lambda body, **kwargs: panels.append(body))
    monkeypatch.setattr(cli_commands.logger, "error", lambda *args, **kwargs: logs.append((args, kwargs)))
    controller = SimpleNamespace(system=SimpleNamespace(workspace=WorkspaceContext.from_path(tmp_path)))
    await cli_commands.SlashCommands(controller, SessionController(), "/voice prepare").voice()
    assert rows[ModelKind.TTS].stage == ModelStage.READY
    assert warnings and "ASR" in warnings[0] and "native details" not in warnings[0]
    assert not logs
    assert panels
def test_voice_status_formats_typed_model_rows():
    from redlotus.TTS import ModelKind, ModelStage, PreparationStatus
    from redlotus.ui.cli_commands import format_voice_model_status

    status = {ModelKind.ASR: PreparationStatus(stage=ModelStage.READY),
              ModelKind.TTS: PreparationStatus(stage=ModelStage.DOWNLOADING, bytes=10, total=20)}
    rendered = format_voice_model_status(status)

    assert "ASR：就绪" in rendered
    assert "TTS：下载中 · 10/20 字节" in rendered
