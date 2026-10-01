"""Real subprocess lifecycle tests, without loading Qt."""
import asyncio
import importlib.util
import json
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
if mode in ('error', 'empty_error'):
    print(json.dumps({'event': 'error', 'error': 'broken atlas' if mode == 'error' else ''}), flush=True)
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
        assert await service.command([]) == ""
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
async def test_control_commands_are_silent_and_status_remains_explicit():
    service = ChildService()
    try:
        for parts in ([], ["on"], ["on", "charcoal"], ["on", "ivory"], ["on", "ivory"]):
            assert await service.command(parts) == ""
        report = await service.command(["status"])
        assert "运行中" in report and "ivory" in report and "100%" in report
        for parts in (["off"], ["off"], [], []):
            assert await service.command(parts) == ""
        assert "已关闭" in await service.command(["status"])
    finally:
        await service.close()
    assert not service._tasks and all(process.returncode is not None for process in service.launched)


@pytest.mark.asyncio
@pytest.mark.parametrize("parts", [[], ["on"]])
@pytest.mark.parametrize("mode,reason", [("error", "broken atlas"), ("empty_error", "失败"), ("timeout", "超时")])
async def test_command_start_failure_remains_visible(parts, mode, reason):
    service = ChildService(mode)
    service.START_TIMEOUT = .25
    try:
        assert reason in await service.command(parts)
        assert (await service.status()).state == "failed"
        assert all(process.returncode is not None for process in service.launched)
    finally:
        await service.close()


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


class MessageService(ProcessPetService):
    STOP_TIMEOUT = .15
    WRITE_TIMEOUT = .15

    def __init__(self, path, mode):
        super().__init__()
        self.path, self.mode = path, mode
        self.scales = []

    def _command(self, character):
        self.scales.append(self._scale)
        code = """
import json, pathlib, sys, time
path, mode, character = sys.argv[1:]
print(json.dumps({'event':'ready','character':character}), flush=True)
if mode == 'scale':
    print(json.dumps({'event':'scale','scale':1.75}), flush=True)
if mode == 'blocked': time.sleep(30)
for line in sys.stdin.buffer:
    message = json.loads(line)
    with pathlib.Path(path).open('a', encoding='utf-8') as out:
        out.write(json.dumps({'at':time.monotonic(),'message':message})+'\\n')
"""
        return [sys.executable, "-u", "-c", code, str(self.path), self.mode, character]


async def records(path, predicate):
    async with asyncio.timeout(3):
        while True:
            rows = await asyncio.to_thread(
                lambda: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                if path.exists() else [])
            if predicate(rows):
                return rows
            await asyncio.sleep(.01)


async def test_fast_stream_coalesces_unicode_and_final_snapshot_is_authoritative(tmp_path):
    path = tmp_path / "messages.jsonl"
    service = MessageService(path, "read")
    try:
        assert (await service.start()).state == "running"
        await service.publish_reply(reply_id="1", phase="start", text="")
        for _ in range(1000):
            await service.publish_reply(reply_id="1", phase="delta", text="你好🪷" * 20)
        final = "最终结果\n" + "🪷正文" * 12000
        await service.publish_reply(reply_id="1", phase="done", text=final)
        rows = await records(path, lambda rows: rows and rows[-1]["message"]["phase"] == "done")
        message = rows[-1]["message"]
        assert message["text"] == final[-32768:] and message["truncated"]
        assert message["reply_id"] == "1" and len(rows) <= 2
        assert len(json.dumps(message, ensure_ascii=False).encode("utf-8")) < 262144
        await service.publish_reply(reply_id="1", phase="delta", text="late")
        await asyncio.sleep(.08)
        assert len((await records(path, bool))) == len(rows)
    finally:
        await service.close()
    assert not service._tasks


async def test_clear_and_next_reply_reject_late_previous_fragments(tmp_path, monkeypatch):
    path = tmp_path / "messages.jsonl"
    service = MessageService(path, "read")
    try:
        await service.start()
        sent_at = []
        writer = service._process.stdin
        write = writer.write

        def record_write(packet):
            sent_at.append(asyncio.get_running_loop().time())
            write(packet)

        monkeypatch.setattr(writer, "write", record_write)
        await service.publish_reply(reply_id="1", phase="start", text="")
        await service.publish_reply(reply_id="1", phase="delta", text="old text")
        await records(path, lambda rows: rows and rows[-1]["message"]["text"] == "old text")
        await service.clear_reply()
        await service.publish_reply(reply_id="1", phase="start", text="")
        await service.publish_reply(reply_id="1", phase="done", text="must not reappear")
        await records(path, lambda rows: rows and rows[-1]["message"]["phase"] == "clear")
        await service.publish_reply(reply_id="2", phase="start", text="")
        await service.publish_reply(reply_id="2", phase="delta", text="new text")
        await service.publish_reply(reply_id="1", phase="start", text="")
        await service.publish_reply(reply_id="1", phase="delta", text="stale")
        await service.publish_reply(reply_id="2", phase="cancelled", text="")
        rows = await records(path, lambda rows: rows and rows[-1]["message"]["phase"] == "cancelled")
        assert rows[-1]["message"]["text"] == "new text"
        assert len(sent_at) == len(rows)
        sent_intervals = [b - a for a, b in zip(sent_at, sent_at[1:])]
        received_intervals = [b["at"] - a["at"] for a, b in zip(rows, rows[1:])]
        assert all(interval >= .035 for interval in sent_intervals), (sent_intervals, received_intervals)
        assert [r["message"]["seq"] for r in rows] == sorted({r["message"]["seq"] for r in rows})
    finally:
        await service.close()


async def test_off_does_not_collect_or_replay_replies(tmp_path):
    path = tmp_path / "messages.jsonl"
    service = MessageService(path, "read")
    await service.publish_reply(reply_id="1", phase="start", text="")
    await service.publish_reply(reply_id="1", phase="done", text="private old reply")
    try:
        await service.start()
        await asyncio.sleep(.1)
        assert not path.exists()
        await service.publish_reply(reply_id="1", phase="start", text="")
        await service.publish_reply(reply_id="1", phase="delta", text="late")
        await asyncio.sleep(.06)
        assert not path.exists()
    finally:
        await service.close()


async def test_scale_receipt_survives_stop_and_character_switch(tmp_path):
    service = MessageService(tmp_path / "messages.jsonl", "scale")
    try:
        await service.start()
        async with asyncio.timeout(2):
            while (await service.status()).scale != 1.75:
                await asyncio.sleep(.01)
        assert "175%" in str(await service.status())
        await service.select("ivory")
        await service.stop()
        await service.start()
        assert service.scales == [1, 1.75, 1.75]
        assert ProcessPetService()._scale == 1
    finally:
        await service.close()


async def test_blocked_reader_cannot_block_publishing_or_leave_background_tasks(tmp_path):
    service = MessageService(tmp_path / "unused.jsonl", "blocked")
    try:
        await service.start()
        await service.publish_reply(reply_id="1", phase="start", text="")
        for _ in range(3):
            async with asyncio.timeout(.05):
                await service.publish_reply(reply_id="1", phase="delta", text="🪷" * 32768)
            await asyncio.sleep(.06)
        ticks = 0
        async with asyncio.timeout(4):
            while (await service.status()).state != "failed":
                ticks += 1
                await asyncio.sleep(.005)
        assert ticks > 2 and (await service.status()).error
    finally:
        await service.close()
    assert not service._tasks


async def test_clear_while_encoding_discards_old_frame_and_keeps_sender_alive(tmp_path, monkeypatch):
    import threading
    from redlotus.pets import service as module

    entered, release = threading.Event(), threading.Event()
    native = json.dumps
    def delayed(value, **kwargs):
        if value.get("reply_id") == "1":
            entered.set()
            assert release.wait(3)
        return native(value, **kwargs)
    monkeypatch.setattr(module.json, "dumps", delayed)
    path = tmp_path / "messages.jsonl"
    service = MessageService(path, "read")
    try:
        await service.start()
        await service.publish_reply(reply_id="1", phase="start", text="")
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(.005)
        await service.clear_reply()
        await service.publish_reply(reply_id="2", phase="start", text="")
        await service.publish_reply(reply_id="2", phase="done", text="new reply")
        release.set()
        rows = await records(path, lambda rows: rows and rows[-1]["message"]["text"] == "new reply")
        assert all(row["message"]["reply_id"] != "1" for row in rows)
    finally:
        release.set()
        await service.close()
