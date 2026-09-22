from __future__ import annotations

import datetime
import json
import os
import platform
from functools import partial
from typing import TYPE_CHECKING

from redlotus.runtime.resources import project_data_dir, prompts_dir

if TYPE_CHECKING:
    from redlotus.tools.registry import SkillsManager


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
    return (prompts_dir() / filename).read_text(encoding="utf-8")

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
            length = (message.metadata or {}).get("instruction_prefix_length")
            if length is not None:
                return instructions[:length]
            # Legacy SDK requests combined role text and the native deferred catalog.
            from pydantic_ai.capabilities._deferred_capability_loader import (
                DEFERRED_CAPABILITY_CATALOG_PREFIX,
            )
            return instructions.partition("\n\n" + DEFERRED_CAPABILITY_CATALOG_PREFIX)[0]
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
    return "\n\n".join(filter(None, (
        get_skills_layout_text(skills_manager).rstrip(), skills_manager.get_skills_summary().rstrip(),
    )))


def project_agent_snapshot() -> str:
    from redlotus.runtime.resources import WorkspaceContext, current_workspace

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
    from redlotus.runtime.resources import current_workspace

    template = load_prompt(template_name)
    values = {
        "current_time": format_prompt_current_time(),
        "skills_layout": get_skills_layout_text(skills_manager),
        "skills_summary": skills_manager.get_skills_summary(),
        "system_info": format_system_info(),
        "long_term_memory": format_long_term_memory_for_prompt(memory_injection),
        "common_conduct": load_prompt("common_conduct.md"),
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


get_manager_system_prompt = partial(_build_role_prompt, "manager_system.md")
get_worker_system_prompt = partial(_build_role_prompt, "worker_system.md")
get_coordinator_system_prompt = partial(_build_role_prompt, "coordinator_system.md")


def window_prompt_content(payload):
    """Keep each complete event separable from the immutable window manifest."""
    from pydantic_ai import ImageUrl
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    packets = [({"events": [event]}, event.get("image_urls", [])) for event in payload["events"]]
    packets.append(({key: value for key, value in payload.items() if key != "events"}, []))
    return [ModelRequest([UserPromptPart([
        json.dumps(packet, ensure_ascii=False), *(ImageUrl(**image) for image in images),
    ])]) for packet, images in packets]
