"""Shared subprocess execution and the configured project Python environment."""

import asyncio
import json
import os
import platform as _platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
from copy import deepcopy
from dataclasses import dataclass, field
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


def _execution_config() -> dict:
    """Load execution settings lazily so importing this leaf has no config I/O."""
    from redlotus.config.app_config import settings

    return deepcopy(settings().get("execution") or {})


def _workspace_for(cwd: str | Path | None, workspace=None):
    if workspace is None:
        from redlotus.runtime.context import active_workspace

        workspace = active_workspace()
    if workspace is not None:
        return workspace
    from redlotus.runtime.context import WorkspaceContext

    return WorkspaceContext.from_path(cwd or Path.cwd())


def _runtime_path(template: str, *, runtime: Path, project_id: str) -> Path:
    value = str(template).replace("{runtime}", str(runtime))
    value = value.replace("{project_id}", project_id)
    path = Path(value).expanduser()
    return path if path.is_absolute() else runtime / path


def _runtime_root(config: dict) -> Path:
    from redlotus.infra.paths import runtime_dir

    templates = (
        str(config.get("environment_dir", "")),
        str(config.get("cache_dir", "")),
    )
    if any("{runtime}" in value for value in templates):
        return runtime_dir().resolve()
    return Path.cwd().resolve()


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
) -> dict[str, str]:
    inherited_names = tuple(config.get("inherit_env") or ())
    variables = {
        name: os.environ[name] for name in inherited_names if name in os.environ
    }
    bin_dir = root / ("Scripts" if _platform.system() == "Windows" else "bin")
    variables["PATH"] = os.pathsep.join(
        value for value in (str(bin_dir), variables.get("PATH", "")) if value
    )
    variables["VIRTUAL_ENV"] = str(root)

    project_cache = cache / project_id
    variables.update(
        {
            "PIP_CACHE_DIR": str(project_cache / "pip"),
            "XDG_CACHE_HOME": str(project_cache / "xdg"),
            "TEMP": str(project_cache / "tmp"),
            "TMP": str(project_cache / "tmp"),
            "TMPDIR": str(project_cache / "tmp"),
            "HOME": str(project_cache / "home"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
    )
    if _platform.system() == "Windows":
        home = project_cache / "home"
        variables.update(
            {
                "USERPROFILE": str(home),
                "APPDATA": str(home / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(home / "AppData" / "Local"),
            }
        )

    for name, value in (overrides or {}).items():
        if name in inherited_names or name in _EXPLICIT_ENV_OVERRIDES:
            variables[name] = str(value)
    if overrides and "PATH" in overrides:
        variables["PATH"] = os.pathsep.join(
            value for value in (str(bin_dir), str(overrides["PATH"])) if value
        )
    return variables


def get_execution_environment(
    *,
    cwd: str | Path | None = None,
    workspace=None,
    overrides: dict[str, str] | None = None,
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

    runtime = _runtime_root(config)
    root = _runtime_path(
        config["environment_dir"], runtime=runtime, project_id=active.project_id
    ).resolve()
    cache = _runtime_path(
        config["cache_dir"], runtime=runtime, project_id=active.project_id
    ).resolve()
    base_command = _configured_base_python(
        config, runtime=runtime, project_id=active.project_id
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
        ),
        base_command,
    )
    return environment


async def ensure_execution_environment(environment: ExecutionEnvironment) -> None:
    """Create a project venv once under a cross-process lock."""
    marker = environment.root / ".redlotus-environment.json"
    from filelock import AsyncFileLock

    from redlotus.infra.persist_utils import atomic_write_json

    timeout = _provision_timeout()
    environment.root.parent.mkdir(parents=True, exist_ok=True)
    lock = environment.root.with_name(environment.root.name + ".create.lock")
    async with AsyncFileLock(lock, timeout=timeout, run_in_executor=False):
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
            if not _same_commands(recorded, environment.base_command):
                raise RuntimeError(
                    "项目 Python 环境由其他基础解释器创建，请修改配置或清理该项目环境后重试。"
                )
            if environment.python.is_file() and state == "ready":
                _prepare_runtime_dirs(environment)
                return
        _prepare_runtime_dirs(environment)
        source = {
            "project_id": environment.project_id,
            "python": str(environment.python),
            "base_command": list(environment.base_command),
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
                stdout, stderr, code = await _run_owned_process(
                    command,
                    shell=False,
                    cwd=str(environment.workspace_root),
                    env=bootstrap_env,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(f"创建项目 Python 环境超时（{timeout} 秒）") from exc
            if code:
                raise RuntimeError(
                    f"创建项目 Python 环境失败（返回码 {code}）: {(stderr or stdout).strip()}"
                )
            if not environment.python.is_file():
                raise RuntimeError(
                    f"Python 环境创建后未找到解释器: {environment.python}"
                )
            atomic_write_json(marker, {**source, "state": next_state})


def _provision_timeout() -> int:
    from redlotus.config.app_config import get_agent_run_policy

    return get_agent_run_policy().max_command_timeout_seconds


def _same_commands(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return len(left) == len(right) and all(
        os.path.normcase(str(Path(a).resolve()))
        == os.path.normcase(str(Path(b).resolve()))
        if index == 0
        else a == b
        for index, (a, b) in enumerate(zip(left, right))
    )


def _prepare_runtime_dirs(environment: ExecutionEnvironment) -> None:
    paths = {
        environment.cache / environment.project_id,
        Path(environment.variables["PIP_CACHE_DIR"]),
        Path(environment.variables["XDG_CACHE_HOME"]),
        Path(environment.variables["TEMP"]),
        Path(environment.variables["HOME"]),
    }
    for name in ("APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        if name in environment.variables:
            paths.add(Path(environment.variables[name]))
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def describe_execution_environment(
    *, cwd: str | Path | None = None, workspace=None
) -> str:
    """Return the selected interpreter and paths without guessing host state."""
    environment = get_execution_environment(cwd=cwd, workspace=workspace)
    return "\n".join(
        (
            f"Project: {environment.project_id}",
            f"Workspace: {environment.workspace_root}",
            f"Python: {environment.python}",
            f"Environment: {environment.root}",
            f"Environment ready: {'yes' if environment.python.is_file() else 'no'}",
            f"Cache: {environment.cache}",
        )
    )


def _shell_tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=False, punctuation_chars=";&|\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = []
    for token in lexer:
        tokens.append(token)
        if _program_name(tokens[0]) in {"cmd", "cmd.exe"} and token.lower() in {
            "/c",
            "/k",
        }:
            # cmd strips its outer quote pair before interpreting the command body.
            tokens.append(lexer.instream.read().strip())
            break
    return tokens


def _command_invocations(command):
    """Expose executable positions, including ordinary nested shell commands."""
    tokens = _shell_tokens(command) if isinstance(command, str) else list(command)
    segment = []
    for token in [*tokens, ";"]:
        if token not in _SEPARATORS:
            segment.append(str(token))
            continue
        if not segment:
            continue
        yield segment
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
                if value.lower() in {"/c", "/k", "-c", "-command"}:
                    yield from _command_invocations(
                        _unquote_shell_word(" ".join(segment[index + 1 :]))
                    )
                    break
        segment = []


def validate_agent_command(command, *, cwd: str) -> None:
    """Apply the configured child-tool permissions before starting a process."""
    from redlotus.runtime.context import current_execution_role

    role = current_execution_role()
    policy = _execution_config().get("permissions", {})
    if role not in policy.get("restricted_roles", []):
        return
    invocations = list(_command_invocations(command))
    denied = set(policy["blocked_commands"])
    for values in invocations:
        if _program_name(values[0]).removesuffix(".exe") in denied:
            raise PermissionError(
                f"Permission denied for {role}: process-control commands are reserved for the runtime."
            )
    texts = [command if isinstance(command, str) else subprocess.list2cmdline(command)]
    for value in dict.fromkeys(value for values in invocations for value in values):
        path = Path(_unquote_shell_word(value))
        if path.suffix.lower() in policy["script_extensions"]:
            path = Path(cwd) / path
            if path.is_file():
                texts.append(path.read_text(encoding="utf-8-sig"))
    for pattern in policy["blocked_code_patterns"]:
        if any(
            re.search(pattern, text, re.IGNORECASE | re.MULTILINE) for text in texts
        ):
            raise PermissionError(
                f"Permission denied for {role}: the command or script contains a restricted process-control operation."
            )


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


def has_background_shell_command(command: str) -> bool:
    """Recognize shell commands without inspecting quoted program source as shell."""
    tokens = _shell_tokens(command)
    if tokens and tokens[-1] == "&":
        return True
    at_command = True
    executable = ""
    for index, token in enumerate(tokens):
        if token in (";", "&", "&&", "|", "||", "\n"):
            at_command = True
            continue
        value = _unquote_shell_word(token)
        if at_command:
            executable = value.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if executable in ("start", "nohup", "setsid", "start-process"):
                return True
            at_command = False
        if executable in (
            "cmd",
            "cmd.exe",
            "powershell",
            "powershell.exe",
            "pwsh",
            "pwsh.exe",
            "sh",
            "bash",
            "zsh",
        ) and value.lower() in ("/c", "/k", "-c", "-command"):
            return has_background_shell_command(
                _unquote_shell_word(" ".join(tokens[index + 1 :]))
            )
    return False


async def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    """杀掉子进程及其后代（无 psutil 依赖），并收尸。"""
    if proc.returncode is not None:
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
) -> tuple[str, str, int | None]:
    """跑子进程并返回 (stdout, stderr, returncode)；取消或超时都会杀掉整棵进程树。

    取代 asyncio.to_thread(subprocess.run, ...)——后者在任务被取消时既不中断阻塞线程、
    也不杀子进程，会留下孤儿进程与卡死的线程池槽位。
    """
    await asyncio.to_thread(validate_agent_command, args, cwd=cwd)
    configured = await asyncio.to_thread(_execution_config)
    if configured:
        environment = await asyncio.to_thread(
            get_execution_environment,
            cwd=cwd,
            workspace=workspace,
            overrides=env,
        )
        validate_pip_command(args, selected_python=environment.python)
        await ensure_execution_environment(environment)
        env = environment.variables
        if not shell:
            args = _rewrite_python_command(args, environment)

    return await _run_owned_process(
        args, shell=shell, cwd=cwd, env=env, timeout=timeout
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
    return (
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
        proc.returncode,
    )
