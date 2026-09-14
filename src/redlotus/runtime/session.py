from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

from redlotus.runtime.context import WorkspaceContext


@dataclass(frozen=True)
class InputAdmission:
    id: str
    sequence: int
    generation: int
    workspace: WorkspaceContext
    turn_id: str | None
    urgent: bool


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
