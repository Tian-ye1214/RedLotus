"""Install one built wheel locally and run the regressions against that installation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import venv
import zipfile


PET_ASSETS = {
    "pets.json", "ASSET-NOTICE.md",
    "charcoal/pet.json", "charcoal/sprites.png",
    "ivory/pet.json", "ivory/sprites.png",
}


def forbidden_asset(name: str) -> bool:
    path = name.replace("\\", "/").lower()
    parts = path.split("/")
    if parts[-1] in {"config.json", ".env", "config.yaml", "installed.json"}:
        return True
    if any(part in {"model", ".downloads", ".staging", ".locks", "__pycache__"} for part in parts):
        return True
    if path.endswith((".onnx", ".tar.bz2", ".part", ".pyc", ".pyo")):
        return True
    if "/redlotus/tts/" in "/" + path and not path.endswith((".py", "/catalog.json")):
        return True
    if "/assets/pets/" in "/" + path:
        return True
    marker = "/redlotus/static/pets/"
    if marker in "/" + path:
        return ("/" + path).split(marker, 1)[1] not in {item.lower() for item in PET_ASSETS}
    return False


def inspect_names(names, *, sdist: bool):
    catalog = "/src/redlotus/TTS/catalog.json" if sdist else "redlotus/TTS/catalog.json"
    if not any(name.endswith(catalog) for name in names):
        raise SystemExit("Speech model catalog is absent from the artifact")
    prefix = "/src/redlotus/static/pets/" if sdist else "redlotus/static/pets/"
    missing = [item for item in sorted(PET_ASSETS) if not any(name.endswith(prefix + item) for name in names)]
    if missing:
        raise SystemExit(f"Pet runtime resources are absent from the artifact: {missing}")
    unsafe = [name for name in names if not name.endswith("/") and forbidden_asset(name)]
    if unsafe:
        raise SystemExit(f"Artifact contains private state, model payload or production pet assets: {unsafe[:5]}")


def inspect_pet_manifest(pack, character):
    if pack.get("id") != character or pack.get("atlas", {}).get("file") != "sprites.png":
        raise SystemExit(f"Pet {character} manifest has an invalid identity or atlas path")
    if pack.get("provenance") != {"notice": "../ASSET-NOTICE.md"}:
        raise SystemExit(f"Pet {character} manifest retains a production or invalid provenance path")
    if not pack.get("frames") or any("file" in frame for frame in pack["frames"].values()):
        raise SystemExit(f"Pet {character} manifest retains individual frame paths or has no frames")


def inspect_pet_resources(names, read):
    catalog_name = next(name for name in names if name.endswith("redlotus/static/pets/pets.json"))
    prefix = catalog_name.removesuffix("pets.json")
    catalog = json.loads(read(catalog_name))
    pets = catalog.get("pets", [])
    if (catalog.get("format") != "redlotus.pet-catalog" or catalog.get("format_version") != 1
            or [(pet.get("id"), pet.get("manifest")) for pet in pets]
            != [(character, f"{character}/pet.json") for character in ("charcoal", "ivory")]):
        raise SystemExit("Pet catalog has invalid character identities or manifest paths")
    for pet in pets:
        inspect_pet_manifest(json.loads(read(prefix + pet["manifest"])), pet["id"])


def inspect_metadata(lines):
    requirements = [line.lower().replace(" ", "") for line in lines if line.startswith("Requires-Dist:")]
    for extra in ("pets", "speech", "all", "build"):
        if f"Provides-Extra: {extra}" not in lines:
            raise SystemExit(f"Wheel does not declare the {extra} extra")
        if extra != "pets":
            for dependency in ("sherpa-onnx==1.13.8", "sounddevice==0.5.6", "silk-python==0.2.8", "soxr==1.1.0", "num2words==0.5.14"):
                if not any(dependency in line and f'extra=="{extra}"' in line for line in requirements):
                    raise SystemExit(f"{extra} extra is missing {dependency}")
        if extra != "speech" and not any(
                line.split(";")[0] in {"requires-dist:pyside6<7,>=6.8", "requires-dist:pyside6>=6.8,<7"}
                and f'extra=="{extra}"' in line for line in requirements):
            raise SystemExit(f"{extra} extra is missing PySide6>=6.8,<7")
    if any(line.startswith("requires-dist:pyside6") and "extra==" not in line for line in requirements):
        raise SystemExit("PySide6 must remain an optional dependency")


def main():
    root = Path(__file__).resolve().parents[1]
    artifacts = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else root / "dist"
    wheels = list(artifacts.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Expected exactly one wheel in the artifact directory")
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        inspect_names(names, sdist=False)
        inspect_pet_resources(names, archive.read)
        metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise SystemExit("Wheel metadata is missing or ambiguous")
        lines = archive.read(metadata[0]).decode("utf-8").splitlines()
        inspect_metadata(lines)
    sdists = list(artifacts.glob("*.tar.gz"))
    if len(sdists) > 1:
        raise SystemExit("Expected at most one sdist in the artifact directory")
    if sdists:
        with tarfile.open(sdists[0], "r:gz") as archive:
            names = [member.name for member in archive.getmembers() if member.isfile()]
            inspect_names(names, sdist=True)
            inspect_pet_resources(names, lambda name: archive.extractfile(name).read())
    runtime = root / ".test-runtime"
    runtime.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wheel-check-", dir=runtime) as temporary:
        location = Path(temporary).resolve()
        if not location.is_relative_to(runtime.resolve()):
            raise RuntimeError("Wheel test environment escaped the project")
        environment = location / "env"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run([str(python), "-I", "-m", "pip", "install", "--no-index", "--no-deps", str(wheels[0])], check=True)
        library = Path(subprocess.check_output(
            [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip())
        # Reuse the caller's third-party test dependencies after the isolated install.
        (library / "test-dependencies.pth").write_text(sysconfig.get_path("purelib") + "\n", encoding="utf-8")
        env = dict(os.environ, REDLOTUS_TEST_INSTALL_ROOT=str(environment), PYTHONUTF8="1")
        # Relative to the pytest config root: expose sibling test helpers, never src/redlotus.
        subprocess.run([str(python), "-I", "-m", "pytest", "-q", "--import-mode=importlib",
                        "-c", str(root / "pyproject.toml"), "-o", "pythonpath=tests",
                        "--basetemp", str(location / "pytest"), str(root / "tests")],
                       cwd=location, env=env, check=True)


if __name__ == "__main__":
    main()
