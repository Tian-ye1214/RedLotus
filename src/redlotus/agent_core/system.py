from redlotus.prompt import load_prompt
from redlotus.tools.BasicTools import BasicToolkit
from redlotus.tools.ManagementTools import TaskManager
from redlotus.tools.WorkerOrchestrator import WorkerOrchestrator
from redlotus.tools.memory import (
    ChatHistory,
)
from redlotus.tools.memory.chat_history import messages_safe_for_new_prompt
from redlotus.agent_core.input_messages import (
    UserMessage,
)

from redlotus.tools.conversation_log import SessionConversationLogs
from redlotus.ModelGateway.ModelChecker import (
    prewarm_effective_max_contexts_by_role_async,
)
from redlotus.config.app_config import get_agent_usage_limits, settings
from redlotus.cli.output import supports_model_stream
from redlotus.cli.render import (
    TextEventStreamHandler,
    clear_model_stream,
    finish_model_stream,
    print_phase,
    print_warning,
    show_model_output,
)
from redlotus.cli.cli_ui import format_user_log_text
from redlotus.infra import logger
import traceback
import json
import time
import asyncio
import uuid
from contextlib import asynccontextmanager, nullcontext
from collections.abc import Callable
from typing import Any, Coroutine, Tuple

from pydantic_ai.exceptions import ModelHTTPError

from redlotus.skills.SkillsManager import SkillsManager
from redlotus.agent_core.goal_mode import run_goal_loop

from redlotus.runtime.lifecycle import (
    AgentRegistry,
)
from redlotus.agent_core.cli_controller import AgentCliController, CliSessionState
from redlotus.agent_core.memory_service import MemoryService
from redlotus.agent_core.roles import create_coordinator_agent
from redlotus.agent_core.runner import AgentRunner
from redlotus.runtime.context import WorkspaceContext, workspace_context
from redlotus.runtime.session import SessionController
from redlotus.runtime.subagents import SubagentFactory
from redlotus.workspace.workspace import current_workspace


def _make_coordinator_stream_handler() -> TextEventStreamHandler | None:
    if not supports_model_stream():
        return None
    return TextEventStreamHandler(title="Coordinator")


def _prompt_for_role(text: str, attachments: list):
    return [text, *attachments] if attachments else text


class AgentSystem:
    """Agent 任务协调系统，管理 Manager/Coordinator 的对话历史与执行流程。"""

    def __init__(
        self,
        *,
        workspace: WorkspaceContext | None = None,
        owner_memory_allowed: bool = True,
        exit_deadline=None,
    ):
        self.workspace = workspace or WorkspaceContext.from_path(current_workspace())
        self._owner_memory_allowed = owner_memory_allowed
        self._session = SessionController()
        self._registry = AgentRegistry()
        self._background_tasks: set[asyncio.Task] = set()
        self._shutdown_done = False
        self._shutdown_task: asyncio.Task | None = None
        self._exit_deadline = exit_deadline
        self.last_turn_error: Exception | None = None
        self._cancel_lock = asyncio.Lock()
        self._skills_manager = SkillsManager()
        self._manager_history = ChatHistory()
        self._current_attachments: list = []
        self._memory_factory = SubagentFactory(
            settings()["memory_perception"]["max_concurrent"]
        )
        self._memory = MemoryService(
            workspace=self.workspace,
            owner_memory_allowed=owner_memory_allowed,
            factory=self._memory_factory,
        )
        self._toolkit = BasicToolkit(
            self._skills_manager,
            extra_worker_tools=self._memory.worker_tools,
            workspace=self.workspace,
        )
        self._task_manager = TaskManager()
        self._planning_lock = asyncio.Lock()
        self._orchestrator = WorkerOrchestrator(
            self._toolkit,
            self._task_manager,
            memory_injection_getter=lambda: self._memory.injection_for_session(),
            registry=self._registry,
        )
        self._memory.bind_runner(
            self._registry, input_source=lambda: self._session.user_inputs
        )
        self._session_logs = SessionConversationLogs(
            on_activate=self._orchestrator.set_conversation_session,
            on_reset=self._orchestrator.clear_conversation_session,
        )
        self._context_prewarmed = False
        self._coordinator_agent = None
        self._current_turn: dict[str, Any] | None = None
        self._cli_turn_id: str | None = None
        self._session_key: str | None = None
        self._cli_controller = AgentCliController(self)

    async def bind_session(self, session_key: str) -> None:
        if self._session_key is not None and self._session_key != session_key:
            self._coordinator_agent = None
            self._memory.reset_injection_snapshot()
        self._session_key = session_key
        await self._registry.ensure_agent(session_key, "coordinator")
        await self._registry.ensure_agent(session_key, "manager")
        self._orchestrator.set_session_key(session_key)

    async def end_session_agents(self, session_key: str) -> None:
        await self._orchestrator.factory.cancel_session(session_key)
        self._session.reset()
        await self._registry.cancel_session(session_key)
        await self._registry.remove_session(session_key)
        if self._session_key == session_key:
            self._session_key = None
            self._coordinator_agent = None
            self._memory.reset_injection_snapshot()
            self._orchestrator.set_session_key(None)

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    @property
    def session_key(self) -> str | None:
        return self._session_key

    @property
    def has_current_turn(self) -> bool:
        return (
            self._current_turn is not None
            or self._session.active
            or self._cancel_lock.locked()
        )

    @property
    def has_current_goal_turn(self) -> bool:
        return bool(self._current_turn and self._current_turn.get("mode") == "goal")

    @property
    def current_goal_iteration(self) -> int:
        if not self.has_current_goal_turn:
            return 0
        return int(self._current_turn.get("goal_iteration") or 0)

    def new_cli_session_state(self) -> CliSessionState:
        return self._cli_controller.new_session_state()

    def _spawn_background(self, coro: Coroutine[Any, Any, Any]) -> None:
        t = asyncio.create_task(coro)
        self._background_tasks.add(t)
        t.add_done_callback(self._background_done)

    def _background_done(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning(
                "Background work failed; durable memory jobs will retry: %s", error
            )

    async def shutdown(self) -> None:
        if self._shutdown_task is None:
            self._shutdown_done = True
            if self._exit_deadline is not None:
                self._exit_deadline.start()
            self._shutdown_task = asyncio.create_task(self._release_resources())
        await asyncio.shield(self._shutdown_task)

    async def _release_resources(self) -> None:
        self._session.queue.discard()
        self._orchestrator.factory.stop()
        self._memory_factory.stop()
        await self.cancel_current_turn()
        self._memory.seal_windows(flush=True)
        if self._session_key:
            await self.end_session_agents(self._session_key)
        pending = list(self._background_tasks)
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        await self._registry.cancel_all()
        await self._orchestrator.factory.close()
        await self._memory_factory.close()
        await self._memory.close()
        await self._toolkit.close()
        logger.info("[lifecycle] shutdown complete")

    def _on_turn_task_done(self, done_task: asyncio.Task) -> None:
        ct = self._current_turn
        if ct is not None and ct.get("task") is done_task:
            self._current_turn = None

    async def cancel_current_turn(self) -> str:
        # Keep admission closed until child threads and their tools have actually exited.
        turn = self._current_turn
        task = (
            turn["task"]
            if turn is not None
            else self._session.task or self._cli_controller._prepare_task
        )
        async with self._cancel_lock:
            self._session.reset()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if turn is not None:
                await self._orchestrator.factory.cancel_turn(turn["turn_id"])
            if self._current_turn is turn:
                self._current_turn = None
        if task is None:
            return "当前没有正在执行的用户任务。"
        return "已停止当前任务；已产生的记录保留，结果标记为取消。"

    def _start_user_turn(self, message, history, *, goal_mode=False):
        if self.has_current_turn or self._shutdown_done:
            return None
        self.last_turn_error = None
        turn_id = uuid.uuid4().hex
        task = asyncio.create_task(
            self._run_user_turn(
                turn_id,
                message,
                history,
                goal_mode=goal_mode,
                conversation_log_hint=message.text[:40],
            )
        )
        self._current_turn = {
            "turn_id": turn_id,
            "task": task,
            "text": message.text,
            "mode": "goal" if goal_mode else "single",
            "goal_iteration": 0,
        }
        task.add_done_callback(self._on_turn_task_done)
        return task

    def _handle_turn_error(self, e: Exception) -> None:
        self.last_turn_error = e
        from redlotus.ModelGateway.input_policy import InputLimitError

        if isinstance(e, InputLimitError):
            print_warning(str(e))
            return
        if isinstance(e, ModelHTTPError):
            body = e.body or {}
            code = body.get("code", "") if isinstance(body, dict) else ""
            if code == "data_inspection_failed":
                print_warning(
                    "模型内容安全审查拦截：您的输入或上下文中包含被判定为不当的内容。"
                    "请尝试换一种表达方式，或 /clear 清空上下文后重试。"
                )
            else:
                print_warning(f"模型请求错误 (HTTP {e.status_code}): {e}")
                logger.error("详细信息:\n%s", traceback.format_exc())
            return
        print_warning(f"未预期的系统错误: {e}")
        logger.error("详细信息:\n%s", traceback.format_exc())

    async def _run_user_turn(
        self, turn_id, message, history, *, goal_mode=False, conversation_log_hint=""
    ):
        def set_iteration(iteration):
            if self._current_turn and self._current_turn["turn_id"] == turn_id:
                self._current_turn["goal_iteration"] = iteration

        try:
            if goal_mode:
                async with self._outer_turn(message, turn_id):
                    await run_goal_loop(
                        self,
                        message=message,
                        history=history,
                        turn_id=turn_id,
                        conversation_log_hint=conversation_log_hint,
                        set_iteration=set_iteration,
                    )
            else:
                await self.run_agent_system(
                    message,
                    history,
                    turn_id=turn_id,
                    conversation_log_hint=conversation_log_hint,
                )
        except asyncio.CancelledError:
            logger.info("用户回合已取消 turn_id=%s", turn_id)
        except Exception as exc:
            self._handle_turn_error(exc)

    async def ask_user(self, question: str) -> str:
        return await self._toolkit.ask_user(question)

    async def wait_for_memory_quiescent(self, timeout: float = 15.0) -> bool:
        async def drain():
            if self._current_turn:
                await asyncio.gather(self._current_turn["task"], return_exceptions=True)
            while self._background_tasks:
                await asyncio.gather(*self._background_tasks, return_exceptions=True)
            return await self._memory.wait_idle(timeout=timeout)

        task = asyncio.create_task(drain())
        try:
            # A status timeout must not cancel durable production or its model request.
            return await asyncio.wait_for(asyncio.shield(task), timeout)
        except TimeoutError:
            task.add_done_callback(
                lambda done: None if done.cancelled() else done.exception()
            )
            return False

    async def _sync_skills_for_user_turn(self) -> None:
        """每次用户输入：在同一实例上重新扫描 skills（静默），避免磁盘 I/O 阻塞事件循环。"""
        await asyncio.to_thread(self._skills_manager.refresh)

    def set_ask_user_handler(self, handler):
        self._toolkit.set_ask_user_handler(handler)

    @property
    def review_store(self):
        return self._toolkit.review_store

    def set_task_directory(self, task_name: str):
        self._toolkit.set_task_directory(task_name)

    async def generate_task_title(self, user_text: str) -> str:
        from redlotus.agent_core.task_title import generate_task_title

        return await generate_task_title(user_text)

    def record_control_result(self, command, target, status, *, accepted):
        from pydantic_ai.messages import TextContent
        from redlotus.infra.paths import session_data_dir
        from redlotus.infra.persist_utils import file_lock, iso_utc_now

        receipt = dict(command=command, target=target, status=status, accepted=accepted,
                       session_id=self._session_key, observed_at=iso_utc_now())
        encoded = json.dumps(receipt, ensure_ascii=False)
        path = session_data_dir(self.workspace) / "control_events.jsonl"
        with file_lock(path):
            with path.open("a", encoding="utf-8") as stream:
                stream.write(encoded + "\n")
        self._session.add_notice([TextContent("【运行控制回执】" + encoded,
                                             metadata={"origin": "runtime_control"})])
        return receipt

    def bind_loaded_snapshot(self, agent, path, meta) -> None:
        self._session_logs.bind_loaded_snapshot(agent, path, meta)
        if agent == "coordinator":
            self._task_manager.restore(meta.get("tasks", []))

    def structured_task_status(self) -> str:
        return self._task_manager.structured_status()

    def add_urgent(self, text: str) -> bool:
        return self._session.add_urgent(text)

    async def add_urgent_message(self, message: UserMessage) -> bool:
        if not self._session.accepting_urgent:
            return False
        await self._toolkit._references.prepare_message(message)
        accepted = self._session.add_urgent(
            message.original_text or message.text, prompt=message.to_prompt()
        )
        if accepted and self._memory.current is not None:
            event = self._memory.current
            event.reference_ids = list(
                dict.fromkeys(
                    [*event.reference_ids, *(ref.id for ref in message.references)]
                )
            )
            self._memory.observations.save(event)
            self._current_attachments.extend(
                part for ref in message.references for part in ref.to_prompt()
            )
        return accepted

    async def switch_workspace(self, path) -> None:
        """Dispose the old session before publishing the new immutable workspace."""
        previous_memory = self._memory
        await self.cancel_current_turn()
        if self._session_key:
            await self.end_session_agents(self._session_key)
        await self._orchestrator.factory.cancel_all()
        await self._toolkit.close()
        handler = self._toolkit._ask_user_handler
        review_store = self.review_store
        self.workspace = WorkspaceContext.from_path(path)
        self._coordinator_agent = None
        self._memory = MemoryService(
            workspace=self.workspace,
            owner_memory_allowed=self._owner_memory_allowed,
            factory=self._memory_factory,
        )
        self._memory.bind_runner(
            self._registry, input_source=lambda: self._session.user_inputs
        )
        self._toolkit = BasicToolkit(
            self._skills_manager,
            workspace=self.workspace,
            extra_worker_tools=self._memory.worker_tools,
        )
        self._toolkit.set_ask_user_handler(handler)
        self._toolkit._review_store = review_store
        self._toolkit._file_lock = review_store._lock
        review_store.clear()
        self._orchestrator._toolkit = self._toolkit
        self._task_manager.reset()
        self._manager_history.reset()
        self._session_logs.reset()
        from redlotus.workspace.workspace import set_workspace

        set_workspace(path)
        self._spawn_background(self._retire_memory(previous_memory))
        self._memory.schedule_processing(recover=True)

    async def _retire_memory(self, memory):
        memory.schedule_processing(flush=True)
        await memory.close()

    @asynccontextmanager
    async def _outer_turn(self, message: UserMessage, turn_id: str):
        async with self._session.turn(
            message.original_text if message.original_text is not None else message.text
        ):
            with workspace_context(self.workspace):
                await self._toolkit._references.prepare_message(message)
                if self._session_key is None:
                    await self.bind_session(uuid.uuid4().hex)
                self._session_logs.ensure(message.text or "session")
                job = await self._memory.begin_turn(
                    self._session_key,
                    turn_id,
                    self._session.user_inputs[0],
                    references=message.references,
                )
                status, error = "success", ""
                self._cli_turn_id = turn_id
                self._current_attachments = [
                    *message.attachments,
                    *(part for ref in message.references for part in ref.to_prompt()),
                ]
                try:
                    yield
                except asyncio.CancelledError:
                    status, error = (
                        "cancelled",
                        "Cancelled by the owner; unfinished work is unverified.",
                    )
                    raise
                except BaseException as exc:
                    status, error = "failed", f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    self._cli_turn_id, self._current_attachments = None, []
                    paths = [
                        str(log.model_messages_path())
                        for name, log in self._session_logs._logs.items()
                        if name == "coordinator"
                    ]
                    self._orchestrator.evidence.pop(turn_id, None)
                    await self._memory.finish_turn(
                        job,
                        status=status,
                        user_inputs=self._session.user_inputs,
                        evidence_paths=paths,
                        error=error,
                    )
                    if not self._shutdown_done:
                        self._memory.schedule_processing()

    async def execute_task_with_manager(
        self, user_input: str, continue_from_previous: bool = False
    ) -> str:
        """Plan a dependency graph and execute it with child Agents.

        Args:
            user_input: Goal and constraints for the plan.
            continue_from_previous: Keep completed tasks/results and add new tasks when True.
        """
        async with self._planning_lock:
            return await self._execute_plan(user_input, continue_from_previous)

    async def _execute_plan(self, user_input: str, continue_from_previous: bool) -> str:
        self._session_logs.ensure(user_input or "task")
        logger.info("[用户]\n%s", user_input)
        tid = self._cli_turn_id

        manager_tools = (
            self._task_manager.create_todo_list,
            self._task_manager.get_todo_list,
            self._toolkit.ask_user,
            *self._memory.worker_tools[:2],
        )
        attachments = self._current_attachments

        if not continue_from_previous:
            self._task_manager.reset()
            self._manager_history.reset()
            print_phase("第一阶段: Manager 规划任务列表")
            tmpl = await asyncio.to_thread(load_prompt, "manager_planning_new.md")
            planning_text = tmpl.format(user_input=user_input)
        else:
            print_phase("第一阶段: 基于用户反馈调整任务")
            current_todo = self._task_manager.get_todo_list()
            tmpl = await asyncio.to_thread(load_prompt, "manager_planning_continue.md")
            planning_text = tmpl.format(
                user_input=user_input, current_todo=current_todo
            )

        planning_prompt = _prompt_for_role(planning_text, attachments)
        result = await self._orchestrator.plan(
            planning_prompt, self._manager_history, turn_id=tid, tools=manager_tools
        )
        show_model_output(result, title="Manager 规划")

        print_phase("第二阶段: 多Worker并行执行任务")

        final_summary = await self._orchestrator.execute_all_tasks_parallel(
            user_input, attachments=attachments, turn_id=tid
        )
        show_model_output(final_summary, title="任务汇总", markdown=False)

        print_phase("第三阶段: 生成最终报告")

        summary_tmpl = await asyncio.to_thread(load_prompt, "manager_summary.md")
        summary_text = summary_tmpl.format(
            user_input=user_input, final_summary=final_summary
        )
        summary_prompt = _prompt_for_role(summary_text, attachments)
        try:
            final_text = await self._orchestrator.plan(
                summary_prompt, self._manager_history, turn_id=tid
            )
            show_model_output(final_text, title="最终报告")
            report = final_text.strip() or final_summary.strip()
        except Exception as exc:
            logger.warning("Manager summary unavailable: %s", exc)
            report = final_summary
        return (
            report
            if self._task_manager.completed
            else "Error: Plan is incomplete.\n" + report
        )

    async def execute_task_with_worker(
        self, task_description: str, user_goal: str = "", retry_info: str = ""
    ) -> Tuple[bool, str]:
        """Delegate one bounded task to a Worker with its own thread and context.

        Args:
            task_description: Self-contained task, requirements and expected evidence.
            user_goal: Overall goal that the child should support.
            retry_info: Observed failures from earlier attempts.
        """
        return await self._orchestrator.execute_task_with_worker(
            task_description,
            user_goal,
            retry_info,
            attachments=self._current_attachments,
            turn_id=self._cli_turn_id,
        )

    async def run_agent_system(
        self,
        message: "UserMessage",
        history: "ChatHistory",
        *,
        conversation_log_hint: str = "",
        conversation_log_extra: dict | None = None,
        turn_id: str | None = None,
        output_transform: Callable[[str], str] | None = None,
        _inside_goal: bool = False,
    ) -> tuple["ChatHistory", str]:
        """Run one user request, or one inner iteration of the current goal."""
        turn_id = turn_id or uuid.uuid4().hex
        boundary = nullcontext() if _inside_goal else self._outer_turn(message, turn_id)
        async with boundary:
            if not self._context_prewarmed:
                await prewarm_effective_max_contexts_by_role_async(
                    reason="非 CLI 首条任务（预取三角色）"
                )
                self._context_prewarmed = True

            await self._sync_skills_for_user_turn()
            self._session.open_inbox()

            self._session_logs.ensure(conversation_log_hint or message.text or "")
            logger.info_file_only("[用户]\n%s", format_user_log_text(message))

            routing_tools = [
                self.execute_task_with_manager,
                self.execute_task_with_worker,
            ]
            if self._coordinator_agent is None:
                from redlotus.prompt import (
                    session_prompt_from_history,
                    memory_from_session_prompt,
                )

                restored = session_prompt_from_history(history.messages)
                if restored is not None:
                    self._memory.reset_injection_snapshot(
                        memory_from_session_prompt(restored)
                    )
                self._coordinator_agent = await create_coordinator_agent(
                    self._skills_manager,
                    self._memory.injection_for_session(),
                    routing_tools if self._owner_memory_allowed else [],
                    self._toolkit.worker_tools(include_browser=True)
                    if self._owner_memory_allowed
                    else [],
                    self.structured_task_status,
                    instructions=restored,
                )
            agent = self._coordinator_agent

            start_time = time.time()
            coord_aid = await self._registry.ensure_agent(
                self._session_key, "coordinator"
            )
            stream_handler = (
                None
                if output_transform is not None
                else _make_coordinator_stream_handler()
            )
            coord_log = self._session_logs.for_agent("coordinator")
            await coord_log.save(
                history.messages,
                extra={"kind": "coordinator", "origin": "restored_context"},
            )
            extra = {
                **(conversation_log_extra or {}),
                "kind": "coordinator",
                "turn_id": turn_id,
                "origin": "goal_instruction" if _inside_goal else "user",
            }

            async def _save_coordinator_node(run: Any) -> None:
                history.set_messages(list(run.all_messages()))
                await coord_log.save(
                    history.messages,
                    extra={**extra, "tasks": self._task_manager.snapshot()},
                )

            try:
                result = await self._registry.run(
                    lambda: AgentRunner().run(
                        agent=agent,
                        prompt=message.to_prompt(),
                        message_history=messages_safe_for_new_prompt(history.messages),
                        usage_limits=get_agent_usage_limits(),
                        event_stream_handler=stream_handler,
                        on_node=_save_coordinator_node,
                        take_urgent=lambda: [
                            *self._session.take_urgent(),
                            *self._session.take_notices(),
                            *self._memory.take_context_notices(),
                        ],
                        on_complete=self._session.close_inbox,
                    ),
                    agent_id=coord_aid,
                    turn_id=turn_id,
                )
            except BaseException:
                self._session.close_inbox()
                clear_model_stream()
                raise
            raw_output = str(result.output or "")
            output = (
                output_transform(raw_output)
                if output_transform is not None
                else raw_output
            )
            if stream_handler is not None:
                finish_model_stream(output, title="Coordinator")
            else:
                show_model_output(output, title="Coordinator")
            history.update(result)
            elapsed = time.time() - start_time

            logger.debug("run_agent_system 完成，耗时 %.2f 秒", elapsed)
            await coord_log.save(
                history.messages,
                extra={**extra, "tasks": self._task_manager.snapshot()},
            )
            return history, output

    async def prepare_cli_session(self) -> tuple[str, ...]:
        return await self._cli_controller.prepare_session()

    async def process_cli_line(
        self,
        raw_input: str,
        state: CliSessionState,
        *,
        wait_for_turn: bool,
        goal_mode: bool = False,
    ) -> str:
        return await self._cli_controller.process_line(
            raw_input, state, wait_for_turn=wait_for_turn, goal_mode=goal_mode
        )

    async def run_interactive(self, *, stop_event: asyncio.Event | None = None) -> None:
        """Run the interactive CLI/TUI."""
        await self._cli_controller.run_interactive(stop_event=stop_event)
