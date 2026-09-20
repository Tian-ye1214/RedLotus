"""Core tasks responsibilities."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from graphlib import CycleError, TopologicalSorter
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from redlotus.models.context import ChatHistory
from redlotus.runtime.context import InputAdmission, TaskStatus


class TaskDefinition(BaseModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    dependencies: list[str] = Field(default_factory=list)


class Task(TaskDefinition):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    retry_count: int = 0
    failure_history: list[str] = Field(default_factory=list)
    worker_chat_history: ChatHistory = Field(default_factory=ChatHistory, exclude=True)
    artifacts: list[str] = Field(default_factory=list)
    tool_summaries: list[str] = Field(default_factory=list)
    blocked_input_id: str | None = None
    user_input: str = ""


class TaskManager:
    """An incrementally updated dependency graph with one authoritative task state."""

    def __init__(self, *, checkpoint=None, user_input=None):
        self.tasks: dict[str, Task] = {}
        self._checkpoint, self.user_input = checkpoint, user_input

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
        parsed = TypeAdapter(list[Task]).validate_python(records)
        tasks = {task.id: task for task in parsed}
        if len(tasks) != len(parsed):
            raise ValueError("Task ids must be unique")
        self._validate(tasks)
        for task in tasks.values():
            if task.status == TaskStatus.IN_PROGRESS:
                task.status = TaskStatus.UNVERIFIED
        self.tasks = tasks

    async def save(self):
        if self._checkpoint is not None:
            await self._checkpoint()

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
            self.tasks = merged
        except (ValueError, CycleError) as exc:
            return f"Error: {exc}"
        await self.save()
        return self.get_todo_list()

    async def resume_task(self, task_id: str) -> str:
        """Resume one blocked task using newly received user input, retaining completed work.

        Args:
            task_id: The existing task awaiting input or explicit retry authorization.

        Returns:
            The saved plan, or an error if no new user input authorizes this resumption.
        """
        task = self.tasks.get(task_id)
        if task is None or task.status not in {TaskStatus.PENDING_CONFIRMATION, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.UNVERIFIED}:
            return "Error: Only an existing blocked task can be resumed."
        identity, text = self.user_input() if self.user_input else (None, "")
        if not identity or identity == task.blocked_input_id or not text.strip():
            return "Error: A new real user input is required to resume this task."
        task.status, task.blocked_input_id, task.user_input = TaskStatus.PENDING, identity, text
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

    def mark_task_complete(self, task_id, result=""):
        task = self.tasks[task_id]
        task.status, task.result = TaskStatus.COMPLETED, result
        return self.get_todo_list()

    def mark_task_failed(self, task_id, reason):
        task = self.tasks[task_id]
        task.failure_history.append(reason)
        task.retry_count += 1
        task.status = TaskStatus.FAILED
        return f"Task {task_id}: {task.status.value}, attempts={task.retry_count}, reason={reason}"

    def get_todo_list(self) -> str:
        """Return each planned task's status, dependencies, retry count and total progress."""
        if not self.tasks:
            return "Task list is empty"
        lines = [
            f"[{task.status.value}] {task.id}: {task.description} deps={task.dependencies} failures={task.retry_count}"
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


class TurnQueue:
    """FIFO work admission; cancelling one turn never kills the queue consumer."""

    def __init__(self, maxsize=0):
        self.pending = deque()
        self.maxsize = maxsize
        self.current = None
        self.worker = None

    def submit(self, work, *, data=None):
        if self.maxsize and len(self.pending) >= self.maxsize:
            raise asyncio.QueueFull
        result = asyncio.get_running_loop().create_future()
        result.add_done_callback(
            lambda done: None if done.cancelled() else done.exception()
        )
        self.pending.append((work, result, data))
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._consume())
        return result

    async def _consume(self):
        try:
            while self.pending:
                work, result, _ = self.pending.popleft()
                if result.cancelled():
                    continue
                self.current = asyncio.create_task(work())
                try:
                    value = await self.current
                    if not result.done():
                        result.set_result(value)
                except asyncio.CancelledError:
                    result.cancel()
                    if asyncio.current_task().cancelling():
                        raise
                except Exception as exc:
                    if not result.done():
                        result.set_exception(exc)
                finally:
                    self.current = None
        finally:
            self.worker = None

    def discard(self):
        while self.pending:
            self.pending.popleft()[1].cancel()

    async def join(self):
        while self.worker and not self.worker.done():
            await asyncio.shield(self.worker)

    async def cancel(self, *, discard=False):
        if discard:
            self.discard()
        current = self.current
        if current and not current.done():
            current.cancel()
            await asyncio.gather(current, return_exceptions=True)


class SessionController:
    """Serial outer turns with FIFO admission and a separate inner-loop inbox."""

    def __init__(self) -> None:
        self.queue = TurnQueue()
        self._turn_lock = asyncio.Lock()
        self._urgent: deque = deque()
        self._notices: deque = deque()
        self._generation = 0
        self._turn_generation = 0
        self._sequence = 0
        self._preparations: set[asyncio.Task] = set()
        self.turn_id: str | None = None
        self.active = False
        self.accepting_urgent = False
        self.task: asyncio.Task | None = None
        self.user_inputs: list[str] = []

    @asynccontextmanager
    async def turn(self, text: str, *, turn_id: str | None = None):
        generation = self._turn_generation
        # asyncio.Lock admits waiters in FIFO order, preserving each prompt boundary.
        async with self._turn_lock:
            if generation != self._turn_generation:
                raise asyncio.CancelledError()
            self.active = True
            self.turn_id = turn_id or uuid4().hex
            self.open_inbox()
            self.task = asyncio.current_task()
            self.user_inputs = [text]
            try:
                yield
            finally:
                self.active = False
                self.close_inbox()
                self.task = None
                self.turn_id = None
                self._urgent.clear()

    @property
    def generation(self):
        """UI callbacks also expire when their current task is stopped."""
        return self._generation, self._turn_generation

    def admit(self, workspace, *, urgent=False, input_id=None) -> InputAdmission:
        self._sequence += 1
        urgent = urgent and self.active and self.accepting_urgent
        return InputAdmission(
            input_id or uuid4().hex,
            self._sequence,
            self._generation,
            workspace,
            self.turn_id if urgent else None,
            urgent,
        )

    def accepts(self, admission: InputAdmission) -> bool:
        return admission.generation == self._generation and (
            not admission.urgent
            or (self.accepting_urgent and admission.turn_id == self.turn_id)
        )

    def queue_urgent(self, admission, prepare) -> None:
        task = asyncio.create_task(prepare)
        self._preparations.add(task)
        task.add_done_callback(self._preparations.discard)
        task.add_done_callback(
            lambda done: None if done.cancelled() else done.exception()
        )
        self._urgent.append((admission, task))

    async def take_urgent(self) -> list:
        """Freeze one request boundary before waiting for its attachments."""
        messages = []
        while self._urgent and not messages:
            pending = list(self._urgent)
            self._urgent.clear()
            for admission, task in pending:
                try:
                    message = await asyncio.shield(task)
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    continue
                if message is not None and self.accepts(admission):
                    messages.append((admission, message))
            # A rejected batch creates no model request. Check later admissions
            # before allowing a final response to close this turn.
        return messages

    def add_notice(self, content) -> None:
        self._notices.append(content)

    def take_notices(self) -> list:
        notices = list(self._notices)
        self._notices.clear()
        return notices

    def open_inbox(self) -> None:
        self.accepting_urgent = True

    def close_inbox(self) -> None:
        self.accepting_urgent = False

    def reset(self, *, discard=False) -> None:
        self._turn_generation += 1
        if discard:
            self._generation += 1
        self.close_inbox()
        for task in tuple(self._preparations):
            task.cancel()
        self._urgent.clear()
        self._notices.clear()
