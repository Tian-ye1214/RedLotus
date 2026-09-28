"""Pure pet animation and eagerly decoded sprite resources; no GUI dependency."""
from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod

from redlotus.runtime.resources import finish_io, owned_path, resource_root

CHARACTERS = ("charcoal", "ivory")


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
    async def load(cls, character: str) -> SpritePet:
        """Load all package resources off-loop and drain the worker on cancellation."""
        return await finish_io(asyncio.to_thread(cls._load, character))

    @classmethod
    def _load(cls, character: str) -> SpritePet:
        if character not in CHARACTERS:
            raise ValueError(f"Unknown pet character: {character}")
        from PIL import Image

        root = owned_path(resource_root(), "static/pets")
        catalog = json.loads(owned_path(root, "pets.json").read_text(encoding="utf-8"))
        try:
            if (catalog["format"] != "redlotus.pet-catalog"
                    or type(catalog["format_version"]) is not int or catalog["format_version"] != 1
                    or not isinstance(catalog["pets"], list) or len(catalog["pets"]) != len(CHARACTERS)):
                raise ValueError("Invalid pet catalog")
            entries = {}
            for entry in catalog["pets"]:
                name = entry["id"]
                if name not in CHARACTERS or name in entries or entry["manifest"] != f"{name}/pet.json":
                    raise ValueError("Invalid pet catalog character or manifest path")
                entries[name] = entry["manifest"]
        except (KeyError, TypeError) as exc:
            raise ValueError("Invalid pet catalog structure") from exc
        path = owned_path(root, entries[character])
        manifest = json.loads(path.read_text(encoding="utf-8"))
        cls._validate_manifest(manifest)
        if manifest["id"] != character:
            raise ValueError("Pet manifest does not match the catalog character")
        spec = manifest["atlas"]
        atlas_path = owned_path(path.parent, spec["file"])
        frames = {}
        with Image.open(atlas_path) as source:
            if source.format != "PNG" or source.size != (spec["width"], spec["height"]):
                raise ValueError("Pet atlas image does not match the manifest")
            with source.convert("RGBA") as atlas:
                for name, record in manifest["frames"].items():
                    x, y, width, height = record["rect"]
                    with atlas.crop((x, y, x + width, y + height)) as frame:
                        frames[name] = frame.tobytes()
        return cls(manifest, frames)

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
                    or manifest["id"] not in CHARACTERS or manifest["initial_action"] != "idle"):
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
