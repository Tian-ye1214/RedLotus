"""Distribution contract for the bundled LanceDB application."""

from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_dependencies_keep_lancedb_without_sqlite_vec():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]

    assert "lancedb>=0.30.2" in dependencies
    assert not any(dependency.lower().startswith("sqlite-vec") for dependency in dependencies)


def test_compatibility_requirements_keep_existing_skill_ecosystem_packages():
    requirements = (ROOT / "requirements.lock").read_text(encoding="utf-8")

    assert "lancedb==0.30.2" in requirements
    assert "pyarrow==24.0.0" in requirements
    assert "akshare==1.18.64" in requirements
    assert "tushare==1.4.29" in requirements
    assert "sqlite-vec==" not in requirements


def test_uv_lock_restores_the_lancedb_dependency_graph():
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")

    for package in (
        'name = "lancedb"',
        'name = "pyarrow"',
        'name = "lance-namespace"',
        'name = "lance-namespace-urllib3-client"',
    ):
        assert package in lock
    assert 'name = "sqlite-vec"' not in lock


def test_pyinstaller_uses_project_runtime_dir_without_sqlite_specific_hook():
    spec = (ROOT / "build.spec").read_text(encoding="utf-8")

    assert "sqlite_vec" not in spec
    assert "collect_dynamic_libs" not in spec
    assert "binaries=[]" in spec
    assert 'runtime_tmpdir=onefile_runtime_dir' in spec
    assert 'Path("WorkDatabase") / "runtime" / "pyinstaller"' in spec


def test_pack_script_keeps_build_work_and_cache_inside_the_project_runtime():
    script = (ROOT / "scripts" / "pack.ps1").read_text(encoding="utf-8")

    assert '$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path' in script
    assert 'Join-Path $root "WorkDatabase\\runtime\\packaging"' in script
    assert '$env:PYINSTALLER_CONFIG_DIR = $cachePath' in script
    assert '$env:UV_PROJECT_ENVIRONMENT = $venvPath' in script
    assert '$env:UV_LINK_MODE = "copy"' in script
    assert '$env:UV_NO_MANAGED_PYTHON = "1"' in script
    assert '$env:UV_PYTHON_DOWNLOADS = "never"' in script
    assert 'uv run --extra build --extra browser pyinstaller @arguments' in script
    assert '$buildExitCode = $LASTEXITCODE' in script
    assert 'exit $buildExitCode' in script


def test_manifest_includes_resources_without_development_configuration():
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "config.default.json" not in manifest
    assert "exclude src/redlotus/config.json" in manifest
    assert "graft src/redlotus/tools/skills" in manifest
    assert "include src/redlotus/api/config.yaml.example" in manifest
    excluded = project["tool"]["setuptools"]["exclude-package-data"]["redlotus"]
    assert "config.json" in excluded
    assert "api/config.yaml" in excluded
