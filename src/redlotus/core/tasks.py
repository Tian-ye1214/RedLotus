"""Manager planning tools and the incremental task dependency graph."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from graphlib import CycleError, TopologicalSorter
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_ai.messages import (
    BaseToolReturnPart,
    ModelRequest,
    ModelResponse,
    TextPart,
)

from redlotus.prompts.message_text import split_messages_into_turns
from redlotus.prompts.prompt import load_prompt
from redlotus.runtime import logging as logger
from redlotus.sessions.context import ChatHistory
from redlotus.sessions.control import UserMessage


class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNVERIFIED = "unverified"
    PENDING_CONFIRMATION = "pending_confirmation"


class TaskDefinition(BaseModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    dependencies: list[str] = Field(default_factory=list)


class Task(TaskDefinition):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    retry_count: int = 0
    max_retries: int = 3
    failure_history: list[str] = Field(default_factory=list)
    worker_chat_history: ChatHistory = Field(default_factory=ChatHistory, exclude=True)
    artifacts: list[str] = Field(default_factory=list)
    tool_summaries: list[str] = Field(default_factory=list)
    input_cursor: tuple[str | None, int] = (None, 0)
    user_updates: list[str] = Field(default_factory=list)


class TaskManager:
    """An incrementally updated dependency graph with one authoritative task state."""

    def __init__(self, *, persist=None, input_source=None):
        self.tasks: dict[str, Task] = {}
        self._persist, self._input_source = persist, input_source
        self._save_lock = asyncio.Lock()

    def reset(self):
        self.tasks.clear()

    @property
    def completed(self):
        return bool(self.tasks) and all(
            task.status == TaskStatus.COMPLETED for task in self.tasks.values()
        )

    def snapshot(self):
        return [task.model_dump(mode="json") for task in self.tasks.values()]

    async def save(self):
        """Serialize plan checkpoints before any dependent work can start."""
        async with self._save_lock:
            if self._persist is not None:
                await self._persist(self.snapshot())

    @staticmethod
    def _validate(tasks):
        ids = set(tasks)
        for task in tasks.values():
            if not task.id or not task.description.strip():
                raise ValueError("Task id and description are required")
            if missing := set(task.dependencies) - ids:
                raise ValueError(
                    f"Task {task.id} has unknown dependencies: {sorted(missing)}"
                )
        TopologicalSorter(
            {key: task.dependencies for key, task in tasks.items()}
        ).prepare()

    def restore(self, records):
        tasks = {
            task.id: task for task in TypeAdapter(list[Task]).validate_python(records)
        }
        self._validate(tasks)
        for task in tasks.values():
            if task.status == TaskStatus.IN_PROGRESS:
                task.status = TaskStatus.UNVERIFIED
        self.tasks = tasks

    async def create_todo_list(self, tasks_json: str) -> str:
        """Add tasks to the plan while preserving existing progress and results.

        Existing task IDs must keep their descriptions and dependencies. Use a new
        ID for a changed definition. All dependencies must exist and be acyclic.

        Args:
            tasks_json: A JSON array of objects with id, description and dependencies.

        Returns:
            The complete current plan, or a validation error without changing the plan.
        """
        try:
            incoming = TypeAdapter(list[TaskDefinition]).validate_json(tasks_json)
            if len({row.id for row in incoming}) != len(incoming):
                raise ValueError("Task ids must be unique")
            merged = dict(self.tasks)
            for row in incoming:
                old = merged.get(row.id)
                if old and (
                    old.description != row.description
                    or old.dependencies != row.dependencies
                ):
                    raise ValueError(
                        f"Task {row.id} already has a different definition; use a new id"
                    )
                merged.setdefault(row.id, Task(**row.model_dump()))
            self._validate(merged)
        except (ValueError, CycleError) as exc:
            return f"Error: {exc}"
        self.tasks = merged
        await self.save()
        return self.get_todo_list()

    def get_all_ready_tasks(self):
        return [
            task
            for task in self.tasks.values()
            if task.status == TaskStatus.PENDING
            and all(
                self.tasks[key].status == TaskStatus.COMPLETED
                for key in task.dependencies
            )
        ]

    async def start(self, task):
        """Record the actual input boundary before dispatching a Worker."""
        turn_id, inputs = self._input_source()
        task.input_cursor = turn_id, len(inputs)
        task.status = TaskStatus.IN_PROGRESS
        await self.save()

    async def finish(self, task, report):
        """Persist one outcome; only a confirmed failure enters automatic retry."""
        task.result = report.model_dump_json()
        task.artifacts = list(dict.fromkeys([*task.artifacts, *report.artifacts]))
        task.tool_summaries.extend(report.risks)
        task.status = {
            "success": TaskStatus.COMPLETED,
            "failed": TaskStatus.FAILED,
            "needs_input": TaskStatus.PENDING_CONFIRMATION,
            "cancelled": TaskStatus.CANCELLED,
            "unverified": TaskStatus.UNVERIFIED,
        }[report.status]
        if report.needs_user_confirmation and report.status not in {"cancelled", "unverified"}:
            task.status = TaskStatus.PENDING_CONFIRMATION
        if task.status == TaskStatus.FAILED:
            task.failure_history.append(task.result)
            task.retry_count += 1
            if task.retry_count <= task.max_retries:
                task.status = TaskStatus.PENDING
        await self.save()

    async def resume(self, task_id):
        """Requeue exactly one blocked task using new, admitted user input."""
        task = self.tasks.get(task_id)
        if task is None or task.status not in {
            TaskStatus.PENDING_CONFIRMATION, TaskStatus.CANCELLED, TaskStatus.UNVERIFIED, TaskStatus.FAILED,
        }:
            return f"Error: Task {task_id} is not blocked."
        turn_id, inputs = self._input_source()
        previous_turn, count = task.input_cursor
        updates = inputs[count:] if turn_id == previous_turn else inputs
        if turn_id is None or not updates:
            return "Error: New user input is required before resuming this task."
        task.user_updates.extend(updates)
        task.status = TaskStatus.PENDING
        await self.save()
        return self.get_todo_list()

    def get_todo_list(self) -> str:
        """Return each planned task's status, dependencies, retry count and total progress."""
        if not self.tasks:
            return "Task list is empty"
        lines = [
            f"[{task.status.value}] {task.id}: {task.description} deps={task.dependencies} retries={task.retry_count}/{task.max_retries}"
            for task in self.tasks.values()
        ]
        done = sum(task.status == TaskStatus.COMPLETED for task in self.tasks.values())
        return "\n".join(["Task List", *lines, f"Progress: {done}/{len(self.tasks)}"])

    def structured_status(self):
        lines = [self.get_todo_list()]
        for task in self.tasks.values():
            waiting = [
                key
                for key in task.dependencies
                if self.tasks[key].status != TaskStatus.COMPLETED
            ]
            if waiting:
                lines.append(f"{task.id} waiting for: {waiting}")
            for label, values in (
                ("failures", task.failure_history),
                ("artifacts", task.artifacts),
                ("tools", task.tool_summaries),
            ):
                if values:
                    lines.append(f"{task.id} {label}: {values}")
        return "\n".join(lines)

    def get_final_summary(self):
        if not self.tasks:
            return "Error: Manager did not create executable tasks."
        details = [
            f"[{task.id}] {task.result or (task.failure_history[-1] if task.failure_history else task.status.value)}"
            for task in self.tasks.values()
        ]
        return "\n".join(
            [
                self.structured_status(),
                *details,
                "All tasks completed successfully!"
                if self.completed
                else "Tasks remain incomplete.",
            ]
        )


    async def execute_plan(self, user_input, continue_from_previous, *, orchestrator, history, tools, attachments, turn_id, presentation) -> str:
        logger.info("[用户]\n%s", user_input)


        if not continue_from_previous:
            self.reset()
            await self.save()
            history.reset()
            presentation.print_phase("第一阶段: Manager 规划任务列表")
            tmpl = await asyncio.to_thread(load_prompt, "manager_planning_new.md")
            planning_text = tmpl.format(user_input=user_input)
        else:
            presentation.print_phase("第一阶段: 基于用户反馈调整任务")
            current_todo = self.get_todo_list()
            tmpl = await asyncio.to_thread(load_prompt, "manager_planning_continue.md")
            planning_text = tmpl.format(
                user_input=user_input, current_todo=current_todo
            )

        planning_prompt = [planning_text, *attachments] if attachments else planning_text
        result = await orchestrator.plan(
            planning_prompt, history, turn_id=turn_id, tools=tools
        )
        presentation.show_model_output(result, title="Manager 规划")

        presentation.print_phase("第二阶段: 多Worker并行执行任务")

        final_summary = await orchestrator.execute_all_tasks_parallel(
            user_input, attachments=attachments, turn_id=turn_id
        )
        presentation.show_model_output(final_summary, title="任务汇总", markdown=False)

        presentation.print_phase("第三阶段: 生成最终报告")

        summary_tmpl = await asyncio.to_thread(load_prompt, "manager_summary.md")
        summary_text = summary_tmpl.format(
            user_input=user_input, final_summary=final_summary
        )
        summary_prompt = [summary_text, *attachments] if attachments else summary_text
        try:
            final_text = await orchestrator.plan(
                summary_prompt, history, turn_id=turn_id
            )
            presentation.show_model_output(final_text, title="最终报告")
            report = final_text.strip() or final_summary.strip()
        except Exception as exc:
            logger.warning("Manager summary unavailable: %s", exc)
            report = final_summary
        return (
            report
            if self.completed
            else "Error: Plan is incomplete.\n" + report
        )


class GoalSignal(str, Enum):
    CONTINUE = "CONTINUE"
    DONE = "DONE"


GOAL_MARKER_RE = re.compile(
    r"<!--\s*REDLOTUS_GOAL\s*:\s*(CONTINUE|DONE)\s*-->",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GoalParseResult:
    signal: GoalSignal
    cleaned_text: str
    marker_count: int

    @property
    def missing_marker(self) -> bool:
        return self.marker_count == 0


def parse_goal_output(text: str) -> GoalParseResult:
    """Strip goal-mode sentinel markers and return the effective signal.

    If no marker is present, goal mode treats the turn as CONTINUE so the next
    prompt can remind the model to add an explicit status marker.
    """
    body = text or ""
    matches = list(GOAL_MARKER_RE.finditer(body))
    cleaned = GOAL_MARKER_RE.sub("", body).strip()
    if not matches:
        return GoalParseResult(GoalSignal.CONTINUE, cleaned, 0)
    signal = GoalSignal(matches[-1].group(1).upper())
    return GoalParseResult(signal, cleaned, len(matches))


def summarize_last_coordinator_turn(messages: list) -> str:
    """Build goal-mode previous_output from the latest turn's assistant text and tool returns."""
    turns = split_messages_into_turns(messages)
    if not turns:
        return ""
    sections: list[str] = []
    for msg in turns[-1]:
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, TextPart):
                    text = (part.content or "").strip()
                    if text:
                        sections.append(text)
        elif isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, BaseToolReturnPart):
                    content = (
                        part.model_response_str()
                        if hasattr(part, "model_response_str")
                        else str(part.content)
                    )
                    tool_name = part.tool_name or "tool"
                    sections.append(f"[{tool_name}]\n{content}")
    return "\n\n".join(sections).strip()


def build_goal_iteration_prompt(
    *,
    original_goal: str,
    iteration: int,
    previous_output: str = "",
    missing_marker_reminder: bool = False,
) -> str:
    template = load_prompt("goal_iteration.md")
    return template.format(
        original_goal=original_goal.strip(),
        iteration=iteration,
        previous_output=previous_output.strip(),
        user_updates="",
        missing_marker_reminder=str(bool(missing_marker_reminder)).lower(),
    )


async def run_goal_loop(
    system: Any,
    *,
    message: UserMessage,
    history: Any,
    turn_id: str | None,
    set_iteration: Callable[[int], None] | None = None,
) -> None:
    original_goal = message.text or ""
    previous_output = ""
    missing_marker = False
    iteration = 0

    while True:
        iteration += 1
        if set_iteration is not None:
            set_iteration(iteration)

        prompt_text = build_goal_iteration_prompt(
            original_goal=original_goal,
            iteration=iteration,
            previous_output=previous_output,
            missing_marker_reminder=missing_marker,
        )

        parse_result: GoalParseResult | None = None

        def output_transform(raw_output: str) -> str:
            nonlocal parse_result
            parse_result = parse_goal_output(raw_output)
            return parse_result.cleaned_text

        prompt_message = replace(message, text=prompt_text)
        _history, output = await system.run_agent_system(
            prompt_message,
            history,
            turn_id=turn_id,
            output_transform=output_transform,
            _inside_goal=True,
        )

        parsed = parse_result or parse_goal_output(output)
        previous_output = summarize_last_coordinator_turn(_history.messages) or output
        missing_marker = parsed.missing_marker

        if parsed.signal == GoalSignal.DONE:
            return


