"""Shared CLI/TUI input admission, command and path completion, and conversation selection."""

from __future__ import annotations

import os
import asyncio
import sys
import time
from dataclasses import dataclass
from typing import Literal, TYPE_CHECKING, Awaitable, Callable
from redlotus.tools.interaction import iter_reference_spans, quote_reference_path, user_message_from_cli_input, load_file_refs
from redlotus.core.session import current_workspace, WorkspaceSnapshot, list_workspace_snapshots, read_saved_model_messages_file
from redlotus.core.config import (
    settings,
    get_agent_roles,
    role_supported_thinking_efforts,
    supported_thinking_efforts,
    user_data_dir,
    project_data_dir,
    session_data_dir,
    safe_name,
)
from pathlib import Path
from prompt_toolkit.completion import Completer, Completion
from collections.abc import Awaitable, Callable
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from redlotus.core.presentation import print_success, print_warning, print_error, print_panel, ContextUsageItem, clear_context_usage, set_context_usage, print_repl_welcome, print_startup_logo
from uuid import uuid4
from redlotus.core import config as app_config, config as logger
from redlotus.core.history import (
    context_usage_breakdown,
    prewarm_effective_max_contexts_by_role_async,
    ChatHistory,
)
from redlotus.core.cli_commands import SlashCommands, interactive_set_api


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


def _history_path() -> Path:
    base = user_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / "history"


def create_prompt_session() -> PromptSession:
    kb = KeyBindings()

    @kb.add("c-c")
    def _interrupt(event) -> None:
        buffer = event.app.current_buffer
        if buffer.text:
            # 有内容：仅清空当前行，不退出
            buffer.reset()
            return
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
        if now - self._last_interrupt_at > 2:
            self._interrupt_hits = 1
        else:
            self._interrupt_hits += 1
        self._last_interrupt_at = now

        if self._interrupt_hits >= 2:
            return True
        print_warning("再次按 Ctrl+C 退出，或输入 /exit、quit。")
        return False

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
                    if stop_task in done:
                        return None
                    return read_task.result().strip()
            except EOFError:
                return None
            except asyncio.CancelledError:
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
        while True:
            if stop_event is not None and stop_event.is_set():
                break
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


def format_snapshot_choices(snapshots: list[WorkspaceSnapshot]) -> str:
    lines = ["选择新建会话或恢复（新 → 旧）：", "", "  0. 新建会话"]
    for index, snapshot in enumerate(snapshots, 1):
        lines.append(f"  {index}. {snapshot.label}")
    lines.extend(["", "输入序号恢复，输入 0 或留空新建。"])
    return "\n".join(lines)


async def legacy_pick_snapshot(
    snapshots: list[WorkspaceSnapshot],
    read_line: ReadLineFn,
) -> WorkspaceSnapshot | None:
    if not snapshots:
        return None
    print_panel(format_snapshot_choices(snapshots), title="加载对话")
    while True:
        raw = await read_line()
        if raw is None:
            return None
        text = raw.strip()
        if not text or text == "0":
            return None
        if not text.isdigit():
            print_error("请输入有效序号。")
            continue
        index = int(text)
        if index < 1 or index > len(snapshots):
            print_error(f"序号超出范围（1-{len(snapshots)}）。")
            continue
        return snapshots[index - 1]


if TYPE_CHECKING:
    from redlotus.core.system import AgentSystem


@dataclass
class CliSessionState:
    history: ChatHistory
    is_first_input: bool = True


class AgentCliController:
    """CLI/TUI orchestration for AgentSystem."""

    EXIT_COMMANDS = {"/exit", "/quit", "exit", "quit", "退出"}
    BUSY_SAFE_COMMANDS = {
        "/agent",
        "/stop",
        "/cd",
        "/status",
        "/cancel",
        "/help",
        "/trace",
        "/tasks",
        "/pwd",
        "/config",
        "/context",
        "/usage",
        "/panel",
        "/skills",
        "/ltm",
        "/stm",
    }

    def __init__(self, system: "AgentSystem") -> None:
        self.system = system
        self._ready = asyncio.Event()
        self._ready.set()
        self._snapshot_picker: (
            Callable[[list[WorkspaceSnapshot]], Awaitable[WorkspaceSnapshot | None]]
            | None
        ) = None
        self._legacy_repl: InteractiveRepl | None = None
        self.config_prompt = None
        self.last_rejected_input: str | None = None

    def set_snapshot_picker(
        self,
        picker: Callable[[list[WorkspaceSnapshot]], Awaitable[WorkspaceSnapshot | None]]
        | None,
    ) -> None:
        self._snapshot_picker = picker

    async def _pick_snapshot(
        self,
        snapshots: list[WorkspaceSnapshot],
    ) -> WorkspaceSnapshot | None:
        if self._snapshot_picker is not None:
            return await self._snapshot_picker(snapshots)
        if self._legacy_repl is not None:
            return await legacy_pick_snapshot(snapshots, self._legacy_repl.read_line)
        return None

    async def enter_current_workspace(self, *, state=None, force_picker=False):
        generation = self.system._session.generation
        state = state or getattr(self, "_active_session_state", None)
        if state is None:
            return None
        snapshots = await asyncio.to_thread(
            list_workspace_snapshots,
            root=session_data_dir(self.system.workspace),
        )
        if not snapshots:
            if force_picker:
                print_warning("当前工作区没有可加载的对话快照。")
            return None
        chosen = await self._pick_snapshot(snapshots)
        if chosen is None or generation != self.system._session.generation:
            return None
        try:
            messages, meta = await asyncio.to_thread(
                read_saved_model_messages_file, chosen.path
            )
            if generation != self.system._session.generation:
                return None
            await self.reset_session(state.history)
            histories = {
                "coordinator": state.history,
                "manager": self.system._manager_history,
            }
            histories[chosen.agent].set_messages(messages)
            await self.system.bind_loaded_snapshot(chosen.agent, chosen.path, meta)
            state.is_first_input = False
            if task_name := meta.get("task_name"):
                self.system._toolkit.set_task_directory(task_name)
            logger.setup_task_logger(
                safe_name(chosen.topic, max_len=50, fallback="loaded")
            )
            print_success(f"已加载 {chosen.agent} 对话（{len(messages)} 条模型消息）。")
            return True
        except (OSError, ValueError) as exc:
            print_warning(f"加载失败: {exc}")
            return None

    def new_session_state(self) -> CliSessionState:
        return CliSessionState(history=ChatHistory())

    async def reset_session(self, history: ChatHistory, *, workspace=None) -> None:
        """Reset or switch a conversation and always reopen the input admission gate."""
        self.last_rejected_input = None
        self._ready.clear()
        try:
            if workspace is None:
                await self.system.reset_session()
            else:
                await self.system.switch_workspace(workspace)
        finally:
            history.reset()
            clear_context_usage()
            self._ready.set()

    async def _prewarm_contexts(self) -> None:
        await prewarm_effective_max_contexts_by_role_async(reason="program startup")
        self.system._context_prewarmed = True

    async def prepare_session(self) -> tuple[str, ...]:
        print_startup_logo()
        print_repl_welcome()
        app_config.reload_config()
        missing = app_config.missing_main_api_keys()
        if missing:
            print_warning(
                "Missing main model API config: "
                + ", ".join(missing)
                + ". Please enter /api or configure .env/config.json first."
            )
            return missing
        await self._prewarm_contexts()
        return ()

    async def _publish_context_usage(self, history: ChatHistory) -> None:
        manager = self.system._manager_history
        if not history.messages and not manager.messages:
            clear_context_usage()
            return
        items = []
        for role, source in (("manager", manager), ("coordinator", history)):
            usage = await asyncio.to_thread(
                context_usage_breakdown, role, source.messages
            )
            items.append(
                ContextUsageItem(
                    role.title(), usage["input"], usage["max"], usage["percent"]
                )
            )
        set_context_usage(items)

    async def _handle_slash_command(
        self, raw_input: str, state: CliSessionState
    ) -> str:
        system = self.system
        if system.has_current_turn:
            command = raw_input.split()[0].lower()
            if command not in self.BUSY_SAFE_COMMANDS:
                print_warning(
                    "A turn is currently running. Use /stop first or wait for it to finish."
                )
                return "continue"

        await system._sync_skills_for_user_turn()
        first_override = await SlashCommands(self, state, raw_input).run()
        if first_override is not None:
            state.is_first_input = first_override
        return "continue"

    async def _start_user_turn_from_raw_input(
        self,
        raw_input: str,
        state: CliSessionState,
        *,
        wait_for_turn: bool,
        goal_mode: bool = False,
        references,
        admission,
    ) -> str:
        system = self.system
        history = state.history
        await self._ready.wait()
        if not system._session.accepts(admission):
            references.cancel()
            return "continue"
        await self._publish_context_usage(history)
        try:
            file_refs = await references
        except (OSError, ValueError) as exc:
            self.last_rejected_input = raw_input
            print_warning(str(exc))
            return "continue"
        if not system._session.accepts(admission):
            return "continue"
        message = user_message_from_cli_input(raw_input)
        message.references = file_refs
        if file_refs:
            print_success(
                f"已解析 {len(file_refs)} 个引用文件："
                + "、".join(ref.name for ref in file_refs)
            )

        if state.is_first_input:
            task_name = await system.generate_task_title(raw_input)
            if not system._session.accepts(admission):
                return "continue"
            logger.setup_task_logger(task_name)
            system._toolkit.set_task_directory(task_name)
            await system.bind_session(uuid4().hex)
            system._session_file.update(metadata={"task_name": task_name, "title": task_name})
            state.is_first_input = False

        if not system._session.accepts(admission):
            return "continue"
        turn_task = system._start_user_turn(
            message, history, goal_mode=goal_mode, turn_id=admission.id
        )
        if turn_task is None:
            raise RuntimeError("Another turn bypassed the session queue")

        if wait_for_turn:
            try:
                await turn_task
            except KeyboardInterrupt:
                print_warning(await system.stop_current_turn())
            except asyncio.CancelledError:
                pass
        return "continue"

    async def process_line(
        self,
        raw_input: str,
        state: CliSessionState,
        *,
        wait_for_turn: bool,
        goal_mode: bool = False,
        input_id: str | None = None,
        urgent: bool = False,
    ) -> str:
        raw_input = raw_input.strip()
        if not raw_input:
            return "continue"

        command = raw_input.lower()
        if command in self.EXIT_COMMANDS:
            self.system._session.queue.discard()
            await self.system.shutdown()
            print_success("Bye.")
            return "break"

        if command in ("/clear", "新任务"):
            await self.reset_session(state.history)
            state.is_first_input = True
            return "continue"

        if raw_input.startswith("/"):
            await self._publish_context_usage(state.history)
            return await self._handle_slash_command(raw_input, state)

        admission = self.system._session.admit(
            self.system.workspace, urgent=urgent, input_id=input_id
        )
        app_config.reload_config()
        missing = app_config.missing_main_api_keys()
        if missing:
            print_warning(
                "缺少主模型 API 配置: "
                + ", ".join(missing)
                + "。请先输入 /api，或在 .env/config.json 中配置。"
            )
            return "continue"

        references = asyncio.create_task(
            load_file_refs(raw_input, workspace=admission.workspace)
        )
        references.add_done_callback(
            lambda done: None if done.cancelled() else done.exception()
        )
        if admission.urgent:
            await self.system.add_urgent_message(
                user_message_from_cli_input(raw_input),
                admission=admission,
                references=references,
            )
            print_success("加急输入已登记，将按提交顺序在下一次请求中处理。")
            return "continue"
        logger.debug(
            "input admitted id=%s sequence=%s urgent=False",
            admission.id,
            admission.sequence,
        )
        future = self.system._session.queue.submit(
            lambda: self._start_user_turn_from_raw_input(
                raw_input,
                state,
                wait_for_turn=True,
                goal_mode=goal_mode,
                references=references,
                admission=admission,
            ),
            data=raw_input,
        )
        future.add_done_callback(
            lambda done: references.cancel() if done.cancelled() else None
        )
        if wait_for_turn:
            await future
        return "continue"

    async def run_interactive(self, *, stop_event: asyncio.Event | None = None) -> None:
        if os.environ.get("REDLOTUS_LEGACY_CLI", "").strip() not in (
            "1",
            "true",
            "TRUE",
            "yes",
        ):
            from redlotus.core.tui import run_textual_tui

            await run_textual_tui(self.system, stop_event=stop_event)
            return

        missing = await self.prepare_session()
        if missing:
            await interactive_set_api()
            app_config.reload_config()
            if not app_config.missing_main_api_keys():
                await self._prewarm_contexts()
        state = self.new_session_state()
        self._active_session_state = state

        async def on_cli_keyboard_interrupt() -> None:
            if self.system.has_current_turn:
                print_warning(await self.system.stop_current_turn())
                return
            raise KeyboardInterrupt

        repl = InteractiveRepl(on_interrupt_during_handler=on_cli_keyboard_interrupt)
        self._legacy_repl = repl
        self.set_snapshot_picker(
            lambda snapshots: legacy_pick_snapshot(snapshots, repl.read_line)
        )
        loaded = await self.enter_current_workspace()
        if loaded:
            state.is_first_input = False

        pending_answer = None
        question_lock = asyncio.Lock()

        async def ask(question: str) -> str:
            nonlocal pending_answer
            async with question_lock:
                print_warning(question)
                pending_answer = asyncio.get_running_loop().create_future()
                try:
                    answer = await pending_answer
                    self.system._session.user_inputs.append(answer)
                    return answer
                finally:
                    pending_answer = None

        self.system.set_ask_user_handler(ask)
        self.set_snapshot_picker(
            lambda snapshots: legacy_pick_snapshot(
                snapshots, lambda: ask("请输入会话编号（留空取消）：")
            )
        )
        stop_event = stop_event or asyncio.Event()
        line_handlers: set[asyncio.Task] = set()

        async def handle_line(raw_input: str) -> None:
            try:
                if (
                    await self.process_line(raw_input, state, wait_for_turn=False)
                    == "break"
                ):
                    stop_event.set()
            except Exception as exc:
                self.system._handle_turn_error(exc)

        async def process_one_line(raw_input: str) -> str:
            if (
                pending_answer is not None
                and not pending_answer.done()
                and not raw_input.startswith("/")
            ):
                pending_answer.set_result(raw_input)
                return "continue"
            if raw_input.split()[:1] == ["/api"]:
                return await self.process_line(raw_input, state, wait_for_turn=False)
            task = asyncio.create_task(handle_line(raw_input))
            line_handlers.add(task)
            task.add_done_callback(line_handlers.discard)
            return "continue"

        try:
            await repl.run(process_one_line, stop_event=stop_event)
        finally:
            for task in line_handlers:
                task.cancel()
            await asyncio.gather(*line_handlers, return_exceptions=True)
            await self.system.shutdown()
