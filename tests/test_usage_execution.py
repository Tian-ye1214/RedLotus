"""Native command and environment regressions from U03–U12."""

import asyncio
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from redlotus.tools import execution as runner
from redlotus.core.agents import execution_role


@pytest.mark.parametrize(
    "code",
    ["print('os.kill(1, 9)')", "class Enemy:\n def kill(self): pass\nEnemy().kill()"],
)
def test_python_text_and_business_methods_are_not_process_control(tmp_path, code):
    with execution_role("worker"):
        runner.validate_agent_command(["python", "-c", code], cwd=str(tmp_path))


def test_javascript_business_method_is_allowed(tmp_path):
    with execution_role("worker"):
        runner.validate_agent_command(
            ["node", "-e", "const enemy={kill(){}}; enemy.kill()"], cwd=str(tmp_path)
        )


@pytest.mark.parametrize(
    "code",
    [
        "import subprocess as sp; p=sp.Popen(['echo','ok']); p.wait()",
        "from subprocess import Popen; Popen(['echo','ok']).communicate()",
        "import subprocess;\nwith subprocess.Popen(['echo','ok']) as p: p.communicate()",
    ],
)
def test_explicitly_waited_children_remain_allowed(tmp_path, code):
    with execution_role("worker"):
        runner.validate_agent_command(["python", "-c", code], cwd=str(tmp_path))


def test_known_unwaited_child_is_rejected_before_start(tmp_path):
    with execution_role("worker"), pytest.raises(PermissionError, match="Background"):
        runner.validate_agent_command(
            ["python", "-c", "import subprocess; subprocess.Popen(['echo','ok'])"],
            cwd=str(tmp_path),
        )


@pytest.mark.parametrize(
    "code",
    [
        "import subprocess; subprocess.run(['taskkill', '/?'])",
        "from subprocess import run as launch; launch(['taskkill', '/?'])",
        "import os as system; system.kill(123, 9)",
        "import subprocess as sp; p=sp.Popen(['echo','ok']); p.terminate()",
    ],
)
def test_known_process_apis_and_static_wrappers_are_rejected(tmp_path, code):
    with execution_role("worker"), pytest.raises(PermissionError):
        runner.validate_agent_command(["python", "-c", code], cwd=str(tmp_path))


def test_utf16_powershell_is_read_without_rewriting(tmp_path):
    path = tmp_path / "正常脚本.ps1"
    path.write_text("Write-Output 'ok'", encoding="utf-16")
    before = path.read_bytes()
    with execution_role("worker"):
        runner.validate_agent_command(
            ["powershell", "-File", str(path)], cwd=str(tmp_path)
        )
    assert path.read_bytes() == before


def test_user_configuration_paths_are_inherited(tmp_path, monkeypatch):
    home = str(tmp_path / "existing-user")
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USERPROFILE", home)
    monkeypatch.setenv("UNLISTED_MODEL_SECRET", "never-export-this")
    config = runner._execution_config()
    variables = runner._build_execution_variables(
        config,
        root=tmp_path / "venv",
        cache=tmp_path / "cache",
        project_id="project",
        overrides=None,
    )
    assert variables["HOME"] == home
    assert variables["USERPROFILE"] == home
    assert "UNLISTED_MODEL_SECRET" not in variables
    assert Path(variables["TEMP"]).is_relative_to(tmp_path / "cache")


async def test_non_python_command_does_not_resolve_or_create_python(
    tmp_path, monkeypatch
):
    config = runner._execution_config()
    config.update(
        python_executable=str(tmp_path / "missing-python.exe"),
        environment_dir=str(tmp_path / "env"),
        cache_dir=str(tmp_path / "cache"),
    )
    monkeypatch.setattr(runner, "_execution_config", lambda: deepcopy(config))
    command = (
        ["cmd", "/c", "echo", "audit-ok"]
        if sys.platform == "win32"
        else ["echo", "audit-ok"]
    )
    result = await runner.run_subprocess(
        command, shell=False, cwd=str(tmp_path), timeout=5
    )
    assert result.returncode == 0 and "audit-ok" in result.stdout
    assert result.python_on_path is None
    assert not (tmp_path / "env").exists()


def test_runtime_root_does_not_depend_on_other_templates(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "redlotus.core.config.runtime_dir", lambda: tmp_path / "actual-runtime"
    )
    config = dict(
        environment_dir=str(tmp_path / "env"),
        cache_dir="cache",
        python_executable="{runtime}/python",
    )
    monkeypatch.setattr(runner, "_execution_config", lambda: deepcopy(config))
    environment = runner.get_execution_environment(cwd=tmp_path, python_required=False)
    assert environment.cache == (tmp_path / "actual-runtime" / "cache").resolve()


def test_same_base_interpreter_can_be_reached_through_a_venv():
    assert runner._python_identity((sys.executable,)) == runner._python_identity(
        (sys._base_executable,)
    )


def test_environment_in_creation_is_not_reported_ready(tmp_path, monkeypatch):
    config = runner._execution_config()
    config.update(
        environment_dir=str(tmp_path / "env"), cache_dir=str(tmp_path / "cache")
    )
    monkeypatch.setattr(runner, "_execution_config", lambda: deepcopy(config))
    environment = runner.get_execution_environment(cwd=tmp_path)
    environment.python.parent.mkdir(parents=True)
    environment.python.write_bytes(b"not an executable")
    (environment.root / ".redlotus-environment.json").write_text(
        json.dumps(dict(base_command=list(environment.base_command), state="creating")),
        encoding="utf-8",
    )
    description = runner.describe_execution_environment(cwd=tmp_path)
    assert "creating" in description and "ready: yes" not in description


@pytest.mark.parametrize(
    "command", ['start "" app.exe', 'powershell -Command "Start-Process app.exe"']
)
async def test_direct_background_command_is_rejected_at_shared_entry(tmp_path, command):
    from redlotus.tools.toolkit import BasicToolkit
    from redlotus.core.agents import WorkspaceContext

    toolkit = BasicToolkit(None, workspace=WorkspaceContext.from_path(tmp_path))
    try:
        result = await toolkit.run_command(command)
        assert "background" in result.lower(), result
    finally:
        await toolkit.close()


async def test_exited_parent_with_live_inherited_pipes_is_not_reported_clean(tmp_path):
    # The only child created by this test self-exits; no external service is touched.
    child = "import time; time.sleep(.4)"
    parent = (
        "import subprocess,sys; subprocess.Popen([sys.executable,'-c',"
        + repr(child)
        + "])"
    )
    kwargs = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    proc = await asyncio.create_subprocess_exec(
        sys._base_executable,
        "-c",
        parent,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,
    )
    async with asyncio.timeout(5):
        while proc.returncode is None:
            await asyncio.sleep(0.01)
        try:
            await runner._terminate_process_tree(proc)
            assert proc.stdout.at_eof() and proc.stderr.at_eof()
        finally:
            await proc.communicate()
