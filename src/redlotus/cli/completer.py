"""Tab 补全：/命令、/agent 参数、@文件路径。"""

from __future__ import annotations

from pathlib import Path
import os
from redlotus.workspace.workspace import current_workspace

from prompt_toolkit.completion import Completer, Completion

from redlotus.cli.completion import completion_for_input, COMMANDS
from redlotus.cli.reference_syntax import quote_reference_path
from redlotus.config.app_config import (
    get_agent_roles,
    role_supported_thinking_efforts,
    supported_thinking_efforts,
)

_COMPLETION_LIMIT = 50


class AgentCompleter(Completer):
    """根据光标前上下文补全命令、角色名或文件路径。"""

    def get_completions(self, document, complete_event):
        yield from input_completions(document.text_before_cursor)


def input_completions(text):
    context = completion_for_input(text)
    if context is None:
        return
    if context.kind == "file_path":
        yield from _iter_file_completions(context.prefix, at_mode=context.at_mode)
        return
    choices = {
        "command": COMMANDS,
        "agent_role": get_agent_roles(),
        "literal_choice": context.choices,
    }
    if context.kind == "effort_value":
        values = (
            ("off", *role_supported_thinking_efforts(context.role))
            if context.role in get_agent_roles()
            else ("off", *supported_thinking_efforts(None))
        )
    else:
        values = choices[context.kind]
    for value in values:
        if value.lower().startswith(context.prefix.lower()):
            yield Completion(
                value, start_position=-len(context.prefix), display_meta=context.kind
            )


def _resolve_parent(fragment: str) -> tuple[Path, str]:
    path = Path(fragment.replace("\\", "/")).expanduser()
    if not path.is_absolute():
        path = current_workspace() / path
    return (
        (path, "")
        if fragment.endswith(("/", "\\")) or not fragment
        else (path.parent, path.name)
    )


def _iter_file_completions(fragment: str, *, at_mode: bool):
    opener = fragment[:1] if fragment[:1] in ('"', "'", "{") else ""
    parent, prefix = _resolve_parent(fragment[1:] if opener else fragment)
    if not parent.exists():
        return

    try:
        children = sorted(
            parent.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
        )
    except (PermissionError, OSError):
        return

    count = 0
    for child in children:
        name = child.name.casefold() if os.name == "nt" else child.name
        match = prefix.casefold() if os.name == "nt" else prefix
        if not name.startswith(match):
            continue
        try:
            candidate = child.relative_to(current_workspace()).as_posix()
        except ValueError:
            candidate = child.as_posix()
        if child.is_dir():
            candidate += "/"
        candidate = quote_reference_path(
            candidate, opener=opener, directory=child.is_dir()
        )
        display = ("@" if at_mode else "") + candidate
        yield Completion(
            candidate,
            start_position=-len(fragment),
            display=display,
            display_meta="dir" if child.is_dir() else "file",
        )
        count += 1
        if count >= _COMPLETION_LIMIT:
            break
