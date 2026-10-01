"""Release artifact checks for the managed Mambo backend."""

import importlib.util
import runpy
import tomllib
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
import setuptools
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("speech_release_verifier", ROOT / "scripts/verify_wheel.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)
build_spec = importlib.util.spec_from_file_location("native_speech_builder", ROOT / "scripts/build_native_speech.py")
builder_module = importlib.util.module_from_spec(build_spec)
build_spec.loader.exec_module(builder_module)

NATIVE = "redlotus/TTS/native/"
WINDOWS_FILES = {
    NATIVE + "redlotus_mambo.exe",
    NATIVE + "NOTICE.md",
    NATIVE + "UPSTREAM-LICENSE.txt",
    NATIVE + "THIRD-PARTY-NOTICES.md",
    NATIVE + "licenses/dependency-license.txt",
}
SOURCE_FILES = {
    "redlotus-1.0/src/" + name for name in WINDOWS_FILES
} | {
    "redlotus-1.0/src/" + NATIVE + "mambo_worker.cpp",
    "redlotus-1.0/src/" + NATIVE + "upstream.patch",
    "redlotus-1.0/src/" + NATIVE + "CompactTrie.hpp",
    "redlotus-1.0/src/" + NATIVE + "PronunciationDictionary.hpp",
    "redlotus-1.0/src/" + NATIVE + "StreamingVocoder.hpp",
    "redlotus-1.0/src/" + NATIVE + "Cargo.lock",
    "redlotus-1.0/scripts/build_native_speech.py",
}


def test_windows_wheel_requires_worker_and_notices():
    verifier.inspect_native_assets(WINDOWS_FILES, wheel_name="redlotus-1.0-py3-none-win_amd64.whl")
    for missing in WINDOWS_FILES:
        with pytest.raises(SystemExit, match="[Mm]ambo|[Nn]ative|[Nn]otice"):
            verifier.inspect_native_assets(WINDOWS_FILES - {missing},
                                           wheel_name="redlotus-1.0-py3-none-win_amd64.whl")


def test_platform_wheel_can_store_python_package_in_data_purelib():
    names = {"redlotus-1.0.data/purelib/" + name for name in WINDOWS_FILES}
    verifier.inspect_native_assets(names, wheel_name="redlotus-1.0-py3-none-win_amd64.whl")


def test_native_wheel_must_have_true_windows_x64_tag():
    with pytest.raises(SystemExit, match="Windows x64|win_amd64"):
        verifier.inspect_native_assets(WINDOWS_FILES,
                                       wheel_name="redlotus-1.0-py3-none-any.whl")
    with pytest.raises(SystemExit, match="Windows x64|win_amd64"):
        verifier.inspect_native_assets(WINDOWS_FILES,
                                       wheel_name="redlotus-1.0-py3-none-win_arm64.whl")
    with pytest.raises(SystemExit, match="py3-none-win_amd64"):
        verifier.inspect_native_assets(WINDOWS_FILES,
                                       wheel_name="redlotus-1.0-cp312-cp312-win_amd64.whl")


def test_windows_wheel_metadata_must_match_its_platform_filename():
    filename = "redlotus-1.0-py3-none-win_amd64.whl"
    verifier.inspect_wheel_tag(filename, ["Root-Is-Purelib: false", "Tag: py3-none-win_amd64"])
    with pytest.raises(SystemExit, match="py3-none-win_amd64"):
        verifier.inspect_wheel_tag(filename, ["Root-Is-Purelib: false", "Tag: py3-none-any"])
    with pytest.raises(SystemExit, match="Root-Is-Purelib"):
        verifier.inspect_wheel_tag(filename, ["Root-Is-Purelib: true", "Tag: py3-none-win_amd64"])


def test_other_platform_wheel_has_no_windows_worker():
    verifier.inspect_native_assets(set(), wheel_name="redlotus-1.0-py3-none-any.whl")
    with pytest.raises(SystemExit, match="Windows x64"):
        verifier.inspect_native_assets({NATIVE + "redlotus_mambo.exe"},
                                       wheel_name="redlotus-1.0-py3-none-any.whl")
    with pytest.raises(SystemExit, match="Windows x64"):
        verifier.inspect_native_assets({"unexpected/" + NATIVE + "redlotus_mambo.exe"},
                                       wheel_name="redlotus-1.0-py3-none-any.whl")


def test_native_license_folder_accepts_only_passive_notices():
    assert not verifier.forbidden_asset(NATIVE + "licenses/dependency-license.txt")
    assert verifier.forbidden_asset(NATIVE + "licenses/installer.exe")
    assert verifier.forbidden_asset(NATIVE + "licenses/model.onnx")
    assert verifier.forbidden_asset(NATIVE + "train.py")


def test_sdist_retains_worker_notices_and_rebuild_support():
    verifier.inspect_native_assets(SOURCE_FILES, sdist=True)
    for missing in SOURCE_FILES:
        with pytest.raises(SystemExit, match="[Mm]ambo|[Nn]ative|[Nn]otice"):
            verifier.inspect_native_assets(SOURCE_FILES - {missing}, sdist=True)


def test_cargo_lock_matches_distributed_rust_notice_checksums():
    native = ROOT / "src/redlotus/TTS/native"
    packages = tomllib.loads((native / "Cargo.lock").read_text(encoding="utf-8"))["package"]
    checksums = {(item["name"], item["version"]): item["checksum"]
                 for item in packages if "checksum" in item}
    rows = [line.split(" | ") for line in (native / "THIRD-PARTY-NOTICES.md").read_text(
        encoding="utf-8").splitlines() if line.startswith("| [")]
    noticed = {tuple(row[0].removeprefix("| [").split("](", 1)[0].rsplit(" ", 1)):
               row[2].strip("`") for row in rows}
    assert len(packages) == 81
    assert len(noticed) == 51
    assert all(checksums.get(key) == digest for key, digest in noticed.items())


def test_artifact_requires_every_source_license_file():
    expected = {"dependency-license.txt", "nested/other-license.md"}
    with pytest.raises(SystemExit, match="license"):
        verifier.inspect_native_assets(WINDOWS_FILES,
                                       wheel_name="redlotus-1.0-py3-none-win_amd64.whl",
                                       expected_licenses=expected)
    with pytest.raises(SystemExit, match="license"):
        verifier.inspect_native_assets(SOURCE_FILES, sdist=True, expected_licenses=expected)


@pytest.mark.parametrize("name", [
    "redlotus-1.0/SpeechProducer/prepare.py",
    "redlotus-1.0/ZipVoice/training.py",
    "redlotus-1.0/scripts/mambo/export.py",
    "redlotus-1.0/scripts/mambo/build_native.py",
    "redlotus-1.0/scripts/train.py",
    "redlotus/TTS/native/model.pt",
    "redlotus/TTS/native/model.safetensors",
    "redlotus/TTS/native/voice.wav",
    "redlotus/TTS/model/cache.npz",
    "redlotus/TTS/checkpoints/decoder.bin",
    "redlotus/tools/skills/voice/mambo-model.tar.gz",
])
def test_artifact_rejects_producer_training_and_model_payload(name):
    assert verifier.forbidden_asset(name)


def test_sdist_allows_only_explicit_release_helpers():
    assert not verifier.forbidden_asset("redlotus-1.0/scripts/build_native_speech.py", sdist=True)
    assert not verifier.forbidden_asset("redlotus-1.0/scripts/verify_wheel.py", sdist=True)
    assert verifier.forbidden_asset("redlotus-1.0/scripts/train.py", sdist=True)


@pytest.mark.parametrize("filename", ["mambo_worker.cpp", "upstream.patch", "CompactTrie.hpp",
                                     "Cargo.lock",
                                     "PronunciationDictionary.hpp", "StreamingVocoder.hpp"])
def test_native_source_support_is_only_allowed_in_sdist(filename):
    name = NATIVE + filename
    assert verifier.forbidden_asset(name)
    assert not verifier.forbidden_asset("redlotus-1.0/src/" + name, sdist=True)


def native_builder_fixture(tmp_path):
    source = tmp_path / "source"
    rust = source / "third_party/tokenizers-cpp/rust"
    rust.mkdir(parents=True)
    build = builder_module.NativeBuild(source, tmp_path / "build", tmp_path / "sdk",
                                       tmp_path / "cmake", tmp_path / "rust-bin", tmp_path / "cargo-home")
    build.native = tmp_path / "native"
    build.native.mkdir()
    (build.native / "Cargo.lock").write_bytes(b"pinned lock")
    return build, rust / "Cargo.lock"


def test_native_builder_installs_lock_without_overwriting_unknown_state(tmp_path):
    build, target = native_builder_fixture(tmp_path)
    lock, expected = build.install_cargo_lock()
    assert lock == target
    assert target.read_bytes() == expected == b"pinned lock"
    assert build.install_cargo_lock() == (target, expected)
    target.write_bytes(b"different lock")
    with pytest.raises(ValueError, match="Cargo.lock"):
        build.install_cargo_lock()
    assert target.read_bytes() == b"different lock"


def test_native_builder_rejects_cargo_lock_drift_after_offline_build(tmp_path, monkeypatch):
    build, target = native_builder_fixture(tmp_path)
    monkeypatch.setattr(build, "prepare_source", lambda: None)
    monkeypatch.setitem(builder_module.__dict__, "os", SimpleNamespace(name="nt"))
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert target.read_bytes() == b"pinned lock"
        if "--build" in args:
            target.write_bytes(b"drifted lock")

    monkeypatch.setattr(builder_module, "subprocess", SimpleNamespace(run=run))
    with pytest.raises(RuntimeError, match="Cargo.lock"):
        build.run()
    assert len(calls) == 2
    assert not (build.native / "redlotus_mambo.exe").exists()


def test_artifact_rejects_changed_worker_bytes():
    with pytest.raises(SystemExit, match="SHA-256"):
        verifier.inspect_worker_digest(lambda _: b"not the release worker", NATIVE + "redlotus_mambo.exe")


@pytest.mark.parametrize("dependency", ["torch", "torchaudio", "transformers", "pytorch-lightning",
                                              "datasets", "speechproducer"])
def test_wheel_metadata_rejects_training_and_producer_dependencies(dependency):
    with pytest.raises(SystemExit, match="training|producer"):
        verifier.inspect_metadata([f"Requires-Dist: {dependency}>=1"])


def test_non_windows_build_filters_native_worker_and_notices(monkeypatch):
    captured = {}
    monkeypatch.setattr(setuptools, "setup", lambda **kwargs: captured.update(kwargs))
    runpy.run_path(str(ROOT / "setup.py"))
    speech_build = captured["cmdclass"]["build_py"]
    files = [r"src\redlotus\TTS\native\redlotus_mambo.exe",
             r"src\redlotus\TTS\native\NOTICE.md",
             r"src\redlotus\static\pets\ivory\sprites.png"]
    monkeypatch.setattr(build_py, "find_data_files", lambda *args: files)
    monkeypatch.setitem(speech_build.find_data_files.__globals__, "os", SimpleNamespace(name="posix"))
    monkeypatch.setitem(speech_build.find_data_files.__globals__, "Path", PurePosixPath)
    command = speech_build(setuptools.Distribution())
    assert command.find_data_files("redlotus", "src/redlotus") == files[2:]


@pytest.mark.parametrize("missing", ["redlotus_mambo.exe", "NOTICE.md", "licenses/dependency.txt"])
def test_windows_sdist_rejects_missing_native_assets_before_archiving(monkeypatch, tmp_path, missing):
    captured = {}
    monkeypatch.setattr(setuptools, "setup", lambda **kwargs: captured.update(kwargs))
    namespace = runpy.run_path(str(ROOT / "setup.py"))
    command_type = captured["cmdclass"].get("sdist", sdist)
    reached_archive = []
    monkeypatch.setattr(sdist, "run", lambda self: reached_archive.append(True))
    native = tmp_path / "native"
    (native / "licenses").mkdir(parents=True)
    for name in ("redlotus_mambo.exe", "NOTICE.md", "UPSTREAM-LICENSE.txt",
                 "THIRD-PARTY-NOTICES.md", "licenses/dependency.txt"):
        if name != missing:
            (native / name).write_bytes(b"fixture")
    runtime = namespace["SpeechWheel"].finalize_options.__globals__
    monkeypatch.setitem(runtime, "os", SimpleNamespace(name="nt"))
    monkeypatch.setitem(runtime, "NATIVE", native)
    monkeypatch.setitem(runtime, "WORKER", native / "redlotus_mambo.exe")
    monkeypatch.setitem(runtime, "NOTICES", tuple(native / name for name in (
        "NOTICE.md", "UPSTREAM-LICENSE.txt", "THIRD-PARTY-NOTICES.md")))
    with pytest.raises(RuntimeError, match="worker|notices|license"):
        command_type(setuptools.Distribution()).run()
    assert not reached_archive
