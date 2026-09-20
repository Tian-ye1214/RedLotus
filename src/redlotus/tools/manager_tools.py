"""Manager planning tools and the incremental task dependency graph."""

from __future__ import annotations

from enum import Enum
from graphlib import CycleError, TopologicalSorter
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from redlotus.core.history import ChatHistory


def manager_tools(task_manager, toolkit, memory) -> tuple:
    """List the complete Manager planning tool set; retain owner memory permissions."""
    return (
        task_manager.create_todo_list,
        task_manager.get_todo_list,
        toolkit.ask_user,
        *([memory.reader.search_memory] if memory.owner_memory_allowed else []),
    )


class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "running"
    COMPLETED = "completed"
    FAILED = "failed"
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


class TaskManager:
    """An incrementally updated dependency graph with one authoritative task state."""

    def __init__(self):
        self.tasks: dict[str, Task] = {}

    def reset(self):
        self.tasks.clear()

    @property
    def completed(self):
        return bool(self.tasks) and all(
            task.status == TaskStatus.COMPLETED for task in self.tasks.values()
        )

    def snapshot(self):
        return [task.model_dump(mode="json") for task in self.tasks.values()]

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
                task.status = TaskStatus.PENDING
        self.tasks = tasks

    def create_todo_list(self, tasks_json: str) -> str:
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
            self.tasks = merged
            return self.get_todo_list()
        except (ValueError, CycleError) as exc:
            return f"Error: {exc}"

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

    def mark_task_complete(self, task_id, result=""):
        task = self.tasks[task_id]
        task.status, task.result = TaskStatus.COMPLETED, result
        return self.get_todo_list()

    def mark_task_failed(self, task_id, reason):
        task = self.tasks[task_id]
        task.failure_history.append(reason)
        task.retry_count += 1
        task.status = (
            TaskStatus.FAILED
            if task.retry_count > task.max_retries
            else TaskStatus.PENDING
        )
        return f"Task {task_id}: {task.status.value}, attempts={task.retry_count}, reason={reason}"

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
