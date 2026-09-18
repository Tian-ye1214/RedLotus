# -*- mode: python ; coding: utf-8 -*-
# PyInstaller onedir：与 main.py 同目录执行
#   pyinstaller build.spec

import os
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata

project = os.path.dirname(os.path.abspath(SPEC))
source_root = Path(project, "src", "redlotus")
bundle_mode = os.environ.get("REDLOTUS_PYINSTALLER_MODE", "onedir")
# PyInstaller resolves a relative runtime_tmpdir from the application's launch
# directory, so a one-file run keeps its transient _MEI directory in the
# selected project's WorkDatabase instead of the system temporary directory.
onefile_runtime_dir = str(Path("WorkDatabase") / "runtime" / "pyinstaller")


def resource_files(source: Path, destination: str):
    """Collect only the files beneath an explicitly approved resource root."""
    return [
        (str(path), str(Path(destination, path.relative_to(source).parent)))
        for path in source.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.relative_to(source).parts
        and path.name != ".env"
        and path.suffix not in {".pyc", ".pyo"}
        and not path.name.endswith(".log")
        and ".log." not in path.name
    ]


datas = [
    (str(source_root / "api" / "config.yaml.example"), "redlotus/api"),
    *resource_files(source_root / "tools" / "skills", "redlotus/tools/skills"),
    *[
        (str(path), "redlotus/prompts")
        for path in (source_root / "prompts").glob("*.md")
    ],
]

a = Analysis(
    [os.path.join(project, "main.py")],
    pathex=[project, os.path.join(project, "src")],
    binaries=[],
    datas=[
        *datas,
        *copy_metadata("genai_prices"),
        *copy_metadata("pydantic_ai_slim"),
    ],
    hiddenimports=[],
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
        runtime_tmpdir=onefile_runtime_dir,
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
