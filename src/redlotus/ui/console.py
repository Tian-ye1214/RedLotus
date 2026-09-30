"""Shared CLI/TUI input admission, command and path completion, and conversation selection."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from redlotus.runtime.config import config_value, settings
from redlotus.runtime.resources import user_data_dir
from redlotus.pets.factory import PetFactory
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redlotus.core.system import AgentSystem
from collections.abc import Awaitable, Callable
from uuid import uuid4

from redlotus.core.gateway import generate_task_title
from redlotus.core.history import (
    context_usage_breakdown,
    prewarm_effective_max_contexts_by_role_async,
)
from redlotus.runtime import config as app_config
from redlotus.runtime import logging as logger
from redlotus.runtime.resources import session_data_dir
from redlotus.sessions.context import ChatHistory
from redlotus.sessions.control import SessionController, UserMessage, load_file_refs, user_message_from_cli_input
from redlotus.ui.cli_commands import (
    SlashCommands,
    WorkspaceSnapshot,
    interactive_set_api,
    list_workspace_snapshots,
)
from redlotus.ui.presentation import (
    ContextUsageItem,
    print_error,
    print_panel,
    print_repl_welcome,
    print_startup_logo,
    print_success,
    print_warning,
    update_output,
)
from redlotus.ui.widgets import (
    AgentCompleter,
    format_snapshot_choices,
    SnapshotAction,
    SnapshotSelection,
)


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
        "/voice",
        "/pets",
    }

    def __init__(self, system: "AgentSystem") -> None:
        self.system = system
        self.pets = PetFactory.service()
        self._ready = asyncio.Event()
        self._ready.set()
        self._admission_lock = asyncio.Lock()
        self._transition = 0
        self._active_transitions = 0
        self._snapshot_picker: (
            Callable[[list[WorkspaceSnapshot]], Awaitable[SnapshotSelection]]
            | None
        ) = None
        self._snapshot_loaded_callback: (
            Callable[[WorkspaceSnapshot, list], None] | None
        ) = None
        self._legacy_repl: InteractiveRepl | None = None
        self.config_prompt = None

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

    async def enter_current_workspace(self, *, state=None, force_picker=False, workspace=None):
        """Lock admission throughout discovery, selection and restoring the chosen session."""
        if self.is_transitioning:
            return None
        self._active_transitions += 1
        self._ready.clear()
        try:
            if workspace is not None:
                await self.reset_session(state.history, workspace=workspace)
            result = await self._choose_current_workspace(state=state, force_picker=force_picker)
            if not force_picker or result is not None:
                self._prepare_session_logs()
            return result
        finally:
            self._active_transitions -= 1
            if not self._active_transitions:
                self._ready.set()

    async def _choose_current_workspace(self, *, state=None, force_picker=False):
        generation = self.system._session.generation
        state = state or getattr(self, "_active_session_state", None)
        if state is None:
            return None
        if not force_picker and app_config.config_value(app_config.settings(), ("storage", "sessions_dir"), kind=str) is None:
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

    def new_session_state(self) -> SessionController:
        self.system._session.reply_output = self.pets.publish_reply
        return self.system._session

    async def pause_current_turn(self):
        async with self._admission_lock:
            if self.is_transitioning:
                return False
            return await self.system._session.pause(self.system, reason='user')

    def _restore_paused_queue(self, state):
        session = self.system._session
        if session.paused and not session.queue.pending:
            for row in session.paused['queued']:
                admission = session.admit(self.system.workspace, input_id=row.get('id'))
                self._queue_input(row['text'], state, admission, goal_mode=row['goal_mode'], references=None, wait_for_turn=False, data=row)

    async def resume_current_turn(self, state):
        async with self._admission_lock:
            if self.is_transitioning:
                return False
            return await state.resume(self.system,
                lambda message, admission, data: state.start(self.system, message, state.history, admission, goal_mode=data['goal_mode'])
                if message.resume else self._start_user_turn_from_raw_input(message.text, state,
                    goal_mode=data['goal_mode'], references=None, admission=admission, input_data=data, message=message),
                lambda: self._restore_paused_queue(state))

    def _queue_input(self, raw_input, state, admission, *, goal_mode, references, wait_for_turn, data=None):
        data = data if data is not None else {'text': raw_input, 'id': admission.id, 'goal_mode': goal_mode}
        future = self.system._session.queue.submit(
            lambda: self._start_user_turn_from_raw_input(raw_input, state,
                goal_mode=goal_mode, references=references, admission=admission, input_data=data), data=data)
        def input_finished(done):
            try:
                done.result()
            except asyncio.CancelledError:
                if references is not None:
                    references.cancel()
            except Exception as error:
                if references is not None:
                    references.cancel()
                if not wait_for_turn:
                    self.system._handle_turn_error(error)
            finally:
                update_output('refresh_status')
        future.add_done_callback(input_finished)
        return future

    def _prepare_session_logs(self):
        try:
            logger.activate_log_dir(logger.prepare_log_dir(self.system.workspace))
            logger.prune_old_logs()
        except (OSError, app_config.ConfigError) as exc:
            print_warning(f"日志清理未完成: {exc}")

    async def reset_session(
        self,
        history: ChatHistory,
        *,
        workspace=None,
        prepare: Callable[[], Awaitable[bool]] | None = None,
        restore: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Reset or switch a conversation and always reopen the input admission gate."""
        self._active_transitions += 1
        self._ready.clear()
        self._transition += 1
        try:
            if prepare is not None and not await prepare():
                return False
            if self.system._memory._processing.locked():
                raise ValueError("记忆重试仍在运行，完成后才能切换或清空会话。")
            await self.pets.clear_reply()
            if restore is not None:
                await restore()
            else:
                if workspace is None:
                    await self.system.reset_session()
                else:
                    await self.system.switch_workspace(workspace)
                history.reset()
            update_output("clear_context_usage")
            self.system.last_rejected_input = None
            if self._active_transitions == 1:
                self._prepare_session_logs()
        except Exception as exc:
            print_warning(f"会话切换失败，已保留原会话: {exc}")
            raise
        finally:
            self._active_transitions -= 1
            if not self._active_transitions:
                self._ready.set()
        return True

    async def _prewarm_contexts(self) -> None:
        await prewarm_effective_max_contexts_by_role_async(reason="program startup")
        self.system._context_prewarmed = True

    async def prepare_session(self) -> tuple[str, ...]:
        print_startup_logo()
        print_repl_welcome()
        try:
            await self.pets.list_pets()
        except (OSError, ValueError) as exc:
            print_warning(f"桌宠资源发现失败：{exc}")
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
        if not history.messages and not self.system._manager_history.messages:
            update_output("clear_context_usage")
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
        update_output("set_context_usage", items)

    async def _handle_slash_command(
        self, raw_input: str, state: SessionController
    ) -> str:
        command = raw_input.split()[0].lower()
        if self.system.has_current_turn:
            if command not in self.BUSY_SAFE_COMMANDS and not (
                self.system._session.is_compressing and command in {"/load", "/compress"}
            ):
                print_warning(
                    "A turn is currently running. Use /stop first or wait for it to finish."
                )
                return "continue"
        if command == "/load" and self.system._session.is_compressing:
            await self.system._session.cancel_compression()

        await asyncio.to_thread(self.system._skills_manager.refresh)
        first_override = await SlashCommands(self, state, raw_input).run()
        if first_override is not None:
            state.is_first_input = first_override
        return "continue"

    async def _start_user_turn_from_raw_input(
        self,
        raw_input: str,
        state: SessionController,
        *,
        goal_mode: bool = False,
        references,
        admission,
        input_data=None,
        message=None,
    ) -> str:
        system = self.system
        await self._ready.wait()
        if not system._session.accepts(admission):
            if references is not None:
                references.cancel()
            return "continue"
        await self._publish_context_usage(state.history)
        try:
            file_refs = message.references if message is not None else await references if references is not None and not references.cancelled() else await load_file_refs(raw_input, workspace=admission.workspace, captured=input_data)
        except (OSError, ValueError) as exc:
            self.system.last_rejected_input = raw_input
            print_warning(str(exc))
            return "continue"
        if not system._session.accepts(admission):
            return "continue"
        message = message or user_message_from_cli_input(raw_input)
        message.references = file_refs
        if file_refs:
            print_success(
                f"已解析 {len(file_refs)} 个引用文件："
                + "、".join(ref.name for ref in file_refs)
            )

        if state.is_first_input:
            if system.session_key is None:
                await system.bind_session(uuid4().hex)
            with system._session.usage(system._session_file):
                task_name = await generate_task_title(raw_input)
            if not system._session.accepts(admission):
                return "continue"
            logger.setup_task_logger(task_name)
            system.toolkit.set_task_directory(task_name)
            await system._durable_write(
                lambda: system._session_file.update(
                    metadata={"task_name": task_name, "title": task_name}
                )
            )
            state.is_first_input = False

        try:
            await system._session.start(system, message, state.history, admission, goal_mode=goal_mode)
        except KeyboardInterrupt:
            print_warning(await system.stop_current_turn())
        except asyncio.CancelledError:
            pass
        return "continue"

    async def process_line(
        self,
        raw_input: str,
        state: SessionController,
        *,
        wait_for_turn: bool,
        goal_mode: bool = False,
        input_id: str | None = None,
        urgent: bool = False,
    ) -> str:
        if not raw_input.strip():
            return "continue"

        if (command := raw_input.strip().lower()) in self.EXIT_COMMANDS:
            self.system._session.queue.discard()
            print_success("Bye.")
            return "break"

        if command in ("/clear", "新任务"):
            await self.reset_session(state.history)
            state.is_first_input = True
            return "continue"

        if command.startswith("/"):
            await self._publish_context_usage(state.history)
            return await self._handle_slash_command(raw_input.strip(), state)

        if not self._ready.is_set():
            return "continue"
        transition = self._transition
        async with self._admission_lock:
            if transition != self._transition or not self._ready.is_set():
                return "continue"
            self._restore_paused_queue(state)
            admission = self.system._session.admit(
                self.system.workspace, urgent=urgent, input_id=input_id
            )
            if missing := app_config.missing_main_api_keys():
                print_warning(
                    "缺少主模型 API 配置: "
                    + ", ".join(missing)
                    + "。请先输入 /api，或在 .env/config.json 中配置。"
                )
                return "continue"
            data = {'text': raw_input, 'id': admission.id, 'goal_mode': goal_mode}
            references = self.system._session.prepare_cli_references(self.system, raw_input, data)
            if self.system._session.paused:
                await references
            if transition != self._transition or not self._ready.is_set() or not self.system._session.accepts(admission):
                return 'continue'
            if admission.urgent:
                await self.system.add_urgent_message(
                    user_message_from_cli_input(raw_input),
                    admission=admission,
                    references=references, input_data=data,
                )
                return "continue"
            logger.debug(
                "input admitted id=%s sequence=%s urgent=False",
                admission.id,
                admission.sequence,
            )
            future = self._queue_input(raw_input, state, admission, goal_mode=goal_mode,
                                       references=references, wait_for_turn=wait_for_turn, data=data)
            await self.system._session.save_pause(self.system)
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
            from redlotus.ui.tui import run_textual_tui

            await run_textual_tui(self, stop_event=stop_event)
            return

        missing = await self.prepare_session()
        if missing:
            await interactive_set_api()
            if not app_config.missing_main_api_keys():
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
                    return await pending_answer
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


ReadLineFn = Callable[[], Awaitable[str | None]]

def _history_path() -> Path | None:
    if not os.getenv("REDLOTUS_DATA_DIR") and config_value(settings(), ("storage", "state_dir"), ..., kind=(str, type(None))) is ...:
        return None
    (base := user_data_dir()).mkdir(parents=True, exist_ok=True)
    return base / "history"


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
        self._on_interrupt_during_handler = on_interrupt_during_handler

    def _create_prompt_session(self) -> PromptSession:
        kb = KeyBindings()

        @kb.add("c-c", eager=True)
        def _interrupt(event) -> None:
            if event.app.current_buffer.text:
                self._interrupt_hits = 0
                return event.app.current_buffer.reset()
            # Use a regular exception so the input task cannot abort the event loop.
            event.app.exit(exception=InterruptedError())

        return PromptSession(
            history=FileHistory(str(path)) if (path := _history_path()) is not None else None,
            completer=AgentCompleter(),
            complete_while_typing=False,
            key_bindings=kb,
            interrupt_exception=InterruptedError,
        )

    def _on_keyboard_interrupt(self) -> bool:
        """处理空行 Ctrl+C。返回 True 表示应退出 REPL。"""
        self._interrupt_hits += 1
        if self._interrupt_hits < 2:
            print_warning("再次按 Ctrl+C 退出，或输入 /exit、quit。")
        return self._interrupt_hits >= 2

    async def read_line(self, *, stop_event: asyncio.Event | None = None) -> str | None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            if self._session is None:
                self._session = self._create_prompt_session()
            try:
                with patch_stdout(raw=True):
                    read_coro = self._session.prompt_async(self.prompt)
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
        while stop_event is None or not stop_event.is_set():
            try:
                line = await self.read_line(stop_event=stop_event)
                if line is None:
                    break
                self._interrupt_hits = 0
                action = await handler(line)
                if action == "break":
                    break
            except (KeyboardInterrupt, InterruptedError):
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


async def legacy_pick_snapshot(
    snapshots: list[WorkspaceSnapshot],
    read_line: ReadLineFn,
) -> SnapshotSelection:
    print_panel(format_snapshot_choices(snapshots), title="加载对话")
    while True:
        try:
            raw = await read_line()
        except (KeyboardInterrupt, InterruptedError):
            raw = None
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


def terminal_driver():
    if sys.platform != "win32":
        return None
    from ctypes import POINTER, cast
    from textual.drivers import win32
    from textual.drivers.windows_driver import WindowsDriver

    class ControlEnterDriver(WindowsDriver):
        def start_application_mode(self):
            if hasattr(self, "_native_reader"):
                return
            native = self._native_reader = win32.KERNEL32.ReadConsoleInputW
            def read(handle, records, size, count):
                result = native(handle, records, size, count)
                rows = cast(records, POINTER(win32.INPUT_RECORD))
                for index in range(cast(count, POINTER(win32.DWORD)).contents.value):
                    row = rows[index]
                    key = row.Event.KeyEvent
                    if (row.EventType == 1 and key.bKeyDown and key.wVirtualKeyCode == 13
                            and key.dwControlKeyState & 0x000C and key.uChar.UnicodeChar == "\r"):
                        key.uChar.UnicodeChar = "\n"
                return result
            read.argtypes, read.restype = native.argtypes, native.restype
            win32.KERNEL32.ReadConsoleInputW = read
            try:
                super().start_application_mode()
            except BaseException:
                win32.KERNEL32.ReadConsoleInputW = self._native_reader
                del self._native_reader
                raise

        def stop_application_mode(self):
            if not hasattr(self, "_native_reader"):
                return
            try:
                super().stop_application_mode()
            finally:
                win32.KERNEL32.ReadConsoleInputW = self._native_reader
                del self._native_reader

        def close(self):
            self.stop_application_mode()
            super().close()

    return ControlEnterDriver
