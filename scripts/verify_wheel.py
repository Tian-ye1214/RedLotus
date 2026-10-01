"""Install one built wheel locally and run the regressions against that installation."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import venv
import zipfile


PET_ASSETS = {
    "ASSET-NOTICE.md",
    "charcoal/pet.json", "charcoal/sprites.png",
    "ivory/pet.json", "ivory/sprites.png",
}
SPEECH_NATIVE_ASSETS = {"redlotus_mambo.exe", "upstream-license.txt", "notice.md",
                        "third-party-notices.md"}
SPEECH_NATIVE_SOURCE = {"mambo_worker.cpp", "upstream.patch", "compacttrie.hpp",
                        "pronunciationdictionary.hpp", "streamingvocoder.hpp", "cargo.lock"}
WORKER_SHA256 = "07a051bdaf8fe7c1bd5fbbf8ad74dc07711d40d7af07a9170288c0b1cd0ce1ba"
RELEASE_SCRIPTS = {"check_structure.py", "verify_wheel.py", "build_native_speech.py",
                   "verify_live.py", "live_cases.py"}


def forbidden_asset(name: str, *, sdist: bool = False) -> bool:
    path = name.replace("\\", "/").lower()
    parts = path.split("/")
    if any(part in {"speechproducer", "zipvoice"} for part in parts):
        return True
    script_parts = parts[1:] if parts[0] == "scripts" else parts[2:] if len(parts) > 1 and parts[1] == "scripts" else None
    if script_parts is not None:
        return not (sdist and "/".join(script_parts) in RELEASE_SCRIPTS)
    if parts[-1] in {"config.json", ".env", "config.yaml", "installed.json"}:
        return True
    if any(part in {"model", "checkpoints", "weights", ".downloads", ".staging", ".locks",
                    "__pycache__"} for part in parts):
        return True
    if path.endswith((".onnx", ".tar.bz2", ".tar.gz", ".part", ".pyc", ".pyo",
                      ".pt", ".pth", ".ckpt", ".safetensors", ".npy", ".npz",
                      ".wav", ".mp3", ".flac", ".bin", ".gguf", ".ort")):
        return True
    if "/redlotus/tts/native/" in "/" + path:
        if "/tts/native/licenses/" in path:
            return not path.endswith((".txt", ".md"))
        allowed = SPEECH_NATIVE_ASSETS | (SPEECH_NATIVE_SOURCE if sdist else set())
        return not any(path.endswith("/tts/native/" + item) for item in allowed)
    if "/redlotus/tts/" in "/" + path and not path.endswith((".py", "/catalog.json")):
        return True
    if "/assets/pets/" in "/" + path:
        return True
    marker = "/redlotus/static/pets/"
    if marker in "/" + path:
        relative = ("/" + path).split(marker, 1)[1]
        parts = relative.split("/")
        return relative != "asset-notice.md" and not (
            len(parts) == 2 and parts[0] and not parts[0].startswith(".")
            and not any(c.isspace() or c == ":" for c in parts[0]) and parts[1] in {"pet.json", "sprites.png"})
    return False


def inspect_names(names, *, sdist: bool):
    catalog = "/src/redlotus/TTS/catalog.json" if sdist else "redlotus/TTS/catalog.json"
    if not any(name.endswith(catalog) for name in names):
        raise SystemExit("Speech model catalog is absent from the artifact")
    prefix = "/src/redlotus/static/pets/" if sdist else "redlotus/static/pets/"
    missing = [item for item in sorted(PET_ASSETS) if not any(name.endswith(prefix + item) for name in names)]
    if missing:
        raise SystemExit(f"Pet runtime resources are absent from the artifact: {missing}")
    pet_files = {name for name in names if "/redlotus/static/pets/" in "/" + name
                 and name.endswith(("/pet.json", "/sprites.png"))}
    for name in pet_files:
        parent = name.rsplit("/", 1)[0]
        if not {parent + "/pet.json", parent + "/sprites.png"} <= pet_files:
            raise SystemExit(f"Pet resource pack is incomplete: {parent}")
    unsafe = [name for name in names if not name.endswith("/") and forbidden_asset(name, sdist=sdist)]
    if unsafe:
        raise SystemExit(f"Artifact contains private state, model payload or production pet assets: {unsafe[:5]}")


def wheel_package_path(name: str) -> str:
    if name.startswith("redlotus/"):
        return name
    parts = name.split("/", 2)
    if (len(parts) == 3 and parts[0].endswith(".data")
            and parts[1] in {"purelib", "platlib"} and parts[2].startswith("redlotus/")):
        return parts[2]
    return ""


def inspect_native_assets(names, *, sdist: bool = False, wheel_name: str = "", expected_licenses=()):
    if sdist:
        required = {"src/redlotus/TTS/native/" + name for name in
                    ("redlotus_mambo.exe", "NOTICE.md", "UPSTREAM-LICENSE.txt",
                     "THIRD-PARTY-NOTICES.md",
                     "mambo_worker.cpp", "upstream.patch", "CompactTrie.hpp",
                     "PronunciationDictionary.hpp", "StreamingVocoder.hpp", "Cargo.lock")}
        required.add("scripts/build_native_speech.py")
        missing = [name for name in sorted(required) if not any(
            item.endswith("/" + name) for item in names)]
        if missing:
            raise SystemExit(f"Mambo source distribution is missing native release files: {missing}")
        marker = "/src/redlotus/TTS/native/licenses/"
        licenses = {item.split(marker, 1)[1] for item in names if marker in item}
        if not licenses:
            raise SystemExit("Mambo source distribution is missing native third-party licenses")
        if expected_licenses and licenses != set(expected_licenses):
            raise SystemExit("Mambo source distribution has an incomplete native license set")
        return next(item for item in names if item.endswith(
            "/src/redlotus/TTS/native/redlotus_mambo.exe"))

    paths = {wheel_package_path(name): name for name in names}
    native_names = {name for name in names if "/redlotus/TTS/native/" in "/" + name}
    native = {path for path in paths if path.startswith("redlotus/TTS/native/")}
    if not wheel_name.endswith("-win_amd64.whl"):
        if native_names:
            raise SystemExit("Windows x64 native files cannot appear in another platform wheel")
        return
    if any(not wheel_package_path(name).startswith("redlotus/TTS/native/") for name in native_names):
        raise SystemExit("Mambo native files are outside the Windows x64 package")
    if not wheel_name.endswith("-py3-none-win_amd64.whl"):
        raise SystemExit("Mambo wheel must use the py3-none-win_amd64 tag")
    required = {"redlotus/TTS/native/" + name for name in
                ("redlotus_mambo.exe", "NOTICE.md", "UPSTREAM-LICENSE.txt",
                 "THIRD-PARTY-NOTICES.md")}
    missing = sorted(required - set(paths))
    if missing:
        raise SystemExit(f"Windows x64 wheel is missing Mambo native files: {missing}")
    license_prefix = "redlotus/TTS/native/licenses/"
    licenses = {path.removeprefix(license_prefix) for path in native
                if path.startswith(license_prefix)}
    if not licenses:
        raise SystemExit("Windows x64 wheel is missing Mambo native third-party licenses")
    if expected_licenses and licenses != set(expected_licenses):
        raise SystemExit("Windows x64 wheel has an incomplete Mambo native license set")
    return paths["redlotus/TTS/native/redlotus_mambo.exe"]


def inspect_worker_digest(read, name: str):
    if hashlib.sha256(read(name)).hexdigest() != WORKER_SHA256:
        raise SystemExit("Mambo worker does not match the trusted release SHA-256")


def inspect_wheel_tag(filename: str, lines):
    if filename.endswith("-win_amd64.whl"):
        if [line for line in lines if line.startswith("Tag: ")] != ["Tag: py3-none-win_amd64"]:
            raise SystemExit("Mambo WHEEL metadata must declare py3-none-win_amd64")
        if "Root-Is-Purelib: false" not in lines:
            raise SystemExit("Mambo WHEEL metadata must declare Root-Is-Purelib: false")


def inspect_pet_manifest(pack, character):
    if (not isinstance(pack, dict) or set(pack) - {"name", "actions", "enter"}
            or not isinstance(pack.get("name"), str) or not pack["name"].strip()
            or not isinstance(pack.get("actions"), dict)
            or set(pack["actions"]) != {"idle", "look", "happy", "drag", "sleep"}):
        raise SystemExit(f"Pet {character} manifest is not a compact two-file pack")


def inspect_pet_resources(names, read):
    for name in names:
        if "/redlotus/static/pets/" in "/" + name and name.endswith("/pet.json"):
            inspect_pet_manifest(json.loads(read(name)), name.split("/")[-2])


def inspect_metadata(lines):
    requirements = [line.lower().replace(" ", "") for line in lines if line.startswith("Requires-Dist:")]
    forbidden_dependencies = {"torch", "torchaudio", "torchvision", "transformers",
                              "pytorch-lightning", "lightning", "datasets", "accelerate",
                              "peft", "safetensors", "onnx", "speechproducer", "zipvoice"}
    for line in requirements:
        package = re.match(r"[a-z0-9_.-]+", line.removeprefix("requires-dist:")).group()
        if package in forbidden_dependencies:
            raise SystemExit(f"Wheel includes a training or producer dependency: {package}")
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
    license_root = root / "src/redlotus/TTS/native/licenses"
    source_licenses = {path.relative_to(license_root).as_posix() for path in license_root.rglob("*")
                       if path.is_file() and path.suffix.lower() in {".txt", ".md"}}
    if not source_licenses:
        raise SystemExit("Mambo native third-party licenses are absent from the source checkout")
    artifacts = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else root / "dist"
    wheels = list(artifacts.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Expected exactly one wheel in the artifact directory")
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        inspect_names(names, sdist=False)
        worker = inspect_native_assets(names, wheel_name=wheels[0].name,
                                       expected_licenses=source_licenses)
        if wheels[0].name.endswith("-win_amd64.whl"):
            inspect_worker_digest(archive.read, worker)
        inspect_pet_resources(names, archive.read)
        wheel_metadata = [name for name in names if name.endswith(".dist-info/WHEEL")]
        if len(wheel_metadata) != 1:
            raise SystemExit("WHEEL metadata is missing or ambiguous")
        inspect_wheel_tag(wheels[0].name, archive.read(wheel_metadata[0]).decode("utf-8").splitlines())
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
            worker = inspect_native_assets(names, sdist=True, expected_licenses=source_licenses)
            inspect_worker_digest(lambda name: archive.extractfile(name).read(), worker)
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
        subprocess.run([str(python), "-I", "-X", "utf8", "-m", "pip", "install", "--no-index", "--no-deps", str(wheels[0])], check=True)
        library = Path(subprocess.check_output(
            [str(python), "-I", "-X", "utf8", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], encoding="utf-8").strip())
        # Reuse the caller's third-party test dependencies after the isolated install.
        (library / "test-dependencies.pth").write_text(sysconfig.get_path("purelib") + "\n", encoding="utf-8")
        env = dict(os.environ, REDLOTUS_TEST_INSTALL_ROOT=str(environment), PYTHONUTF8="1")
        # Relative to the pytest config root: expose sibling test helpers, never src/redlotus.
        subprocess.run([str(python), "-I", "-X", "utf8", "-m", "pytest", "-q", "--import-mode=importlib",
                        "-c", str(root / "pyproject.toml"), "-o", "pythonpath=tests",
                        "--basetemp", str(location / "pytest"), str(root / "tests")],
                       cwd=location, env=env, check=True)


if __name__ == "__main__":
    main()
