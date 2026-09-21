"""Tool and Skill discovery, invocation telemetry, and command/path permissions."""

from __future__ import annotations

import ast
import functools
import inspect
import io
import json
import re
import shlex
import subprocess
import threading
import time
import tokenize
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from redlotus.runtime import logging as logger
from redlotus.runtime.resources import (
    skills_dir,
    user_skills_dir,
)
from redlotus.runtime.resources import (
    skills_dir as shipped_skills_dir,
)
from redlotus.sessions.context import (
    TRACE_STORE,
    current_short_agent_id,
    current_turn_id,
)
from redlotus.tools.execution import run_subprocess


def read_script(path: Path) -> str:
    data = path.read_bytes()
    try:
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16")
        encoding = (
            tokenize.detect_encoding(io.BytesIO(data).readline)[0]
            if path.suffix.lower() in {".py", ".pyw"}
            else "utf-8-sig"
        )
        return data.decode(encoding)
    except (UnicodeError, SyntaxError, LookupError) as exc:
        raise ValueError(f"Cannot decode script '{path}': {exc}") from exc


def code_without_literals(source: str) -> str:
    """Mask ordinary strings/comments before checking known non-Python APIs."""
    return re.sub(
        r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|/\*[\s\S]*?\*/|//[^\n]*|\#[^\n]*""",
        " ",
        source,
    )


class PythonCommandCheck(ast.NodeVisitor):
    def __init__(self, inspect_command, *, restricted):
        self.inspect_command = inspect_command
        self.restricted = restricted
        self.names = {}
        self.values = {}
        self.unwaited = set()

    def check(self, source: str) -> None:
        tree = ast.parse(source)
        self.visit(tree)
        if self.unwaited:
            raise PermissionError(
                "Background process launch requires an explicit wait or communicate in the same script."
            )

    def resolve(self, node):
        if isinstance(node, ast.Name):
            return self.names.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self.resolve(node.value)
            return f"{base}.{node.attr}" if base else None
        if isinstance(node, ast.Call):
            return self.resolve(node.func)
        return None

    def literal(self, node):
        if isinstance(node, ast.Name):
            return self.values.get(node.id)
        if isinstance(node, (ast.List, ast.Tuple)):
            items = []
            for item in node.elts:
                value = self.literal(item.value if isinstance(item, ast.Starred) else item)
                if value is None or (isinstance(item, ast.Starred) and not isinstance(value, (list, tuple))):
                    return None
                items.extend(value if isinstance(item, ast.Starred) else [value])
            return items
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.literal(node.left), self.literal(node.right)
            if isinstance(left, (str, list, tuple)) and type(left) is type(right):
                return left + right
            return None
        try:
            return ast.literal_eval(node)
        except (ValueError, TypeError):
            return None

    def command(self, value):
        """Known launch APIs require a completely resolved command before execution."""
        explicit = isinstance(value, str) and bool(value)
        explicit = explicit or (
            isinstance(value, (list, tuple)) and bool(value)
            and all(isinstance(part, str) for part in value) and bool(value[0])
        )
        if explicit:
            self.inspect_command(value)
        elif self.restricted:
            raise PermissionError("Use an explicit command: the process target or arguments cannot be resolved before execution.")

    def visit_Import(self, node):
        for alias in node.names:
            self.names[alias.asname or alias.name.split(".")[0]] = alias.name

    def visit_ImportFrom(self, node):
        for alias in node.names:
            self.names[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def visit_Assign(self, node):
        self.visit(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.names.pop(target.id, None)
                self.values.pop(target.id, None)
                resolved = self.resolve(node.value)
                if resolved:
                    self.names[target.id] = resolved
                if isinstance(node.value, ast.Call) and resolved == "subprocess.Popen":
                    self.unwaited.discard(id(node.value))
                    self.unwaited.add(target.id)
                value = self.literal(node.value)
                if value is not None:
                    self.values[target.id] = value

    def visit_Call(self, node):
        self.generic_visit(node)
        name = self.resolve(node.func)
        owner, _, method = (name or "").rpartition(".")
        process_operation = name in {"os.kill", "os.killpg", "signal.pthread_kill"} or (
            owner in {"psutil.Process", "subprocess.Popen", "multiprocessing.Process"}
            and method in {"kill", "terminate"}
        )
        if self.restricted and process_operation:
            raise PermissionError(
                f"Permission denied: restricted process operation {name}."
            )
        if name in {
            "subprocess.run", "subprocess.call", "subprocess.check_call",
            "subprocess.check_output", "subprocess.Popen", "os.system", "os.popen",
            "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
        }:
            argument = (
                node.args[0]
                if node.args
                else next(
                    (
                        item.value
                        for item in node.keywords
                        if item.arg in {"args", "command", "cmd"}
                    ),
                    None,
                )
            )
            if name == "asyncio.create_subprocess_exec":
                argument = ast.List(elts=node.args)
            command = self.literal(argument)
            for item in node.keywords:
                if item.arg == "executable":
                    executable = self.literal(item.value)
                    self.command([executable])
                if item.arg == "shell" and self.literal(item.value) is True and isinstance(command, list):
                    command = " ".join(command)
            self.command(command)
        if name == "subprocess.Popen":
            self.unwaited.add(id(node))
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "wait",
            "communicate",
            "__exit__",
        }:
            owner = node.func.value
            if (
                isinstance(owner, ast.Call)
                and self.resolve(owner.func) == "subprocess.Popen"
            ):
                self.unwaited.discard(id(owner))
            elif isinstance(owner, ast.Name):
                self.unwaited.discard(owner.id)

    def visit_With(self, node):
        for item in node.items:
            self.visit(item.context_expr)
            if self.resolve(item.context_expr) == "subprocess.Popen":
                self.unwaited.discard(id(item.context_expr))
                if isinstance(item.optional_vars, ast.Name):
                    self.names[item.optional_vars.id] = "subprocess.Popen"
        for statement in node.body:
            self.visit(statement)

    def visit_FunctionDef(self, node):
        names, values, unwaited = self.names.copy(), self.values.copy(), self.unwaited
        self.unwaited = set()
        for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
            self.names.pop(arg.arg, None)
            self.values.pop(arg.arg, None)
        self.generic_visit(node)
        pending = self.unwaited
        self.names, self.values, self.unwaited = names, values, unwaited
        if pending:
            raise PermissionError(
                "Background process launch requires an explicit wait or communicate in the same function."
            )

    visit_AsyncFunctionDef = visit_FunctionDef


class JavaScriptCommandCheck(PythonCommandCheck):
    """Resolve known child_process imports and literal arguments; never execute source."""

    def check(self, source):
        tokens = re.findall(
            r'''//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`|[\w$]+|\.\.\.|=>|\n|[^\s]''',
            source,
        )
        tokens = [token for token in tokens if not token.startswith(("//", "/*"))]
        wrappers = {
            "exec": "shell", "execSync": "shell", "spawn": "argv",
            "spawnSync": "argv", "execFile": "argv", "execFileSync": "argv", "fork": "script",
        }
        for index, token in enumerate(tokens):
            if token == "import":
                end = next((n for n in range(index + 1, len(tokens)) if tokens[n] == "from"), None)
                if end is not None and self._module(tokens[end + 1:end + 2]):
                    self._bind(tokens[index + 1:end], imported=True)
            if token == "=":
                end = self._expression_end(tokens, index + 1)
                expression = tokens[index + 1:end]
                if index and tokens[index - 1] == "}":
                    start = index - 2
                    while start >= 0 and tokens[start] != "{":
                        start -= 1
                    if self._qualified(expression) == "child_process":
                        self._bind(tokens[start:index])
                elif index and re.fullmatch(r"[\w$]+", tokens[index - 1]):
                    name = tokens[index - 1]
                    self.names.pop(name, None)
                    self.values.pop(name, None)
                    self.names[name] = self._qualified(expression)
                    self.values[name] = self._value(expression)
            if token != "(":
                continue
            name = self._callee(tokens, index)
            if not name or not name.startswith("child_process."):
                continue
            kind = wrappers.get(name.split(".")[-1])
            if kind is None:
                continue
            arguments, _ = self._arguments(tokens, index + 1)
            program = self._value(arguments[0]) if arguments else None
            if kind == "shell":
                self.command(program)
                continue
            argv = self._value(arguments[1]) if len(arguments) > 1 else []
            if len(arguments) > 1 and ("=>" in arguments[1] or arguments[1][:1] == ["function"]):
                argv = []
            command = [program, *argv] if isinstance(program, str) and isinstance(argv, list) else None
            if kind == "script" and command:
                command.insert(0, "node")
            options = arguments[2] if len(arguments) > 2 else []
            if command and any(options[n:n + 3] == ["shell", ":", "true"] for n in range(len(options))):
                command = " ".join(command)
            self.command(command)

    def _module(self, tokens):
        return self._value(tokens) in {"child_process", "node:child_process"}

    def _qualified(self, tokens):
        if tokens[:2] == ["require", "("] and len(tokens) >= 4 and self._module(tokens[2:3]) and tokens[3] == ")":
            base, rest = "child_process", tokens[4:]
        elif tokens:
            base, rest = self.names.get(tokens[0]), tokens[1:]
        else:
            return None
        return base + "." + rest[1] if base and len(rest) >= 2 and rest[0] == "." else base

    def _callee(self, tokens, end):
        start = end - 1
        if start >= 2 and tokens[start - 1] == ".":
            start -= 2
            if tokens[start] == ")" and start >= 3:
                start -= 3
        return self._qualified(tokens[start:end]) if start >= 0 else None

    def _bind(self, tokens, *, imported=False):
        if tokens[:1] == ["{"]:
            for group in " ".join(tokens[1:-1]).split(","):
                names = group.split()
                if names:
                    self.names[names[-1]] = "child_process." + names[0]
        elif tokens:
            self.names[tokens[-1] if imported and tokens[0] == "*" else tokens[0]] = "child_process"

    @staticmethod
    def _expression_end(tokens, start):
        depth = 0
        for index in range(start, len(tokens)):
            token = tokens[index]
            if depth == 0 and token in {";", ",", "\n"}:
                return index
            depth += (token in {"(", "[", "{"}) - (token in {")", "]", "}"})
            if depth < 0:
                return index
        return len(tokens)

    @staticmethod
    def _arguments(tokens, start):
        groups, current, depth = [], [], 0
        for index in range(start, len(tokens)):
            token = tokens[index]
            if depth == 0 and token in {",", ")"}:
                groups.append(current)
                current = []
                if token == ")":
                    return groups, index
                continue
            current.append(token)
            depth += (token in {"(", "[", "{"}) - (token in {")", "]", "}"})
        return [], len(tokens)

    def _value(self, tokens):
        try:
            expression = " ".join("*" if token == "..." else token for token in tokens)
            return self.literal(ast.parse(expression, mode="eval").body)
        except SyntaxError:
            return None


def readable_roots(*, work_base: Path) -> tuple[Path, ...]:
    """Agent 可读根：当前项目 + 随包基线技能 + 运行时技能 overlay。"""
    roots = [work_base.resolve()]
    for d in (skills_dir(), user_skills_dir()):
        try:
            roots.append(d.resolve())
        except OSError:
            pass
    return tuple(roots)


def is_under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def assert_readable_path(path: Path, *, work_base: Path) -> Path:
    """解析后的路径必须落在 当前项目 或技能目录（基线 / overlay）下。"""
    resolved = path.resolve()
    for root in readable_roots(work_base=work_base):
        if is_under_root(resolved, root):
            return resolved
    roots = ", ".join(str(r) for r in readable_roots(work_base=work_base))
    raise ValueError(f"Path not allowed (must be under: {roots}): {resolved}")


def resolve_readable_path(name: str, *, work_base: Path) -> Path:
    """相对路径：技能路径锚定到基线/overlay，其余锚定到 当前项目；绝对路径须落在可读根内。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("Path name must not be empty")
    work = work_base.resolve()

    p_in = Path(name).expanduser()
    if p_in.is_absolute():
        return assert_readable_path(p_in, work_base=work)

    norm = name.replace("\\", "/").strip("/")
    low = norm.lower()
    # 兼容旧写法 src/skills；归一到 skills/...
    if low == "src/skills" or low.startswith("src/skills/"):
        norm = norm[len("src/") :]
        low = norm.lower()
    if low == "skills" or low.startswith("skills/"):
        rel = norm[len("skills") :].lstrip("/")
        for base in (skills_dir(), user_skills_dir()):
            cand = (base / rel).resolve() if rel else base.resolve()
            if cand.exists():
                return assert_readable_path(cand, work_base=work)
        # 默认落在可写 overlay（供新建 / 安装技能）
        cand = (
            (user_skills_dir() / rel).resolve() if rel else user_skills_dir().resolve()
        )
        return assert_readable_path(cand, work_base=work)

    return assert_readable_path((work / name).resolve(), work_base=work)


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    instructions: str
    resources: dict[str, str] = field(default_factory=dict)


class SkillsManager:
    """Discover bundled/installed Skills and expose their read/execute operations."""

    FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
    IGNORED_RESOURCE_DIRS = {".git", "__pycache__", ".idea", ".vscode"}

    def __init__(self, skills_dir: str | Path | None = None, *, workspace=None):
        self.skills_dir = (
            Path(skills_dir) if skills_dir is not None else user_skills_dir(workspace)
        )
        self.workspace = workspace
        self._roots = (shipped_skills_dir(), self.skills_dir)
        self._refresh_lock = threading.Lock()
        self.skills = {}
        self.refresh()

    def _read_skill(self, path):
        content = path.read_text(encoding="utf-8")
        match = self.FRONTMATTER_PATTERN.match(content)
        if match is None:
            raise ValueError("Missing YAML front matter")
        meta = yaml.safe_load(match[1])
        if not isinstance(meta, dict):
            raise ValueError("Skill metadata must be a YAML mapping")
        name = str(meta.get("name") or path.parent.name)
        return Skill(
            name,
            str(meta.get("description", "")),
            path.parent,
            content[match.end() :].strip(),
        )

    def refresh(self) -> None:
        with self._refresh_lock:
            fresh = {}
            # Only the writable overlay is created; its entries override bundled Skills.
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            for root in self._roots:
                for path in root.glob("*/SKILL.md"):
                    try:
                        skill = self._read_skill(path)
                        fresh[skill.name] = skill
                    except (OSError, ValueError, yaml.YAMLError) as exc:
                        logger.warning("无法加载 Skill %s: %s", path, exc)
            self.skills = fresh

    def get_all_metadata(self) -> list[Skill]:
        return [self.skills[name] for name in sorted(self.skills)]

    def get_skills_summary(self) -> str:
        rows = [
            f"- **{skill.name}**: {skill.description}"
            for skill in self.get_all_metadata()
        ]
        return "\n".join(rows)

    def list_available_skills(self) -> str:
        """List available Skills by name, description and directory without loading their bodies."""
        return (
            "\n".join(
                f"{s.name}: {s.description} ({s.path})" for s in self.get_all_metadata()
            )
            or "当前没有可用的 Skills。"
        )

    def get_skill_instructions(self, skill_name: str) -> str:
        """Load one Skill's instructions and resource list when that Skill is needed.

        Args:
            skill_name: An exact name from list_available_skills.

        Returns:
            The Skill instructions with its identity and available resources, or an error."""
        skill = self.skills.get(skill_name)
        if skill is None:
            return f"Error: Skill '{skill_name}' not found. Available: {', '.join(self.skills)}"
        resources = self.list_skill_resources(skill_name)
        return json.dumps({
            "name": skill.name,
            "description": skill.description,
            "instructions": skill.instructions,
            "resources": resources,
        }, ensure_ascii=False)

    def list_skill_resources(self, skill_name: str) -> list[str]:
        skill = self.skills.get(skill_name)
        if skill is None:
            return []
        return [
            str(path.relative_to(skill.path))
            for path in skill.path.rglob("*")
            if path.is_file()
            and path.name != "SKILL.md"
            and not self.IGNORED_RESOURCE_DIRS.intersection(
                path.relative_to(skill.path).parts
            )
        ]

    def _resource_path(self, skill_name, resource_name):
        root = self.skills[skill_name].path.resolve()
        path = (root / resource_name).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Resource must remain inside the Skill directory")
        return path

    def load_skill_resource(self, skill_name: str, resource_name: str) -> str:
        """Read a named resource within a Skill directory without executing it.

        Args:
            skill_name: The exact registered Skill name.
            resource_name: The resource path relative to that Skill directory.

        Returns:
            The resource text and identity, or an error for a missing or forbidden path."""
        try:
            skill = self.skills[skill_name]
            if resource_name not in skill.resources:
                path = self._resource_path(skill_name, resource_name)
                skill.resources[resource_name] = path.read_text(encoding="utf-8")
            return json.dumps({
                "skill": skill_name,
                "resource": resource_name,
                "content": skill.resources[resource_name],
            }, ensure_ascii=False)
        except (KeyError, OSError, ValueError) as exc:
            return f"Error loading Skill resource: {exc}"

    def refresh_skills(self) -> str:
        """Refresh bundled and installed Skills, then report the available count."""
        self.refresh()
        return f"Skills 已刷新。当前共有 {len(self.skills)} 个 Skills 可用。"

    async def execute_skill_script(
        self, skill_name: str, script_name: str, args: str = "", timeout: float | None = None
    ) -> str:
        """Run a Skill script through the same project and process checks as command tools.

        Args:
            skill_name: The exact registered Skill name.
            script_name: A Python, shell, batch or PowerShell script relative to the Skill directory.
            args: Arguments for the script; quote arguments that contain spaces.
            timeout: Optional seconds, capped by the configured command limit; omitted uses that limit.

        Returns:
            The actual exit code and output, or a path, permission or execution error."""
        executors = {
            ".py": ["python"],
            ".sh": ["bash"],
            ".bat": ["cmd", "/c"],
            ".ps1": ["powershell", "-File"],
        }
        try:
            script = self._resource_path(skill_name, script_name)
            if not script.is_file():
                raise FileNotFoundError(script_name)
            command = [
                *executors[script.suffix.lower()],
                str(script),
                *shlex.split(args),
            ]
            result = await run_subprocess(
                command,
                shell=False,
                cwd=str(self.skills[skill_name].path),
                timeout=timeout,
                workspace=self.workspace,
            )
            return result.to_text()
        except subprocess.TimeoutExpired as exc:
            return f"Error: Skill script timed out ({exc.timeout} seconds)"
        except (KeyError, OSError, ValueError) as exc:
            return f"Error executing Skill script: {exc}"

_notify_callback: ContextVar[Callable[[str], None] | None] = ContextVar(
    "user_notify_callback", default=None
)


def tool_result_succeeded(result: Any) -> bool:
    """Classify explicit business failures as failures even when the tool returned normally."""
    if hasattr(result, "return_value"):
        result = result.return_value
    if isinstance(result, tuple) and result and isinstance(result[0], bool):
        return result[0]
    if isinstance(result, dict):
        if result.get("success") is False or result.get("status") in (
            "failed",
            "error",
            "cancelled",
            "needs_input",
        ):
            return False
        return not result.get("error")
    if isinstance(result, str):
        text = result.strip()
        if not text or re.match(
            r"(?i)^(error|failed|failure|cancelled|错误|失败|已取消)\b[:：]?", text
        ):
            return False
        if re.search(r"(?i)(?:return|exit)\s*code\s*[:=]\s*-?[1-9]\d*", text):
            return False
        if text.startswith("{"):
            try:
                return tool_result_succeeded(json.loads(text))
            except ValueError:
                pass
    return result is not None


def set_user_notify_callback(fn: Callable[[str], None] | None) -> None:
    _notify_callback.set(fn)


def wrap_tools_for_user_notify(tools: list[Any]) -> list[Any]:
    """工厂：给每个可调用工具套壳——调用时发 🔧 通知 + 记一条 TRACE 事件。"""
    if not tools:
        return tools
    return [
        _wrap(t) if callable(t) and not inspect.isclass(t) else t for t in tools
    ]


def _notify(name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    """把一次调用以 "🔧 名字 [agent] · 参数" 推给用户；无回调则降级为 debug。"""

    def brief(v: Any) -> str:
        text = repr(v)
        return text if len(text) <= 80 else f"{text[:79]}…"

    parts = [f"🔧 {name}"]
    if agent_id := current_short_agent_id():
        parts.append(f"[{agent_id}]")
    if kwargs:
        parts.append(", ".join(f"{k}={brief(v)}" for k, v in list(kwargs.items())[:5]))
    elif args:
        parts.append(", ".join(brief(v) for v in args[:3]))
    line = " · ".join(parts)

    callback = _notify_callback.get()
    if callback:
        logger.info(line)
        try:
            callback(line)
        except Exception:
            logger.debug("user_notify_callback 执行失败", exc_info=True)
    else:
        logger.debug(line)


def _record(
    name: str,
    t0: float,
    success: bool,
    result: Any = None,
    error: BaseException | None = None,
) -> None:
    """把一次工具调用的结果写入 TRACE_STORE。"""
    text = result if isinstance(result, str) else repr(result)
    TRACE_STORE.record(
        current_turn_id(),
        "tool_call",
        tool_name=name,
        agent_id=current_short_agent_id() or "",
        success=success,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        output_chars=len(text or ""),
        error=f"{type(error).__name__}: {error}" if error else "",
    )


def _run_wrapped(
    fn: Callable[..., Any],
    name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    t0 = time.monotonic()
    _notify(name, args, kwargs)
    try:
        result = fn(*args, **kwargs)
    except Exception as e:
        _record(name, t0, False, error=e)
        raise
    _record(name, t0, tool_result_succeeded(result), result=result)
    return result


def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
    """给单个工具套壳：调用前发通知，调用后记事件；区分协程与普通函数。"""
    if getattr(fn, "_notify_tool_wrapped", False):
        return fn
    name = getattr(fn, "__name__", "tool")

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            t0 = time.monotonic()
            _notify(name, args, kwargs)
            try:
                result = await fn(*args, **kwargs)
            except BaseException as e:
                _record(name, t0, False, error=e)
                raise
            _record(name, t0, tool_result_succeeded(result), result=result)
            return result
    else:

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return _run_wrapped(fn, name, args, kwargs)

    wrapper._notify_tool_wrapped = True  # type: ignore[attr-defined]
    return wrapper
