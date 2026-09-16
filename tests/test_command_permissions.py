"""Tool-boundary checks using native commands, without OS hooks or sandbox APIs."""

import pytest

from redlotus.core.agents import WorkspaceContext
from redlotus.core.agents import SubagentFactory
from redlotus.core.agents import SubagentSpec
from redlotus.tools.registry import SkillsManager
from redlotus.tools.toolkit import BasicToolkit


async def in_worker(tmp_path, execute):
    factory = SubagentFactory(1)
    try:
        return await factory.run(
            SubagentSpec("test", "turn", WorkspaceContext.from_path(tmp_path)), execute
        )
    finally:
        await factory.close()


async def test_subagent_is_denied_process_control_commands(tmp_path):
    async def execute():
        toolkit = BasicToolkit(None, workspace=WorkspaceContext.from_path(tmp_path))
        try:
            # Help has no destructive effect if the guard regresses.
            return await toolkit.run_command("taskkill /?")
        finally:
            await toolkit.close()

    result = await in_worker(tmp_path, execute)
    assert "Permission denied" in result, result


async def test_subagent_skill_script_uses_the_same_permission_check(tmp_path):
    skill = tmp_path / "skills" / "sample"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: sample\ndescription: test\n---\nSample", encoding="utf-8"
    )
    (skill / "check.py").write_text(
        "import os\ndef stop():\n    os.kill(987654321, 9)\nprint('unrestricted')\n",
        encoding="utf-8",
    )

    async def execute():
        manager = SkillsManager(
            tmp_path / "skills", workspace=WorkspaceContext.from_path(tmp_path)
        )
        return await manager.execute_skill_script("sample", "check.py")

    result = await in_worker(tmp_path, execute)
    assert "Permission denied" in result, result


async def test_ordinary_subagent_command_keeps_working(tmp_path):
    async def execute():
        toolkit = BasicToolkit(None, workspace=WorkspaceContext.from_path(tmp_path))
        try:
            return await toolkit.run_command(
                "python -c \"print(19 * 23); print('kill is text')\""
            )
        finally:
            await toolkit.close()

    result = await in_worker(tmp_path, execute)
    assert "437" in result and "kill is text" in result, result


@pytest.mark.parametrize(
    "command",
    [
        '"C:\\Windows\\System32\\taskkill.exe" /PID 123 /F',
        'cmd /v:on /c ""C:\\Windows\\System32\\taskkill.exe" /PID 123 /F"',
        'powershell -NoProfile -Command "Get-Process -Id 123 | Stop-Process"',
        'python -c "import os; os.kill(123, 9)"',
        'python -c "from os import kill; kill(123, 9)"',
        'python -c "import psutil; psutil.Process(123).terminate()"',
        "powershell -EncodedCommand ZXhhbXBsZQ==",
        "wmic process where ProcessId=123 call terminate",
    ],
)
async def test_common_process_control_forms_are_rejected_before_execution(
    tmp_path, command
):
    from redlotus.tools.execution import validate_agent_command

    async def execute():
        validate_agent_command(command, cwd=str(tmp_path))

    with pytest.raises(PermissionError, match="Permission denied"):
        await in_worker(tmp_path, execute)
