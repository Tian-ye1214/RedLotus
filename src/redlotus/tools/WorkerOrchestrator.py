from __future__ import annotations

import asyncio
import copy
import functools
import inspect
import uuid
from typing import Callable

from redlotus.agent_core.runner import AgentRunner
from redlotus.config.app_config import (
    get_agent_run_policy,
    get_agent_usage_limits,
)
from redlotus.ModelGateway.agent_factory import (
    create_agent,
    create_function_toolset,
    create_worker_toolsets_and_capabilities,
)
from redlotus.ModelGateway.model_factory import ModelTarget
from redlotus.prompt import with_runtime_context
from redlotus.prompt import get_worker_system_prompt, get_manager_system_prompt
from redlotus.runtime.lifecycle import AgentRegistry
from redlotus.runtime.subagents import SubagentFactory, SubagentSpec
from redlotus.runtime.worker_result import SubagentResult
from redlotus.tools.ManagementTools import Task, TaskManager, TaskStatus
from redlotus.tools.conversation_log import ConversationLog
from redlotus.tools.memory import ChatHistory
from redlotus.tools.memory.chat_history import messages_safe_for_new_prompt


class WorkerOrchestrator:
    """Plan dependencies on the parent loop; execute every child through one factory."""

    def __init__(
        self,
        toolkit,
        task_manager: TaskManager,
        *,
        memory_injection_getter: Callable[[], str] | None = None,
        registry: AgentRegistry,
    ):
        self._toolkit = toolkit
        self._task_manager = task_manager
        self._memory_injection_getter = memory_injection_getter or (lambda: "")
        self._registry = registry
        self.factory = SubagentFactory(get_agent_run_policy().max_worker_concurrent)
        self._session_key: str | None = None
        self._conversation_date: str | None = None
        self._conversation_topic: str | None = None
        self.evidence: dict[str, list[str]] = {}

    def set_session_key(self, session_key: str | None) -> None:
        self._session_key = session_key

    def set_conversation_session(self, date: str, topic: str) -> None:
        self._conversation_date, self._conversation_topic = date, topic

    def clear_conversation_session(self) -> None:
        self._conversation_date = self._conversation_topic = None
        self.evidence.clear()

    async def _execute(
        self,
        prompt,
        history: ChatHistory,
        *,
        turn_id: str | None,
        task_id: str,
        role: str = "worker",
        planning_tools: tuple = (),
    ):
        if self._session_key is None:
            raise RuntimeError("Worker requires a bound session")
        owner_loop = asyncio.get_running_loop()
        target = ModelTarget.for_role(role)
        # Snapshot before starting the thread: no mutable messages or clients cross loops.
        messages = copy.deepcopy(messages_safe_for_new_prompt(history.messages))
        memory = self._memory_injection_getter()
        date, topic = self._conversation_date, self._conversation_topic
        spec = SubagentSpec(
            self._session_key, turn_id, self._toolkit.workspace, role=role
        )
        log = ConversationLog(
            role,
            date,
            topic,
            sub_id=f"{task_id}-{uuid.uuid4().hex[:8]}",
            workspace=spec.workspace,
        )
        extra = {
            "kind": role,
            "origin": "subagent_instruction",
            "turn_id": turn_id,
            "task_id": task_id,
        }
        await log.save(
            messages, extra={**extra, "turn_id": None, "origin": "restored_context"}
        )
        path = log.model_messages_path()
        if path is not None:
            self.evidence.setdefault(turn_id or "", []).append(str(path))

        async def execute_child():
            toolkit = self._toolkit.clone_for_worker(owner_loop)
            local_history = ChatHistory()
            local_history.set_messages(messages)
            try:
                if role == "worker":
                    toolsets, capabilities = create_worker_toolsets_and_capabilities(
                        toolkit.worker_tool_groups(include_browser=True)
                    )
                    instructions = get_worker_system_prompt(
                        toolkit.skills_manager, memory
                    )
                    output_type = SubagentResult
                else:

                    def bridge(fn):
                        @functools.wraps(fn)
                        async def call(*args, **kwargs):
                            async def invoke():
                                value = fn(*args, **kwargs)
                                return (
                                    await value if inspect.isawaitable(value) else value
                                )

                            return await asyncio.wrap_future(
                                asyncio.run_coroutine_threadsafe(invoke(), owner_loop)
                            )

                        return call

                    toolsets = [
                        create_function_toolset(
                            [bridge(t) for t in planning_tools], toolset_id="planning"
                        )
                    ]
                    capabilities = []
                    instructions = get_manager_system_prompt(
                        toolkit.skills_manager, memory
                    )
                    output_type = str
                agent = create_agent(
                    target,
                    instructions=instructions,
                    toolsets=toolsets,
                    capabilities=capabilities,
                    output_type=output_type,
                    role=role,
                )

                async def save_node(run):
                    local_history.set_messages(list(run.all_messages()))
                    await log.save(local_history.messages, extra=extra)

                result = await AgentRunner().run(
                    agent=agent,
                    prompt=with_runtime_context(copy.deepcopy(prompt)),
                    message_history=local_history.messages,
                    usage_limits=get_agent_usage_limits(),
                    on_node=save_node,
                )
                return result.output, list(result.all_messages())
            finally:
                await log.save(local_history.messages, extra=extra)
                await toolkit.close()

        async def run_child():
            return await self.factory.run(spec, execute_child)

        try:
            agent_id = await self._registry.ensure_agent(
                self._session_key, role, task_id
            )
            report, returned_messages = await self._registry.run(
                run_child, agent_id=agent_id, turn_id=turn_id
            )
            history.set_messages(returned_messages)
            return report
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if role != "worker":
                raise
            return SubagentResult(
                status="failed", summary=f"{type(exc).__name__}: {exc}"
            )

    async def plan(
        self, prompt, history: ChatHistory, *, turn_id: str | None, tools: tuple = ()
    ) -> str:
        return await self._execute(
            prompt,
            history,
            turn_id=turn_id,
            task_id="planning",
            role="manager",
            planning_tools=tools,
        )

    async def execute_task_with_worker(
        self,
        task_description: str,
        user_goal: str = "",
        retry_info: str = "",
        attachments: list | None = None,
        *,
        turn_id: str | None,
    ) -> tuple[bool, str]:
        prompt = f"[Delegated task, not a new user preference]\nGoal: {user_goal}\nTask: {task_description}"
        if retry_info:
            prompt += f"\nPrevious failure: {retry_info}"
        content = [prompt, *attachments] if attachments else prompt
        report = await self._execute(
            content, ChatHistory(), turn_id=turn_id, task_id=uuid.uuid4().hex[:8]
        )
        return report.success, report.model_dump_json()

    async def _execute_task(
        self, task: Task, user_goal: str, attachments: list | None, turn_id: str | None
    ):
        task.status = TaskStatus.IN_PROGRESS
        dependencies = "\n".join(
            self._task_manager.tasks[d].result for d in task.dependencies
        )
        prompt = f"[Delegated task]\nGoal: {user_goal}\nTask: {task.description}\nDependencies:\n{dependencies}"
        if task.failure_history:
            prompt += "\nPrevious failures:\n" + "\n".join(task.failure_history)
        content = [prompt, *attachments] if attachments else prompt
        try:
            report = await self._execute(
                content, task.worker_chat_history, turn_id=turn_id, task_id=task.id
            )
            task.artifacts = list(dict.fromkeys([*task.artifacts, *report.artifacts]))
            task.tool_summaries.extend(report.risks)
            if report.status == "needs_input" or report.needs_user_confirmation:
                task.status = TaskStatus.PENDING_CONFIRMATION
                task.result = report.model_dump_json()
            elif report.success:
                self._task_manager.mark_task_complete(task.id, report.model_dump_json())
            else:
                self._task_manager.mark_task_failed(task.id, report.model_dump_json())
        except asyncio.CancelledError:
            task.status = TaskStatus.FAILED
            task.failure_history.append(
                "Cancelled by the owner; completion is unverified."
            )
            raise

    async def execute_all_tasks_parallel(
        self, user_goal: str, attachments: list | None = None, *, turn_id: str | None
    ) -> str:
        # The factory is the sole concurrency limit; TaskGroup owns cancellation of the batch.
        while ready := self._task_manager.get_all_ready_tasks():
            async with asyncio.TaskGroup() as group:
                for task in ready:
                    group.create_task(
                        self._execute_task(task, user_goal, attachments, turn_id)
                    )
        return self._task_manager.get_final_summary()
