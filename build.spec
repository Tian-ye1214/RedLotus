# -*- mode: python ; coding: utf-8 -*-
# PyInstaller onedir：与 main.py 同目录执行
#   pyinstaller build.spec

import importlib.util
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, copy_metadata
from playwright.sync_api import sync_playwright

project = os.path.dirname(os.path.abspath(SPEC))
source_root = Path(project, "src", "redlotus")
bundle_mode = os.environ.get("REDLOTUS_PYINSTALLER_MODE", "onedir")
# Match Windows' loader order: Qt uses the OS ICU, while unrelated tools on PATH
# can supply an incompatible ICU DLL with the same unversioned filename.
if os.name == "nt":
    os.environ["PATH"] = str(Path(os.environ["SystemRoot"], "System32")) + os.pathsep + os.environ.get("PATH", "")
# Playwright's frozen transport uses its package-local browser installation.
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
with sync_playwright() as playwright:
    if not Path(playwright.chromium.executable_path).is_file():
        raise SystemExit('Before building: set PLAYWRIGHT_BROWSERS_PATH=0 and run python -m playwright install chromium')

for package in ("sherpa_onnx", "sounddevice", "pysilk", "soxr", "num2words"):
    if importlib.util.find_spec(package) is None:
        raise SystemExit("Before building a speech-enabled EXE: install RedLotus[speech]")

if importlib.util.find_spec("PySide6") is None:
    raise SystemExit("Before building a pets-enabled EXE: install RedLotus[pets]")


def resource_files(source: Path, destination: str):
    """Collect only the files beneath an explicitly approved resource root."""
    return [
        (str(path), str(Path(destination, path.relative_to(source).parent)))
        for path in source.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.relative_to(source).parts
        and path.name not in {".env", "config.json"}
        and path.suffix not in {".pyc", ".pyo"}
        and not path.name.endswith(".log")
        and ".log." not in path.name
        and not any(part in {"model", ".downloads", ".staging", ".locks"} for part in path.relative_to(source).parts)
        and path.suffix not in {".onnx", ".part"}
        and not path.name.endswith(".tar.bz2")
    ]


datas = [
    (str(Path(project, "LICENSE")), "."),
    (str(source_root / "api" / "config.yaml.example"), "redlotus/api"),
    (str(source_root / "TTS" / "catalog.json"), "redlotus/TTS"),
    *[
        (str(path), str(Path("redlotus/static/pets", path.relative_to(source_root / "static" / "pets").parent)))
        for pattern in ("pets.json", "ASSET-NOTICE.md", "*/pet.json", "*/sprites.png")
        for path in (source_root / "static" / "pets").glob(pattern)
    ],
    *resource_files(source_root / "tools" / "skills", "redlotus/tools/skills"),
    *[
        (str(path), "redlotus/prompts")
        for path in (source_root / "prompts").glob("*.md")
    ],
]

a = Analysis(
    [os.path.join(project, "main.py")],
    pathex=[project, os.path.join(project, "src")],
    binaries=[
        *collect_dynamic_libs("sherpa_onnx"),
        *collect_dynamic_libs("pysilk"),
        *collect_dynamic_libs("soxr"),
        *collect_dynamic_libs("_sounddevice_data"),
    ],
    datas=[
        *datas,
        *copy_metadata("genai_prices"),
        *copy_metadata("pydantic_ai_slim"),
        *copy_metadata("sherpa-onnx"),
        *copy_metadata("sounddevice"),
        *copy_metadata("silk-python"),
        *copy_metadata("soxr"),
        *copy_metadata("num2words"),
        *copy_metadata("PySide6"),
        *copy_metadata("PySide6_Essentials"),
        *copy_metadata("shiboken6"),
    ],
    hiddenimports=[
        "redlotus.pets.desktop", "redlotus.pets.model", "redlotus.pets.factory", "redlotus.pets.service",
        "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets",
        "redlotus.TTS.audio", "redlotus.TTS.asr", "redlotus.TTS.tts", "redlotus.TTS.service",
        "sherpa_onnx", "sounddevice", "_sounddevice_data", "pysilk",
        "pysilk.backends.cython._silk", "pysilk.backends.cffi._silk", "soxr",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Optional SDK imports found in a shared development environment: notebook
    # display, local tensor embeddings, and the unused Hugging Face gateway.
    # RedLotus uses its configured HTTP embedding service and four SDK protocols;
    # Skill scripts still run in the configured external interpreter.
    excludes=["IPython", "torch", "transformers", "huggingface_hub"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

if bundle_mode == "onefile":
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        name="Agent",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="Agent",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
    )

    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="Agent",
    )
