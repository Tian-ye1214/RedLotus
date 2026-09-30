"""Public two-file packs, discovery, cancellation and reload behavior."""
import asyncio
import json
import importlib.util
from pathlib import Path
import shutil
import threading

from PIL import Image
import pytest


def make_pack(root, character, *, color=(220, 50, 40, 255)):
    directory = root / character
    directory.mkdir(parents=True, exist_ok=True)
    spec = {"name": f"角色 {character}", "actions": {
        "idle": [[0, 100], [1, 200]], "look": [[2, 150]],
        "happy": [[3, 120]], "drag": [[4, 180]], "sleep": [[5, 900]],
    }, "enter": {"sleep": [[0, 350]]}}
    (directory / "pet.json").write_text(json.dumps(spec), encoding="utf-8")
    with Image.new("RGBA", (300, 200), color) as atlas:
        atlas.save(directory / "sprites.png")
    return directory


@pytest.fixture
def packs(tmp_path, monkeypatch):
    from redlotus.pets.model import PetCatalog
    builtin, user = tmp_path / "builtin", tmp_path / "user"
    for character in ("charcoal", "ivory"):
        make_pack(builtin, character)
    make_pack(user, "momo")
    monkeypatch.setattr(PetCatalog, "roots", staticmethod(lambda: (builtin, user)))
    monkeypatch.setattr(PetCatalog, "cached", ())
    return builtin, user


@pytest.mark.asyncio
async def test_discovery_accepts_third_role_overrides_and_isolates_invalid_pack(packs):
    from redlotus.pets.model import PetCatalog, SpritePet
    builtin, user = packs
    custom = make_pack(user, "charcoal", color=(30, 60, 90, 128))
    broken = make_pack(user, "broken")
    (broken / "sprites.png").write_bytes(b"broken")
    catalog = PetCatalog()
    entries = {entry.id: entry for entry in await catalog.refresh()}
    assert set(entries) == {"charcoal", "ivory", "momo", "broken"}
    assert entries["charcoal"].directory == custom.resolve()
    assert entries["charcoal"].source == "用户" and entries["ivory"].source == "内置"
    assert entries["broken"].error and not entries["momo"].error
    info = await catalog.resolve("charcoal")
    pet = await SpritePet.load(info.directory)
    assert pet.size == (100, 100) and pet.frames[pet.frame_id] == bytes([30, 60, 90, 128]) * 10000
    (custom / "pet.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="charcoal"):
        await catalog.resolve("charcoal")
    shutil.rmtree(custom)
    assert (await catalog.resolve("charcoal")).directory == (builtin / "charcoal").resolve()


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["json", "name", "action", "empty", "index", "duration", "bool", "enter", "extra", "png", "grid", "missing"])
async def test_pack_validation_is_precise_and_has_no_configurable_paths(packs, problem):
    from redlotus.pets.model import PetCatalog
    directory = packs[1] / "momo"
    path = directory / "pet.json"
    spec = json.loads(path.read_text(encoding="utf-8"))
    if problem == "name": spec["name"] = ""
    if problem == "action": spec["actions"].pop("look")
    if problem == "empty": spec["actions"]["idle"] = []
    if problem == "index": spec["actions"]["idle"][0][0] = 6
    if problem == "duration": spec["actions"]["idle"][0][1] = 0
    if problem == "bool": spec["actions"]["idle"][0][0] = True
    if problem == "enter": spec["enter"]["unknown"] = [[0, 1]]
    if problem == "extra": spec["atlas"] = {"file": "../outside.png"}
    if problem == "png": (directory / "sprites.png").write_bytes(b"broken")
    if problem == "grid":
        with Image.new("RGBA", (101, 100)) as atlas:
            atlas.save(directory / "sprites.png")
    path.write_text("{broken" if problem == "json" else json.dumps(spec), encoding="utf-8")
    if problem == "missing": path.unlink()
    entries = {entry.id: entry for entry in await PetCatalog().refresh()}
    assert entries["momo"].error and not entries["ivory"].error
    with pytest.raises(ValueError, match="momo"):
        await PetCatalog().resolve("momo")


@pytest.mark.asyncio
async def test_cached_completions_never_touch_disk(packs, monkeypatch):
    from redlotus.pets.model import PetCatalog
    from redlotus.ui.widgets import completion_for_input
    await PetCatalog().refresh()
    def forbidden(*args, **kwargs):
        pytest.fail("Completing an input must not scan or read files")
    monkeypatch.setattr(Path, "iterdir", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    assert "momo" in completion_for_input("/pets on ").choices
    assert {"list", "reload"} <= set(completion_for_input("/pets ").choices)


@pytest.mark.asyncio
async def test_reload_changes_pid_keeps_scale_and_invalid_pack_keeps_old_child(packs):
    from test_pets_service import ChildService
    service = ChildService()
    try:
        first = await service.start("momo")
        service._scale = 1.72
        assert await service.command(["reload"]) == ""
        second = await service.status()
        assert second.pid != first.pid and second.character == "momo" and second.scale == 1.72
        (packs[1] / "momo" / "pet.json").write_text("{broken", encoding="utf-8")
        assert "momo" in await service.command(["reload"])
        assert (await service.status()).pid == second.pid and (await service.status()).state == "running"
        assert "momo" in await service.command(["list"])
        assert "内置" in await service.command(["list"])
    finally:
        await service.close()
    assert not service._tasks and all(child.returncode is not None for child in service.launched)


@pytest.mark.asyncio
async def test_corrupt_image_dimensions_do_not_abort_discovery(packs, monkeypatch):
    from redlotus.pets.model import PetCatalog
    opened = Image.open
    def refuse(path, *args, **kwargs):
        if Path(path).parent.name == "momo":
            raise Image.DecompressionBombError("invalid image dimensions")
        return opened(path, *args, **kwargs)
    monkeypatch.setattr(Image, "open", refuse)
    entries = {entry.id: entry for entry in await PetCatalog().refresh()}
    assert entries["momo"].error and not entries["charcoal"].error


@pytest.mark.asyncio
async def test_close_cancels_reload_before_waiting_for_other_discovery(packs, monkeypatch):
    from test_pets_service import ChildService
    service = ChildService()
    await service.start("momo")
    info = await service._catalog.resolve("momo")
    scanning, release, prepared = threading.Event(), threading.Event(), asyncio.Event()
    def slow_scan():
        scanning.set()
        assert release.wait(5)
        return (info,)
    async def ready_later(character):
        await prepared.wait()
        return info
    monkeypatch.setattr(service._catalog, "_scan", slow_scan)
    monkeypatch.setattr(service._catalog, "resolve", ready_later)
    scan = asyncio.create_task(service.list_pets())
    assert await asyncio.to_thread(scanning.wait, 2)
    reload = asyncio.create_task(service.reload())
    await asyncio.sleep(.02)
    close = asyncio.create_task(service.close())
    try:
        await asyncio.sleep(.02)
        prepared.set()
        await asyncio.sleep(.3)
        assert len(service.launched) == 1
    finally:
        release.set()
        prepared.set()
        await asyncio.gather(scan, reload, close, return_exceptions=True)
    assert not service._tasks and all(child.returncode is not None for child in service.launched)


@pytest.mark.asyncio
async def test_repeated_reload_and_deleted_pack_do_not_leave_children(packs):
    from test_pets_service import ChildService
    service = ChildService("slow")
    try:
        await service.start("momo")
        await asyncio.gather(*(service.reload() for _ in range(6)))
        current = await service.status()
        assert current.state == "running"
        assert sum(child.returncode is None for child in service.launched) == 1
        shutil.rmtree(packs[1] / "momo")
        assert "未知" in await service.command(["reload"])
        assert (await service.status()).pid == current.pid
    finally:
        await service.close()
    assert not service._tasks and all(child.returncode is not None for child in service.launched)


@pytest.mark.asyncio
@pytest.mark.skipif(importlib.util.find_spec("PySide6") is None, reason="requires Qt")
async def test_real_child_uses_resolved_external_pack_and_reloads_it(packs, monkeypatch):
    from redlotus.pets.service import ProcessPetService
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    service = ProcessPetService()
    try:
        first = await service.start("momo")
        assert first.state == "running", first
        assert str((packs[1] / "momo").resolve()) in service._command("momo")
        make_pack(packs[1], "momo", color=(30, 70, 150, 255))
        service._scale = 1.72
        second = await service.reload()
        assert second.state == "running" and second.pid != first.pid and second.scale == 1.72
        await service.publish_reply(reply_id="1", phase="start", text="")
        await service.publish_reply(reply_id="1", phase="done", text="**换装完成**")
        await asyncio.sleep(.1)
        assert (await service.status()).state == "running"
    finally:
        await service.close()
    assert not service._tasks


@pytest.mark.asyncio
async def test_reload_while_off_does_not_launch(packs):
    from test_pets_service import ChildService
    service = ChildService()
    assert await service.command(["reload"]) == ""
    assert not service.launched and (await service.status()).state == "off"
    await service.close()


@pytest.mark.asyncio
async def test_successful_noop_does_not_replay_previous_validation_error(packs):
    from test_pets_service import ChildService
    service = ChildService()
    try:
        current = await service.start()
        assert "missing" in await service.command(["on", "missing"])
        assert await service.command(["on", "charcoal"]) == ""
        assert (await service.status()).pid == current.pid
        custom = make_pack(packs[1], "charcoal")
        (custom / "pet.json").write_text("{broken", encoding="utf-8")
        assert "无效" in await service.command(["reload"])
        assert await service.command(["on"]) == ""
        assert (await service.status()).pid == current.pid
        await service.stop()
        assert "missing" in await service.command(["on", "missing"])
        assert await service.command(["off"]) == ""
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancelled_close_still_drains_discovery(packs, monkeypatch):
    from test_pets_service import ChildService
    service = ChildService("stubborn")
    await service.start()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def scan():
        started.set()
        try:
            assert release.wait(5)
            return ()
        finally:
            finished.set()
    monkeypatch.setattr(service._catalog, "_scan", scan)
    listing = asyncio.create_task(service.list_pets())
    assert await asyncio.to_thread(started.wait, 2)
    close = asyncio.create_task(service.close())
    try:
        await asyncio.sleep(.02)
        close.cancel()
        await asyncio.sleep(.7)
        assert not close.done(), "Close must drain its discovery worker even when cancelled"
        assert not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await close
        assert finished.is_set() and not service._tasks
    finally:
        release.set()
        await asyncio.gather(close, listing, return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stop", "close", "cancel"])
async def test_reload_read_cancellation_drains_worker_and_never_launches_late(packs, monkeypatch, operation):
    from test_pets_service import ChildService
    from redlotus.pets.model import SpritePet
    service = ChildService()
    await service.start("momo")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    load = SpritePet._load
    def delayed(directory):
        started.set()
        try:
            assert release.wait(5)
            return load(directory)
        finally:
            finished.set()
    monkeypatch.setattr(SpritePet, "_load", delayed)
    task = asyncio.create_task(service.reload())
    closing = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        if operation == "cancel":
            task.cancel()
        else:
            closing = asyncio.create_task(getattr(service, operation)())
        await asyncio.sleep(.02)
        assert not task.done() and not finished.is_set()
        release.set()
        await asyncio.gather(task, *([closing] if closing else []), return_exceptions=True)
        assert finished.is_set() and len(service.launched) == 1
    finally:
        release.set()
        await asyncio.gather(task, *([closing] if closing else []), return_exceptions=True)
        await service.close()
    assert not service._tasks and all(child.returncode is not None for child in service.launched)
