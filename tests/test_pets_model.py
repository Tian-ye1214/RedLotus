"""Pure animation contracts and off-loop loading for the desktop pet."""
import asyncio
import copy
import json
import threading
from pathlib import Path

import pytest
from PIL import Image


@pytest.fixture
def manifest():
    names = ["idle_0", "idle_1", "look_0", "look_1", "happy_0", "happy_1",
             "drag_0", "drag_1", "sleep_enter", "sleep_0", "sleep_1"]
    def action(mode, priority, sequence, enter):
        return {"mode": mode, "priority": priority, "enter": enter,
                "sequence": [{"frame": frame, "duration_ms": duration} for frame, duration in sequence]}
    return {
        "format": "redlotus.pet-actions", "format_version": 1, "id": "charcoal",
        "canvas": {"width": 2, "height": 2, "sampling": "nearest"},
        "atlas": {"file": "sprites.png", "width": 8, "height": 6, "columns": 4, "rows": 3},
        "initial_action": "idle",
        "frames": {name: {"rect": [index % 4 * 2, index // 4 * 2, 2, 2]}
                   for index, name in enumerate(names)},
        "actions": {
            "idle": action("loop", 0, [("idle_0", 125), ("idle_1", 375)], []),
            "look": action("hold", 10, [("look_0", 125), ("look_1", 125)], []),
            "happy": {**action("once", 20, [("happy_0", 125), ("happy_1", 375)], []),
                      "on_complete": "resolve_pointer"},
            "drag": action("loop", 30, [("drag_0", 125), ("drag_1", 125)], []),
            "sleep": action("loop", 0, [("sleep_0", 500), ("sleep_1", 500)],
                            [{"frame": "sleep_enter", "duration_ms": 250}]),
        },
        "interaction": {"idle_after_ms": 30000, "coalesce_repeated_action": "happy", "events": {
            "pointer_enter": "look", "pointer_leave": "idle", "primary_click": "happy",
            "drag_start": "drag", "drag_end": "resolve_pointer", "idle_timeout": "sleep",
        }},
    }


def make_pet(manifest):
    from redlotus.pets.model import SpritePet
    return SpritePet(manifest, {frame: bytes([index, 40, 80, 255]) * 4
                               for index, frame in enumerate(manifest["frames"])})


def test_model_contract_is_abstract():
    from redlotus.pets.model import CHARACTERS, PetModel, SpritePet
    assert CHARACTERS == ("charcoal", "ivory")
    assert {"action", "frame_id", "size", "frames", "advance", "interact"} <= PetModel.__abstractmethods__
    assert issubclass(SpritePet, PetModel)
    with pytest.raises(TypeError):
        PetModel()


def test_idle_and_sleep_follow_durations_and_enter_sleep_only_once(manifest):
    pet = make_pet(manifest)
    assert pet.size == (2, 2)
    assert pet.frame_id == "idle_0"
    assert len(pet.frames[pet.frame_id]) == 16
    assert pet.advance(10) == "idle_0"
    assert pet.advance(10.125) == "idle_1"
    assert pet.advance(10.5) == "idle_0"
    assert pet.advance(39.999) == "idle_1"
    assert pet.advance(40) == "sleep_enter"
    assert pet.action == "sleep"
    assert pet.advance(40.25) == "sleep_0"
    assert pet.advance(40.75) == "sleep_1"
    assert pet.advance(41.25) == "sleep_0"
    assert pet.advance(140.75) == "sleep_1"


def test_late_tick_preserves_sleep_entry_time(manifest):
    pet = make_pet(manifest)
    pet.advance(10)
    assert pet.advance(42.75) == "sleep_1"
    assert pet.action == "sleep"


def test_hover_holds_last_frame_and_leaving_restarts_idle_timer(manifest):
    pet = make_pet(manifest)
    pet.interact("pointer_enter", 5)
    assert pet.action == "look"
    assert pet.frame_id == "look_0"
    assert pet.advance(5.125) == "look_1"
    assert pet.advance(55) == "look_1"
    pet.interact("pointer_enter", 55)
    assert pet.frame_id == "look_1"
    pet.interact("pointer_leave", 56)
    assert pet.action == "idle"
    assert pet.frame_id == "idle_0"
    assert pet.advance(85.999) == "idle_1"
    assert pet.advance(86) == "sleep_enter"


def test_happy_coalesces_clicks_and_resolves_current_pointer(manifest):
    pet = make_pet(manifest)
    pet.interact("pointer_enter", 1)
    pet.interact("primary_click", 1)
    assert pet.frame_id == "happy_0"
    pet.interact("primary_click", 1.25)
    assert pet.frame_id == "happy_1"
    pet.interact("pointer_leave", 1.375)
    assert pet.action == "happy"
    assert pet.advance(1.5) == "idle_0"
    assert pet.action == "idle"
    pet.interact("primary_click", 2)
    pet.interact("pointer_enter", 2.25)
    assert pet.action == "happy"
    assert pet.advance(2.5) == "look_0"
    assert pet.action == "look"


def test_late_tick_advances_resolved_animation_from_happy_completion(manifest):
    pet = make_pet(manifest)
    pet.interact("primary_click", 1)
    assert pet.advance(1.625) == "idle_1"
    assert pet.action == "idle"


def test_drag_interrupts_happy_and_ignores_lower_priority_events(manifest):
    pet = make_pet(manifest)
    pet.interact("pointer_enter", 1)
    pet.interact("primary_click", 1)
    pet.interact("drag_start", 1.125)
    assert pet.frame_id == "drag_0"
    pet.interact("primary_click", 1.25)
    assert pet.action == "drag"
    assert pet.frame_id == "drag_1"
    pet.interact("pointer_leave", 1.375)
    assert pet.action == "drag"
    pet.interact("drag_end", 1.5)
    assert pet.action == "idle"
    assert pet.frame_id == "idle_0"
    pet.interact("drag_start", 2)
    pet.interact("pointer_enter", 2.125)
    pet.interact("drag_end", 2.25)
    assert pet.action == "look"
    assert pet.frame_id == "look_0"


@pytest.mark.parametrize(("event", "action"), [
    ("pointer_enter", "look"), ("primary_click", "happy"), ("drag_start", "drag"),
])
def test_interaction_wakes_sleep_immediately(manifest, event, action):
    pet = make_pet(manifest)
    pet.advance(1)
    pet.advance(40)
    assert pet.action == "sleep"
    pet.interact(event, 40)
    assert pet.action == action


def test_animation_reports_completion_for_once_and_holds_last_frame(manifest):
    from redlotus.pets.model import SpriteAnimation
    animation = SpriteAnimation(manifest["actions"]["happy"], 10)
    assert animation.advance(10.125) == "happy_1"
    assert not animation.completed
    assert animation.advance(10.5) == "happy_1"
    assert animation.completed
    assert animation.ends_at == 10.5


@pytest.mark.parametrize(("path", "value"), [
    (("format",), "foreign.pet"),
    (("format_version",), True),
    (("id",), "unknown"),
    (("initial_action",), "missing"),
    (("canvas", "width"), 101),
    (("canvas", "height"), 0),
    (("canvas", "sampling"), "smooth"),
    (("atlas", "width"), -1),
    (("atlas", "rows"), 1),
    (("frames", "idle_0", "rect"), [-1, 0, 2, 2]),
    (("frames", "idle_0", "rect"), [8, 0, 2, 2]),
    (("frames", "idle_0", "rect"), [0, 0, 1, 2]),
    (("frames", "idle_0", "rect"), [False, 0, 2, 2]),
    (("actions", "happy", "mode"), "repeat"),
    (("actions", "happy", "on_complete"), "missing"),
    (("actions", "idle", "sequence"), []),
    (("actions", "sleep", "enter"), [{"frame": "missing", "duration_ms": 100}]),
    (("actions", "idle", "sequence"), [{"frame": "idle_0", "duration_ms": 0}]),
    (("actions", "idle", "sequence"), [{"frame": "idle_0", "duration_ms": -1}]),
    (("actions", "idle", "sequence"), [{"frame": "idle_0", "duration_ms": float("nan")}]),
    (("actions", "drag", "priority"), 0),
    (("interaction", "idle_after_ms"), 0),
    (("interaction", "events", "drag_end"), "idle"),
])
def test_rejects_invalid_manifest_before_playback(manifest, path, value):
    record = manifest
    for key in path[:-1]:
        record = record[key]
    record[path[-1]] = value
    with pytest.raises(ValueError, match="manifest"):
        make_pet(manifest)


@pytest.mark.parametrize("bad_manifest", [None, [], {}, {"canvas": []}])
def test_rejects_malformed_manifest_with_a_clear_error(bad_manifest):
    from redlotus.pets.model import SpritePet
    with pytest.raises(ValueError, match="manifest"):
        SpritePet(bad_manifest, {})


@pytest.mark.parametrize("change", ["missing", "truncated", "not_bytes"])
def test_rejects_incomplete_decoded_frames(manifest, change):
    from redlotus.pets.model import SpritePet
    frames = make_pet(manifest).frames.copy()
    if change == "missing":
        del frames["idle_0"]
    else:
        frames["idle_0"] = b"short" if change == "truncated" else [0] * 16
    with pytest.raises(ValueError, match="frame"):
        SpritePet(manifest, frames)


def test_unknown_pointer_event_does_not_change_state(manifest):
    pet = make_pet(manifest)
    pet.advance(0)
    with pytest.raises(ValueError, match="event"):
        pet.interact("surprise", 29)
    assert pet.advance(30) == "sleep_enter"


@pytest.fixture
def resources(tmp_path, monkeypatch, manifest):
    from redlotus.pets import model
    package = tmp_path / "package"
    root = package / "static" / "pets"
    for character in model.CHARACTERS:
        directory = root / character
        directory.mkdir(parents=True)
        spec = copy.deepcopy(manifest)
        spec["id"] = character
        (directory / "pet.json").write_text(json.dumps(spec), encoding="utf-8")
        with Image.new("RGBA", (8, 6)) as atlas:
            for index, frame in enumerate(spec["frames"].values()):
                x, y, width, height = frame["rect"]
                atlas.paste((index, 40, 80, 128), (x, y, x + width, y + height))
            atlas.save(directory / "sprites.png")
    catalog = {"format": "redlotus.pet-catalog", "format_version": 1,
               "pets": [{"id": name, "manifest": f"{name}/pet.json"} for name in model.CHARACTERS]}
    (root / "pets.json").write_text(json.dumps(catalog), encoding="utf-8")
    monkeypatch.setattr(model, "resource_root", lambda: package, raising=False)
    return root


async def test_load_predecodes_each_frame_off_loop_and_playback_never_reads_disk(resources, monkeypatch):
    from redlotus.pets import model
    threads = []
    root, read, convert = model.resource_root, Path.read_text, Image.Image.convert
    def locate():
        threads.append(threading.get_ident())
        return root()
    def read_text(path, *args, **kwargs):
        threads.append(threading.get_ident())
        return read(path, *args, **kwargs)
    def convert_image(image, *args, **kwargs):
        threads.append(threading.get_ident())
        return convert(image, *args, **kwargs)
    monkeypatch.setattr(model, "resource_root", locate)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Image.Image, "convert", convert_image)
    pet = await model.SpritePet.load("charcoal")
    assert threads and all(thread != threading.get_ident() for thread in threads)
    assert pet.frames["idle_0"] == bytes([0, 40, 80, 128]) * 4
    assert pet.frames["happy_1"] == bytes([5, 40, 80, 128]) * 4
    def forbidden(*args, **kwargs):
        pytest.fail("Playback must use predecoded frames")
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Image, "open", forbidden)
    pet.interact("primary_click", 1)
    assert pet.advance(1.125) == "happy_1"


async def test_delayed_read_keeps_loop_responsive_and_cancel_waits_for_worker(resources, monkeypatch):
    from redlotus.pets.model import SpritePet
    started, release, drained = threading.Event(), threading.Event(), threading.Event()
    read = Path.read_text
    def slow_read(path, *args, **kwargs):
        if path.name != "pet.json":
            return read(path, *args, **kwargs)
        started.set()
        try:
            assert release.wait(3)
            return read(path, *args, **kwargs)
        finally:
            drained.set()
    monkeypatch.setattr(Path, "read_text", slow_read)
    task = asyncio.create_task(SpritePet.load("charcoal"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), .5)
        task.cancel()
        await asyncio.sleep(.02)
        task.cancel()
        await asyncio.sleep(.02)
        assert not task.done()
        assert not drained.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert drained.is_set()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("problem", ["format", "version", "duplicate", "missing", "traversal", "not_object"])
async def test_load_rejects_invalid_catalog(resources, problem):
    from redlotus.pets.model import SpritePet
    path = resources / "pets.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    if problem == "format":
        catalog["format"] = "other"
    elif problem == "version":
        catalog["format_version"] = True
    elif problem == "duplicate":
        catalog["pets"][1] = catalog["pets"][0]
    elif problem == "missing":
        catalog["pets"].pop()
    elif problem == "traversal":
        catalog["pets"][0]["manifest"] = "../pet.json"
    else:
        catalog = []
    path.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(ValueError, match="catalog"):
        await SpritePet.load("charcoal")


@pytest.mark.parametrize("problem", ["identity", "json", "atlas_path", "atlas_size", "atlas_corrupt"])
async def test_load_rejects_invalid_manifest_or_atlas(resources, problem):
    from redlotus.pets.model import SpritePet
    path = resources / "charcoal" / "pet.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if problem == "identity":
        manifest["id"] = "ivory"
    elif problem == "atlas_path":
        manifest["atlas"]["file"] = "../../outside.png"
    elif problem == "atlas_size":
        with Image.new("RGBA", (7, 6)) as atlas:
            atlas.save(path.parent / "sprites.png")
    elif problem == "atlas_corrupt":
        (path.parent / "sprites.png").write_bytes(b"not a PNG")
    path.write_text("{broken" if problem == "json" else json.dumps(manifest), encoding="utf-8")
    with pytest.raises((ValueError, OSError)):
        await SpritePet.load("charcoal")


async def test_unknown_character_rejected_without_resource_reads(monkeypatch):
    from redlotus.pets import model
    def forbidden():
        pytest.fail("Unknown characters must be rejected before locating resources")
    monkeypatch.setattr(model, "resource_root", forbidden, raising=False)
    with pytest.raises(ValueError, match="character"):
        await model.SpritePet.load("../charcoal")
