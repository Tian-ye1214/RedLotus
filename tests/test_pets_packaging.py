"""Desktop-pet resources stay complete and independent of production originals."""

import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tomllib

import pytest

from redlotus.runtime.resources import resource_root


ROOT = Path(__file__).resolve().parents[1]
ASSETS = {
    "pets.json", "ASSET-NOTICE.md",
    "charcoal/pet.json", "charcoal/sprites.png",
    "ivory/pet.json", "ivory/sprites.png",
}
spec = importlib.util.spec_from_file_location("pet_wheel_verifier", ROOT / "scripts/verify_wheel.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def test_runtime_assets_are_complete_and_self_contained():
    root = resource_root() / "static/pets"
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    assert actual == ASSETS
    catalog = json.loads((root / "pets.json").read_text(encoding="utf-8"))
    assert catalog["format"] == "redlotus.pet-catalog"
    assert [pet["id"] for pet in catalog["pets"]] == ["charcoal", "ivory"]
    for pet in catalog["pets"]:
        manifest = root / pet["manifest"]
        pack = json.loads(manifest.read_text(encoding="utf-8"))
        assert pack["id"] == pet["id"]
        assert pack["canvas"]["width"] == pack["canvas"]["height"] == 100
        assert set(pack["actions"]) == {"idle", "look", "happy", "drag", "sleep"}
        assert len(pack["frames"]) == 14
        assert set(pack["provenance"]) == {"notice"}
        assert (manifest.parent / pack["provenance"]["notice"]).resolve() == root / "ASSET-NOTICE.md"
        png = (manifest.parent / pack["atlas"]["file"]).read_bytes()
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", png[16:24])
        assert (width, height) == (pack["atlas"]["width"], pack["atlas"]["height"])
        assert png[25] == 6  # PNG RGBA color type.
        for frame in pack["frames"].values():
            assert set(frame) == {"rect"}
            x, y, w, h = frame["rect"]
            assert w == h == 100 and 0 <= x <= width - w and 0 <= y <= height - h
        for action in pack["actions"].values():
            for frame in action["enter"] + action["sequence"]:
                assert frame["frame"] in pack["frames"] and frame["duration_ms"] > 0


def test_pets_are_a_four_file_namespace_package():
    spec = importlib.util.find_spec("redlotus.pets")
    assert spec is not None and spec.origin is None
    root = Path(next(iter(spec.submodule_search_locations)))
    assert {path.name for path in root.rglob("*.py")} == {
        "model.py", "factory.py", "service.py", "desktop.py",
    }


def test_qt_is_only_an_optional_dependency():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert not any("pyside" in item.lower() for item in project["dependencies"])
    for extra in ("pets", "all", "build"):
        assert "PySide6>=6.8,<7" in project["optional-dependencies"][extra]


def test_parent_entry_imports_without_loading_qt():
    code = """
import importlib.abc
import sys
class NoQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == 'PySide6' or fullname.startswith('PySide6.'):
            raise AssertionError('The parent process must not import Qt')
sys.meta_path.insert(0, NoQt())
from redlotus.api.base import main
from redlotus.pets.factory import PetFactory
PetFactory.service()
assert not any(name == 'PySide6' or name.startswith('PySide6.') for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            env=os.environ | {"PYTHONPATH": str(resource_root().parent)},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("sdist", [False, True])
def test_archive_checks_require_every_runtime_pet_asset(sdist):
    prefix = "redlotus-1.0/src/" if sdist else ""
    names = [prefix + "redlotus/TTS/catalog.json"]
    names += [prefix + "redlotus/static/pets/" + name for name in ASSETS]
    verifier.inspect_names(names, sdist=sdist)
    names.remove(prefix + "redlotus/static/pets/ivory/sprites.png")
    with pytest.raises(SystemExit, match="[Pp]et"):
        verifier.inspect_names(names, sdist=sdist)


@pytest.mark.parametrize("name", [
    "redlotus/static/pets/charcoal/reference.png",
    "redlotus/static/pets/charcoal/frames/idle_01.png",
    "redlotus/static/pets/generation.json",
    "redlotus-1.0/assets/pets/v0.1/charcoal/pet.json",
])
def test_archive_checks_reject_production_pet_assets(name):
    assert verifier.forbidden_asset(name)


@pytest.mark.parametrize("field", ["frame", "reference", "generation", "notice", "atlas"])
def test_archive_checks_reject_dangling_pet_manifest_paths(field):
    pack = {"id": "charcoal", "atlas": {"file": "sprites.png"},
            "frames": {"idle_01": {"rect": [0, 0, 100, 100]}},
            "provenance": {"notice": "../ASSET-NOTICE.md"}}
    if field == "frame":
        pack["frames"]["idle_01"]["file"] = "frames/idle_01.png"
    elif field in {"reference", "generation"}:
        pack["provenance"][field] = "../generation.json"
    elif field == "notice":
        pack["provenance"][field] = "../../private.md"
    else:
        pack["atlas"]["file"] = "../../private.png"
    with pytest.raises(SystemExit, match="[Pp]et"):
        verifier.inspect_pet_manifest(pack, "charcoal")


def test_wheel_metadata_requires_qt_for_each_pet_extra():
    lines = [f"Provides-Extra: {extra}" for extra in ("pets", "speech", "all", "build")]
    speech = ("sherpa-onnx==1.13.8", "sounddevice==0.5.6", "silk-python==0.2.8", "soxr==1.1.0", "num2words==0.5.14")
    lines += [f'Requires-Dist: {dependency}; extra == "{extra}"'
              for dependency in speech for extra in ("speech", "all", "build")]
    lines += [f'Requires-Dist: PySide6<7,>=6.8; extra == "{extra}"' for extra in ("pets", "all", "build")]
    verifier.inspect_metadata(lines)
    lines.pop()
    with pytest.raises(SystemExit, match="[Pp]y[Ss]ide6"):
        verifier.inspect_metadata(lines)
