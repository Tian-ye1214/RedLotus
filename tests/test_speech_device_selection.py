"""Fake PortAudio devices exercise selection without touching real hardware."""

import asyncio
import sys
import threading
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import numpy as np
import pytest

from redlotus.TTS import PCMChunk, SpeechBusy, SpeechUnavailable
from redlotus.TTS import audio
from redlotus.TTS.audio import AudioCapture
from redlotus.TTS import AudioDevices
from redlotus.TTS.audio import AudioPlayer


@pytest.fixture
def devices(monkeypatch):
    state = SimpleNamespace(
        entries=[
            {"name": "Speakers", "hostapi": 0, "max_input_channels": 0},
            {"name": "Built-in Mic", "hostapi": 0, "max_input_channels": 1},
            {"name": "USB Mic", "hostapi": 1, "max_input_channels": 2},
        ],
        next_entries=None, default_index=1, calls=[], streams=[], fail_initialize=False,
    )

    def query_devices(device=None, *, kind=None):
        state.calls.append(("query", threading.get_ident()))
        if kind == "input":
            if state.default_index is None:
                raise RuntimeError("no default input")
            return dict(state.entries[state.default_index], index=state.default_index)
        return list(state.entries)

    def query_hostapis(index=None):
        apis = [{"name": "WASAPI"}, {"name": "MME"}]
        return apis if index is None else apis[index]

    def check_input_settings(**kwargs):
        state.calls.append(("check", threading.get_ident(), kwargs))

    def terminate():
        state.calls.append(("terminate", threading.get_ident()))

    def initialize():
        state.calls.append(("initialize", threading.get_ident()))
        if state.fail_initialize:
            state.fail_initialize = False
            raise RuntimeError("PortAudio init failed")
        if state.next_entries is not None:
            state.entries = state.next_entries
            state.next_entries = None

    class Stream:
        def __init__(self, **kwargs):
            state.calls.append(("open", threading.get_ident(), kwargs))
            self.kwargs = kwargs
            self.closed = False
            state.streams.append(self)

        def start(self):
            state.calls.append(("start", threading.get_ident()))

        def abort(self):
            state.calls.append(("abort", threading.get_ident()))

        def stop(self):
            state.calls.append(("stop", threading.get_ident()))

        def close(self):
            self.closed = True
            state.calls.append(("close", threading.get_ident()))

        def write(self, samples):
            pass

    state.sd = SimpleNamespace(
        query_devices=query_devices, query_hostapis=query_hostapis,
        check_input_settings=check_input_settings, _terminate=terminate,
        _initialize=initialize, default=SimpleNamespace(device=(1, 0)),
        InputStream=Stream, OutputStream=Stream,
    )
    monkeypatch.setitem(sys.modules, "sounddevice", state.sd)
    return state


@pytest.mark.asyncio
async def test_lists_inputs_and_marks_actual_default_off_thread(devices):
    from redlotus.TTS import InputDevice

    main_thread = threading.get_ident()
    result = await AudioDevices.inputs()
    assert result == [
        InputDevice(1, "Built-in Mic", "WASAPI", True),
        InputDevice(2, "USB Mic", "MME", False),
    ]
    assert all(call[1] != main_thread for call in devices.calls)
    with pytest.raises(FrozenInstanceError):
        result[0].index = 8
    devices.default_index = None
    assert all(not item.is_default for item in await AudioDevices.inputs())


@pytest.mark.asyncio
async def test_selected_capture_pins_identity_and_re_resolves_after_refresh(devices):
    selected = (await AudioDevices.inputs())[1]
    capture = AudioCapture(device=selected)
    await capture.check_available()
    assert capture.input_device == selected
    assert devices.calls[-1][2]["device"] == 2
    devices.next_entries = [devices.entries[2], devices.entries[0], devices.entries[1]]
    await AudioDevices.inputs(refresh=True)
    await capture.start()
    assert capture.input_device.index == 0
    assert devices.streams[0].kwargs["device"] == 0
    assert devices.sd.default.device == (1, 0)
    await capture.close()
    assert devices.streams[0].closed


@pytest.mark.asyncio
async def test_default_capture_uses_checked_index_when_identical_names_are_stable(devices):
    devices.entries.append({"name": "Built-in Mic", "hostapi": 0, "max_input_channels": 1})
    capture = AudioCapture()
    await capture.check_available()
    await capture.start()
    assert capture.input_device.index == 1
    assert devices.streams[0].kwargs["device"] == 1
    await capture.close()


@pytest.mark.asyncio
async def test_new_selection_of_duplicate_device_works_after_old_choice_becomes_ambiguous(devices):
    previous = (await AudioDevices.inputs())[1]
    devices.next_entries = [
        {"name": "USB Mic", "hostapi": 1, "max_input_channels": 1},
        {"name": "USB Mic", "hostapi": 1, "max_input_channels": 1},
    ]
    current = await AudioDevices.inputs(refresh=True)
    with pytest.raises(SpeechUnavailable, match="所选麦克风不可用，请重新选择。"):
        await AudioCapture(device=previous).check_available()
    capture = AudioCapture(device=current[1])
    await capture.check_available()
    await capture.start()
    assert devices.streams[0].kwargs["device"] == 1
    await capture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [[], [
    {"name": "USB Mic", "hostapi": 1, "max_input_channels": 1},
    {"name": "USB Mic", "hostapi": 1, "max_input_channels": 1},
]])
async def test_selected_device_disappears_or_becomes_ambiguous(devices, replacement):
    selected = (await AudioDevices.inputs())[1]
    capture = AudioCapture(device=selected)
    await capture.check_available()
    devices.next_entries = replacement
    await AudioDevices.inputs(refresh=True)
    with pytest.raises(SpeechUnavailable, match="所选麦克风不可用，请重新选择。"):
        await capture.start()
    assert devices.streams == []


@pytest.mark.asyncio
async def test_no_default_still_lists_devices_but_default_capture_has_native_cause(devices):
    devices.default_index = None
    assert len(await AudioDevices.inputs()) == 2
    with pytest.raises(SpeechUnavailable, match="系统默认输入设备和权限") as failed:
        await AudioCapture().check_available()
    assert isinstance(failed.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_selected_format_failure_has_friendly_message_and_native_cause(devices):
    selected = (await AudioDevices.inputs())[1]
    native = RuntimeError("unsupported native format")

    def fail_format(**kwargs):
        raise native

    devices.sd.check_input_settings = fail_format
    with pytest.raises(SpeechUnavailable, match="所选麦克风不可用，请重新选择。") as failed:
        await AudioCapture(device=selected).check_available()
    assert failed.value.__cause__ is native
    assert devices.streams == []


@pytest.mark.asyncio
async def test_refresh_rejected_while_capture_or_player_is_open(devices):
    capture = AudioCapture()
    await capture.start()
    with pytest.raises(SpeechBusy, match="请先结束录音或播报，再刷新设备。"):
        await AudioDevices.inputs(refresh=True)
    await capture.close()

    entered = asyncio.Event()

    async def source():
        entered.set()
        await asyncio.Event().wait()
        yield PCMChunk(np.zeros(1, dtype=np.float32), 24000)

    player = AudioPlayer()
    task = asyncio.create_task(player.play(source()))
    await asyncio.wait_for(entered.wait(), 2)
    with pytest.raises(SpeechBusy, match="请先结束录音或播报，再刷新设备。"):
        await AudioDevices.inputs(refresh=True)
    await player.stop()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not any(call[0] == "terminate" for call in devices.calls)
    await AudioDevices.inputs(refresh=True)


@pytest.mark.asyncio
async def test_failed_native_close_keeps_refresh_guard_until_close_retries(devices):
    capture = AudioCapture()
    await capture.start()
    native_close = devices.streams[0].close
    first = True

    def close_once():
        nonlocal first
        if first:
            first = False
            raise RuntimeError("native close failed")
        native_close()

    devices.streams[0].close = close_once
    with pytest.raises(RuntimeError, match="native close failed"):
        await capture.close()
    with pytest.raises(SpeechBusy):
        await AudioDevices.inputs(refresh=True)
    await capture.close()
    await AudioDevices.inputs(refresh=True)


@pytest.mark.asyncio
async def test_refresh_does_not_terminate_while_native_open_is_in_flight(devices):
    entered = threading.Event()
    release = threading.Event()
    original_start = devices.sd.InputStream.start

    def blocked_start(self):
        entered.set()
        release.wait(2)
        original_start(self)

    devices.sd.InputStream.start = blocked_start
    capture = AudioCapture()
    task = asyncio.create_task(capture.start())
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 2)
    refresh = asyncio.create_task(AudioDevices.inputs(refresh=True))
    try:
        await asyncio.sleep(.02)
        assert not any(call[0] == "terminate" for call in devices.calls)
    finally:
        release.set()
    await task
    with pytest.raises(SpeechBusy):
        await refresh
    await capture.close()


@pytest.mark.asyncio
async def test_refresh_rejected_while_capture_preflight_is_in_flight(devices, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    capture = AudioCapture()

    async def blocked_check():
        entered.set()
        await release.wait()
        capture.input_device = (await AudioDevices.inputs())[0]

    monkeypatch.setattr(capture, "check_available", blocked_check)
    task = asyncio.create_task(capture.start())
    await asyncio.wait_for(entered.wait(), 2)
    try:
        with pytest.raises(SpeechBusy, match="请先结束录音或播报，再刷新设备。"):
            await AudioDevices.inputs(refresh=True)
    finally:
        release.set()
    await task
    await capture.close()
    assert not any(call[0] == "terminate" for call in devices.calls)


@pytest.mark.asyncio
async def test_close_waits_for_opening_preflight_before_releasing_refresh_guard(devices, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    capture = AudioCapture()

    async def blocked_check():
        entered.set()
        await release.wait()
        capture.input_device = (await AudioDevices.inputs())[0]

    monkeypatch.setattr(capture, "check_available", blocked_check)
    start = asyncio.create_task(capture.start())
    await asyncio.wait_for(entered.wait(), 2)
    closing = asyncio.create_task(capture.close())
    try:
        await asyncio.sleep(.02)
        assert not closing.done()
        with pytest.raises(SpeechBusy):
            await AudioDevices.inputs(refresh=True)
    finally:
        release.set()
    await start
    await closing
    assert devices.streams[0].closed


@pytest.mark.asyncio
async def test_cancelled_refresh_drains_reinitialization_before_return(devices):
    entered = threading.Event()
    release = threading.Event()
    original_initialize = devices.sd._initialize

    def blocked_initialize():
        entered.set()
        release.wait(2)
        original_initialize()

    devices.sd._initialize = blocked_initialize
    task = asyncio.create_task(AudioDevices.inputs(refresh=True))
    assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 2)
    task.cancel()
    try:
        await asyncio.sleep(.02)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [call[0] for call in devices.calls if call[0] in {"terminate", "initialize"}] == [
        "terminate", "initialize",
    ]
    assert len(await AudioDevices.inputs()) == 2


@pytest.mark.asyncio
async def test_failed_reinitialization_can_be_retried(devices):
    devices.fail_initialize = True
    with pytest.raises(RuntimeError, match="PortAudio init failed"):
        await AudioDevices.inputs(refresh=True)
    assert len(await AudioDevices.inputs(refresh=True)) == 2
    assert [call[0] for call in devices.calls].count("initialize") == 2
