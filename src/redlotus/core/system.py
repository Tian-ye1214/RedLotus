"""Core system responsibilities."""

from __future__ import annotations

import asyncio
import time
import traceback
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager, nullcontext
from typing import Any, Tuple

from pydantic_ai.exceptions import ModelHTTPError

import redlotus.runtime.resources as _runtime_resources
from redlotus.core.agents import AgentRegistry, SubagentFactory
from redlotus.core.goals import run_goal_loop
from redlotus.core.session import AgentSession
from redlotus.core.tasks import SessionController, TaskManager
from redlotus.documents.interaction import UserMessage, format_user_log_text
from redlotus.memory.service import MemoryService
from redlotus.models.context import (
    ChatHistory,
    messages_safe_for_new_prompt,
    prewarm_effective_max_contexts_by_role_async,
)
from redlotus.models.gateway import AgentRunner, create_coordinator_agent
from redlotus.prompts.prompt import load_prompt
from redlotus.runtime.config import get_agent_usage_limits, validate_runtime_configuration
from redlotus.runtime.context import (
    EventEmitter,
    WorkspaceContext,
    current_workspace,
    workspace_context,
)
from redlotus.tools.base_tools import BasicToolkit
from redlotus.tools.registry import SkillsManager
from redlotus.tools.worker_tools import WorkerOrchestrator, manager_tools, worker_tools


def _make_coordinator_stream_handler(system):
    if not system.events.emit("supports_model_stream"):
        return None
    session, generation = system.session_key, system._session.generation
    return system.events.emit("TextEventStreamHandler",
        title="Coordinator",
        is_current=lambda: (system.session_key, system._session.generation) == (session, generation),
    )


def _prompt_for_role(text: str, attachments: list):
    return [text, *attachments] if attachments else text


class AgentSystem(AgentSession):
    """Agent 任务协调系统，管理 Manager/Coordinator 的对话历史与执行流程。"""

    def __init__(
        self,
        *,
        workspace: WorkspaceContext | None = None,
        owner_memory_allowed: bool = True,
        exit_deadline=None,
        session_controller: SessionController | None = None,
        events=None,
    ):
        validate_runtime_configuration()
        self.events = events or EventEmitter()
        self.workspace = workspace or WorkspaceContext.from_path(current_workspace())
        _runtime_resources.activate_log_dir(_runtime_resources.prepare_log_dir(self.workspace))
        self._owner_memory_allowed = owner_memory_allowed
        self._session = session_controller or SessionController()
        self._registry = AgentRegistry()
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
            registry_factory=AgentRegistry,
        )
        self._toolkit = BasicToolkit(
            self._skills_manager,
            workspace=self.workspace,
            events=self.events,
        )
        self._task_save_lock = asyncio.Lock()
        self._last_user_input = (None, "")
        self._task_manager = TaskManager(checkpoint=self._save_task_plan, user_input=lambda: self._last_user_input)
        self._planning_lock = asyncio.Lock()
        self._orchestrator = WorkerOrchestrator(
            self._toolkit,
            self._task_manager,
            memory=self._memory,
            memory_injection_getter=lambda: self._memory.injection_for_session(),
            registry=self._registry,
            persist=self._durable_write,
            factory=self._factory,
        )
        self._memory.bind_runner(
            self._registry, input_source=lambda: self._session.user_inputs
        )
        self._session_file = None
        self._context_prewarmed = False
        self._coordinator_agent = None
        self._current_turn: dict[str, Any] | None = None
        self._cli_turn_id: str | None = None
        self._session_key: str | None = None
        self._compression_future = None
        self._storage_retry = asyncio.Event()
        self._storage_paused = False

    @property
    def current_goal_iteration(self) -> int:
        if not self.has_current_goal_turn:
            return 0
        return int(self._current_turn.get("goal_iteration") or 0)


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
        from redlotus.models.providers import InputLimitError

        if isinstance(e, InputLimitError):
            self.events.emit("print_warning", str(e))
            return
        if isinstance(e, ModelHTTPError):
            body = e.body or {}
            code = body.get("code", "") if isinstance(body, dict) else ""
            if code == "data_inspection_failed":
                self.events.emit("print_warning",
                    "模型内容安全审查拦截：您的输入或上下文中包含被判定为不当的内容。"
                    "请尝试换一种表达方式，或 /clear 清空上下文后重试。"
                )
            else:
                message = f"模型请求错误 (HTTP {e.status_code}): {e}"
                if e.status_code in (401, 403):
                    message += "\n请检查实际生效的 API 凭据，或使用 /api 配置后重试。"
                self.events.emit("print_warning", message)
                _runtime_resources.error("详细信息:\n%s", traceback.format_exc(), file_only=True)
            return
        self.events.emit("print_warning", f"未预期的系统错误: {e}")
        _runtime_resources.error("详细信息:\n%s", traceback.format_exc())

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
            _runtime_resources.info("用户回合已取消 turn_id=%s", turn_id)
        except Exception as exc:
            self._handle_turn_error(exc)

    async def ask_user(self, question: str) -> str:
        return await self._toolkit.ask_user(question)

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

    @asynccontextmanager
    async def _outer_turn(self, message: UserMessage, turn_id: str):
        async with self._session.turn(
            message.original_text
            if message.original_text is not None
            else message.text,
            turn_id=turn_id,
        ):
            with workspace_context(self.workspace):
                await self._toolkit._references.prepare_message(message)
                if self._session_key is None:
                    await self.bind_session(uuid.uuid4().hex)
                await self.record_user_input(turn_id, message)
                if not self._session_file.metadata.get("title"):
                    await self._durable_write(lambda: self._session_file.update(metadata={"title": message.text or "session"}))
                job = await self._durable_write(lambda: self._memory.begin_turn(
                    self._session_key,
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
                    with self._usage_scope():
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
                            self.events.emit("print_warning", f"取消状态尚未保存，已提交进度保留: {exc}")
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
            return await self._execute_plan(user_input, continue_from_previous)

    async def _execute_plan(self, user_input: str, continue_from_previous: bool) -> str:
        _runtime_resources.info("[用户]\n%s", user_input)
        tid = self._cli_turn_id

        planning_tools = manager_tools(self._task_manager, self._toolkit, self._memory)
        attachments = self._current_attachments

        if not continue_from_previous:
            self._task_manager.reset()
            self._manager_history.reset()
            self._orchestrator.plan_id = uuid.uuid4().hex
            await self._save_task_plan()
            self.events.emit("print_phase", "第一阶段: Manager 规划任务列表")
            tmpl = await asyncio.to_thread(load_prompt, "manager_planning_new.md")
            planning_text = tmpl.format(user_input=user_input)
        else:
            self.events.emit("print_phase", "第一阶段: 基于用户反馈调整任务")
            current_todo = self._task_manager.get_todo_list()
            tmpl = await asyncio.to_thread(load_prompt, "manager_planning_continue.md")
            planning_text = tmpl.format(
                user_input=user_input, current_todo=current_todo
            )

        planning_prompt = _prompt_for_role(planning_text, attachments)
        result = await self._orchestrator.plan(
            planning_prompt, self._manager_history, turn_id=tid, tools=planning_tools
        )
        self.events.emit("show_model_output", result, title="Manager 规划")

        self.events.emit("print_phase", "第二阶段: 多Worker并行执行任务")

        final_summary = await self._orchestrator.execute_all_tasks_parallel(
            user_input, attachments=attachments, turn_id=tid
        )
        self.events.emit("show_model_output", final_summary, title="任务汇总", markdown=False)

        self.events.emit("print_phase", "第三阶段: 生成最终报告")

        summary_tmpl = await asyncio.to_thread(load_prompt, "manager_summary.md")
        summary_text = summary_tmpl.format(
            user_input=user_input, final_summary=final_summary
        )
        summary_prompt = _prompt_for_role(summary_text, attachments)
        try:
            final_text = await self._orchestrator.plan(
                summary_prompt, self._manager_history, turn_id=tid
            )
            self.events.emit("show_model_output", final_text, title="最终报告")
            report = final_text.strip() or final_summary.strip()
        except Exception as exc:
            _runtime_resources.warning("Manager summary unavailable: %s", exc)
            report = final_summary
        return (
            report
            if self._task_manager.completed
            else "Error: Plan is incomplete.\n" + report
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

            self._session.open_inbox()

            _runtime_resources.info_file_only("[用户]\n%s", format_user_log_text(message))

            routing_tools = [
                self.execute_task_with_manager,
                self.execute_task_with_worker,
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
                from redlotus.prompts.prompt import get_coordinator_system_prompt
                restored = await self._durable_write(lambda: self._session_file.prompt_snapshot(
                    "coordinator", lambda: restored or get_coordinator_system_prompt(
                        self._skills_manager, self._memory.injection_for_session(),
                    ),
                ))
                self._coordinator_agent = await create_coordinator_agent(
                    self._skills_manager,
                    self._memory.injection_for_session(),
                    routing_tools if self._owner_memory_allowed else [],
                    worker_tools(self._toolkit, self._memory)
                    if self._owner_memory_allowed
                    else [],
                    self.structured_task_status,
                    instructions=restored,
                    persist_context=self._commit_request_context,
                )
            agent = self._coordinator_agent

            start_time = time.time()
            coord_aid = await self._registry.ensure_agent(
                self._session_key, "coordinator"
            )
            stream_handler = (
                None
                if output_transform is not None
                else _make_coordinator_stream_handler(self)
            )
            extra = {
                **(conversation_log_extra or {}),
                "kind": "coordinator",
                "turn_id": turn_id,
                "origin": "goal_instruction" if _inside_goal else "user",
            }

            async def _save_coordinator_node(run: Any) -> None:
                candidate = ChatHistory()
                candidate.set_messages(list(run.all_messages()))
                await self._checkpoint(candidate, turn_id)
                history.set_messages(candidate.messages)

            try:
                result = await self._registry.run(
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
                    self.events.emit("end_model_stream", "已停止" if isinstance(exc, asyncio.CancelledError) else "执行失败")
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
                self.events.emit("finish_model_stream", output, title="Coordinator")
            else:
                self.events.emit("show_model_output", output, title="Coordinator")
            history.update(result)
            elapsed = time.time() - start_time

            _runtime_resources.debug("run_agent_system 完成，耗时 %.2f 秒", elapsed)
            await self._checkpoint(history, turn_id)
            worker_storage = self._session_file.role_file("worker", create=False)
            if worker_storage is not None and (not self._task_manager.tasks or self._task_manager.completed):
                await self._durable_write(lambda: worker_storage.compact(
                    keep_turn_ids=set(), release_turn_id=turn_id,
                ))
            return history, output
