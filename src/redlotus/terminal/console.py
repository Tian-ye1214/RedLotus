"""Terminal console responsibilities."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from redlotus.documents.interaction import iter_reference_spans, quote_reference_path
from redlotus.presentation.output import (
    print_error,
    print_panel,
    print_success,
    print_warning,
)
from redlotus.presentation.snapshots import WorkspaceSnapshot
from redlotus.runtime.config import (
    ConfigError,
    get_agent_roles,
    load_config,
    role_supported_thinking_efforts,
    settings,
    supported_thinking_efforts,
)
from redlotus.runtime.context import current_workspace
from redlotus.runtime.files import user_data_dir
from redlotus.runtime.resources import (
    ExitDeadline,
    close_all_clients,
    install_stop_handlers,
)
from redlotus.runtime.setup import prepare_startup_configuration


async def run_cli(system=None):
    """Run the interactive RedLotus CLI/TUI."""
    from redlotus.core.system import AgentSystem
    from redlotus.terminal.controller import AgentCliController

    if system is None:
        load_config()
        system = AgentSystem()
    stop_event = asyncio.Event()
    install_stop_handlers(stop_event)
    try:
        await AgentCliController(system).run_interactive(stop_event=stop_event)
    finally:
        await system.shutdown()
        await close_all_clients()
    return system


def main() -> None:
    """CLI entrypoint used by root ``main.py``."""
    try:
        load_config()
        if not asyncio.run(prepare_startup_configuration()):
            return
        from redlotus.core.system import AgentSystem

        deadline = ExitDeadline(settings()["lifecycle"]["shutdown_grace_seconds"])
        try:
            system = AgentSystem(exit_deadline=deadline)
            asyncio.run(run_cli(system))
        finally:
            deadline.close()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from None


COMMAND_HELP = {
    "/help": "显示本帮助",
    "/exit": "退出程序（也接受 quit、exit、退出）",
    "/quit": "退出程序",
    "/clear": "清空上下文并开启新对话（也接受“新任务”，旧快照保留）",
    "/status": "查看 Agent 生命周期与调用状态",
    "/config": "查看配置摘要",
    "/context": "查看上下文 token 用量分解与压缩阈值",
    "/usage": "查看用量与计费统计，可指定日志路径",
    "/panel": "查看工作区运行和历史总览；--all 显示全部会话",
    "/LTM": "show / clear / retry：查看、清空或重试全局长期记忆",
    "/STM": "show / clear / retry：查看、清空或重试当前项目情景记忆",
    "/pwd": "查看当前项目目录",
    "/cd": "/cd <path>：切换项目并加载该项目对话",
    "/skills": "查看已加载 Skills",
    "/agent": "/agent <role> <预设或模型名>：下一次请求切换模型，保留会话",
    "/effort": "/effort <role> off 或支持的级别：查看或设置思考",
    "/api": "查看配置对话；/api embedding 配置 embedding/rerank 接口",
    "/compress": "压缩 Manager / Coordinator 上下文",
    "/cancel": "/cancel <invocation_id> 或 /cancel agent <agent_id>：取消调用",
    "/stop": "停止当前任务，保留会话",
    "/load": "选择并加载当前项目对话快照",
    "/trace": "/trace <turn_id>：查看追踪记录",
    "/tasks": "查看任务状态与依赖",
}


COMMANDS = tuple(COMMAND_HELP)


CompletionKind = Literal[
    "command", "agent_role", "effort_value", "literal_choice", "file_path"
]


_SUBCOMMAND_CHOICES: dict[str, tuple[str, ...]] = {
    "/ltm": ("show", "clear", "retry"),
    "/stm": ("show", "clear", "retry"),
    "/cancel": ("agent",),
    "/api": ("embedding",),
}


@dataclass(frozen=True)
class InputCompletion:
    """Describes what to complete for a given input prefix."""

    kind: CompletionKind
    prefix: str
    at_mode: bool = False
    choices: tuple[str, ...] = ()
    role: str = ""


def completion_for_input(text: str) -> InputCompletion | None:
    """Return completion context for *text*, or None if no completion applies."""
    if text.startswith("/") and " " not in text:
        return InputCompletion(kind="command", prefix=text)

    if text.startswith("/agent "):
        prefix = text[len("/agent ") :]
        if " " not in prefix:
            return InputCompletion(kind="agent_role", prefix=prefix)
        role, selected = prefix.split(" ", 1)
        if " " not in selected:
            return InputCompletion(
                kind="literal_choice",
                prefix=selected,
                choices=tuple(settings().get("model_presets", {})),
                role=role,
            )
        return None

    if text.startswith("/effort "):
        rest = text[len("/effort ") :]
        if " " not in rest:
            return InputCompletion(kind="agent_role", prefix=rest)
        role, prefix = rest.split(" ", 1)
        if " " not in prefix.strip():
            return InputCompletion(
                kind="effort_value", prefix=prefix, role=role.lower()
            )
        return None

    if text.startswith("/cd "):
        prefix = text.split(" ", 1)[1] if " " in text else ""
        return InputCompletion(kind="file_path", prefix=prefix)

    for cmd, choices in _SUBCOMMAND_CHOICES.items():
        if text.lower().startswith(cmd + " "):
            prefix = text[len(cmd) + 1 :]
            if " " not in prefix:
                return InputCompletion(
                    kind="literal_choice", prefix=prefix, choices=choices
                )
            return None

    references = list(iter_reference_spans(text, root=current_workspace()))
    if references:
        reference = references[-1]
        if reference.end == len(text) and not (reference.opener and reference.closed):
            return InputCompletion(
                kind="file_path", prefix=text[reference.start + 1 :], at_mode=True
            )

    return None


_COMPLETION_LIMIT = 50


class AgentCompleter(Completer):
    """根据光标前上下文补全命令、角色名或文件路径。"""

    def get_completions(self, document, complete_event):
        yield from input_completions(document.text_before_cursor)


def input_completions(text):
    if context := completion_for_input(text):
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
    path = path if path.is_absolute() else current_workspace() / path
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


def _history_path() -> Path:
    (base := user_data_dir()).mkdir(parents=True, exist_ok=True)
    return base / "history"


def create_prompt_session() -> PromptSession:
    kb = KeyBindings()

    @kb.add("c-c")
    def _interrupt(event) -> None:
        if event.app.current_buffer.text:
            # 有内容：仅清空当前行，不退出
            return event.app.current_buffer.reset()
        # 空行：交给外层 KeyboardInterrupt 处理（支持连按两次退出）
        event.app.exit(exception=KeyboardInterrupt())

    return PromptSession(
        history=FileHistory(str(_history_path())),
        completer=AgentCompleter(),
        complete_while_typing=False,
        key_bindings=kb,
    )


class InteractiveRepl:
    """TTY 交互循环；非 TTY 回退到标准 input。"""

    def __init__(
        self,
        *,
        prompt: str = "\n📝 请输入您的任务: ",
        on_interrupt_during_handler: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.prompt = prompt
        self._session: PromptSession | None = None
        self._interrupt_hits = 0
        self._last_interrupt_at = 0.0
        self._on_interrupt_during_handler = on_interrupt_during_handler

    def _get_session(self) -> PromptSession:
        if self._session is None:
            self._session = create_prompt_session()
        return self._session

    def _on_keyboard_interrupt(self) -> bool:
        """处理空行 Ctrl+C。返回 True 表示应退出 REPL。"""
        now = time.monotonic()
        self._interrupt_hits = 1 if now - self._last_interrupt_at > 2 else self._interrupt_hits + 1
        self._last_interrupt_at = now
        if self._interrupt_hits < 2:
            print_warning("再次按 Ctrl+C 退出，或输入 /exit、quit。")
        return self._interrupt_hits >= 2

    async def read_line(self, *, stop_event: asyncio.Event | None = None) -> str | None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            session = self._get_session()
            try:
                with patch_stdout(raw=True):
                    read_coro = session.prompt_async(self.prompt)
                    if stop_event is None:
                        return (await read_coro).strip()
                    read_task = asyncio.create_task(read_coro)
                    stop_task = asyncio.create_task(stop_event.wait())
                    done, pending = await asyncio.wait(
                        {read_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                        try:
                            await t
                        except asyncio.CancelledError:
                            pass
                    return None if stop_task in done else read_task.result().strip()
            except (EOFError, asyncio.CancelledError):
                return None
        try:
            return (await asyncio.to_thread(input, self.prompt)).strip()
        except EOFError:
            return None

    async def run(
        self,
        handler: Callable[[str], Awaitable[str]],
        *,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """
        handler 返回 "continue" | "break"。
        空行 Ctrl+C：连按两次退出；有内容时 Ctrl+C 仅清空输入行。
        """
        await self._loop(handler, stop_event)

    async def _loop(
        self,
        handler: Callable[[str], Awaitable[str]],
        stop_event: asyncio.Event | None,
    ) -> None:
        while stop_event is None or not stop_event.is_set():
            try:
                line = await self.read_line(stop_event=stop_event)
                if line is None:
                    break
                self._interrupt_hits = 0
                action = await handler(line)
                if action == "break":
                    break
            except KeyboardInterrupt:
                if self._on_interrupt_during_handler is not None:
                    try:
                        await self._on_interrupt_during_handler()
                        continue
                    except KeyboardInterrupt:
                        pass
                if self._on_keyboard_interrupt():
                    print_success("再见！")
                    break
            except asyncio.CancelledError:
                break


ReadLineFn = Callable[[], Awaitable[str | None]]


class SnapshotAction(str, Enum):
    NEW = "new"
    RESTORE = "restore"
    CANCEL = "cancel"


@dataclass(frozen=True)
class SnapshotSelection:
    action: SnapshotAction
    snapshot: WorkspaceSnapshot | None = None


@dataclass(frozen=True)
class VisibleConversationEntry:
    role: Literal["用户", "助手"]
    text: str


def visible_conversation_entries(messages) -> list[VisibleConversationEntry]:
    """Keep only human-readable turns when replaying a restored conversation."""
    entries = []
    for message in messages:
        if isinstance(message, ModelRequest):
            parts = []
            for part in message.parts:
                if not isinstance(part, UserPromptPart):
                    continue
                content = part.content
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, (list, tuple)) and content:
                    # UserMessage.to_prompt() keeps the original request first;
                    # later entries are references, media, or runtime metadata.
                    if isinstance(content[0], str):
                        parts.append(content[0])
            role = "用户"
        elif isinstance(message, ModelResponse):
            parts = [part.content for part in message.parts if isinstance(part, TextPart)]
            role = "助手"
        else:
            continue
        text = "\n".join(part for part in parts if part.strip())
        if text:
            entries.append(VisibleConversationEntry(role, text))
    return entries


def format_snapshot_choices(snapshots: list[WorkspaceSnapshot]) -> str:
    return "\n".join(
        [
            "选择新建会话或恢复（新 → 旧）：",
            "",
            "  0. 新建会话",
            *(f"  {index}. {snapshot.label}" for index, snapshot in enumerate(snapshots, 1)),
            "  c. 取消",
            "",
            "输入序号恢复，输入 0 新建，留空或 c 取消。",
        ]
    )


async def legacy_pick_snapshot(
    snapshots: list[WorkspaceSnapshot],
    read_line: ReadLineFn,
) -> SnapshotSelection:
    print_panel(format_snapshot_choices(snapshots), title="加载对话")
    while True:
        raw = await read_line()
        if raw is None:
            return SnapshotSelection(SnapshotAction.CANCEL)
        text = raw.strip()
        if not text or text.lower() in ("c", "cancel"):
            return SnapshotSelection(SnapshotAction.CANCEL)
        if text == "0":
            return SnapshotSelection(SnapshotAction.NEW)
        if not text.isdigit():
            print_error("请输入有效序号。")
            continue
        index = int(text)
        if index < 1 or index > len(snapshots):
            print_error(f"序号超出范围（1-{len(snapshots)}）。")
            continue
        snapshot = snapshots[index - 1]
        if not snapshot.is_loadable:
            print_error("该会话条目无法加载，请选择其他会话或新建会话。")
            continue
        return SnapshotSelection(SnapshotAction.RESTORE, snapshot)
