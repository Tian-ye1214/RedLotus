"""Terminal controller responsibilities."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

import redlotus.runtime.config as _runtime_config
import redlotus.runtime.resources as _runtime_resources
from redlotus.documents.interaction import load_file_refs, user_message_from_cli_input
from redlotus.models.context import (
    ChatHistory,
    context_usage_breakdown,
    prewarm_effective_max_contexts_by_role_async,
)
from redlotus.presentation.output import (
    ContextUsageItem,
    clear_context_usage,
    presentation_events,
    print_repl_welcome,
    print_startup_logo,
    print_success,
    print_warning,
    set_context_usage,
)
from redlotus.presentation.snapshots import WorkspaceSnapshot, list_workspace_snapshots
from redlotus.runtime.files import session_data_dir
from redlotus.terminal.commands import SlashCommands, interactive_set_api
from redlotus.terminal.console import (
    InteractiveRepl,
    SnapshotAction,
    SnapshotSelection,
    legacy_pick_snapshot,
)


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

    def __init__(self, system) -> None:
        self.system = system
        self.system.events.handlers.update(presentation_events().handlers)
        self.system.events.handlers["input_rejected"] = self._input_rejected
        self._ready = asyncio.Event()
        self._ready.set()
        self._admission_lock = asyncio.Lock()
        self._transition = 0
        self._snapshot_picker: (
            Callable[[list[WorkspaceSnapshot]], Awaitable[SnapshotSelection]]
            | None
        ) = None
        self._snapshot_loaded_callback: (
            Callable[[WorkspaceSnapshot, list], None] | None
        ) = None
        self._legacy_repl: InteractiveRepl | None = None
        self.config_prompt = None
        self.last_rejected_input: str | None = None

    def _input_rejected(self, text):
        """Keep rejected attachment input available for editing in the composer."""
        self.last_rejected_input = text

    @property
    def is_transitioning(self) -> bool:
        return not self._ready.is_set()

    def set_snapshot_picker(
        self,
        picker: Callable[[list[WorkspaceSnapshot]], Awaitable[SnapshotSelection]]
        | None,
    ) -> None:
        self._snapshot_picker = picker

    def set_snapshot_loaded_callback(
        self,
        callback: Callable[[WorkspaceSnapshot, list], None] | None,
    ) -> None:
        self._snapshot_loaded_callback = callback

    async def _pick_snapshot(
        self,
        snapshots: list[WorkspaceSnapshot],
    ) -> SnapshotSelection:
        if self._snapshot_picker is not None:
            return await self._snapshot_picker(snapshots)
        if self._legacy_repl is not None:
            return await legacy_pick_snapshot(snapshots, self._legacy_repl.read_line)
        return SnapshotSelection(SnapshotAction.CANCEL)

    async def enter_current_workspace(self, *, state=None, force_picker=False):
        """Lock admission throughout discovery, selection and restoring the chosen session."""
        if self.is_transitioning:
            return None
        self._ready.clear()
        try:
            return await self._choose_current_workspace(state=state, force_picker=force_picker)
        finally:
            self._ready.set()

    async def _choose_current_workspace(self, *, state=None, force_picker=False):
        generation = self.system._session.generation
        state = state or getattr(self, "_active_session_state", None)
        if state is None:
            return None
        snapshots = await asyncio.to_thread(
            list_workspace_snapshots,
            root=session_data_dir(self.system.workspace),
            include_unloadable=True,
        )
        if not snapshots:
            if not force_picker:
                return None
            print_warning("当前工作区没有可加载的对话快照。可新建会话或取消。")
        selection = await self._pick_snapshot(snapshots)
        if generation != self.system._session.generation:
            return None
        if selection.action is SnapshotAction.CANCEL:
            return None
        if selection.action is SnapshotAction.NEW:
            await self.reset_session(state.history)
            state.is_first_input = True
            return False
        chosen = selection.snapshot
        if chosen is None or not chosen.is_loadable:
            print_warning("所选会话无法加载，请选择其他会话或新建会话。")
            return None
        restored_messages = None
        try:
            async def restore():
                nonlocal restored_messages
                restored_messages = await self.system.bind_loaded_snapshot(
                    chosen.path, state=state, title=chosen.title
                )

            if not await self.reset_session(
                state.history, restore=restore
            ):
                return None
            if self._snapshot_loaded_callback is not None:
                try:
                    self._snapshot_loaded_callback(chosen, restored_messages)
                except Exception as exc:
                    print_warning(f"已恢复会话，但无法显示历史对话: {exc}")
            print_success(f"已加载 {chosen.agent} 对话（{len(restored_messages)} 条模型消息）。")
            return True
        except (OSError, ValueError) as exc:
            print_warning(f"加载失败: {exc}")
            return None

    def new_session_state(self) -> CliSessionState:
        return CliSessionState(history=ChatHistory())

    async def reset_session(
        self,
        history: ChatHistory,
        *,
        workspace=None,
        prepare: Callable[[], Awaitable[bool]] | None = None,
        restore: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Reset or switch a conversation and always reopen the input admission gate."""
        self._ready.clear()
        self._transition += 1
        try:
            if prepare is not None and not await prepare():
                return False
            if restore is not None:
                await restore()
            else:
                if workspace is None:
                    await self.system.reset_session()
                else:
                    await self.system.switch_workspace(workspace)
                history.reset()
            clear_context_usage()
            self.last_rejected_input = None
        except Exception as exc:
            print_warning(f"会话切换失败，已保留原会话: {exc}")
            raise
        finally:
            self._ready.set()
        return True

    async def _prewarm_contexts(self) -> None:
        await prewarm_effective_max_contexts_by_role_async(reason="program startup")
        self.system._context_prewarmed = True

    async def prepare_session(self) -> tuple[str, ...]:
        print_startup_logo()
        print_repl_welcome()
        _runtime_config.reload_config()
        missing = _runtime_config.missing_main_api_keys()
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
        if not history.messages and not self.system._manager_history.messages:
            clear_context_usage()
            return
        items = []
        for role, source in (
            ("manager", self.system._manager_history),
            ("coordinator", history),
        ):
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
        command = raw_input.split()[0].lower()
        if self.system.has_current_turn:
            if command not in self.BUSY_SAFE_COMMANDS and not (
                self.system.is_compressing and command in {"/load", "/compress"}
            ):
                print_warning(
                    "A turn is currently running. Use /stop first or wait for it to finish."
                )
                return "continue"
        if command == "/load" and self.system.is_compressing:
            await self.system.cancel_compression()

        await self.system._sync_skills_for_user_turn()
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
        await self._ready.wait()
        if not system._session.accepts(admission):
            references.cancel()
            return "continue"
        await self._publish_context_usage(state.history)
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
            if system.session_key is None:
                await system.bind_session(uuid4().hex)
            task_name = await system.generate_task_title(raw_input)
            if not system._session.accepts(admission):
                return "continue"
            _runtime_resources.setup_task_logger(task_name)
            system._toolkit.set_task_directory(task_name)
            await system._durable_write(
                lambda: system._session_file.update(
                    metadata={"task_name": task_name, "title": task_name}
                )
            )
            state.is_first_input = False

        if not system._session.accepts(admission):
            return "continue"
        turn_task = system._start_user_turn(
            message, state.history, goal_mode=goal_mode, turn_id=admission.id
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
        if not (raw_input := raw_input.strip()):
            return "continue"

        if (command := raw_input.lower()) in self.EXIT_COMMANDS:
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

        if not self._ready.is_set():
            return "continue"
        transition = self._transition
        async with self._admission_lock:
            if transition != self._transition or not self._ready.is_set():
                return "continue"
            if not urgent:
                await self.system.retry_saved_state()
                if transition != self._transition or not self._ready.is_set():
                    return "continue"
            admission = self.system._session.admit(
                self.system.workspace, urgent=urgent, input_id=input_id
            )
            _runtime_config.reload_config()
            if missing := _runtime_config.missing_main_api_keys():
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
                return "continue"
            _runtime_resources.debug(
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
            def input_finished(done):
                try:
                    done.result()
                except asyncio.CancelledError:
                    references.cancel()
                except Exception as error:
                    references.cancel()
                    if not wait_for_turn:
                        self.system._handle_turn_error(error)

            future.add_done_callback(input_finished)
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
            from redlotus.terminal.tui import run_textual_tui

            await run_textual_tui(self, stop_event=stop_event)
            return

        missing = await self.prepare_session()
        if missing:
            await interactive_set_api()
            _runtime_config.reload_config()
            if not _runtime_config.missing_main_api_keys():
                await self._prewarm_contexts()
        state = self.new_session_state()
        self._active_session_state = state

        async def on_cli_keyboard_interrupt() -> None:
            if not self.system.has_current_turn:
                raise KeyboardInterrupt
            print_warning(await self.system.stop_current_turn())

        repl = InteractiveRepl(on_interrupt_during_handler=on_cli_keyboard_interrupt)
        self._legacy_repl = repl
        self.set_snapshot_picker(
            lambda snapshots: legacy_pick_snapshot(snapshots, repl.read_line)
        )
        await self.enter_current_workspace()

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
