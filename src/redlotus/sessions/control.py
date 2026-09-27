"""Conversation identity, input ordering, incremental recovery, and saved-session discovery."""

from __future__ import annotations

from dataclasses import dataclass

from redlotus.prompts.message_text import message_has_user_prompt
from redlotus.api.media import (ReferenceSpan, iter_reference_spans, load_file_refs, parse_file_paths,
                               quote_reference_path, resolve_ref_path)

import asyncio
import inspect
from collections import deque
from contextlib import asynccontextmanager
from uuid import uuid4

from filelock import AsyncFileLock, Timeout

from redlotus.runtime import logging as logger
from redlotus.runtime.resources import (
    WorkspaceContext,
    bind_context,
    bind_to_loop,
    current_workspace,
    finish_io,
    workspace_context,
)
from redlotus.sessions.context import ChatHistory, UserMessage
from redlotus.sessions.context import _CANCELLING_WRITE, _USAGE_RECORDER, make_agent_id


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

    def __init__(self):
        self.pending = deque()
        self.current = self.current_data = self.worker = None
        self.ready = asyncio.Event()
        self.ready.set()

    def submit(self, work, *, data=None, first=False):
        result = asyncio.get_running_loop().create_future()
        result.add_done_callback(
            lambda done: None if done.cancelled() else done.exception()
        )
        (self.pending.appendleft if first else self.pending.append)((work, result, data))
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._consume())
        return result

    async def _consume(self):
        try:
            while self.pending:
                await self.ready.wait()
                if not self.pending:
                    break
                work, result, self.current_data = self.pending.popleft()
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
                    self.current = self.current_data = None
        finally:
            self.worker = None

    def discard(self):
        while self.pending:
            self.pending.popleft()[1].cancel()
        self.ready.set()

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
        self.history = ChatHistory()
        self.is_first_input = True
        self.agent = self.question = None
        self.deliveries = {}
        self.question_lock = asyncio.Lock()
        self._storage_retry = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self.storage_paused = False
        self._compression_future = None
        self._turn_lock = asyncio.Lock()
        self._urgent: deque = deque()
        self._notices: deque = deque()
        self._generation = self._turn_generation = self._sequence = 0
        self._preparations: set[asyncio.Task] = set()
        self.turn_id: str | None = None
        self.active = False
        self.accepting_urgent = False
        self.task: asyncio.Task | None = None
        self.user_inputs: list[str] = []
        self.recorded_input_ids: set[str] = set()
        self.paused = None
        self.control_busy = False
        self.pending_inputs = {}

    async def save_pause(self, system):
        paused, storage = self.paused, system._session_file
        if paused and storage:
            paused['queued'] = [data for _, result, data in self.queue.pending if not result.cancelled()]
            try:
                await self.write(lambda: storage.update(metadata={'paused_turn': paused}), storage=storage, cancelling=True)
            except asyncio.CancelledError as exc:
                if isinstance(exc.__cause__, OSError):
                    raise exc.__cause__
                raise

    def restore_pause(self, storage):
        self.paused = storage.metadata.get('paused_turn')
        if self.paused:
            self.queue.ready.clear()

    async def pause(self, system, *, reason):
        if self.control_busy or self.paused or not system.has_current_turn:
            return False
        turn = system._current_turn
        if turn and turn['task'].done():
            return False
        message = turn['message'] if turn else None
        request = self.queue.current_data or ({'text': message.original_text or message.text, 'id': turn['turn_id'],
                                             'goal_mode': turn['mode'] == 'goal'} if turn else None)
        if request is None:
            return False
        self.control_busy = True
        self.queue.ready.clear()
        generation = self._generation
        inherited = message.resume['supplements'] if message and message.resume else []
        pending = list({row['id']: row for row in [*inherited, *self.pending_inputs.values()]}.values())
        preparations = tuple(self._preparations)
        job = system._memory.current or (turn.get('observation') if turn else None)
        self.paused = dict(request=request, reason=reason, turn_id=turn['turn_id'] if turn else None,
                           goal_iteration=turn['goal_iteration'] if turn else 0,
                           user_inputs=list(self.user_inputs) if turn else [request['text']], reference_ids=list(job.reference_ids) if job else [ref.id for ref in message.references] if message else [],
                           supplements=pending, queued=[])
        try:
            if reason == 'user':
                await system.cancel_current_turn()
                await self.queue.cancel()
            elif turn:
                self.reset()
                await system._orchestrator.factory.cancel_turn(turn['turn_id'])
            await asyncio.gather(*preparations, return_exceptions=True)
            if generation != self._generation:
                return False
            self.paused['supplements'] = [row for row in pending if not row.get('delivered')]
            if system._session_file is None:
                await system.bind_session(uuid4().hex, generation=self.generation)
            if turn:
                self.paused['submitted'] = any(message_has_user_prompt(item) for item in system._session_file.read_turn(turn['turn_id']))
                if self.paused['submitted']:
                    self.paused['supplements'] = [row for row in self.paused['supplements'] if row['id'] not in {old['id'] for old in inherited}]
            await self.save_pause(system)
            return True
        finally:
            self.control_busy = False
            system.presentation.update_output('refresh_status')

    async def resume(self, system, execute, restore_queue):
        """Restore one serializable paused request ahead of its preserved FIFO queue."""
        if self.control_busy or not self.paused:
            return False
        self.control_busy = True
        saved, generation, storage = self.paused, self.generation, system._session_file
        try:
            from redlotus.sessions.context import repair_interrupted_tool_calls
            message = UserMessage(saved['request']['text'], resume=saved if saved['turn_id'] else None)
            message.references = await load_file_refs(message.text, workspace=system.workspace, captured={'reference_ids': saved['reference_ids']})
            for row in saved['supplements']:
                message.references.extend(await load_file_refs(row['text'], workspace=system.workspace, captured=row))
            if generation != self.generation or self.paused is not saved:
                return False
            self.history.set_messages(repair_interrupted_tool_calls(self.history.messages))
            restore_queue()
            await self.save_pause(system)
            if generation != self.generation or storage is not system._session_file or self.paused is not saved:
                return False
            async def resumed():
                if generation != self.generation or storage is not system._session_file:
                    return
                try:
                    await execute(message, self.admit(system.workspace, input_id=saved['request'].get('id')), saved['request'])
                finally:
                    if generation == self.generation and storage is system._session_file and not self.paused:
                        await system._durable_write(lambda: storage.update(metadata={'paused_turn': None}))
                    system.presentation.update_output('refresh_status')
            self.queue.submit(resumed, data=saved['request'], first=True)
            self.paused = None
            self.queue.ready.set()
            return True
        finally:
            self.control_busy = False
            system.presentation.update_output('refresh_status')

    async def resume_inputs(self, system, message):
        await system._durable_write(lambda: system._session_file.record_input(message.resume['request'].get('id') or message.resume['turn_id'], message))
        for row in message.resume['supplements']:
            if not row.get('recorded'):
                self.pending_inputs[row['id']] = row
                await system.record_user_input(row['id'], UserMessage(row['text'], references=[
                    ref for ref in message.references if ref.id in row.get('reference_ids', [])]))
        job = system._memory.current
        job.user_inputs = list(self.user_inputs)
        await system._durable_write(lambda: system._memory.observations.save(job))

    async def take_inputs(self, system):
        prompts = []
        batch = await self.take_urgent()
        for admission, message in batch:
            await system.record_user_input(admission.id, message)
            prompts.append(message.to_prompt())
            logger.debug(
                "input consumed id=%s sequence=%s turn=%s",
                admission.id,
                admission.sequence,
                admission.turn_id,
            )
            system._current_attachments.extend(
                [*message.attachments, *(part for ref in message.references for part in ref.to_prompt())]
            )
        for admission, _ in batch:
            if row := self.pending_inputs.get(admission.id):
                row['consumed'] = True
        return [
            *prompts,
            *self.take_notices(),
            *system._memory.take_context_notices(),
        ]

    def commit_inputs(self):
        for row in [*self.pending_inputs.values(), *(self.paused['supplements'] if self.paused else [])]:
            if row.get('consumed'):
                row['delivered'] = True
                self.pending_inputs.pop(row['id'], None)

    @asynccontextmanager
    async def turn(self, text: str, *, turn_id: str | None = None, user_inputs=None):
        generation = self._turn_generation
        # asyncio.Lock admits waiters in FIFO order, preserving each prompt boundary.
        async with self._turn_lock:
            if generation != self._turn_generation:
                raise asyncio.CancelledError()
            self.active = True
            self.turn_id = turn_id or uuid4().hex
            self.open_inbox()
            self.task = asyncio.current_task()
            self.user_inputs = list(user_inputs) if user_inputs is not None else [text]
            self.pending_inputs.clear()
            self.recorded_input_ids.clear()
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

    def consume_recorded_input(self, identity, message, context, event):
        """Apply one durable input to its original turn and observation."""
        if context != (self.generation, self.turn_id) or identity in self.recorded_input_ids:
            return False
        self.recorded_input_ids.add(identity)
        if identity == self.turn_id:
            return False  # The outer turn seeded its first input before persistence.
        self.user_inputs.append(message.original_text if message.original_text is not None else message.text)
        if identity in self.pending_inputs:
            self.pending_inputs[identity]['recorded'] = True
        if event is None or event.turn_id != self.turn_id:
            return False
        event.user_inputs = list(self.user_inputs)
        event.reference_ids = list(dict.fromkeys([*event.reference_ids, *(ref.id for ref in message.references)]))
        return True

    def admit(self, workspace, *, urgent=False, input_id=None) -> InputAdmission:
        self._sequence += 1
        urgent = urgent and self.active and self.accepting_urgent
        if not urgent:
            self._storage_retry.set()
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

    def track_preparation(self, prepare):
        task = asyncio.create_task(prepare)
        self._preparations.add(task)
        task.add_done_callback(self._preparations.discard)
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return task

    async def prepare_message(self, system, message, *, prepare=None):
        generation = self.generation
        if prepare:
            message.attachments = await self.track_preparation(prepare())
        await system.toolkit._references.prepare_message(message)
        if generation != self.generation:
            raise asyncio.CancelledError()
        return message

    async def start(self, system, message, history, admission, *, goal_mode=False, prepare=None):
        await self.prepare_message(system, message, prepare=prepare)
        if not self.accepts(admission):
            raise asyncio.CancelledError()
        if not (message.text or message.attachments or message.references):
            return ""
        if self.queue.current_data is not None:
            self.queue.current_data["reference_ids"] = [ref.id for ref in message.references]
        task = system._start_user_turn(message, history, turn_id=admission.id, **({"goal_mode": True} if goal_mode else {}))
        if task is None:
            raise RuntimeError("Another turn bypassed the session queue")
        if message.resume is not None:
            system.record_control_result("resume", message.resume["turn_id"], "running", accepted=True)
        return await task

    def queue_urgent(self, admission, prepare) -> None:
        self._urgent.append((admission, self.track_preparation(prepare)))

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

    def add_notice(self, content, *, context=None) -> None:
        self._notices.append((context or (self.generation, self.turn_id), content))

    def take_notices(self) -> list:
        notices = [content for context, content in self._notices
                   if context == (self.generation, self.turn_id)]
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
            self.paused = None
            self.queue.ready.set()
        self.close_inbox()
        if self.question is not None:
            self.question.cancel()
        for task in tuple(self._preparations):
            task.cancel()
        self._urgent.clear()
        self.pending_inputs.clear()
        self._notices.clear()


    def usage(self, storage):
        """Bind model receipts to their original session and its existing durable writer."""
        generation, owner = self.generation, asyncio.current_task()

        async def record(messages, *, role, invocation, agent_id=None, cancelling=False):
            with workspace_context(storage.workspace):
                await self.write(
                    lambda: storage.record_usage(messages, role=role, invocation=invocation,
                                                 agent_id=agent_id or make_agent_id(storage.session_id, role)),
                    storage=storage,
                    cancelling=cancelling or owner.cancelling() or generation != self.generation,
                )

        return bind_context(_USAGE_RECORDER, bind_to_loop(record, asyncio.get_running_loop()) if storage else None)

    async def write(self, operation, *, storage=None, cancelling=False):
        """Hold failed checkpoints until new input, but never wait during cancellation."""
        owner = asyncio.current_task()

        def checkpoint(action):
            try:
                return action()
            except OSError:
                self.storage_paused = True
                raise

        async def attempt():
            async with self._write_lock:
                with bind_context(_CANCELLING_WRITE, lambda: cancelling or owner.cancelling()):
                    if storage is not None:
                        await finish_io(asyncio.to_thread(checkpoint, storage.retry_pending))
                    result = await finish_io(asyncio.to_thread(checkpoint, operation))
                    if inspect.isawaitable(result):
                        result = await result
                self.storage_paused = False
                return result

        while True:
            self._storage_retry.clear()
            try:
                try:
                    return await (finish_io(attempt()) if cancelling or owner.cancelling() else attempt())
                except Timeout as exc:
                    if cancelling or owner.cancelling():
                        raise
                    async with AsyncFileLock(exc.lock_file, run_in_executor=False):
                        pass
            except OSError as exc:
                self.storage_paused = True
                if cancelling or owner.cancelling():
                    raise asyncio.CancelledError() from exc
                logger.warning(f"保存失败，任务已暂停，输入已保留: {exc}。恢复存储后提交普通输入重试。")
                await self._storage_retry.wait()


    @property
    def is_compressing(self):
        return self._compression_future is not None and not self._compression_future.done()


    async def cancel_compression(self):
        """Invalidate the queued control operation without cancelling ordinary tasks."""
        future = self._compression_future
        if future is not None and not future.done():
            self.reset()
            future.cancel()
            await self.queue.cancel()


    async def compress(self, operation, *, busy):
        """Serialize detached compression and persist its candidate before publishing it."""
        if self.is_compressing:
            return ["上下文压缩正在处理中。"]
        if busy or self.queue.pending:
            return ["当前任务正在运行，请先停止或等待完成。"]
        self._compression_future = future = self.queue.submit(operation)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            return ["上下文压缩已取消，未提交候选不再写回。"]
        finally:
            if self._compression_future is future:
                self._compression_future = None



def user_message_from_cli_input(raw_input: str) -> UserMessage:
    """Reference resolution happens once in the input controller, never in file contents."""
    return UserMessage(text=raw_input, original_text=raw_input)


