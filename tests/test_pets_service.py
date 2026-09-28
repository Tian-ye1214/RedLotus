"""Real subprocess lifecycle tests, without loading Qt."""
import asyncio
import importlib.util
import sys

import pytest

from redlotus.pets.service import PetService, ProcessPetService


class ChildService(ProcessPetService):
    START_TIMEOUT = 1.5
    STOP_TIMEOUT = 0.15

    def __init__(self, mode="ready"):
        super().__init__()
        self.mode = mode
        self.launched = []

    def _command(self, character):
        code = """
import json, sys, time
mode, character = sys.argv[1:]
if mode == 'slow': time.sleep(.5)
if mode == 'timeout': time.sleep(20)
if mode == 'error':
    print(json.dumps({'event': 'error', 'error': 'broken atlas'}), flush=True)
    sys.exit(1)
if mode == 'stderr':
    sys.stderr.write('x' * 200000)
    sys.stderr.flush()
print(json.dumps({'event': 'ready', 'character': character}), flush=True)
if mode == 'crash':
    time.sleep(.2)
    sys.exit(7)
if mode == 'stubborn': time.sleep(20)
sys.stdin.buffer.read()
"""
        return [sys.executable, "-u", "-c", code, self.mode, character]

    async def _launch(self, character):
        await super()._launch(character)
        self.launched.append(self._process)


@pytest.mark.asyncio
async def test_start_is_idempotent_and_invalid_switch_preserves_child():
    service = ChildService()
    assert isinstance(service, PetService)
    try:
        first, second = await asyncio.gather(service.start(), service.start())
        assert first.state == second.state == "running"
        assert first.pid == second.pid
        assert len(service.launched) == 1
        with pytest.raises(ValueError):
            await service.start("missing")
        assert (await service.status()).pid == first.pid
        switched = await service.select("ivory")
        assert switched.character == "ivory" and switched.pid != first.pid
        assert service.launched[0].returncode is not None
        await service.stop()
        assert (await service.start()).character == "ivory"
    finally:
        await service.stop()
    assert all(p.returncode is not None for p in service.launched)


@pytest.mark.asyncio
async def test_stop_cancels_pending_readiness_without_blocking_loop():
    service = ChildService("slow")
    start = asyncio.create_task(service.start())
    ticks = 0
    while not service.launched:
        await asyncio.sleep(.005)
        ticks += 1
    stopped = await service.stop()
    await start
    assert ticks and stopped.state == "off"
    assert all(p.returncode is not None for p in service.launched)
    assert not service._tasks


@pytest.mark.asyncio
async def test_concurrent_toggles_and_caller_cancel_reap_children():
    service = ChildService("slow")
    operations = [asyncio.create_task(service.toggle()) for _ in range(10)]
    await asyncio.gather(*operations)
    assert (await service.status()).state == "off"
    pending = asyncio.create_task(service.start())
    while not service.launched:
        await asyncio.sleep(.005)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert (await service.status()).state == "off"
    assert not service._tasks
    assert all(p.returncode is not None for p in service.launched)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,reason", [("error", "broken atlas"), ("timeout", "超时")])
async def test_failed_start_is_reported_and_reaped(mode, reason):
    service = ChildService(mode)
    service.START_TIMEOUT = .25
    result = await service.start()
    assert result.state == "failed" and reason in result.error
    assert all(p.returncode is not None for p in service.launched)
    assert not service._tasks
    await service.stop()


@pytest.mark.asyncio
async def test_crash_and_backpressure_do_not_break_controller():
    service = ChildService("crash")
    assert (await service.start()).state == "running"
    await asyncio.sleep(.4)
    assert (await service.status()).state == "failed"
    assert "7" in (await service.status()).error
    service.mode = "stderr"
    assert (await service.start()).state == "running"
    await service.stop()
    assert not service._tasks


@pytest.mark.asyncio
async def test_stop_escalates_and_commands_validate_before_mutation():
    service = ChildService("stubborn")
    try:
        assert "运行" in await service.command([])
        pid = (await service.status()).pid
        assert "用法" in await service.command(["off", "extra"])
        assert "角色" in await service.command(["on", "wrong"])
        assert (await service.status()).pid == pid
        assert "charcoal" in await service.command(["status"])
        await asyncio.wait_for(service.command(["off"]), 2)
        assert (await service.status()).state == "off"
    finally:
        await service.stop()
    assert not service._tasks


@pytest.mark.asyncio
async def test_cancel_during_native_spawn_keeps_handle_until_reaped(monkeypatch):
    native_spawn = asyncio.create_subprocess_exec
    created, release = asyncio.Event(), asyncio.Event()
    children = []

    async def late_handle(*args, **kwargs):
        process = await native_spawn(*args, **kwargs)
        children.append(process)
        created.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", late_handle)
    service = ChildService()
    start = asyncio.create_task(service.start())
    await asyncio.wait_for(created.wait(), 2)
    start.cancel()
    await asyncio.sleep(.02)
    assert not start.done(), "cancellation must wait for native work to return ownership"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await start
    assert children[0].returncode is not None
    assert (await service.status()).state == "off"
    assert not service._tasks


@pytest.mark.asyncio
async def test_superseded_start_cannot_overwrite_new_selection():
    service = ChildService("slow")
    old = asyncio.create_task(service.start("charcoal"))
    while not service.launched:
        await asyncio.sleep(.005)
    try:
        switched = await service.start("ivory")
        await old
        assert switched.state == "running" and switched.character == "ivory"
        assert len(service.launched) == 2
        assert service.launched[0].returncode is not None
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_application_close_blocks_late_ui_commands():
    service = ChildService("slow")
    pending = asyncio.create_task(service.start())
    while not service.launched:
        await asyncio.sleep(.005)
    await service.close()
    await pending
    assert (await service.start("ivory")).state == "off"
    assert (await service.toggle()).state == "off"
    assert len(service.launched) == 1 and service.launched[0].returncode is not None
    assert not service._tasks


@pytest.mark.asyncio
async def test_same_character_retry_does_not_join_cancelled_start(monkeypatch):
    native = asyncio.create_subprocess_exec
    created, release = asyncio.Event(), asyncio.Event()

    async def delayed_handle(*args, **kwargs):
        process = await native(*args, **kwargs)
        created.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_handle)
    service = ChildService()
    original = asyncio.create_task(service.start("ivory"))
    await asyncio.wait_for(created.wait(), 2)
    original.cancel()
    await asyncio.sleep(.01)
    retry = asyncio.create_task(service.start("ivory"))
    await asyncio.sleep(.01)
    release.set()
    try:
        old, new = await asyncio.gather(original, retry, return_exceptions=True)
        assert isinstance(old, asyncio.CancelledError)
        assert new.state == "running" and len(service.launched) == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_repeated_caller_cancellation_finishes_complete_cleanup():
    service = ChildService()
    original = asyncio.create_task(service.start())
    while service._operation is None:
        await asyncio.sleep(0)

    def cancel_at_ready(_):
        original.cancel()
        asyncio.get_running_loop().call_soon(original.cancel)

    service._operation.add_done_callback(cancel_at_ready)
    try:
        with pytest.raises(asyncio.CancelledError):
            await original
        assert (await service.status()).state == "off"
        assert all(p.returncode is not None for p in service.launched)
        assert not service._tasks
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_source_child_uses_current_installation_not_inherited_checkout(tmp_path, monkeypatch):
    package = tmp_path / "redlotus" / "pets"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "desktop.py").write_text(
        "import json,sys\nprint('OTHER_CHECKOUT',file=sys.stderr,flush=True)\n"
        "print(json.dumps({'event':'ready','character':sys.argv[1]}),flush=True)\n"
        "sys.stdin.buffer.read()\n")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.chdir(tmp_path)
    service = ProcessPetService()
    try:
        result = await service.start()
        assert "OTHER_CHECKOUT" not in service._stderr
        if importlib.util.find_spec("PySide6"):
            assert result.state == "running", result.error
        else:
            assert result.state == "failed" and "RedLotus[pets]" in result.error
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32" or importlib.util.find_spec("PySide6") is None,
                    reason="Windows pet subprocess acceptance")
async def test_abrupt_parent_termination_reaps_real_qt_child():
    import ctypes
    from ctypes import wintypes
    import json
    import os
    from redlotus.runtime.resources import resource_root

    code = """
import asyncio, json
from redlotus.pets.factory import PetFactory
async def main():
    pet = PetFactory.service()
    status = await pet.start()
    print(json.dumps({'pid':status.pid,'state':status.state,'error':status.error}),flush=True)
    await asyncio.Event().wait()
asyncio.run(main())
"""
    parent = await asyncio.create_subprocess_exec(
        sys.executable, "-u", "-c", code, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(os.environ, PYTHONPATH=str(resource_root().parent), QT_QPA_PLATFORM="offscreen"))
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = None
    try:
        ready = json.loads(await asyncio.wait_for(parent.stdout.readline(), 20))
        assert ready["state"] == "running", ready
        handle = api.OpenProcess(0x100001 | 0x1000, False, ready["pid"])
        assert handle
        parent.kill()
        await parent.wait()
        assert await asyncio.to_thread(api.WaitForSingleObject, handle, 5000) == 0
        exit_code = wintypes.DWORD()
        assert api.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and exit_code.value == 0
    finally:
        if parent.returncode is None:
            parent.kill()
        await parent.communicate()
        if handle:
            if api.WaitForSingleObject(handle, 0) != 0:
                api.TerminateProcess(handle, 1)
                await asyncio.to_thread(api.WaitForSingleObject, handle, 5000)
            api.CloseHandle(handle)
