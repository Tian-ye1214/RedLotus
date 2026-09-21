"""Worker execution tools, deferred loading and factory-managed delegation."""

from __future__ import annotations

import asyncio
import copy
import inspect
import uuid
from typing import Callable
from pydantic_ai.capabilities import Capability
from redlotus.core.agents import (
    AgentRegistry, SubagentFactory, SubagentResult, SubagentSpec, bind_to_loop,
)
from redlotus.core.config import get_agent_usage_limits
from redlotus.core.gateway import AgentRunner, ModelTarget, create_agent, create_function_toolset
from redlotus.core.history import ChatHistory, messages_safe_for_new_prompt
from redlotus.prompts.prompt import (
    get_manager_system_prompt, get_worker_system_prompt, with_runtime_context,
)
from redlotus.tools.manager_tools import Task, TaskManager, TaskStatus


def worker_tool_groups(toolkit, memory, *, owner_loop=None, include_browser=True) -> dict[str, list]:
    """List every Worker tool, preserving resident reads and deferred execution groups."""
    skills, browser = toolkit.skills_manager, toolkit._browser_session
    memory_tools = (
        [memory.reader.search_memory, memory.remember, memory.update_memory, memory.delete_memory]
        if memory.owner_memory_allowed else []
    )
    if owner_loop is not None:
        memory_tools = [bind_to_loop(tool, owner_loop) for tool in memory_tools]
    return {
        "core": [
            toolkit.list_files,
            toolkit.read_file,
            toolkit.search_in_files,
            toolkit.search_web,
            toolkit.ask_user,
            toolkit._references.read_reference,
        ],
        "file_mutation": [toolkit.write_file, toolkit.edit_file],
        "execution": [toolkit.run_command, toolkit.execution_environment],
        "media": [toolkit.generate_image, toolkit.extract_text],
        "memory": memory_tools,
        "skills": [
            skills.list_available_skills,
            skills.get_skill_instructions,
            skills.load_skill_resource,
            skills.refresh_skills,
            skills.execute_skill_script,
        ],
        "browser": [
            browser.browser_navigate,
            browser.browser_get_content,
            browser.browser_screenshot,
            browser.browser_click,
            browser.browser_fill,
            browser.browser_press_key,
            browser.browser_wait_for_selector,
            browser.browser_evaluate,
            browser.browser_close,
        ] if include_browser else [],
    }


def worker_tools(toolkit, memory) -> list:
    """Reuse the complete execution set when the Coordinator executes a task directly."""
    return [tool for tools in worker_tool_groups(toolkit, memory, include_browser=True).values() for tool in tools]


def create_worker_toolsets(toolkit, memory, owner_loop, *, include_browser):
    """Create native SDK capabilities from explicit callables and their own docstrings."""
    resident, capabilities = [], []
    groups = worker_tool_groups(toolkit, memory, owner_loop=owner_loop, include_browser=include_browser)
    for group, tools in groups.items():
        if not tools:
            continue
        identity = "worker_" + group
        deferred = group != "core"
        toolset = create_function_toolset(tools, toolset_id=identity, defer_loading=deferred)
        if deferred:
            capabilities.append(Capability(
                id=identity,
                description="\n\n".join(f"{tool.__name__}: {inspect.getdoc(tool)}" for tool in tools),
                toolsets=[toolset],
                defer_loading=True,
            ))
        else:
            resident.append(toolset)
    return resident, capabilities


class WorkerOrchestrator:
    """Plan dependencies on the parent loop; execute every child through one factory."""

    def __init__(
        self,
        toolkit,
        task_manager: TaskManager,
        *,
        memory,
        memory_injection_getter: Callable[[], str] | None = None,
        registry: AgentRegistry,
        persist,
        factory: SubagentFactory | None = None,
    ):
        self._toolkit = toolkit
        self.memory = memory
        self._task_manager = task_manager
        self._memory_injection_getter = memory_injection_getter or (lambda: "")
        self._registry = registry
        self._persist = persist
        self.factory = factory or SubagentFactory()
        self.session_file = None
        self._session_key: str | None = None

    def set_session_key(self, session_key: str | None) -> None:
        self._session_key = session_key

    async def _execute(
        self,
        prompt,
        history: ChatHistory,
        *,
        turn_id: str | None,
        task_id: str,
        role: str = "worker",
        planning_tools: tuple = (),
        include_browser: bool = False,
    ):
        if self._session_key is None:
            raise RuntimeError("Worker requires a bound session")
        owner_loop = asyncio.get_running_loop()
        persist = bind_to_loop(self._persist, owner_loop)
        session_key, session_file = self._session_key, self.session_file
        source_toolkit, memory_service = self._toolkit, self.memory
        target = ModelTarget.for_role(role)
        # Snapshot before starting the thread: no mutable messages or clients cross loops.
        messages = copy.deepcopy(messages_safe_for_new_prompt(history.messages))
        memory = self._memory_injection_getter()
        spec = SubagentSpec(
            session_key, turn_id, source_toolkit.workspace, role=role
        )
        invocation = uuid.uuid4().hex

        async def execute_child():
            toolkit = source_toolkit.clone_for_worker(owner_loop)
            local_history = ChatHistory()
            local_history.set_messages(messages)
            try:
                if role == "worker":
                    toolsets, capabilities = create_worker_toolsets(
                        toolkit, memory_service, owner_loop, include_browser=include_browser
                    )
                    instructions = get_worker_system_prompt(
                        toolkit.skills_manager, memory
                    )
                    output_type = SubagentResult
                else:
                    toolsets = [
                        create_function_toolset(
                            [bind_to_loop(t, owner_loop) for t in planning_tools], toolset_id="planning"
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
                    if session_file:
                        await persist(lambda: session_file.role_file(role).save_context(
                            local_history.messages, turn_id=turn_id,
                            agent_id=agent_id, invocation=invocation,
                        ))

                result = await AgentRunner().run(
                    agent=agent,
                    prompt=with_runtime_context(copy.deepcopy(prompt)),
                    message_history=local_history.messages,
                    usage_limits=get_agent_usage_limits(),
                    on_node=save_node,
                )
                return result.output, list(result.all_messages())
            finally:
                await toolkit.close()

        async def run_child():
            return await self.factory.run(spec, execute_child)

        try:
            agent_id = await self._registry.ensure_agent(
                session_key, role, task_id
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
            content, ChatHistory(), turn_id=turn_id, task_id=uuid.uuid4().hex[:8],
            include_browser=True,
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
                content, task.worker_chat_history, turn_id=turn_id, task_id=task.id,
                include_browser=False,
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
