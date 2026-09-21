"""Main Agent coordination, user turns, goal execution, and conversation lifecycle."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager, nullcontext
from typing import Any, Tuple

from redlotus.core.agents import AgentRegistry, SubagentFactory
from redlotus.core.gateway import AgentRunner, create_coordinator_agent
from redlotus.core.history import (
    compress_histories,
    prewarm_effective_max_contexts_by_role_async,
)
from redlotus.core.tasks import TaskManager, TaskStatus, run_goal_loop
from redlotus.memory.service import MemoryService
from redlotus.runtime import logging as logger
from redlotus.runtime.config import get_agent_usage_limits
from redlotus.runtime.resources import (
    WorkspaceContext,
    current_workspace,
    finish_file_io,
    session_data_dir,
    workspace_context,
)
from redlotus.sessions.context import (
    ChatHistory,
    messages_safe_for_new_prompt,
    repair_interrupted_tool_calls,
)
from redlotus.sessions.control import SessionController, UserMessage
from redlotus.sessions.storage import SessionFile
from redlotus.tools.base_tools import BasicToolkit
from redlotus.tools.registry import SkillsManager
from redlotus.tools.worker_tools import WorkerOrchestrator, manager_tools, worker_tools


def _make_coordinator_stream_handler(system) :
    if not system.presentation.supports_model_stream():
        return None
    session, generation = system.session_key, system._session.generation
    return system.presentation.TextEventStreamHandler(
        title="Coordinator",
        is_current=lambda: (system.session_key, system._session.generation) == (session, generation),
    )




class AgentSystem:
    """Agent 任务协调系统，管理 Manager/Coordinator 的对话历史与执行流程。"""

    def __init__(
        self,
        *,
        presentation,
        workspace: WorkspaceContext | None = None,
        owner_memory_allowed: bool = True,
        exit_deadline=None,
        input_controller: SessionController | None = None,
    ):
        self.presentation = presentation
        self.last_rejected_input = None
        self.workspace = workspace or WorkspaceContext.from_path(current_workspace())
        logger.activate_log_dir(logger.prepare_log_dir(self.workspace))
        self._owner_memory_allowed = owner_memory_allowed
        self._session = input_controller or SessionController()
        self.registry = AgentRegistry()
        self._shutdown_done = False
        self._shutdown_task: asyncio.Task | None = None
        self._exit_deadline = exit_deadline
        self.last_turn_error: Exception | None = None
        self._cancel_lock = asyncio.Lock()
        self._skills_manager = SkillsManager(workspace=self.workspace)
        self._manager_history = ChatHistory()
        self._current_attachments: list = []
        self._factory = SubagentFactory()
        self._memory = MemoryService(
            workspace=self.workspace,
            owner_memory_allowed=owner_memory_allowed,
            factory=self._factory,
        )
        self.toolkit = BasicToolkit(
            self._skills_manager,
            workspace=self.workspace, show_diff=presentation.show_file_diff,
        )
        self._task_manager = TaskManager(
            persist=self._save_tasks,
            input_source=lambda: (self._session.turn_id, self._session.user_inputs),
        )
        self._planning_lock = asyncio.Lock()
        self._orchestrator = WorkerOrchestrator(
            self.toolkit,
            self._task_manager,
            memory=self._memory,
            memory_injection_getter=lambda: self._memory.injection_for_session(),
            registry=self.registry,
            persist=self._durable_write,
            factory=self._factory,
        )
        self._memory.bind_runner(
            self.registry, input_source=lambda: self._session.user_inputs
        )
        self._session_file = None
        self._context_prewarmed = False
        self._coordinator_agent = None
        self._current_turn: dict[str, Any] | None = None
        self._cli_turn_id: str | None = None
        self.session_key: str | None = None

    async def bind_session(self, session_key: str, *, storage=None, generation=None, task_title=None) -> None:
        previous = self._session_file
        if storage is None:
            storage = previous if previous and previous.session_id == session_key else await self._durable_write(lambda: SessionFile.create(
                session_data_dir(self.workspace), self.workspace.project_id,
                session_id=session_key, workspace=self.workspace,
            ))
        if storage.project_id != self.workspace.project_id or storage.session_id != session_key:
            raise ValueError("会话身份或项目不匹配")
        storage.acquire_use()
        try:
            await self.registry.ensure_agent(session_key, "coordinator")
            await self.registry.ensure_agent(session_key, "manager")
            if generation is not None and (generation != self._session.generation or self._shutdown_done):
                raise ValueError("加载已取消；目标会话未提交")
            if task_title is not None:
                logger.setup_task_logger(task_title)
            if previous is not None and previous is not storage:
                previous.release_use()
        except BaseException:
            if storage is not previous:
                storage.release_use()
            raise
        if previous is not storage:
            self._coordinator_agent = None
            self._memory.unbind_session()
            self._memory.reset_injection_snapshot()
        self.session_key, self._session_file = session_key, storage
        self._memory.bind_session(storage)
        self._orchestrator.session_file = storage
        self._orchestrator.set_session_key(session_key)

    async def end_session_agents(self, session_key: str) -> None:
        await self._orchestrator.factory.cancel_session(session_key)
        self._session.reset(discard=True)
        await self.registry.cancel_session(session_key)
        await self.registry.remove_session(session_key)
        if self.session_key == session_key:
            self._memory.unbind_session()
            self._session_file.release_use()
            self.session_key = None
            self._session_file = None
            self._coordinator_agent = None
            self._memory.reset_injection_snapshot()
            self._orchestrator.set_session_key(None)
            self._orchestrator.session_file = None
            self._session.storage_paused = False

    async def reset_session(self, *, close_memory=False) -> None:
        """Release resources before discarding a conversation's recoverable state."""
        self._session.reset(discard=True)
        self.presentation.update_output("clear_model_stream")
        self._session.queue.discard()
        await self.cancel_current_turn()
        await self._factory.cancel_all()
        await self.toolkit.close()
        if close_memory:
            await self._memory.store.close()
        if self.session_key:
            await self.end_session_agents(self.session_key)
        self._task_manager.reset()
        self.toolkit.reset_task_directory()
        self._manager_history.reset()
        self._session_file = None
        self._memory.reset_injection_snapshot()
        self._memory.unbind_session()
        self._session.storage_paused = False
        self._session.take_notices()
        self._session.queue.discard()



    @property
    def has_current_turn(self) -> bool:
        return (
            self._current_turn is not None
            or self._session.active
            or self._session.queue.current is not None
            or self._cancel_lock.locked()
        )

    @property
    def has_current_goal_turn(self) -> bool:
        return bool(self._current_turn and self._current_turn.get("mode") == "goal")







    async def _compress_context(self, history):
        from redlotus.sessions.context import make_agent_id

        storage, generation = self._session_file, self._session.generation
        if storage is None:
            return ["当前没有可保存的会话，未应用压缩。"]
        sources = {"coordinator": history, "manager": self._manager_history}

        async def persist(candidates):
            for role in reversed(sources):
                candidate = candidates[role] or sources[role]
                if role != "coordinator" and not candidate.messages:
                    continue
                target = storage if role == "coordinator" else storage.role_file(role)
                identity = {} if role == "coordinator" else {
                    "agent_id": make_agent_id(storage.session_id, role, "planning"),
                }
                await self._durable_write(lambda: target.save_context(candidate.messages, turn_id=None, **identity))

        return await compress_histories(
            sources, task_state=self._task_manager.structured_status(), persist=persist,
            is_current=lambda: storage is self._session_file and generation == self._session.generation,
        )

    @property
    def current_goal_iteration(self) -> int:
        if not self.has_current_goal_turn:
            return 0
        return int(self._current_turn.get("goal_iteration") or 0)


    async def shutdown(self) -> None:
        if self._shutdown_task is None:
            self._shutdown_done = True
            if self._exit_deadline is not None:
                self._exit_deadline.start()
            self._shutdown_task = asyncio.create_task(self._release_resources())
        await asyncio.shield(self._shutdown_task)

    async def _release_resources(self) -> None:
        self._session.reset(discard=True)
        self._session.queue.discard()
        self._factory.stop()
        await self.cancel_current_turn()
        if self.session_key:
            await self.end_session_agents(self.session_key)
        await self.registry.cancel_all()
        await self._factory.close()
        await self._memory.close()
        await self.toolkit.close()
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
            else self._session.task or self._session.queue.current
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

    async def stop_current_turn(self) -> str:
        """Stop a user task from any interface and preserve the actual control receipt."""
        active = self.has_current_turn
        result = await self.cancel_current_turn()
        self.record_control_result(
            "stop", "current_turn", "cancelled" if active else "not_running", accepted=active
        )
        return result

    def _start_user_turn(self, message, history, *, goal_mode=False, turn_id=None):
        if self._current_turn or self._session.active or self._cancel_lock.locked() or self._shutdown_done:
            return None
        self.last_turn_error = None
        turn_id = turn_id or uuid.uuid4().hex
        task = asyncio.create_task(
            self._run_user_turn(
                turn_id,
                message,
                history,
                goal_mode=goal_mode,
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


    async def _run_user_turn(
        self, turn_id, message, history, *, goal_mode=False
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
                        set_iteration=set_iteration,
                    )
            else:
                await self.run_agent_system(
                    message,
                    history,
                    turn_id=turn_id,
                )
        except asyncio.CancelledError:
            logger.info("用户回合已取消 turn_id=%s", turn_id)
        except Exception as exc:
            self._handle_turn_error(exc)


    async def wait_for_memory_quiescent(self, timeout: float = 15.0) -> bool:
        async def drain():
            if self._current_turn:
                await asyncio.gather(self._current_turn["task"], return_exceptions=True)
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
        async def recorded_answer(question):
            generation = self._session.generation
            answer = (await handler(question) if inspect.iscoroutinefunction(handler)
                      else await asyncio.to_thread(handler, question))
            if isinstance(answer, str) and generation == self._session.generation:
                await self.record_user_input(uuid.uuid4().hex, UserMessage(answer))
            return answer

        self.toolkit.set_ask_user_handler(recorded_answer if handler else None)

    async def record_user_input(self, identity, message):
        """Persist a consumed input against its bound session, including tool questions."""
        storage = self._session_file
        if storage is not None and self._session.active:
            await self._durable_write(lambda: storage.record_input(identity, message))




    def record_control_result(self, command, target, status, *, accepted):
        from pydantic_ai.messages import TextContent

        from redlotus.runtime.resources import iso_utc_now

        receipt = dict(
            command=command,
            target=target,
            status=status,
            accepted=accepted,
            session_id=self.session_key,
            observed_at=iso_utc_now(),
        )
        encoded = json.dumps(receipt, ensure_ascii=False)
        if self._session_file:
            try:
                self._session_file.update(metadata={"last_control_result": receipt})
            except OSError as exc:
                self.presentation.print_warning(f"控制回执尚未保存: {exc}")
        self._session.add_notice(
            [
                TextContent(
                    encoded, metadata={"origin": "runtime_control"}
                )
            ]
        )
        return receipt

    async def bind_loaded_snapshot(self, path, *, state, title):
        """Validate the complete candidate before replacing the current session."""
        generation = self._session.generation
        storage = SessionFile.load(path, workspace=self.workspace)
        storage.acquire_use()
        try:
            if storage.project_id != self.workspace.project_id:
                raise ValueError("会话不属于当前项目")
            metadata = storage.metadata
            task_name = metadata.get("task_name")
            if task_name is not None and not isinstance(task_name, str):
                raise ValueError("会话 task_name 必须是文本")
            tasks = TaskManager()
            tasks.restore(metadata.get("tasks", []))
            messages = storage.model_messages()
            repaired = repair_interrupted_tool_calls(messages)
            from redlotus.sessions.context import make_agent_id
            for task in tasks.tasks.values():
                task.worker_chat_history.set_messages(repair_interrupted_tool_calls(storage.role_messages(
                    "worker", agent_id=make_agent_id(storage.session_id, "worker", task.id),
                )))
            manager_messages = repair_interrupted_tool_calls(storage.role_messages(
                "manager", agent_id=make_agent_id(storage.session_id, "manager", "planning"),
            ))
            history, manager_history = ChatHistory(), ChatHistory()
            history.set_messages(repaired)
            manager_history.set_messages(manager_messages)
            active = metadata.get("active_turn")
            if active or repaired != messages or tasks.snapshot() != metadata.get("tasks", []):
                await finish_file_io(asyncio.to_thread(lambda: storage.save_context(
                    repaired, turn_id=(active.get("turn_id") or active.get("id")) if active else None,
                    metadata={"active_turn": None, "interrupted_turn": dict(active, status="interrupted") if active else None,
                              "tasks": tasks.snapshot()},
                )))
            if generation != self._session.generation or self._shutdown_done:
                raise ValueError("加载已取消；目标会话未提交")
            previous_key = self.session_key
            await self.bind_session(storage.session_id, storage=storage, generation=generation, task_title=title)
            self._session.reset(discard=True)
            self._session.queue.discard()
            self.presentation.update_output("clear_model_stream")
            state.history, state.is_first_input = history, False
            self._manager_history = manager_history
            self._task_manager.tasks = tasks.tasks
            self.toolkit.reset_task_directory()
            if task_name:
                self.toolkit.set_task_directory(task_name)
            self._current_attachments = []
            self._session.storage_paused = False
            async def release_previous():
                results = await asyncio.gather(
                    self._factory.cancel_all(), self.toolkit.close(), return_exceptions=True,
                )
                if previous_key and previous_key != storage.session_id:
                    try:
                        await self.registry.cancel_session(previous_key)
                        await self.registry.remove_session(previous_key)
                    except Exception as exc:
                        results.append(exc)
                return results

            cleanup = asyncio.create_task(release_previous())
            try:
                results = await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                results = await cleanup
            for result in results:
                if isinstance(result, BaseException):
                    self.presentation.print_warning(f"会话已加载，但旧资源释放未完成: {result}")
            return repaired
        finally:
            if self._session_file is not storage:
                storage.release_use()


    def add_urgent(self, text: str) -> bool:
        if not self._session.accepting_urgent:
            return False
        self._session.add_notice(text)
        return True

    async def add_urgent_message(
        self, message: UserMessage, *, admission=None, references=None
    ) -> bool:
        admission = admission or self._session.admit(self.workspace, urgent=True)
        if not admission.urgent or not self._session.accepts(admission):
            return False
        store = self.toolkit._references

        async def prepare():
            try:
                if references is not None:
                    message.references = await references
                if not self._session.accepts(admission):
                    return None
                await store.prepare_message(message)
                return message if self._session.accepts(admission) else None
            except (OSError, ValueError) as exc:
                if self._session.accepts(admission):
                    self.last_rejected_input = (
                        message.original_text or message.text
                    )
                    self.presentation.print_warning(str(exc))
                return None

        self._session.queue_urgent(admission, prepare())
        logger.info_file_only(
            "input admitted id=%s sequence=%s turn=%s urgent=True",
            admission.id,
            admission.sequence,
            admission.turn_id,
        )
        return True

    async def _take_inner_inputs(self):
        prompts = []
        for admission, message in await self._session.take_urgent():
            await self.record_user_input(admission.id, message)
            self._session.user_inputs.append(message.original_text or message.text)
            prompts.append(message.to_prompt())
            logger.debug(
                "input consumed id=%s sequence=%s turn=%s",
                admission.id,
                admission.sequence,
                admission.turn_id,
            )
            if self._memory.current is not None:
                event = self._memory.current
                event.reference_ids = list(
                    dict.fromkeys(
                        [*event.reference_ids, *(ref.id for ref in message.references)]
                    )
                )
                await self._durable_write(lambda: self._memory.observations.save(event))
            self._current_attachments.extend(message.attachments)
            self._current_attachments.extend(
                part for ref in message.references for part in ref.to_prompt()
            )
        return [
            *prompts,
            *self._session.take_notices(),
            *self._memory.take_context_notices(),
        ]

    async def switch_workspace(self, path) -> None:
        """Prepare the target before releasing or replacing the current conversation."""
        workspace = WorkspaceContext.from_path(path)
        if not workspace.root.is_dir():
            raise NotADirectoryError(workspace.root)
        log_dir = logger.prepare_log_dir(workspace)
        skills = SkillsManager(workspace=workspace)
        memory = MemoryService(
            workspace=workspace,
            owner_memory_allowed=self._owner_memory_allowed,
            factory=self._factory,
        )
        memory.bind_runner(
            self.registry, input_source=lambda: self._session.user_inputs
        )
        toolkit = BasicToolkit(
            skills, workspace=workspace, show_diff=self.presentation.show_file_diff,
        )
        toolkit.set_ask_user_handler(self.toolkit._ask_user_handler)
        review_store = self.toolkit.review_store
        toolkit._review_store, toolkit._file_lock = review_store, review_store._lock
        await self.reset_session(close_memory=True)
        self.workspace, self._memory, self.toolkit, self._skills_manager = workspace, memory, toolkit, skills
        self._coordinator_agent = None
        from redlotus.runtime.resources import set_workspace

        set_workspace(workspace.root)
        logger.activate_log_dir(log_dir)
        review_store.clear()
        self._orchestrator._toolkit = toolkit
        self._orchestrator.memory = memory

    @asynccontextmanager
    async def _outer_turn(self, message: UserMessage, turn_id: str):
        async with self._session.turn(
            message.original_text
            if message.original_text is not None
            else message.text,
            turn_id=turn_id,
        ):
            with workspace_context(self.workspace):
                await self.toolkit._references.prepare_message(message)
                if self.session_key is None:
                    await self.bind_session(uuid.uuid4().hex)
                await self.record_user_input(turn_id, message)
                if not self._session_file.metadata.get("title"):
                    await self._durable_write(lambda: self._session_file.update(metadata={"title": message.text or "session"}))
                job = await self._durable_write(lambda: self._memory.begin_turn(
                    self.session_key,
                    turn_id,
                    self._session.user_inputs[0],
                    references=message.references,
                ))
                status, error = "success", ""
                self._cli_turn_id = turn_id
                self._current_attachments = [
                    *message.attachments,
                    *(part for ref in message.references for part in ref.to_prompt()),
                ]
                try:
                    await self._sync_skills_for_user_turn()
                    with self._session.usage(self._session_file):
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
                    paths = [str(self._session_file.path)]
                    def finish():
                        return self._memory.finish_turn(
                            job, status=status, user_inputs=self._session.user_inputs,
                            evidence_paths=paths, error=error,
                        )
                    if status == "cancelled":
                        try:
                            await finish()
                        except OSError as exc:
                            self.presentation.print_warning(f"取消状态尚未保存，已提交进度保留: {exc}")
                    else:
                        await self._durable_write(finish)
                    if not self._shutdown_done:
                        self._memory.schedule_processing()

    async def execute_task_with_manager(
        self, user_input: str, continue_from_previous: bool = False
    ) -> str:
        """Delegate a multi-step goal to Manager planning and dependent Worker tasks.

        Args:
            user_input: The goal, constraints and evidence that the plan must preserve.
            continue_from_previous: Continue the existing plan and keep completed results.

        Returns:
            The final report or an explicit incomplete-plan error with task results.
        """
        async with self._planning_lock:
            return await self._task_manager.execute_plan(
                user_input, continue_from_previous, orchestrator=self._orchestrator,
                history=self._manager_history, tools=manager_tools(self._task_manager, self.toolkit, self._memory),
                attachments=self._current_attachments, turn_id=self._cli_turn_id, presentation=self.presentation,
            )


    async def resume_task(self, task_id: str) -> str:
        """Resume a blocked planned task by ID after the user supplies new input.

        Completed tasks keep their results. Cancelled or unverified work requires
        the user's explicit retry instruction after checking possible side effects.
        The task receives admitted user input, not model-generated replacement text.

        Args:
            task_id: The existing blocked task ID shown in the current plan.
        """
        async with self._planning_lock:
            result = await self._task_manager.resume(task_id)
            if result.startswith("Error:"):
                return result
            from redlotus.sessions.context import make_agent_id
            task = self._task_manager.tasks[task_id]
            task.worker_chat_history.set_messages(repair_interrupted_tool_calls(
                await asyncio.to_thread(self._session_file.role_messages, "worker",
                                        agent_id=make_agent_id(self.session_key, "worker", task_id)),
            ))
            return await self._orchestrator.execute_all_tasks_parallel(
                "\n".join(self._session.user_inputs), self._current_attachments, turn_id=self._cli_turn_id,
            )

    async def execute_task_with_worker(
        self, task_description: str, user_goal: str = "", retry_info: str = ""
    ) -> Tuple[bool, str]:
        """Delegate one bounded execution task to a factory-managed Worker.

        Args:
            task_description: The task, allowed scope and required deliverables.
            user_goal: The parent goal and constraints needed to interpret the task.
            retry_info: Earlier failure evidence relevant to this attempt.

        Returns:
            A success flag and the structured Worker result with status and evidence.
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

            self._session.open_inbox()

            logger.info_file_only("[用户]\n%s", self.presentation.format_user_log_text(message))

            routing_tools = [
                self.execute_task_with_manager,
                self.execute_task_with_worker,
                self.resume_task,
            ]
            if self._coordinator_agent is None:
                from redlotus.prompts.prompt import (
                    memory_from_session_prompt,
                    session_prompt_from_history,
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
                    worker_tools(self.toolkit, self._memory)
                    if self._owner_memory_allowed
                    else [],
                    self._task_manager.structured_status,
                    instructions=restored,
                    persist_context=self._commit_request_context,
                )
            agent = self._coordinator_agent

            start_time = time.time()
            coord_aid = await self.registry.ensure_agent(
                self.session_key, "coordinator"
            )
            stream_handler = (
                None
                if output_transform is not None
                else _make_coordinator_stream_handler(self)
            )
            async def _save_coordinator_node(run: Any) -> None:
                candidate = ChatHistory()
                candidate.set_messages(list(run.all_messages()))
                await self._checkpoint(candidate, turn_id)
                history.set_messages(candidate.messages)

            try:
                result = await self.registry.run(
                    lambda: AgentRunner().run(
                        agent=agent,
                        prompt=message.to_prompt(),
                        message_history=messages_safe_for_new_prompt(history.messages),
                        usage_limits=get_agent_usage_limits(),
                        event_stream_handler=stream_handler,
                        on_node=_save_coordinator_node,
                        take_urgent=self._take_inner_inputs,
                        on_complete=self._session.close_inbox,
                    ),
                    agent_id=coord_aid,
                    turn_id=turn_id,
                )
            except BaseException as exc:
                self._session.close_inbox()
                if stream_handler is not None and stream_handler._is_current():
                    self.presentation.update_output("end_model_stream", "已停止" if isinstance(exc, asyncio.CancelledError) else "执行失败")
                raise
            if stream_handler is not None and not stream_handler._is_current():
                raise asyncio.CancelledError("The reply belongs to a previous session.")
            raw_output = str(result.output or "")
            output = (
                output_transform(raw_output)
                if output_transform is not None
                else raw_output
            )
            if stream_handler is not None:
                self.presentation.finish_model_stream(output, title="Coordinator")
            else:
                self.presentation.show_model_output(output, title="Coordinator")
            history.update(result)
            elapsed = time.time() - start_time

            logger.debug("run_agent_system 完成，耗时 %.2f 秒", elapsed)
            await self._checkpoint(history, turn_id)
            worker_storage = self._session_file.role_file("worker", create=False)
            if worker_storage is not None:
                from redlotus.sessions.context import make_agent_id
                retained = {
                    make_agent_id(self.session_key, "worker", task.id)
                    for task in self._task_manager.tasks.values() if task.status != TaskStatus.COMPLETED
                }
                await self._durable_write(lambda: worker_storage.compact(
                    keep_turn_ids=set(), release_turn_id=turn_id, keep_agent_ids=retained,
                ))
            return history, output

    async def _commit_request_context(self, messages):
        """Commit an automatic checkpoint before the SDK can send or adopt it."""
        storage = self._session_file
        session_id = self.session_key
        candidate = ChatHistory()
        candidate.set_messages(messages)
        await self._checkpoint(candidate, self._cli_turn_id)
        if storage is not self._session_file or session_id != self.session_key:
            raise asyncio.CancelledError("Session changed during checkpoint persistence.")

    async def _save_tasks(self, tasks):
        storage = self._session_file
        await self._durable_write(lambda: storage.update(metadata={"tasks": tasks}))

    async def _checkpoint(self, history, turn_id):
        """Append model context; the task state machine owns its checkpoints."""
        storage, messages = self._session_file, list(history.messages)
        await self._durable_write(lambda: storage.save_context(
            messages,
            turn_id=turn_id,
        ))




    async def _durable_write(self, operation, *, cancelling=False):
        with workspace_context(self.workspace):
            return await self._session.write(operation, storage=self._session_file, cancelling=cancelling)

    async def compress_context(self, history):
        with self._session.usage(self._session_file):
            return await self._session.compress(lambda: self._compress_context(history), busy=self.has_current_turn)

    def _handle_turn_error(self, error):
        self.last_turn_error = error
        self.presentation.handle_turn_error(error)
