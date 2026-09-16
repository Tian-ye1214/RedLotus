"""Known launch wrappers must not hide a restricted command or an unknown target."""

import pytest

from redlotus.core.agents import execution_role
from redlotus.tools.execution import validate_agent_command


@pytest.mark.parametrize('command', [
    ['powershell', '-Command', '& { taskkill /? }'],
    ['powershell', '-Command', 'if ($true) { taskkill /? }'],
    ['powershell', '-Command', 'if (taskkill /?) { Write-Output done }'],
    ['powershell', '-Command', 'while (Get-Process | Stop-Process -WhatIf) { break }'],
    ['powershell', '-Com', 'taskkill /?'],
    ['pwsh', '-Co', 'taskkill /?'],
    ['powershell', '-Command', 'Start-Process taskkill -ArgumentList "/?" -Wait'],
    ['powershell', '-Command', 'Start-Process -FilePath taskkill -Wait -ArgumentList "/?"'],
    ['powershell', '-Command', 'Start-Process cmd -Wait -ArgumentList "/c taskkill /?"'],
    ['powershell', '-Command', "$target = 'taskkill'; & $target /?"],
    ['python', '-c', "import subprocess; subprocess.run(['taskkill', *['/?']])"],
    ['python', '-c', "import subprocess as sp; cmd=['taskkill'] + ['/?']; sp.run(cmd)"],
    ['python', '-c', "from subprocess import run as launch; name='task'+'kill'; launch([name,'/?'])"],
    ['node', '-e', "require('child_process').spawnSync('taskkill', ['/?']);"],
    ['node', '-e', "const cp=require('node:child_process'); cp.execSync('taskkill /?');"],
    ['node', '-e', "const {execFileSync: launch}=require('child_process'); launch('taskkill',['/?']);"],
    ['node', '-e', "import {spawn as launch} from 'node:child_process'; launch('taskkill',['/?']);"],
    ['node', '-e', "import cp from 'child_process'; cp.exec('taskkill /?');"],
    ['node', '-e', "const cp=require('child_process'); const target='taskkill'; cp.spawn(target,['/?']);"],
    ['node', '-e', "const cp=require('child_process')\nconst target='taskkill'\ncp.spawnSync(target,['/?'])"],
    ['node', '-e', "const cp=require('child_process'); cp.spawnSync('echo', ['ok; taskkill /?'], {shell:true});"],
    ['python', '-c', "import subprocess; subprocess.run(['echo','ok; taskkill /?'], shell=True)"],
    ['python', '-c', "import subprocess; subprocess.run(['echo','/?'], executable='taskkill')"],
    ['powershell', '-Command', "Start-Process cmd -Wait -ArgumentList '/c','taskkill /?'"],
])
def test_restricted_command_in_wrapper_is_rejected(tmp_path, command):
    with execution_role('worker'), pytest.raises(PermissionError):
        validate_agent_command(command, cwd=str(tmp_path))


@pytest.mark.parametrize('command', [
    ['python', '-c', 'import subprocess; subprocess.run(command_from_file)'],
    ['python', '-c', "import subprocess; subprocess.run(['echo', *arguments_from_file])"],
    ['python', '-c', 'import os; os.system(command_from_file)'],
    ['node', '-e', "const cp=require('child_process'); cp.spawnSync(commandFromFile, args);"],
    ['node', '-e', "const {exec}=require('child_process'); exec(commandFromFile);"],
    ['powershell', '-Command', '& $program $arguments'],
    ['powershell', '-Command', 'if (& $program) { Write-Output done }'],
    ['powershell', '-Command', 'Start-Process $program -Wait'],
    ['python', '-c', "import subprocess; subprocess.run(['echo','ok'], executable=program_from_file)"],
])
def test_unresolved_launch_requires_an_explicit_command(tmp_path, command):
    with execution_role('worker'), pytest.raises(PermissionError, match='explicit|明确'):
        validate_agent_command(command, cwd=str(tmp_path))


@pytest.mark.parametrize('command', [
    ['python', '-c', "import subprocess; args=['ok']; subprocess.run(['echo', *args])"],
    ['python', '-c', "import subprocess as sp; name='echo'; command=[name]+['ok']; sp.run(command)"],
    ['powershell', '-Command', "if ($true) { Write-Output 'taskkill is text' }"],
    ['powershell', '-Command', "if (Test-Path 'taskkill') { Write-Output 'ok' }"],
    ['powershell', '-Command', 'Start-Process cmd -Wait -ArgumentList "/c echo ok"'],
    ['node', '-e', "const cp=require('child_process'); cp.spawnSync('node', ['--version']);"],
    ['node', '-e', "const {execFileSync: launch}=require('child_process'); launch('node',['--version']);"],
    ['node', '-e', "const cp=require('child_process')\nconst program='node'\ncp.spawnSync(program,['--version'])"],
    ['node', '-e', "const cp=require('child_process'); const args=['--version']; cp.spawnSync('node',[...args]);"],
    ['node', '-e', "const sprite={kill(){return 1}}; console.log('process.kill(1)'); sprite.kill();"],
])
def test_known_safe_launches_and_ordinary_text_remain_allowed(tmp_path, command):
    with execution_role('worker'):
        validate_agent_command(command, cwd=str(tmp_path))
