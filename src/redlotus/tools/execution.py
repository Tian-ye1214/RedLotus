"""Existing interpreters, project caches, and owned command execution."""

from __future__ import annotations

import asyncio
import codecs
import json
import locale
import os
import platform as _platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field, replace
from functools import wraps
from pathlib import Path

from redlotus.runtime.config import get_agent_run_policy, settings

_SEPARATORS = {";", "&", "&&", "|", "||", "\n"}
_PYTHON_NAMES = {"python", "python.exe", "python3", "python3.exe"}
_PY_LAUNCHER_NAMES = {"py", "py.exe"}
_PIP_NAMES = {"pip", "pip.exe", "pip3", "pip3.exe"}
_PIP_RESTRICTED_OPTIONS = {
    "--user",
    "--target",
    "--prefix",
    "--root",
    "--python",
    "--isolated",
    "--system",
    "-t",
}
_UV_NAMES = {"uv", "uv.exe"}


@dataclass(frozen=True)
class ExecutionEnvironment:
    """The existing interpreter and the project environment passed to commands."""

    workspace_root: Path
    project_id: str
    root: Path | None
    python: Path | None
    cache: Path
    variables: dict[str, str] = field(repr=False)


@dataclass(frozen=True)
class CommandResult:
    """The process outcome and launch context; neither implies task completion."""

    stdout: str
    stderr: str
    returncode: int | None
    command: str | tuple[str, ...]
    cwd: str
    python_on_path: str | None = None
    output_decoded: bool = True

    def to_text(self) -> str:
        context = [
            f"Return code: {self.returncode}",
            f"Working directory: {self.cwd}",
            f"Command: {self.command}",
        ]
        if self.python_on_path:
            context.append(f"Python on PATH: {self.python_on_path}")
        if not self.output_decoded:
            context.insert(
                0,
                "Error: command output could not be decoded; execution is not verified.",
            )
        return "\n".join(
            [*context, f"stdout:\n{self.stdout}", f"stderr:\n{self.stderr}"]
        )


def _workspace_for(cwd: str | Path | None, workspace=None):
    if workspace is None:
        from redlotus.runtime.resources import active_workspace

        workspace = active_workspace()
    if workspace is not None:
        return workspace
    from redlotus.runtime.resources import WorkspaceContext

    return WorkspaceContext.from_path(cwd or Path.cwd())


def _extended_path(path: Path) -> str:
    value = str(path)
    if _platform.system() == "Windows" and not value.startswith("\\\\?\\"):
        return "\\\\?\\" + value
    return value


def existing_python() -> Path:
    """Use the host interpreter, or an external PATH interpreter when frozen."""
    if not getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found and not _same_path(found, Path(sys.executable)):
            return Path(found).resolve()
    if launcher := shutil.which("py"):
        result = subprocess.run(
            [launcher, "-3", "-c", "import sys; print(sys.executable)"],
            capture_output=True,
            timeout=get_agent_run_policy().max_command_timeout_seconds,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if not result.returncode:
            path = Path(os.fsdecode(result.stdout.strip()))
            if path.is_file() and not _same_path(path, Path(sys.executable)):
                return path.resolve()
    raise FileNotFoundError("未找到外部 Python，请将现有 Python 加入 PATH；普通命令和聊天仍可使用。")


def execution_cache_dir(workspace) -> Path:
    """Resolve owned, regenerable caches under the configured project runtime."""
    from redlotus.runtime.resources import runtime_dir

    return runtime_dir(workspace).resolve() / "cache"


def get_execution_environment(
    *, cwd: str | Path | None = None, workspace=None,
    overrides: dict[str, str] | None = None, python_required: bool = True,
) -> ExecutionEnvironment:
    """Describe the existing environment without creating or rebuilding Python."""
    from redlotus.runtime.resources import runtime_dir

    active = _workspace_for(cwd, workspace)
    runtime = runtime_dir(active).resolve()
    python = existing_python() if python_required or not getattr(sys, "frozen", False) else None
    root = (
        Path(sys.prefix).resolve() if not getattr(sys, "frozen", False)
        else python.parent.parent if python and python.parent.name.lower() in {"scripts", "bin"}
        else python.parent if python else None
    )
    # OS/CLI interfaces preserve Git, SSH and proxies, without model credentials.
    inherited = {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "HOME", "USERPROFILE",
        "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "XDG_CONFIG_HOME",
        "SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
        "GIT_SSH", "GIT_SSH_COMMAND", "NPM_CONFIG_USERCONFIG",
        "GIT_ASKPASS", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "GIT_TERMINAL_PROMPT",
        "PLAYWRIGHT_BROWSERS_PATH", "CLAWHUB_WORKDIR",
    }
    variables = {k: v for k, v in os.environ.items() if k.upper() in inherited}
    variables.update({k: str(v) for k, v in (overrides or {}).items() if k.upper() in inherited})
    cache = execution_cache_dir(active)
    project_cache = cache / active.project_id
    for name, folder in (
        ("PIP_CACHE_DIR", "pip"), ("npm_config_cache", "npm"),
        ("UV_CACHE_DIR", "uv"), ("XDG_CACHE_HOME", "xdg"),
    ):
        variables[name] = str(project_cache / folder)
    for name in ("TEMP", "TMP", "TMPDIR"):
        variables[name] = str(runtime / "tmp")
    variables.update(PYTHONUTF8="1", PYTHONNOUSERSITE="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    if python:
        variables.update(PIP_PYTHON=str(python), UV_PYTHON=str(python))
        bins = [str(python.parent)]
        if os.name == "nt":
            bins.append(str(root / "Scripts"))
        path_key = next((k for k in variables if k.upper() == "PATH"), "PATH")
        variables[path_key] = os.pathsep.join([*bins, variables.get(path_key, "")])
    if getattr(sys, "frozen", False):
        bundle = str(getattr(sys, "_MEIPASS", ""))
        if bundle:
            path_key = next((k for k in variables if k.upper() == "PATH"), "PATH")
            variables[path_key] = os.pathsep.join(
                item for item in variables.get(path_key, "").split(os.pathsep)
                if not item or not Path(item).resolve().is_relative_to(Path(bundle).resolve())
            )
        if "LD_LIBRARY_PATH_ORIG" in os.environ:
            variables["LD_LIBRARY_PATH"] = os.environ["LD_LIBRARY_PATH_ORIG"]
    return ExecutionEnvironment(active.root.resolve(), active.project_id, root, python, cache, variables)


def _prepare_runtime_dirs(environment: ExecutionEnvironment) -> None:
    """Create owned cache/temp directories and their cleanup ownership marker."""
    for name in ("PIP_CACHE_DIR", "XDG_CACHE_HOME", "TEMP", "npm_config_cache", "UV_CACHE_DIR"):
        Path(environment.variables[name]).mkdir(parents=True, exist_ok=True)
    (environment.cache / environment.project_id / ".redlotus-cache").write_text(
        json.dumps({"project_id": environment.project_id}), encoding="utf-8"
    )


def describe_execution_environment(*, cwd=None, workspace=None) -> str:
    """Report the real interpreter and install location without provisioning."""
    try:
        environment = get_execution_environment(cwd=cwd, workspace=workspace)
    except (OSError, ValueError) as exc:
        return f"Environment state: unavailable\nReason: {exc}"
    return "\n".join((
        f"Project: {environment.project_id}", f"Workspace: {environment.workspace_root}",
        f"Python: {environment.python}", f"Install environment: {environment.root}",
        f"Environment state: {'ready' if environment.python.is_file() else 'missing'}",
        "This is the existing environment. Dependency installation changes this environment.",
        "pip and uv target the Python shown above; the pip program may be hosted in another environment.",
        f"Cache: {environment.cache}",
    ))


def _shell_tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=False, punctuation_chars=";&|\n{}")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = []
    for token in lexer:
        tokens.extend(re.findall(r"&&|\|\||[;&|\n{}]", token) if token and all(c in ";&|\n{}" for c in token) else [token])
        if _program_name(tokens[0]) in {"cmd", "cmd.exe"} and token.lower() in {
            "/c",
            "/k",
        }:
            # cmd strips its outer quote pair before interpreting the command body.
            tokens.append(lexer.instream.read().strip())
            break
    return tokens


def _command_invocations(command, *, posix_shell=None):
    """Expose executable positions, including ordinary nested shell commands."""
    tokens = _shell_tokens(command) if isinstance(command, str) else list(command)
    if posix_shell is None:
        posix_shell = _platform.system() != "Windows"
    if isinstance(command, str) and posix_shell and "&" in tokens:
        raise PermissionError(
            "Background shell launches are not allowed; wait for the command to finish."
        )
    segment = []
    for token in [*tokens, ";"]:
        if token not in _SEPARATORS | {"{", "}"}:
            segment.append(str(token))
            continue
        if not segment:
            continue
        yield segment
        if segment[0].lower() in {"if", "elseif", "while", "until", "switch"}:
            condition = " ".join(segment[1:]).strip()
            if condition.startswith("(") and condition.endswith(")"):
                condition = condition[1:-1].strip()
                # Scalar/variable tests are values; a command in the condition still executes.
                if condition and not re.match(r'''^(?:\$[\w]+|\d|["'])''', condition):
                    yield from _command_invocations(condition, posix_shell=posix_shell)
        if _program_name(segment[0]) in {
            "cmd",
            "cmd.exe",
            "powershell",
            "powershell.exe",
            "pwsh",
            "pwsh.exe",
            "sh",
            "bash",
            "zsh",
        }:
            for index, value in enumerate(segment):
                option = value.lower()
                powershell = _program_name(segment[0]).removesuffix(".exe") in {"powershell", "pwsh"}
                if option in {"/c", "/k", "-c", "-command"} or (
                    powershell and option.startswith("-c") and "-command".startswith(option)
                ):
                    yield from _command_invocations(
                        _unquote_shell_word(" ".join(segment[index + 1 :])),
                        posix_shell=_program_name(segment[0]) in {"sh", "bash", "zsh"},
                    )
                    break
        segment = []


def validate_agent_command(command, *, cwd: str) -> None:
    """One policy entry for commands, inline programs and executed scripts."""
    from redlotus.sessions.context import current_execution_role
    from redlotus.tools.registry import (
        JavaScriptCommandCheck,
        PythonCommandCheck,
        code_without_literals,
        read_script,
    )

    role = current_execution_role()
    restricted = role in {"worker", "manager"}
    visited = set()

    def source(text, *, python=False, javascript=False, shell=False):
        if python:
            try:
                PythonCommandCheck(inspect, restricted=restricted).check(text)
            except SyntaxError as exc:
                raise ValueError(f"Cannot inspect Python source: {exc}") from exc
        else:
            if shell:
                inspect(text)
            if javascript:
                JavaScriptCommandCheck(inspect, restricted=restricted).check(text)
            if restricted and any(
                re.search(pattern, code_without_literals(text), re.I | re.M)
                for pattern in (r"\bprocess\s*\.\s*kill\s*\(", r"\b(?:TerminateProcess|NtTerminateProcess|TerminateJobObject)\s*\(")
            ):
                raise PermissionError(
                    f"Permission denied for {role}: restricted process API."
                )

    def inspect(value):
        for values in _command_invocations(value):
            name = _program_name(values[0]).removesuffix(".exe")
            words = [_unquote_shell_word(item) for item in values]
            lower = [item.lower() for item in words]
            if restricted and (
                words[0].startswith(("$", "%")) and "=" not in words and "=" not in words[0]
            ):
                raise PermissionError("Use an explicit command; the executable cannot be resolved before execution.")
            if name in {"start", "nohup", "setsid", "start-process"} and not (
                name == "start-process" and "-wait" in lower
            ):
                raise PermissionError(
                    "Background process launches are not allowed; run the command synchronously."
                )
            if restricted and (
                name in {
                    "kill", "pkill", "killall", "taskkill", "tskill", "stop-process",
                    "spps", "shutdown", "restart-computer", "stop-computer",
                    "stop-service", "restart-service",
                }
                or (
                    name in {"powershell", "pwsh"}
                    and any(
                        item in {"-encodedcommand", "-enc", "-ec"}
                        or (item.startswith("-e") and "-encodedcommand".startswith(item))
                        for item in lower
                    )
                )
                or (
                    name == "wmic"
                    and "process" in lower
                    and "call" in lower
                    and "terminate" in lower
                )
            ):
                raise PermissionError(
                    f"Permission denied for {role}: process-control commands are reserved for the runtime."
                )
            if name == "start-process":
                target, arguments = None, []
                index = 1
                while index < len(words):
                    option = lower[index]
                    if option.startswith("-f") and "-filepath".startswith(option):
                        index += 1
                        target = words[index] if index < len(words) else None
                    elif option.startswith("-a") and "-argumentlist".startswith(option):
                        index += 1
                        start = index
                        while index < len(words) and not lower[index].startswith("-"):
                            index += 1
                        raw_arguments = " ".join(values[start:index])
                        # PowerShell's comma-separated string array becomes one OS command line.
                        lexer = shlex.shlex(raw_arguments, posix=False, punctuation_chars=",")
                        lexer.whitespace_split, lexer.commenters = True, ""
                        arguments = _shell_tokens(" ".join(
                            _unquote_shell_word(part) for part in lexer if part != ","
                        )) if raw_arguments else None
                        index -= 1
                    elif not option.startswith("-") and target is None:
                        target = words[index]
                    index += 1
                if not target or arguments is None or any("$" in part for part in [target, *arguments]):
                    if restricted:
                        raise PermissionError("Use an explicit command for Start-Process, including its arguments.")
                else:
                    inspect([target, *arguments])
            python = name in {"python", "python3", "py"}
            inline = "-c" if python else "-e" if name in {"node", "nodejs"} else None
            if inline and inline in lower:
                index = lower.index(inline)
                if index + 1 < len(words):
                    source(words[index + 1], python=python, javascript=not python)
                continue
            programs = {
                "python",
                "python3",
                "py",
                "powershell",
                "pwsh",
                "sh",
                "bash",
                "zsh",
                "node",
                "nodejs",
                "cmd",
            }
            candidates = words[1:] if name in programs else words[:1]
            for item in candidates:
                path = Path(cwd) / item
                if (
                    path.suffix.lower() not in {".py", ".pyw", ".ps1", ".sh", ".bat", ".cmd", ".js", ".mjs", ".cjs"}
                    or not path.is_file()
                ):
                    continue
                path = path.resolve()
                if path in visited:
                    continue
                visited.add(path)
                extension = path.suffix.lower()
                source(
                    read_script(path),
                    python=extension in {".py", ".pyw"},
                    javascript=extension in {".js", ".mjs", ".cjs"},
                    shell=extension in {".ps1", ".sh", ".bat", ".cmd"},
                )
                break

    inspect(command)


def _program_name(value: str) -> str:
    return _unquote_shell_word(value).replace("\\", "/").rsplit("/", 1)[-1].lower()


def _is_bare(value: str) -> bool:
    value = _unquote_shell_word(value)
    return "/" not in value and "\\" not in value and not Path(value).drive


def _same_path(left: str | Path, right: Path) -> bool:
    return os.path.normcase(str(Path(left).expanduser().resolve())) == os.path.normcase(
        str(right.resolve())
    )


def _selected_pip(python: Path) -> Path:
    folder = python.parent
    if os.name == "nt" and folder.name.lower() != "scripts":
        folder /= "Scripts"
    return folder / ("pip.exe" if os.name == "nt" else "pip")


def validate_pip_command(
    command: str | list[str] | tuple[str, ...], *, selected_python: Path
) -> None:
    """Reject installs that silently escape the reported execution environment."""
    tokens = (
        _shell_tokens(command)
        if isinstance(command, str)
        else [str(x) for x in command]
    )
    selected_pip = _selected_pip(selected_python)
    segment: list[str] = []

    def inspect(values: list[str]) -> None:
        if not values:
            return
        program = _unquote_shell_word(values[0])
        name = _program_name(program)
        if name in {
            "cmd",
            "cmd.exe",
            "powershell",
            "powershell.exe",
            "pwsh",
            "pwsh.exe",
            "sh",
            "bash",
            "zsh",
        }:
            lowered = [_unquote_shell_word(value).lower() for value in values]
            for index, value in enumerate(lowered):
                if value in ("/c", "/k", "-c", "-command"):
                    nested = _unquote_shell_word(" ".join(values[index + 1 :]))
                    validate_pip_command(
                        nested,
                        selected_python=selected_python,
                    )
                    return
        pip_index = None
        interpreter = None
        if name in _PIP_NAMES:
            pip_index = 0
        elif name in _PYTHON_NAMES | _PY_LAUNCHER_NAMES:
            lowered = [_unquote_shell_word(value).lower() for value in values]
            for index, value in enumerate(lowered[:-1]):
                if value in ("-c", "-m"):
                    if value == "-m" and lowered[index + 1] == "pip":
                        pip_index = index + 1
                        interpreter = program
                    break
        elif name in _UV_NAMES:
            lowered = [_unquote_shell_word(value).lower() for value in values]
            if lowered[1:2] == ["pip"]:
                pip_index = 1
        if pip_index is None:
            return
        if (
            interpreter
            and (name in _PY_LAUNCHER_NAMES or not _is_bare(interpreter))
            and not _same_path(interpreter, selected_python)
        ):
            raise ValueError("pip 请求指定了其他 Python 解释器，请使用当前执行环境。")
        if (
            pip_index == 0
            and not _is_bare(program)
            and not _same_path(program, selected_pip)
        ):
            raise ValueError("pip 请求指定了其他安装环境，请使用当前执行环境。")
        for value in values[pip_index + 1 :]:
            option = _unquote_shell_word(value).lower().split("=", 1)[0]
            if option in _PIP_RESTRICTED_OPTIONS:
                raise ValueError(f"禁止 pip 安装参数 {option}，请使用当前执行环境。")

    for token in tokens:
        if token in _SEPARATORS:
            inspect(segment)
            segment = []
        else:
            segment.append(token)
    inspect(segment)


def _rewrite_python_command(args, environment: ExecutionEnvironment, *, shell=False):
    """Use existing pip to manage the selected Python, including pip-less venvs."""
    values = [_unquote_shell_word(value) for value in _shell_tokens(args)] if shell else list(args)
    if not values:
        return args
    name = _program_name(str(values[0]))
    module_pip = name in _PYTHON_NAMES and values[1:3] == ["-m", "pip"]
    pip = shutil.which("pip", path=next(
        (v for k, v in environment.variables.items() if k.upper() == "PATH"), ""
    ))
    if module_pip and pip:
        if shell:
            prefix = re.match(r'''^\s*(?:"[^"]+"|'[^']+'|\S+)\s+-m\s+pip(?=\s|$)''', args)
            if prefix:
                quoted = subprocess.list2cmdline([pip]) if os.name == "nt" else shlex.quote(pip)
                return quoted + args[prefix.end():]
        else:
            return [pip, *values[3:]]
    if shell:
        return args
    if name in _PYTHON_NAMES and (
        _is_bare(str(values[0])) or _same_path(values[0], environment.python)
    ):
        values[0] = _extended_path(environment.python)
    elif _is_bare(str(values[0])) and name in _PIP_NAMES:
        values[:1] = [pip] if pip else [str(environment.python), "-m", "pip"]
    return values


def _unquote_shell_word(word: str) -> str:
    return (
        word[1:-1]
        if len(word) >= 2 and word[0] in "\"'" and word[-1] == word[0]
        else word
    )


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """杀掉子进程及其后代（无 psutil 依赖），并收尸。"""
    timeout = settings()["lifecycle"]["process_termination_timeout_seconds"]
    if proc.returncode is not None:
        # An exited parent can leave inherited pipes open. Do not claim tree
        # cleanup before they close, or target a PID whose owner may have changed.
        try:
            await asyncio.wait_for(proc.communicate(), timeout=timeout)
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
            await asyncio.wait_for(killer.wait(), timeout=timeout)
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
    await asyncio.wait_for(proc.communicate(), timeout=timeout)


async def run_subprocess(
    args,
    *,
    shell: bool,
    cwd: str,
    env: dict | None = None,
    timeout: float | None = None,
    workspace=None,
) -> CommandResult:
    """Run a command with its launch evidence, reclaiming owned processes on cancellation."""
    timeout = get_agent_run_policy().clamp_command_timeout(timeout)
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


def page_action(operation):
    """Serialize page actions and report browser failures to the Agent."""

    @wraps(operation)
    async def run(self, *args, **kwargs):
        async with self._lock:
            try:
                await self._start()
                policy = settings()["browser"]
                self._page.set_default_timeout(policy["action_timeout_seconds"] * 1000)
                self._page.set_default_navigation_timeout(policy["navigation_timeout_seconds"] * 1000)
                return await operation(self, *args, **kwargs)
            except (ImportError, RuntimeError) as exc:
                return f"Error: Browser unavailable: {exc}"
            except self._browser_error as exc:
                return f"Error: {operation.__name__}: {exc}"

    return run


class PlaywrightBrowserSession:
    """A lazy browser owned and closed by the Agent's event loop."""

    def __init__(self, workspace):
        self.workspace = workspace
        self._lock = asyncio.Lock()
        self._playwright = self._browser = self._page = None
        self._browser_error = ()

    async def _start(self):
        if self._page is not None:
            return
        from playwright.async_api import Error, async_playwright

        self._browser_error = Error
        self._playwright = await async_playwright().start()
        try:
            config = settings()
            headless = str(config["BROWSER_HEADLESS"]).strip().lower() not in ("0", "false", "no")
            self._browser = await self._playwright.chromium.launch(headless=headless)
            self._page = await self._browser.new_page(
                viewport=config["browser"]["viewport"], locale=config["browser"]["locale"]
            )
        except BaseException:
            await self._close()
            raise

    async def _close(self):
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            if self._playwright is not None:
                await self._playwright.stop()
            self._playwright = self._browser = self._page = None

    async def close(self):
        async with self._lock:
            await self._close()

    @page_action
    async def browser_navigate(
        self, url: str, wait_until: str = "domcontentloaded"
    ) -> str:
        """Open a URL in this Agent's browser page.

        Args:
            url: The full URL to open.
            wait_until: The Playwright navigation event to wait for.

        Returns:
            The resulting page URL and title, or a browser error."""
        await self._page.goto(url, wait_until=wait_until)
        return f"OK\nURL: {self._page.url}\nTitle: {await self._page.title()}"

    @page_action
    async def browser_get_content(self) -> str:
        """Read the current page's URL and full visible body text."""
        text = await self._page.locator("body").inner_text()
        return f"URL: {self._page.url}\n{text}"

    @page_action
    async def browser_screenshot(self, name: str, full_page: bool = False) -> str:
        """Save a screenshot of this Agent's current browser page.

        Args:
            name: Destination path within the allowed project paths.
            full_page: Capture the entire page when true, otherwise the visible viewport.

        Returns:
            The saved screenshot path, or a browser error."""
        from redlotus.tools.registry import resolve_readable_path

        path = resolve_readable_path(name, work_base=self.workspace.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        await self._page.screenshot(path=str(path), full_page=full_page)
        return f"Screenshot saved: {path}"

    @page_action
    async def browser_click(self, selector: str) -> str:
        """Click a matching element in the current page.

        Args:
            selector: A Playwright selector for the intended element.

        Returns:
            The clicked selector, or a browser error."""
        await self._page.click(selector)
        return f"Clicked: {selector}"

    @page_action
    async def browser_fill(self, selector: str, text: str) -> str:
        """Replace the value of a matching input in the current page.

        Args:
            selector: A Playwright selector for the input element.
            text: The value to fill.

        Returns:
            The filled selector, or a browser error."""
        await self._page.fill(selector, text)
        return f"Filled: {selector}"

    @page_action
    async def browser_press_key(self, key: str) -> str:
        """Press a key or shortcut in the current browser page.

        Args:
            key: The Playwright key name or shortcut, such as Enter or Control+A.

        Returns:
            The pressed key, or a browser error."""
        await self._page.keyboard.press(key)
        return f"Pressed: {key}"

    @page_action
    async def browser_wait_for_selector(
        self, selector: str, timeout_ms: int | None = None
    ) -> str:
        """Wait for a matching element to become visible in the current page.

        Args:
            selector: A Playwright selector for the intended element.
            timeout_ms: Optional milliseconds; omitted uses the configured browser action timeout.

        Returns:
            The visible selector, or a browser error."""
        await self._page.wait_for_selector(selector, timeout=timeout_ms)
        return f"Visible: {selector}"

    @page_action
    async def browser_evaluate(self, javascript_expression: str) -> str:
        """Evaluate JavaScript within this Agent's browser page.

        Args:
            javascript_expression: JavaScript to evaluate in the current page context.

        Returns:
            The evaluation result, or a browser error."""
        return repr(await self._page.evaluate(javascript_expression))

    async def browser_close(self) -> str:
        """Close this Agent's browser page and release its browser resources."""
        await self.close()
        return "Browser closed"
