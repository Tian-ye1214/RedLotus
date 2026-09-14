from __future__ import annotations

import re
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from redlotus.infra import logger
from redlotus.infra.paths import skills_dir as shipped_skills_dir, user_skills_dir
from redlotus.infra.subprocess_runner import run_subprocess


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
            Path(skills_dir) if skills_dir is not None else user_skills_dir()
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
        return "\n".join(
            [
                "## 可用的 Agent Skills",
                *rows,
                "使用 get_skill_instructions(skill_name) 获取详细指令。",
            ]
        )

    def list_available_skills(self) -> str:
        """List available Skills, descriptions and absolute resource directories."""
        return (
            "\n".join(
                f"{s.name}: {s.description} ({s.path})" for s in self.get_all_metadata()
            )
            or "当前没有可用的 Skills。"
        )

    def get_skill_instructions(self, skill_name: str) -> str:
        """Read a Skill's complete instructions before using it; also lists optional resources."""
        skill = self.skills.get(skill_name)
        if skill is None:
            return f"Error: Skill '{skill_name}' not found. Available: {', '.join(self.skills)}"
        resources = self.list_skill_resources(skill_name)
        return (
            f"# Skill: {skill.name}\n{skill.description}\n\n{skill.instructions}\n\n资源：\n"
            + "\n".join(resources)
        )

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
        """Read a Skill resource on demand, including guides, templates and script sources."""
        try:
            skill = self.skills[skill_name]
            if resource_name not in skill.resources:
                path = self._resource_path(skill_name, resource_name)
                skill.resources[resource_name] = path.read_text(encoding="utf-8")
            return f"# 资源: {skill_name}/{resource_name}\n\n{skill.resources[resource_name]}"
        except (KeyError, OSError, ValueError) as exc:
            return f"Error loading Skill resource: {exc}"

    def refresh_skills(self) -> str:
        """Rescan installed and bundled Skills after adding or changing a Skill."""
        self.refresh()
        return f"Skills 已刷新。当前共有 {len(self.skills)} 个 Skills 可用。"

    async def execute_skill_script(
        self, skill_name: str, script_name: str, args: str = "", timeout: float = 300
    ) -> str:
        """Run a script inside its Skill directory without adding its source to context.

        Supports Python, Bash, batch and PowerShell. Quote arguments containing spaces.
        Python uses the configured project interpreter; timeout also reaps child processes.
        """
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
            stdout, stderr, code = await run_subprocess(
                command,
                shell=False,
                cwd=str(self.skills[skill_name].path),
                timeout=timeout,
                workspace=self.workspace,
            )
            return f"返回码: {code}\n输出:\n{stdout}{stderr}"
        except subprocess.TimeoutExpired:
            return f"Error: Skill script timed out ({timeout} seconds)"
        except (KeyError, OSError, ValueError) as exc:
            return f"Error executing Skill script: {exc}"

    @property
    def tools(self):
        return [
            self.list_available_skills,
            self.get_skill_instructions,
            self.load_skill_resource,
            self.refresh_skills,
            self.execute_skill_script,
        ]
