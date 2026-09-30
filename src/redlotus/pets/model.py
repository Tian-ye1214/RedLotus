"""Pure pet animation and eagerly decoded sprite resources; no GUI dependency."""
from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from redlotus.runtime.resources import finish_io, owned_path, resource_root

@dataclass(frozen=True)
class PetInfo:
    id: str
    name: str
    directory: Path
    source: str
    error: str = ""


class PetCatalog:
    """Discover two-file packs; the terminal only consumes the in-memory cache."""

    cached: tuple[PetInfo, ...] = ()

    def __init__(self):
        self._serial = asyncio.Lock()

    @staticmethod
    def roots():
        return resource_root() / "static/pets", Path.home() / ".redlotus/pets"

    @staticmethod
    def check_id(character):
        if (not isinstance(character, str) or not character or character.startswith(".")
                or any(c.isspace() or c in '/\\:' for c in character)):
            raise ValueError("Invalid pet character ID")

    @classmethod
    def _scan(cls):
        from PIL import Image
        entries = {}
        for root, source in zip(cls.roots(), ("内置", "用户")):
            if not root.exists():
                continue
            for path in sorted(root.iterdir()):
                if not path.is_dir() or path.name.startswith("."):
                    continue
                name, error = path.name, ""
                try:
                    cls.check_id(path.name)
                    directory = owned_path(root, path.name).resolve()
                    pet = SpritePet._load(directory)
                    name = pet.name
                except (OSError, ValueError, TypeError, KeyError, RecursionError, Image.DecompressionBombError) as exc:
                    error = str(exc)
                entries[path.name] = PetInfo(path.name, name, path.absolute(), source, error)
        return tuple(entries[key] for key in sorted(entries))

    async def refresh(self) -> tuple[PetInfo, ...]:
        async with self._serial:
            entries = await finish_io(asyncio.to_thread(self._scan))
            type(self).cached = entries
            return entries

    async def resolve(self, character: str) -> PetInfo:
        self.check_id(character)
        entries = await self.refresh()
        for entry in entries:
            if entry.id == character:
                if entry.error:
                    raise ValueError(f"桌宠角色 {character} 无效：{entry.error}")
                return entry
        raise ValueError(f"未知桌宠角色：{character}；使用 /pets list 查看可用角色")


class PetModel(ABC):
    """Monotonic-second animation contract consumed by the desktop window."""

    @property
    @abstractmethod
    def action(self) -> str:
        """Current action name."""

    @property
    @abstractmethod
    def frame_id(self) -> str:
        """Current decoded frame identifier."""

    @property
    @abstractmethod
    def size(self) -> tuple[int, int]:
        """Width and height of each RGBA frame."""

    @property
    @abstractmethod
    def frames(self) -> dict[str, bytes]:
        """All decoded, row-major RGBA frames."""

    @abstractmethod
    def advance(self, now: float) -> str:
        """Advance to a monotonic timestamp and return the current frame ID."""

    @abstractmethod
    def interact(self, event: str, now: float) -> None:
        """Apply a pointer event at a monotonic timestamp."""


class SpriteAnimation:
    """One entry sequence followed by a loop, held pose, or single playback."""

    def __init__(self, action: dict, started: float):
        self.started = started
        self.mode = action["mode"]
        self.enter = tuple((item["frame"], item["duration_ms"]) for item in action["enter"])
        self.sequence = tuple((item["frame"], item["duration_ms"]) for item in action["sequence"])
        self.enter_ms = sum(duration for _, duration in self.enter)
        self.sequence_ms = sum(duration for _, duration in self.sequence)
        self.ends_at = started + (self.enter_ms + self.sequence_ms) / 1000
        self.completed = False
        self.frame_id = (self.enter or self.sequence)[0][0]

    def advance(self, now: float) -> str:
        elapsed = max(0, (now - self.started) * 1000)
        self.completed = self.mode == "once" and now >= self.ends_at
        if elapsed < self.enter_ms:
            steps = self.enter
        else:
            elapsed -= self.enter_ms
            steps = self.sequence
            if self.mode == "loop":
                elapsed %= self.sequence_ms
        self.frame_id = steps[-1][0]
        for frame, duration in steps:
            if elapsed < duration:
                self.frame_id = frame
                break
            elapsed -= duration
        return self.frame_id


class SpritePet(PetModel):
    """The bundled characters' five actions and pointer-priority rules.

    The first advance or interaction starts the clock. Construction already
    exposes the initial frame so a window can paint before its first tick.
    """

    def __init__(self, manifest: dict, frames: dict[str, bytes]):
        self._validate_manifest(manifest)
        self._actions = manifest["actions"]
        self._interaction = manifest["interaction"]
        self._size = (manifest["canvas"]["width"], manifest["canvas"]["height"])
        if (not isinstance(frames, dict) or frames.keys() != manifest["frames"].keys()
                or any(not isinstance(data, bytes) or len(data) != self._size[0] * self._size[1] * 4
                       for data in frames.values())):
            raise ValueError("Invalid decoded pet frames")
        self._frames = frames.copy()
        self._action = manifest["initial_action"]
        self._animation = None
        first = self._actions[self._action]
        self._frame_id = (first["enter"] or first["sequence"])[0]["frame"]
        self._pointer_inside = False
        self._last_interaction = None

    @classmethod
    async def load(cls, character: str | Path) -> SpritePet:
        """Load all package resources off-loop and drain the worker on cancellation."""
        directory = character if isinstance(character, Path) else (await PetCatalog().resolve(character)).directory
        return await finish_io(asyncio.to_thread(cls._load, directory))

    @classmethod
    def _load(cls, directory: Path) -> SpritePet:
        from PIL import Image
        spec = json.loads(owned_path(directory, "pet.json").read_text(encoding="utf-8"))
        frames = {}
        with Image.open(owned_path(directory, "sprites.png")) as source:
            if source.format != "PNG" or any(size % 100 for size in source.size):
                raise ValueError("sprites.png must be a PNG on a 100×100 grid")
            manifest = cls._expand(spec, directory.name, source.size)
            cls._validate_manifest(manifest)
            with source.convert("RGBA") as atlas:
                for name, record in manifest["frames"].items():
                    x, y, width, height = record["rect"]
                    with atlas.crop((x, y, x + width, y + height)) as frame:
                        frames[name] = frame.tobytes()
        pet = cls(manifest, frames)
        pet.name = spec["name"]
        return pet

    @staticmethod
    def _expand(spec, character, size):
        """Translate the compact public pack into the existing animation contract."""
        modes = {"idle": "loop", "look": "hold", "happy": "once", "drag": "loop", "sleep": "loop"}
        if (not isinstance(spec, dict) or set(spec) - {"name", "actions", "enter"}
                or not isinstance(spec.get("name"), str) or not spec["name"].strip()
                or not isinstance(spec.get("actions"), dict) or spec["actions"].keys() != modes.keys()
                or not isinstance(spec.get("enter", {}), dict) or set(spec.get("enter", {})) - modes.keys()):
            raise ValueError("pet.json requires name and five actions; only enter is optional")
        columns, rows = size[0] // 100, size[1] // 100
        actions, frames = {}, {}
        for index, (action, mode) in enumerate(modes.items()):
            record = {"mode": mode, "priority": index * 10 if action != "sleep" else 0}
            for phase, steps in (("enter", spec.get("enter", {}).get(action, [])), ("sequence", spec["actions"][action])):
                if not isinstance(steps, list) or (phase == "sequence" and not steps):
                    raise ValueError(f"Invalid {action} {phase}: expected frame/duration pairs")
                record[phase] = []
                for step in steps:
                    if (not isinstance(step, list) or len(step) != 2 or any(type(n) is not int for n in step)
                            or not 0 <= step[0] < columns * rows or step[1] <= 0):
                        raise ValueError(f"Invalid {action} frame index or duration: {step}")
                    frame, duration = step
                    frames[str(frame)] = {"rect": [frame % columns * 100, frame // columns * 100, 100, 100]}
                    record[phase].append({"frame": str(frame), "duration_ms": duration})
            actions[action] = record
        actions["happy"]["on_complete"] = "resolve_pointer"
        return {"format": "redlotus.pet-actions", "format_version": 1, "id": character, "initial_action": "idle",
                "canvas": {"width": 100, "height": 100, "sampling": "nearest"},
                "atlas": {"file": "sprites.png", "width": size[0], "height": size[1], "columns": columns, "rows": rows},
                "frames": frames, "actions": actions, "interaction": {"idle_after_ms": 30000,
                "coalesce_repeated_action": "happy", "events": {"pointer_enter": "look", "pointer_leave": "idle",
                "primary_click": "happy", "drag_start": "drag", "drag_end": "resolve_pointer", "idle_timeout": "sleep"}}}

    @property
    def action(self) -> str:
        return self._action

    @property
    def frame_id(self) -> str:
        return self._frame_id

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    @property
    def frames(self) -> dict[str, bytes]:
        return self._frames

    def _activate(self, action: str, now: float) -> None:
        self._action = action
        self._animation = SpriteAnimation(self._actions[action], now)
        self._frame_id = self._animation.frame_id

    def advance(self, now: float) -> str:
        if self._animation is None:
            self._last_interaction = now
            self._activate(self._action, now)
        self._animation.advance(now)
        if self._animation.completed:
            self._activate("look" if self._pointer_inside else "idle", self._animation.ends_at)
        sleep_at = self._last_interaction + self._interaction["idle_after_ms"] / 1000
        if self._action == "idle" and now >= sleep_at:
            self._activate("sleep", sleep_at)
        self._frame_id = self._animation.advance(now)
        return self._frame_id

    def interact(self, event: str, now: float) -> None:
        if event not in self._interaction["events"] or event == "idle_timeout":
            raise ValueError(f"Unknown pet pointer event: {event}")
        self.advance(now)
        self._last_interaction = now
        if event in ("pointer_enter", "pointer_leave"):
            self._pointer_inside = event == "pointer_enter"
        target = self._interaction["events"][event]
        if target == "resolve_pointer":
            target = "look" if self._pointer_inside else "idle"
        if self._action == "drag" and event != "drag_end":
            return
        if self._action == "happy" and self._actions[target]["priority"] < self._actions["happy"]["priority"]:
            return
        if target != self._action:
            self._activate(target, now)

    @staticmethod
    def _validate_manifest(manifest: dict) -> None:
        try:
            if (manifest["format"] != "redlotus.pet-actions"
                    or type(manifest["format_version"]) is not int or manifest["format_version"] != 1
                    or not isinstance(manifest["id"], str) or not manifest["id"] or manifest["initial_action"] != "idle"):
                raise ValueError("Invalid pet manifest identity or initial action")
            canvas, atlas, frames = manifest["canvas"], manifest["atlas"], manifest["frames"]
            size = (canvas["width"], canvas["height"])
            if (any(type(value) is not int or not 0 < value <= 100 for value in size)
                    or canvas["sampling"] != "nearest"):
                raise ValueError("Invalid pet manifest canvas")
            if (any(type(atlas[key]) is not int or atlas[key] <= 0
                    for key in ("width", "height", "columns", "rows"))
                    or not isinstance(atlas["file"], str) or not atlas["file"]
                    or atlas["width"] != size[0] * atlas["columns"]
                    or atlas["height"] != size[1] * atlas["rows"]):
                raise ValueError("Invalid pet manifest atlas dimensions")
            if not isinstance(frames, dict) or not frames:
                raise ValueError("Invalid pet manifest frames")
            for name, frame in frames.items():
                rect = frame["rect"]
                if (not isinstance(name, str) or not name or not isinstance(rect, list) or len(rect) != 4
                        or any(type(value) is not int for value in rect)):
                    raise ValueError("Invalid pet manifest frame rectangle")
                x, y, width, height = rect
                if (x < 0 or y < 0 or (width, height) != size
                        or x + width > atlas["width"] or y + height > atlas["height"]):
                    raise ValueError("Pet manifest frame rectangle exceeds the atlas")
            actions = manifest["actions"]
            modes = {"idle": "loop", "look": "hold", "happy": "once", "drag": "loop", "sleep": "loop"}
            if not isinstance(actions, dict) or actions.keys() != modes.keys():
                raise ValueError("Invalid pet manifest actions")
            for name, action in actions.items():
                if action["mode"] != modes[name] or type(action["priority"]) is not int:
                    raise ValueError("Invalid pet manifest action mode or priority")
                for phase in ("enter", "sequence"):
                    steps = action[phase]
                    if not isinstance(steps, list) or (phase == "sequence" and not steps):
                        raise ValueError("Invalid pet manifest animation sequence")
                    for step in steps:
                        if (step["frame"] not in frames or type(step["duration_ms"]) is not int
                                or step["duration_ms"] <= 0):
                            raise ValueError("Invalid pet manifest animation frame or duration")
            if not (actions["drag"]["priority"] > actions["happy"]["priority"] > actions["look"]["priority"]
                    > max(actions["idle"]["priority"], actions["sleep"]["priority"])):
                raise ValueError("Invalid pet manifest action priorities")
            interaction = manifest["interaction"]
            events = {"pointer_enter": "look", "pointer_leave": "idle", "primary_click": "happy",
                      "drag_start": "drag", "drag_end": "resolve_pointer", "idle_timeout": "sleep"}
            if (type(interaction["idle_after_ms"]) is not int or interaction["idle_after_ms"] <= 0
                    or interaction["coalesce_repeated_action"] != "happy"
                    or interaction["events"] != events or actions["happy"]["on_complete"] != "resolve_pointer"):
                raise ValueError("Invalid pet manifest interaction rules")
        except (KeyError, TypeError, AttributeError, IndexError) as exc:
            raise ValueError("Invalid pet manifest structure") from exc
