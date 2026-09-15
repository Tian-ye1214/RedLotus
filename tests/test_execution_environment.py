"""Auxiliary execution-environment tests; the venv case uses the local test interpreter."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest


async def test_first_environment_creation_can_be_cancelled_and_retried(tmp_path):
    from redlotus.infra import subprocess_runner
    from redlotus.runtime.context import WorkspaceContext
    from redlotus.runtime.subagents import SubagentFactory, SubagentSpec

    environment = subprocess_runner.get_execution_environment(cwd=tmp_path)
    factory = SubagentFactory(1)
    spec = SubagentSpec("session", "turn", WorkspaceContext.from_path(tmp_path))

    async def run():
        return await subprocess_runner.run_subprocess(
            ["python", "-c", "print('ready')"],
            shell=False,
            cwd=str(tmp_path),
            timeout=30,
        )

    first = asyncio.create_task(factory.run(spec, run))
    try:
        async with asyncio.timeout(20):
            while not environment.python.is_file():
                await asyncio.sleep(0.01)
        started = time.monotonic()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert time.monotonic() - started < 3
        result = await factory.run(spec, run)
        assert result.returncode == 0 and "ready" in result.stdout, result.stderr
    finally:
        await factory.close()


def _config(runtime: Path) -> dict:
    from copy import deepcopy
    from redlotus.config.app_config import settings

    config = deepcopy(settings()["execution"])
    config.update(
        python_executable=sys.executable,
        environment_dir=str(runtime / "environments" / "{project_id}"),
        cache_dir=str(runtime / "cache"),
    )
    return config


async def test_python_command_is_provisioned_in_the_configured_project_environment(
    tmp_path, monkeypatch
):
    from redlotus.infra import subprocess_runner

    runtime = tmp_path / "runtime"
    monkeypatch.setattr(
        subprocess_runner, "_execution_config", lambda: _config(runtime)
    )

    result = await subprocess_runner.run_subprocess(
        ["python", "-c", "import sys; print(sys.prefix); print(sys.executable)"],
        shell=False,
        cwd=str(tmp_path),
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    environment = subprocess_runner.get_execution_environment(cwd=tmp_path)
    assert str(environment.root) in result.stdout
    assert environment.root.is_dir()
    assert str(Path(sys.executable).parent) not in result.stdout


def test_environment_only_inherits_allowlisted_values_and_keeps_explicit_overrides(
    tmp_path, monkeypatch
):
    from redlotus.infra import subprocess_runner

    runtime = tmp_path / "runtime"
    monkeypatch.setattr(
        subprocess_runner, "_execution_config", lambda: _config(runtime)
    )
    monkeypatch.setenv("UNLISTED_SECRET", "must-not-reach-child")

    environment = subprocess_runner.get_execution_environment(
        cwd=tmp_path,
        overrides={"CLAWHUB_WORKDIR": str(runtime / "clawhub")},
    )

    assert environment.variables["CLAWHUB_WORKDIR"].endswith("clawhub")
    assert "UNLISTED_SECRET" not in environment.variables

    override = subprocess_runner.get_execution_environment(
        cwd=tmp_path,
        overrides={"PATH": "C:\\explicit-path"},
    )
    first_path = override.variables["PATH"].split(os.pathsep)[0]
    assert first_path == str(override.root / "Scripts")


def test_pip_rejects_an_explicit_external_interpreter_or_install_target(tmp_path):
    from redlotus.infra.subprocess_runner import validate_pip_command

    selected = tmp_path / "venv" / "Scripts" / "python.exe"
    with pytest.raises(ValueError, match="其他 Python"):
        validate_pip_command(
            f'"C:\\other\\python.exe" -m pip install demo',
            selected_python=selected,
        )
    with pytest.raises(ValueError, match="--target"):
        validate_pip_command(
            "python -m pip install demo --target=other", selected_python=selected
        )
    with pytest.raises(ValueError, match="--user"):
        validate_pip_command(
            'powershell -Command "pip install demo --user"',
            selected_python=selected,
        )
    validate_pip_command(
        "python -c \"print('pip install --user')\"", selected_python=selected
    )
    with pytest.raises(ValueError, match="--user"):
        validate_pip_command(
            "python -I -X utf8 -m pip install demo --user",
            selected_python=selected,
        )
    with pytest.raises(ValueError, match="-t"):
        validate_pip_command("pip install demo -t other", selected_python=selected)
    with pytest.raises(ValueError, match="--python"):
        validate_pip_command(
            "uv pip install demo --python other", selected_python=selected
        )
    with pytest.raises(ValueError, match="其他 Python"):
        validate_pip_command("py -3 -m pip install demo", selected_python=selected)
    with pytest.raises(ValueError, match="其他 Python"):
        validate_pip_command(
            'cmd /d /c ""C:\\other\\python.exe" -m pip install demo"',
            selected_python=selected,
        )


async def test_existing_environment_rejects_a_changed_base_interpreter(
    tmp_path, monkeypatch
):
    from redlotus.infra import subprocess_runner

    runtime = tmp_path / "runtime"
    config = _config(runtime)
    monkeypatch.setattr(subprocess_runner, "_execution_config", lambda: config)
    environment = subprocess_runner.get_execution_environment(cwd=tmp_path)
    environment.python.parent.mkdir(parents=True)
    environment.python.write_bytes(b"placeholder")
    (environment.root / ".redlotus-environment.json").write_text(
        json.dumps({"base_command": ["C:\\other\\python.exe"]}), encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="基础解释器"):
        await subprocess_runner.ensure_execution_environment(environment)


async def test_skill_scripts_delegate_bare_python_to_the_shared_runner(
    tmp_path, monkeypatch
):
    from redlotus.runtime.context import WorkspaceContext
    from redlotus.skills import SkillsManager as skills_module
    from redlotus.skills.SkillsManager import SkillsManager

    skill_root = tmp_path / "skills" / "demo"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: demo\ndescription: test\n---\nInstructions", encoding="utf-8"
    )
    (skill_root / "check.py").write_text("print('ok')", encoding="utf-8")
    manager = SkillsManager(
        tmp_path / "skills", workspace=WorkspaceContext.from_path(tmp_path)
    )
    calls = {}

    async def run(args, **kwargs):
        from redlotus.infra.subprocess_runner import CommandResult

        calls["args"] = args
        calls["kwargs"] = kwargs
        return CommandResult("ok", "", 0, tuple(args), kwargs["cwd"])

    monkeypatch.setattr(skills_module, "run_subprocess", run)
    assert "Return code: 0" in await manager.execute_skill_script("demo", "check.py")
    assert calls["args"][0] == "python"
    assert calls["kwargs"]["workspace"].project_id == manager.workspace.project_id


async def test_clawhub_command_passes_only_explicit_environment_override(
    tmp_path, monkeypatch
):
    from redlotus.runtime.context import WorkspaceContext
    from redlotus.tools import BasicTools as basic_tools
    from redlotus.tools.BasicTools import BasicToolkit

    toolkit = BasicToolkit(None, workspace=WorkspaceContext.from_path(tmp_path))
    calls = {}

    async def run(args, **kwargs):
        from redlotus.infra.subprocess_runner import CommandResult

        calls["args"] = args
        calls["kwargs"] = kwargs
        return CommandResult("", "", 0, args, kwargs["cwd"])

    monkeypatch.setattr(basic_tools, "run_subprocess", run)
    monkeypatch.setenv("UNLISTED_SECRET", "must-not-reach-child")
    result = await toolkit.run_command("clawhub status")
    assert "return code: 0" in result.lower()
    assert calls["kwargs"]["env"].keys() == {"CLAWHUB_WORKDIR"}
    assert calls["kwargs"]["workspace"].project_id == toolkit.workspace.project_id
    await toolkit.close()
