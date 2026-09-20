"""Tools registry responsibilities."""

from __future__ import annotations

import functools
import inspect
import json
import re
import shlex
import subprocess
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

import redlotus.runtime.resources as _runtime_resources
from redlotus.execution.process import run_subprocess
from redlotus.runtime.context import (
    TRACE_STORE,
    AgentRunPolicy,
    current_short_agent_id,
    current_turn_id,
)
from redlotus.runtime.files import skills_dir, user_skills_dir
from redlotus.runtime.files import skills_dir as shipped_skills_dir


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
                        _runtime_resources.warning("无法加载 Skill %s: %s", path, exc)
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
        self, skill_name: str, script_name: str, args: str = "", timeout: float = 300
    ) -> str:
        """Run a Skill script through the same project and process checks as command tools.

        Args:
            skill_name: The exact registered Skill name.
            script_name: A Python, shell, batch or PowerShell script relative to the Skill directory.
            args: Arguments for the script; quote arguments that contain spaces.
            timeout: Maximum execution time in seconds.

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
        except subprocess.TimeoutExpired:
            return f"Error: Skill script timed out ({timeout} seconds)"
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


def wrap_tools_for_user_notify(
    tools: list[Any], *, policy: AgentRunPolicy | None = None
) -> list[Any]:
    """工厂：给每个可调用工具套壳——调用时发 🔧 通知 + 记一条 TRACE 事件。"""
    if not tools:
        return tools
    return [
        _wrap(t, policy) if callable(t) and not inspect.isclass(t) else t for t in tools
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
        _runtime_resources.info(line)
        try:
            callback(line)
        except Exception:
            _runtime_resources.debug("user_notify_callback 执行失败", exc_info=True)
    else:
        _runtime_resources.debug(line)


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
    policy: AgentRunPolicy | None,
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
    return _model_result(result, policy)


def _model_result(result: Any, policy: AgentRunPolicy | None) -> Any:
    return result


def _wrap(fn: Callable[..., Any], policy: AgentRunPolicy | None) -> Callable[..., Any]:
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
            return _model_result(result, policy)
    else:

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return _run_wrapped(fn, policy, name, args, kwargs)

    wrapper._notify_tool_wrapped = True  # type: ignore[attr-defined]
    return wrapper
