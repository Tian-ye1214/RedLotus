from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

from redlotus.config import app_config
from redlotus.infra import logger
from redlotus.infra.persist_utils import safe_name
from redlotus.ModelGateway.ModelChecker import (
    context_usage_breakdown,
    prewarm_effective_max_contexts_by_role_async,
)
from redlotus.agent_core.input_messages import user_message_from_cli_input
from redlotus.cli.file_ref import load_file_refs
from redlotus.cli.output import ContextUsageItem, clear_context_usage, set_context_usage
from redlotus.cli.render import print_repl_welcome, print_success, print_warning
from redlotus.cli.repl import InteractiveRepl
from redlotus.cli.cli_commands import SlashCommands, interactive_set_api
from redlotus.cli.cli_ui import print_startup_logo
from redlotus.tools.memory import ChatHistory
from redlotus.workspace.workspace import WorkspaceSnapshot, list_workspace_snapshots
from redlotus.tools.conversation_log import read_saved_model_messages_file
from redlotus.infra.paths import project_data_dir
from redlotus.workspace.workspace_picker import legacy_pick_snapshot

if TYPE_CHECKING:
    from redlotus.agent_core.system import AgentSystem


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
        self._preparing = False
        self._prepare_task: asyncio.Task | None = None
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
        state = state or getattr(self, "_active_session_state", None)
        if state is None:
            return None
        snapshots = await asyncio.to_thread(
            list_workspace_snapshots, root=project_data_dir(self.system.workspace)
        )
        if not snapshots:
            if force_picker:
                print_warning("当前工作区没有可加载的对话快照。")
            return None
        chosen = (
            snapshots[0]
            if len(snapshots) == 1 and not force_picker
            else await self._pick_snapshot(snapshots)
        )
        if chosen is None:
            return None
        try:
            messages, meta = await asyncio.to_thread(
                read_saved_model_messages_file, chosen.path
            )
            await self.reset_session(state.history)
            histories = {
                "coordinator": state.history,
                "manager": self.system._manager_history,
            }
            histories[chosen.agent].set_messages(messages)
            self.system.bind_loaded_snapshot(chosen.agent, chosen.path, meta)
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

    async def reset_session(self, history: ChatHistory) -> None:
        system = self.system
        self.last_rejected_input = None
        self._ready.clear()
        system._session.queue.discard()
        await system.cancel_current_turn()
        system._spawn_background(system._memory.process_pending(flush=True))
        if system._session_key:
            await system.end_session_agents(system._session_key)
        system._task_manager.reset()
        await system._toolkit.close()
        system._toolkit.reset_task_directory()
        system._manager_history.reset()
        system._session_logs.reset()
        system._memory.reset_injection_snapshot()
        system._session.queue.discard()
        history.reset()
        clear_context_usage()
        self._ready.set()

    async def _prewarm_contexts(self) -> None:
        await prewarm_effective_max_contexts_by_role_async(reason="program startup")
        self.system._context_prewarmed = True

    async def prepare_session(self) -> tuple[str, ...]:
        self.system._spawn_background(self.system._memory.process_pending())
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
    ) -> str:
        system = self.system
        history = state.history
        await self._ready.wait()
        self._preparing = True
        self._prepare_task = asyncio.current_task()
        try:
            await self._publish_context_usage(history)
            try:
                file_refs = await references
            except ValueError as exc:
                self.last_rejected_input = raw_input
                print_warning(str(exc))
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
                logger.setup_task_logger(task_name)
                system._toolkit.set_task_directory(task_name)
                safe_session_key = safe_name(task_name, max_len=50, fallback="task")
                await system.bind_session(safe_session_key)
                state.is_first_input = False

            turn_task = system._start_user_turn(message, history, goal_mode=goal_mode)
            if turn_task is None:
                raise RuntimeError("Another turn bypassed the session queue")

        finally:
            self._preparing = False
            self._prepare_task = None

        if wait_for_turn:
            try:
                await turn_task
            except KeyboardInterrupt:
                print_warning(await system.cancel_current_turn())
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

        if command == "/urgent" or command.startswith("/urgent "):
            text = raw_input[len("/urgent") :].strip()
            if not text:
                print_warning("用法：/urgent <内容>")
                return "continue"
            message = user_message_from_cli_input(text)
            try:
                message.references = await load_file_refs(
                    text, workspace=self.system.workspace
                )
            except ValueError as exc:
                print_warning(str(exc))
                return "continue"
            if await self.system.add_urgent_message(message):
                print_success("加急输入将在本批工具完成后的下一次请求中处理。")
                return "continue"
            raw_input = text

        if raw_input.startswith("/"):
            await self._publish_context_usage(state.history)
            return await self._handle_slash_command(raw_input, state)

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
            load_file_refs(raw_input, workspace=self.system.workspace)
        )
        references.add_done_callback(
            lambda done: None if done.cancelled() else done.exception()
        )
        future = self.system._session.queue.submit(
            lambda: self._start_user_turn_from_raw_input(
                raw_input,
                state,
                wait_for_turn=True,
                goal_mode=goal_mode,
                references=references,
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
            from redlotus.cli.tui import run_textual_tui

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
                print_warning(await self.system.cancel_current_turn())
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
