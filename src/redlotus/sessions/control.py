"""Conversation identity, input ordering, incremental recovery, and saved-session discovery."""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from redlotus.prompts.message_text import message_has_user_prompt
from redlotus.runtime.network import ModelInputPolicy

if TYPE_CHECKING:
    from redlotus.tools.references import ReferenceFile

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
from redlotus.sessions.context import UserMessage as UserMessage
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
        self.current = None
        self.current_data = None
        self.worker = None
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
                    self.current = None
                    self.current_data = None
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
        self._storage_retry = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self.storage_paused = False
        self._compression_future = None
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
        future = self.queue.submit(operation)
        self._compression_future = future
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


# A period can end the preceding sentence; normal email local parts cannot end in one.
_AT_START = re.compile(r"(?<![A-Za-z0-9_%+-])@")
_WORD = re.compile(r"\S*")
_CLOSERS = {'"': '"', "'": "'", "{": "}"}


@dataclass(frozen=True)
class ReferenceSpan:
    start: int
    end: int
    value: str
    opener: str = ""
    closed: bool = True
    alternatives: tuple[str, ...] = ()


def resolve_ref_path(value: str, root: Path) -> Path:
    return (root / Path(value).expanduser()).resolve()


def _existing_path(value: str, root: Path, *, file_only=False) -> bool:
    if not value:
        return False
    try:
        path = resolve_ref_path(value, root)
        return path.is_file() if file_only else path.exists()
    except (OSError, RuntimeError, ValueError):
        return False


def _punctuation(text: str) -> bool:
    return bool(text) and all(
        unicodedata.category(char).startswith("P") for char in text
    )


def _inline_suffix(suffix: str) -> bool:
    if suffix[0] == ".":
        return _punctuation(suffix)  # A sentence ending, never a missing .backup file.
    return suffix[0] not in "-_/\\" and bool(
        re.match(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", suffix)
        or unicodedata.category(suffix[0]).startswith("P")
    )


def _existing_prefix(value: str, root: Path) -> str | None:
    if _existing_path(value, root):
        return value
    for length in range(len(value) - 1, 0, -1):
        if _inline_suffix(value[length:]) and _existing_path(
            value[:length], root, file_only=True
        ):
            return value[:length]
    return None


def _spaced_prefix(text: str, start: int, word_end: int, root: Path) -> str | None:
    """Extend an unquoted path across spaces only when a real file matches."""
    tail = text[word_end:].split("\n", 1)[0].split("\r", 1)[0]
    for match in re.finditer(r"\s+\S+", tail):
        end = word_end + match.end()
        candidate = _existing_prefix(text[start:end], root)
        if candidate is not None and len(candidate.rstrip()) > word_end - start:
            return candidate.rstrip()
        if "@" in match.group():
            break
    return None


def iter_reference_spans(text: str, *, root: Path) -> Iterator[ReferenceSpan]:
    cursor, adjacent = 0, False
    while cursor < len(text):
        match = _AT_START.search(text, cursor)
        if adjacent and text[cursor] == "@":
            start = cursor
        elif match is not None:
            start = match.start()
        else:
            return
        value_start = start + 1
        opener = text[value_start : value_start + 1]
        if opener in _CLOSERS:
            close = text.find(_CLOSERS[opener], value_start + 1)
            end = len(text) if close == -1 else close + 1
            yield ReferenceSpan(
                start,
                end,
                text[value_start + 1 : end if close == -1 else close],
                opener,
                close != -1,
            )
            cursor, adjacent = end, True
            continue

        word_end = _WORD.match(text, value_start).end()
        value = text[value_start:word_end]
        prefix = _existing_prefix(value, root)
        spaced = _spaced_prefix(text, value_start, word_end, root)
        alternatives = ()
        if spaced and spaced != prefix:
            if prefix and _existing_path(prefix, root, file_only=True):
                alternatives = (prefix, spaced)
            prefix = spaced
        if prefix is not None:
            # A filename can contain @; prefer the longest existing path before
            # interpreting any remaining markers as separate references.
            consumed = value_start + len(prefix)
            yield ReferenceSpan(start, consumed, prefix, alternatives=alternatives)
            cursor, adjacent = consumed, True
            continue

        separator = re.search(r"[@，。、；！？,;]", value)
        end = word_end if separator is None else value_start + separator.start()
        yield ReferenceSpan(start, end, text[value_start:end])
        cursor, adjacent = end, True


def quote_reference_path(value: str, *, opener: str = "", directory=False) -> str:
    ambiguous = any(char.isspace() or char in "@{}\"',;，；" for char in value)
    if not opener and not ambiguous:
        return value
    for quote in dict.fromkeys([opener, '"', "'", "{"]):
        if quote and _CLOSERS[quote] not in value:
            return quote + value + ("" if directory else _CLOSERS[quote])
    raise ValueError("文件名包含无法用引号或花括号包裹的分隔符。")


def parse_file_paths(text: str, *, root: Path | None = None) -> list[Path]:
    root = root or current_workspace()
    candidates = []
    remaining = list(text)
    for reference in iter_reference_spans(text, root=root):
        if reference.alternatives:
            raise ValueError(
                "引用路径存在歧义，请用引号或花括号指定："
                + "、".join(reference.alternatives)
            )
        if not reference.closed:
            raise ValueError(f"引用路径未闭合：{text[reference.start :]}")
        if value := reference.value.strip():
            if value.lower().startswith(("http://", "https://")):
                raise ValueError(f"引用仅支持本地文件：{value}")
            candidates.append((reference.start, resolve_ref_path(value, root)))
        remaining[reference.start : reference.end] = " " * (
            reference.end - reference.start
        )
    media = {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".bmp",
        ".mp4",
        ".mov",
        ".mkv",
        ".avi",
        ".webm",
    }
    for match in re.finditer(r'"([^"\n]+)"|\'([^\'\n]+)\'|(\S+)', "".join(remaining)):
        value = next(v for v in match.groups() if v is not None)
        if Path(value).suffix.lower() in media:
            path = resolve_ref_path(value, root)
            if path.is_file():
                candidates.append((match.start(), path))
    unique = {}
    for _, path in sorted(candidates, key=lambda item: item[0]):
        unique.setdefault(os.path.normcase(str(path)), path)
    return list(unique.values())


async def load_file_refs(
    text: str, *, role: str = "coordinator", workspace: WorkspaceContext | None = None, captured=None
) -> list[ReferenceFile]:
    from redlotus.tools.references import ReferenceStore

    workspace = workspace or WorkspaceContext.from_path(current_workspace())
    store = ReferenceStore(workspace)
    if captured is not None and 'reference_ids' in captured:
        return list(await asyncio.gather(*(store.parse(store.load(key)) for key in captured['reference_ids'])))
    paths = parse_file_paths(text, root=workspace.root)
    policy = ModelInputPolicy.for_role(role) if paths else None
    if paths and len(paths) > policy.max_files:
        raise ValueError(
            f"最多引用 {policy.max_files} 个文件，本次引用 {len(paths)} 个。"
        )
    errors, sizes = [], []
    for path in paths:
        if not path.is_file():
            errors.append(f"{path}: 文件不存在或不是普通文件")
            continue
        size = path.stat().st_size
        sizes.append(size)
        if size > policy.max_file_bytes:
            errors.append(
                f"{path}: {size:,} 字节，超过单文件限额 {policy.max_file_bytes:,} 字节"
            )
    if errors:
        raise ValueError("引用文件失败：\n" + "\n".join(errors))
    if paths:
        policy.check(sizes)
    async def capture():
        snapshots = await asyncio.gather(*(store.capture_file(path, policy=policy) for path in paths))
        if captured is not None:
            captured['reference_ids'] = [ref.id for ref in snapshots]
        return snapshots
    snapshots = await finish_io(capture())
    async def read(reference):
        try:
            return await store.parse(reference)
        except Exception as exc:
            raise ValueError(f"引用文件 {reference.name} 解析失败：{exc}") from exc

    return list(await asyncio.gather(*(read(reference) for reference in snapshots)))


