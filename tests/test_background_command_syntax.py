import sys

import pytest

from redlotus.tools.execution import validate_agent_command
from redlotus.core.agents import WorkspaceContext
from redlotus.tools.toolkit import BasicToolkit


async def test_python_start_method_is_not_a_background_shell_command(tmp_path):
    toolkit = BasicToolkit(None, workspace=WorkspaceContext.from_path(tmp_path))
    command = f'"{sys.executable}" -c "import threading; t=threading.Thread(target=lambda: print(73104)); t.start(); t.join()"'
    result = await toolkit.run_command(command)
    assert "73104" in result, result
    await toolkit.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Exercises native cmd.exe quoting")
async def test_cmd_outer_quotes_preserve_quoted_program_and_arguments(tmp_path):
    import asyncio

    command = f'cmd /d /v:on /c ""{sys.executable}" -c "print(94713)" & echo CODE=!ERRORLEVEL!"'
    native = await asyncio.create_subprocess_shell(
        command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await native.communicate()
    assert native.returncode == 0 and b"94713" in stdout, stderr
    toolkit = BasicToolkit(None, workspace=WorkspaceContext.from_path(tmp_path))
    try:
        result = await toolkit.run_command(command)
        assert "94713" in result and "CODE=0" in result, result
    finally:
        await toolkit.close()


@pytest.mark.parametrize(
    "command",
    [
        'start "" app.exe',
        "echo ok & start app.exe",
        'cmd /c "start app.exe"',
        'powershell -Command "Start-Process app.exe"',
        "nohup task &",
        "echo ok; setsid task",
        'sh -c "sleep 60 &"',
    ],
)
def test_actual_background_commands_remain_blocked(tmp_path, command):
    with pytest.raises(PermissionError, match="Background"):
        validate_agent_command(command, cwd=str(tmp_path))


@pytest.mark.parametrize(
    "command",
    [
        "echo start",
        'echo "a & b"',
        "python -c \"print('start')\"",
        "powershell -Command \"Write-Output 'start'\"",
    ],
)
def test_command_arguments_are_not_shell_commands(tmp_path, command):
    validate_agent_command(command, cwd=str(tmp_path))
