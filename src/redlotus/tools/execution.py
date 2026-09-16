"""Configured execution environments and owned process lifecycles."""

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
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path


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
    "-t",
}
_UV_NAMES = {"uv", "uv.exe"}
_EXPLICIT_ENV_OVERRIDES = {"CLAWHUB_WORKDIR", "PLAYWRIGHT_BROWSERS_PATH"}


@dataclass(frozen=True)
class ExecutionEnvironment:
    """A project-scoped interpreter and the minimal environment passed to it."""

    workspace_root: Path
    project_id: str
    root: Path
    python: Path
    cache: Path
    variables: dict[str, str] = field(repr=False)
    base_command: tuple[str, ...] = field(default_factory=tuple, repr=False)


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


def _execution_config() -> dict:
    """Load execution settings lazily so importing this leaf has no config I/O."""
    from redlotus.core.config import settings

    return deepcopy(settings().get("execution") or {})


def _workspace_for(cwd: str | Path | None, workspace=None):
    if workspace is None:
        from redlotus.core.agents import active_workspace

        workspace = active_workspace()
    if workspace is not None:
        return workspace
    from redlotus.core.agents import WorkspaceContext

    return WorkspaceContext.from_path(cwd or Path.cwd())


def _runtime_path(template: str, *, runtime: Path, project_id: str) -> Path:
    value = str(template).replace("{runtime}", str(runtime))
    value = value.replace("{project_id}", project_id)
    path = Path(value).expanduser()
    return path if path.is_absolute() else runtime / path


def _runtime_root() -> Path:
    from redlotus.core.config import runtime_dir

    return runtime_dir().resolve()


def _configured_base_python(
    config: dict, *, runtime: Path, project_id: str
) -> tuple[str, ...]:
    configured = str(config.get("python_executable", "")).strip()
    if configured:
        configured = _unquote_shell_word(configured)
        if "{runtime}" in configured or "{project_id}" in configured:
            configured = str(
                _runtime_path(configured, runtime=runtime, project_id=project_id)
            )
        candidate = Path(configured).expanduser()
        found = str(candidate) if candidate.is_file() else shutil.which(configured)
        if not found:
            raise FileNotFoundError(
                f"配置的 Python 解释器不存在或不在 PATH 中: {configured}"
            )
        resolved = Path(found).resolve()
        if getattr(sys, "frozen", False) and os.path.normcase(
            str(resolved)
        ) == os.path.normcase(str(Path(sys.executable).resolve())):
            raise ValueError("冻结程序不能把自身可执行文件作为外部 Python 解释器。")
        return (str(resolved),)

    if getattr(sys, "frozen", False):
        found = shutil.which("python") or shutil.which("python3")
        if found:
            resolved = Path(found).resolve()
            if os.path.normcase(str(resolved)) != os.path.normcase(
                str(Path(sys.executable).resolve())
            ):
                return (str(resolved),)
        launcher = shutil.which("py")
        if launcher:
            return (str(Path(launcher).resolve()), "-3")
        raise FileNotFoundError(
            "冻结程序未找到外部 Python 解释器，请在 config.json execution.python_executable 中配置。"
        )
    return (str(Path(sys.executable).resolve()),)


def _build_execution_variables(
    config: dict,
    *,
    root: Path,
    cache: Path,
    project_id: str,
    overrides: dict[str, str] | None,
    use_python: bool = True,
) -> dict[str, str]:
    inherited_names = tuple(config.get("inherit_env") or ())
    variables = {
        name: os.environ[name] for name in inherited_names if name in os.environ
    }
    bin_dir = root / ("Scripts" if _platform.system() == "Windows" else "bin")
    replacements = dict(
        runtime=_runtime_root(),
        environment=root,
        cache=cache,
        project_id=project_id,
    )
    for name, template in config.get("variables", {}).items():
        value = str(template)
        for token, replacement in replacements.items():
            value = value.replace("{" + token + "}", str(replacement))
        variables[name] = value
    for name, value in (overrides or {}).items():
        if name in inherited_names or name in _EXPLICIT_ENV_OVERRIDES:
            variables[name] = str(value)
    if use_python:
        variables["VIRTUAL_ENV"] = str(root)
        variables["PATH"] = os.pathsep.join(
            value for value in (str(bin_dir), variables.get("PATH", "")) if value
        )
    return variables


def get_execution_environment(
    *,
    cwd: str | Path | None = None,
    workspace=None,
    overrides: dict[str, str] | None = None,
    python_required: bool = True,
) -> ExecutionEnvironment:
    """Resolve configuration and paths without starting an interpreter."""
    config = _execution_config()
    active = _workspace_for(cwd, workspace)
    workspace_root = Path(active.root).resolve()
    if not config:
        variables = dict(os.environ)
        variables.update(
            {name: str(value) for name, value in (overrides or {}).items()}
        )
        return ExecutionEnvironment(
            workspace_root,
            active.project_id,
            Path(sys.prefix).resolve(),
            Path(sys.executable).resolve(),
            Path(sys.prefix).resolve(),
            variables,
            (str(Path(sys.executable).resolve()),),
        )

    runtime = _runtime_root()
    root = _runtime_path(
        config["environment_dir"], runtime=runtime, project_id=active.project_id
    ).resolve()
    cache = _runtime_path(
        config["cache_dir"], runtime=runtime, project_id=active.project_id
    ).resolve()
    base_command = (
        _configured_base_python(config, runtime=runtime, project_id=active.project_id)
        if python_required
        else ()
    )
    python = (
        root
        / ("Scripts" if _platform.system() == "Windows" else "bin")
        / ("python.exe" if _platform.system() == "Windows" else "python")
    )
    environment = ExecutionEnvironment(
        workspace_root,
        active.project_id,
        root,
        python,
        cache,
        _build_execution_variables(
            config,
            root=root,
            cache=cache,
            project_id=active.project_id,
            overrides=overrides,
            use_python=python_required,
        ),
        base_command,
    )
    return environment


async def ensure_execution_environment(environment: ExecutionEnvironment) -> None:
    """Create a project venv once under a cross-process lock."""
    marker = environment.root / ".redlotus-environment.json"
    from filelock import AsyncFileLock

    from redlotus.core.config import atomic_write_json

    timeout = _provision_timeout()
    environment.root.parent.mkdir(parents=True, exist_ok=True)
    lock = environment.root.with_name(environment.root.name + ".create.lock")
    async with AsyncFileLock(lock, timeout=timeout, run_in_executor=False):
        identity = await asyncio.to_thread(_python_identity, environment.base_command)
        state = "creating"
        if environment.python.is_file() and not marker.is_file():
            raise RuntimeError(
                "项目 Python 环境缺少基础解释器记录，拒绝复用；请清理该项目环境后重试。"
            )
        if marker.is_file():
            try:
                marker_data = json.loads(marker.read_text(encoding="utf-8"))
                recorded = tuple(str(value) for value in marker_data["base_command"])
                state = marker_data.get("state", "ready")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise RuntimeError(
                    "项目 Python 环境来源标记损坏，拒绝复用；请清理该项目环境后重试。"
                ) from exc
            recorded_identity = marker_data.get("identity")
            if recorded_identity is None:
                recorded_identity = await asyncio.to_thread(_python_identity, recorded)
            if recorded_identity != identity:
                raise RuntimeError(
                    "项目 Python 环境由其他基础解释器创建，请修改配置或清理该项目环境后重试。"
                )
            if environment.python.is_file() and state == "ready":
                actual = await asyncio.to_thread(
                    _python_identity, (str(environment.python),)
                )
                if actual != identity:
                    raise RuntimeError(
                        "项目 Python 环境解释器与来源记录不一致，拒绝复用。"
                    )
                if "identity" not in marker_data:
                    atomic_write_json(marker, {**marker_data, "identity": identity})
                _prepare_runtime_dirs(environment)
                return
        _prepare_runtime_dirs(environment)
        source = {
            "project_id": environment.project_id,
            "python": str(environment.python),
            "base_command": list(environment.base_command),
            "identity": identity,
            "state": state,
        }
        atomic_write_json(marker, source)
        steps = []
        if state != "python_ready" or not environment.python.is_file():
            steps.append(
                (
                    [
                        *environment.base_command,
                        "-m",
                        "venv",
                        "--without-pip",
                        str(environment.root),
                    ],
                    "python_ready",
                )
            )
        steps.append(
            (
                [
                    str(environment.python),
                    "-m",
                    "ensurepip",
                    "--upgrade",
                    "--default-pip",
                ],
                "ready",
            )
        )
        bootstrap_env = {
            name: value
            for name, value in environment.variables.items()
            if name not in {"VIRTUAL_ENV", "PATH"}
        }
        bootstrap_env["PATH"] = os.environ.get("PATH", "")
        for command, next_state in steps:
            try:
                result = await _run_owned_process(
                    command,
                    shell=False,
                    cwd=str(environment.workspace_root),
                    env=bootstrap_env,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(f"创建项目 Python 环境超时（{timeout} 秒）") from exc
            if result.returncode or not result.output_decoded:
                raise RuntimeError(f"创建项目 Python 环境失败: {result.to_text()}")
            if not environment.python.is_file():
                raise RuntimeError(
                    f"Python 环境创建后未找到解释器: {environment.python}"
                )
            atomic_write_json(marker, {**source, "state": next_state})


def _provision_timeout() -> int:
    from redlotus.core.config import get_agent_run_policy

    return get_agent_run_policy().max_command_timeout_seconds


def _python_identity(command: tuple[str, ...]) -> dict:
    """Ask the interpreter itself, rather than comparing venv launcher paths."""
    probe = (
        "import json,os,sys,struct; print(json.dumps(dict("
        "base=os.path.normcase(os.path.realpath(sys._base_executable)),"
        "implementation=sys.implementation.name,version=list(sys.version_info[:2]),"
        "bits=struct.calcsize('P')*8)))"
    )
    config = _execution_config()
    env = {
        name: os.environ[name]
        for name in config.get("inherit_env", [])
        if name in os.environ
    }
    kwargs = (
        {"creationflags": subprocess.CREATE_NO_WINDOW}
        if _platform.system() == "Windows"
        else {}
    )
    try:
        result = subprocess.run(
            [*command, "-c", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=_provision_timeout(),
            **kwargs,
        )
        if result.returncode:
            raise ValueError(result.stderr.strip())
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"无法验证基础解释器 {command}: {exc}") from exc


def _prepare_runtime_dirs(environment: ExecutionEnvironment) -> None:
    project_cache = environment.cache / environment.project_id
    paths = {project_cache}
    for name in (
        "PIP_CACHE_DIR",
        "XDG_CACHE_HOME",
        "TEMP",
        "TMP",
        "TMPDIR",
        "npm_config_cache",
        "UV_CACHE_DIR",
    ):
        if name in environment.variables:
            paths.add(Path(environment.variables[name]))
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
    (project_cache / ".redlotus-cache").write_text(
        json.dumps({"project_id": environment.project_id}), encoding="utf-8"
    )


def describe_execution_environment(
    *, cwd: str | Path | None = None, workspace=None
) -> str:
    """Return the selected interpreter and paths without guessing host state."""
    try:
        environment = get_execution_environment(cwd=cwd, workspace=workspace)
    except (OSError, ValueError) as exc:
        return f"Environment state: error\nReason: {exc}"
    state = _environment_state(environment)
    return "\n".join(
        (
            f"Project: {environment.project_id}",
            f"Workspace: {environment.workspace_root}",
            f"Python: {environment.python}",
            f"Environment: {environment.root}",
            f"Environment state: {state}",
            f"Environment ready: {'yes' if state == 'ready' else 'no'}",
            "Python/pip commands automatically prepare or reuse this environment; no manual activation is needed.",
            f"Cache: {environment.cache}",
        )
    )


def _environment_state(environment: ExecutionEnvironment) -> str:
    marker = environment.root / ".redlotus-environment.json"
    if not marker.is_file():
        return (
            "error: missing identity record"
            if environment.python.is_file()
            else "missing"
        )
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
        state = record.get("state", "ready")
        if state != "ready":
            return state
        if not environment.python.is_file():
            return "error: interpreter missing"
        expected = _python_identity(environment.base_command)
        recorded = record.get("identity") or _python_identity(
            tuple(record["base_command"])
        )
        if (
            recorded != expected
            or _python_identity((str(environment.python),)) != expected
        ):
            return "incompatible"
        return "ready"
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        return f"error: {exc}"


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
    from redlotus.core.agents import current_execution_role
    from redlotus.tools.registry import PythonCommandCheck, JavaScriptCommandCheck
    from redlotus.tools.registry import code_without_literals
    from redlotus.tools.registry import read_script

    role = current_execution_role()
    policy = _execution_config().get("permissions", {})
    if not policy:
        return
    restricted = role in policy.get("restricted_roles", [])
    visited = set()

    def source(text, *, python=False, javascript=False, shell=False):
        if python:
            try:
                PythonCommandCheck(policy, inspect, restricted=restricted).check(text)
            except SyntaxError as exc:
                raise ValueError(f"Cannot inspect Python source: {exc}") from exc
        else:
            if shell:
                inspect(text)
            if javascript:
                JavaScriptCommandCheck(policy, inspect, restricted=restricted).check(text)
            if restricted and any(
                re.search(pattern, code_without_literals(text), re.I | re.M)
                for pattern in policy["blocked_script_patterns"]
            ):
                raise PermissionError(
                    f"Permission denied for {role}: restricted process API."
                )

    def inspect(value):
        for values in _command_invocations(value):
            name = _program_name(values[0]).removesuffix(".exe")
            words = [_unquote_shell_word(item) for item in values]
            lower = [item.lower() for item in words]
            if restricted and policy.get("require_explicit_commands") and (
                words[0].startswith(("$", "%")) and "=" not in words and "=" not in words[0]
            ):
                raise PermissionError("Use an explicit command; the executable cannot be resolved before execution.")
            if name in policy["background_commands"] and not (
                name == "start-process" and "-wait" in lower
            ):
                raise PermissionError(
                    "Background process launches are not allowed; run the command synchronously."
                )
            if restricted and (
                name in policy["blocked_commands"]
                or (
                    name in {"powershell", "pwsh"}
                    and any(
                        item in policy["blocked_shell_options"]
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
                    if restricted and policy.get("require_explicit_commands"):
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
                    path.suffix.lower() not in policy["script_extensions"]
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
    return folder / ("pip.exe" if _platform.system() == "Windows" else "pip")


def validate_pip_command(
    command: str | list[str] | tuple[str, ...], *, selected_python: Path
) -> None:
    """Reject pip installs that escape the selected venv or its project paths."""
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
            if len(lowered) >= 2 and lowered[1:3] == ["pip", "install"]:
                pip_index = 1
        if pip_index is None:
            return
        if (
            interpreter
            and (name in _PY_LAUNCHER_NAMES or not _is_bare(interpreter))
            and not _same_path(interpreter, selected_python)
        ):
            raise ValueError("pip 请求指定了其他 Python 解释器，请使用当前项目环境。")
        if (
            pip_index == 0
            and not _is_bare(program)
            and not _same_path(program, selected_pip)
        ):
            raise ValueError("pip 请求指定了其他安装环境，请使用当前项目环境。")
        for value in values[pip_index + 1 :]:
            option = _unquote_shell_word(value).lower().split("=", 1)[0]
            if option in _PIP_RESTRICTED_OPTIONS:
                raise ValueError(f"禁止 pip 安装参数 {option}，请使用当前项目环境。")

    for token in tokens:
        if token in _SEPARATORS:
            inspect(segment)
            segment = []
        else:
            segment.append(token)
    inspect(segment)


def _rewrite_python_command(args, environment: ExecutionEnvironment):
    values = list(args)
    if not values:
        return values
    name = _program_name(str(values[0]))
    if _is_bare(str(values[0])) and name in _PYTHON_NAMES:
        values[0] = str(environment.python)
    elif _is_bare(str(values[0])) and name in _PIP_NAMES:
        values[0] = str(_selected_pip(environment.python))
    return values


def _unquote_shell_word(word: str) -> str:
    return (
        word[1:-1]
        if len(word) >= 2 and word[0] in "\"'" and word[-1] == word[0]
        else word
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
    # the root process has exited, which matters when retrying venv creation.
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
    configured = await asyncio.to_thread(_execution_config)
    python_on_path = None
    if configured:
        python_required = any(
            _program_name(values[0]) in _PYTHON_NAMES | _PY_LAUNCHER_NAMES | _PIP_NAMES
            or (_program_name(values[0]) in _UV_NAMES and "pip" in values[1:3])
            for values in _command_invocations(args)
        )
        environment = await asyncio.to_thread(
            get_execution_environment,
            cwd=cwd,
            workspace=workspace,
            overrides=env,
            python_required=python_required,
        )
        validate_pip_command(args, selected_python=environment.python)
        if python_required:
            await ensure_execution_environment(environment)
            python_on_path = str(environment.python)
        else:
            await asyncio.to_thread(_prepare_runtime_dirs, environment)
        env = environment.variables
        if not shell:
            args = _rewrite_python_command(args, environment)

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
    encodings = _execution_config()["output_encodings"]
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
