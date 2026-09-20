"""Prompts prompt responsibilities."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redlotus.tools.registry import SkillsManager

import datetime
import json
import os
import platform

from redlotus.runtime.files import project_data_dir, prompts_dir


def get_skills_summary(skills_manager: SkillsManager) -> str:
    return skills_manager.get_skills_summary()


def format_system_info() -> str:
    return json.dumps({
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cores": os.cpu_count(),
        "shell": os.environ.get("COMSPEC" if os.name == "nt" else "SHELL"),
    }, ensure_ascii=False)


def format_prompt_current_time() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def with_runtime_context(content) -> list:
    """Append volatile information to new input without rewriting cached instructions."""
    from pydantic_ai import TextContent

    parts = list(content) if isinstance(content, list) else [content]
    parts.append(
        TextContent(
            content=json.dumps({"current_time": format_prompt_current_time()}),
            metadata={"origin": "runtime_context"},
        )
    )
    return parts


def load_prompt(filename: str) -> str:
    filepath = prompts_dir() / filename
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


def get_skills_layout_text(skills_manager: SkillsManager) -> str:
    root = skills_manager.skills_dir.resolve()
    skills_root_path = f"Local absolute path: `{root}`"
    return load_prompt("skills_layout.md").format(skills_root_path=skills_root_path)


def format_long_term_memory_for_prompt(memory_injection: str) -> str:
    text = (memory_injection or "").strip()
    if not text:
        return ""
    if not text.startswith("<core_memory>"):
        text = "<core_memory>\n" + text + "\n</core_memory>"
    return "## Persistent memory (MEMORY.md)\n\n" + text


def session_prompt_from_history(messages) -> str | None:
    for message in reversed(messages):
        if instructions := getattr(message, "instructions", None):
            return instructions
    return None


def memory_from_session_prompt(instructions: str) -> str:
    marker = "## Persistent memory (MEMORY.md)\n\n"
    if marker not in instructions:
        return ""
    content = instructions.split(marker, 1)[1]
    return (
        content.split("</core_memory>", 1)[0] + "</core_memory>"
        if "</core_memory>" in content
        else content.strip()
    )


def get_skills_as_in_system_prompt(skills_manager: SkillsManager) -> str:
    layout = get_skills_layout_text(skills_manager).rstrip()
    summary = get_skills_summary(skills_manager).rstrip()
    if not layout:
        return summary
    if not summary:
        return layout
    return f"{layout}\n\n{summary}"


def get_common_conduct() -> str:
    return load_prompt("common_conduct.md")


def project_agent_snapshot() -> str:
    from redlotus.runtime.context import WorkspaceContext, current_workspace

    workspace = WorkspaceContext.from_path(current_workspace())
    path = project_data_dir(workspace) / "AGENT.md"
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    return json.dumps({"source": str(path), "project_instructions": text}, ensure_ascii=False) if text else ""


def _build_role_prompt(
    template_name: str,
    skills_manager: SkillsManager,
    memory_injection: str = "",
) -> str:
    from redlotus.runtime.context import current_workspace

    template = load_prompt(template_name)
    values = {
        "current_time": format_prompt_current_time(),
        "skills_layout": get_skills_layout_text(skills_manager),
        "skills_summary": get_skills_summary(skills_manager),
        "system_info": format_system_info(),
        "long_term_memory": format_long_term_memory_for_prompt(memory_injection),
        "common_conduct": get_common_conduct(),
    }
    rendered = template
    for name, value in values.items():
        rendered = rendered.replace("{" + name + "}", value)
    # Substitute known slots without interpreting other authored braces.
    sections = [rendered]
    sections.extend(
        values[name] for name in ("system_info", "skills_layout", "skills_summary", "long_term_memory", "common_conduct")
        if "{" + name + "}" not in template
    )
    sections.extend([
        project_agent_snapshot(),
        json.dumps({
            "project": str(current_workspace()),
            "deliverables": str(current_workspace() / "WorkDatabase"),
        }, ensure_ascii=False),
    ])
    return "\n\n".join(section.strip() for section in sections if section.strip())


def get_manager_system_prompt(
    skills_manager: SkillsManager,
    memory_injection: str = "",
) -> str:
    return _build_role_prompt("manager_system.md", skills_manager, memory_injection)


def get_worker_system_prompt(
    skills_manager: SkillsManager,
    memory_injection: str = "",
) -> str:
    return _build_role_prompt("worker_system.md", skills_manager, memory_injection)


def get_coordinator_system_prompt(
    skills_manager: SkillsManager,
    memory_injection: str = "",
) -> str:
    return _build_role_prompt("coordinator_system.md", skills_manager, memory_injection)


def window_prompt_content(payload):
    """Serialize the fixed perception window without clipping its evidence."""
    return json.dumps(payload, ensure_ascii=False)
