"""Execution process responsibilities."""

from __future__ import annotations

import asyncio
import codecs
import locale
import os
import platform as _platform
import signal
import subprocess
from dataclasses import replace

from redlotus.execution.commands import (
    _PIP_NAMES,
    _PY_LAUNCHER_NAMES,
    _PYTHON_NAMES,
    _UV_NAMES,
    CommandResult,
    _command_invocations,
    _prepare_runtime_dirs,
    _program_name,
    _rewrite_python_command,
    get_execution_environment,
    validate_agent_command,
    validate_pip_command,
)


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """杀掉子进程及其后代（无 psutil 依赖），并收尸。"""
    if proc.returncode is not None:
        # An exited parent can leave inherited pipes open. Do not claim tree
        # cleanup before they close, or target a PID whose owner may have changed.
        try:
            await asyncio.wait_for(proc.communicate(), timeout=5)
        except TimeoutError as exc:
            raise RuntimeError(
                "Parent exited but inherited pipes remain open; descendant ownership is unknown and cleanup is unverified."
            ) from exc
        return
    try:
        if _platform.system() == "Windows":
            # /T 杀整棵树：shell 会经 cmd.exe 再起真正的子进程。
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            await asyncio.wait_for(killer.wait(), timeout=5)
            if killer.returncode and proc.returncode is None:
                raise PermissionError(
                    f"taskkill could not terminate process tree {proc.pid} (exit {killer.returncode})"
                )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise  # Do not report successful tree cleanup when the OS denied it.
    # Drain inherited pipes too: descendants can still be releasing files after
    # the root process has exited.
    await asyncio.wait_for(proc.communicate(), timeout=5)


async def run_subprocess(
    args,
    *,
    shell: bool,
    cwd: str,
    env: dict | None = None,
    timeout: float,
    workspace=None,
) -> CommandResult:
    """Run a command with its launch evidence, reclaiming owned processes on cancellation."""
    await asyncio.to_thread(validate_agent_command, args, cwd=cwd)
    python_required = any(
        _program_name(values[0]) in _PYTHON_NAMES | _PY_LAUNCHER_NAMES | _PIP_NAMES
        or (_program_name(values[0]) in _UV_NAMES and "pip" in values[1:3])
        for values in _command_invocations(args)
    )
    environment = await asyncio.to_thread(
        get_execution_environment, cwd=cwd, workspace=workspace,
        overrides=env, python_required=python_required,
    )
    if python_required:
        validate_pip_command(args, selected_python=environment.python)
        args = _rewrite_python_command(args, environment, shell=shell)
    await asyncio.to_thread(_prepare_runtime_dirs, environment)
    env = environment.variables
    python_on_path = str(environment.python) if environment.python else None

    result = await _run_owned_process(
        args, shell=shell, cwd=cwd, env=env, timeout=timeout
    )
    return replace(result, python_on_path=python_on_path)


def _decode_output(data: bytes, encodings: list[str]) -> tuple[str, bool]:
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encodings = ["utf-16"]
    candidates = [
        locale.getencoding() if value == "locale" else value for value in encodings
    ]
    for encoding in candidates:
        try:
            return data.decode(encoding), True
        except UnicodeDecodeError:
            continue
    # Keep every byte and the original exit code; do not silently replace text.
    return (
        f"[Cannot decode output using {candidates}; raw bytes escaped below]\n"
        + data.decode("ascii", errors="backslashreplace"),
        False,
    )


async def _run_owned_process(
    args, *, shell: bool, cwd: str, env: dict | None, timeout: float
):
    """Own one external process from creation through timeout or cancellation."""

    kwargs = {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "cwd": cwd,
        "env": env,
    }
    if _platform.system() == "Windows":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True  # 独立进程组，便于 killpg

    if shell:
        proc = await asyncio.create_subprocess_shell(args, **kwargs)
    else:
        proc = await asyncio.create_subprocess_exec(*args, **kwargs)

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        await _terminate_process_tree(proc)
        raise subprocess.TimeoutExpired(args, timeout)
    except asyncio.CancelledError:
        await _terminate_process_tree(proc)
        raise
    encodings = ["utf-8-sig", locale.getencoding()]
    stdout, stdout_decoded = _decode_output(out, encodings)
    stderr, stderr_decoded = _decode_output(err, encodings)
    return CommandResult(
        stdout,
        stderr,
        proc.returncode,
        args if isinstance(args, str) else tuple(args),
        cwd,
        output_decoded=stdout_decoded and stderr_decoded,
    )
