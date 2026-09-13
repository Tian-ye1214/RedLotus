import sys

import pytest

from redlotus.infra.subprocess_runner import has_background_shell_command
from redlotus.runtime.context import WorkspaceContext
from redlotus.tools.BasicTools import BasicToolkit


async def test_python_start_method_is_not_a_background_shell_command(tmp_path):
    toolkit = BasicToolkit(None, workspace=WorkspaceContext.from_path(tmp_path))
    command = f'"{sys.executable}" -c "import threading; t=threading.Thread(target=lambda: print(73104)); t.start(); t.join()"'
    result = await toolkit.run_command(command)
    assert "73104" in result, result
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
    ],
)
def test_actual_background_commands_remain_blocked(command):
    assert has_background_shell_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "echo start",
        'echo "a & b"',
        "python -c \"print('start')\"",
        "powershell -Command \"Write-Output 'start'\"",
    ],
)
def test_command_arguments_are_not_shell_commands(command):
    assert not has_background_shell_command(command)
