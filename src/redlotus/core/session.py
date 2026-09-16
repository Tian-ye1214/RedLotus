"""Conversation identity, input ordering, incremental recovery, and saved-session discovery."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4
from filelock import FileLock
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse
from collections import deque
from contextlib import asynccontextmanager, contextmanager
from redlotus.core.agents import (
    InputAdmission,
    TurnQueue,
    WorkspaceContext,
    conversations_root,
    current_workspace,
    set_workspace,
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _write_with_cleanup(path, operation):
    """Retry one unchanged file transaction only after approved space reclamation."""
    try:
        operation()
    except OSError as exc:
        from redlotus.core.config import retry_after_storage_cleanup
        retry_after_storage_cleanup(path, exc, operation)


def _response_id(message):
    """Use request identity for accounting even when a response's displayed text changes."""
    return ":".join(str(value or "") for value in (
        message.provider_name, message.model_name, message.provider_response_id, message.timestamp.isoformat()
    ))


def _response_usage(message, **metadata):
    """Project actual model response counters; control receipts carry no model usage."""
    if not isinstance(message, ModelResponse) or (message.metadata or {}).get("origin") == "execution_status":
        return None
    return dict(metadata, model_name=message.model_name, provider_name=message.provider_name,
                timestamp=message.timestamp.isoformat(), usage=asdict(message.usage))


class SessionFile:
    """Append completed updates; compact only when retained evidence changes."""

    def __init__(self, path, *, lock=None, recover=True):
        self.path = Path(path)
        self._lock = lock or FileLock(self.path.with_suffix(".lock"))
        self._mutex = threading.RLock()
        self._recover_partial = recover
        self.recovered_partial_write = False
        self._view = []
        self._pending_update = None
        self._use_lock = None
        self._read()

    @classmethod
    def create(cls, root, project_id, *, session_id=None, title=""):
        identity = session_id or uuid4().hex
        root = Path(root).resolve()
        path = root / identity / "model_messages.json"
        if not path.resolve().is_relative_to(root):
            raise ValueError("Session ID must remain inside the session directory")
        header = dict(session_id=identity, project_id=project_id, title=title,
                      created_at=datetime.now(timezone.utc).isoformat())
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(path.with_suffix(".lock")):
            if path.exists():
                raise FileExistsError(path)
            cls._replace_file(path, {**header, "updates": []})
        return cls(path)

    @classmethod
    def load(cls, path, *, lock=None, recover=True):
        return cls(path, lock=lock, recover=recover)

    def acquire_use(self):
        """Protect this loaded instance from cleanup until its owner releases it."""
        with self._mutex, self._lock:
            if self._use_lock is None:
                if not self.path.is_file():
                    raise FileNotFoundError(f"会话恢复文件已不存在: {self.path}")
                self._use_lock = FileLock(
                    self.path.parent / f".use-{os.getpid()}-{uuid4().hex}.lock",
                    thread_local=False,
                )
                self._use_lock.acquire()

    def release_use(self):
        """Release only this instance's ownership marker, including repeated shutdown."""
        with self._mutex, self._lock:
            if self._use_lock is not None:
                self._use_lock.release()
                Path(self._use_lock.lock_file).unlink(missing_ok=True)
                self._use_lock = None

    @property
    def session_id(self):
        return self.header["session_id"]

    @property
    def project_id(self):
        return self.header["project_id"]

    @property
    def metadata(self):
        with self._locked_state():
            return deepcopy(self._metadata)

    @property
    def completed_turns(self):
        with self._locked_state():
            return self._metadata.get("completed_turns", 0)

    def _version(self):
        stat = self.path.stat()
        return stat.st_size, stat.st_mtime_ns

    def _read(self):
        with self._mutex, self._lock:
            self.recovered_partial_write = False
            previous_view = self._view
            previous_records = getattr(self, "_records", {})
            raw = self.path.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                if exc.reason != "unexpected end of data" or exc.end != len(raw):
                    raise
                text = raw[:exc.start].decode("utf-8")
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = self._recover(text)
            if (not isinstance(data, dict) or not isinstance(data.get("updates"), list)
                    or not all(isinstance(data.get(key), str) for key in ("session_id", "project_id", "title", "created_at"))):
                raise ValueError(f"会话结构无效: {self.path}")
            updates = data["updates"]
            for number, update in enumerate(updates, 1):
                if not isinstance(update, dict):
                    raise ValueError(f"会话事务结构无效: {self.path}, transaction={number}")
                commit = update.get("commit")
                if commit and commit != self._commit_tag(update, number):
                    if number != len(updates):
                        raise ValueError(f"会话中间事务校验失败: {self.path}, transaction={number}")
                    updates.pop()
                    self.recovered_partial_write = True
                    break
            if self.recovered_partial_write:
                if not self._recover_partial:
                    raise ValueError(f"会话含未完成事务，未修改原文件: {self.path}")
                self._replace(data)
            self.header = {key: value for key, value in data.items() if key != "updates"}
            self._records, self._prompts, self._metadata, self._turns = {}, {}, {}, {}
            self._jobs, self._usage, self._digests = {}, {}, {}
            self._texts = {}
            self._pending_jobs = set()
            self._context, self._view = [], []
            self._next_id = 0
            for update in data["updates"]:
                self._apply(update)
            if self._context == [key for _, key in previous_view] and all(
                previous_records.get(key) == self._records.get(key) for _, key in previous_view
            ):
                self._view = previous_view
            self._count = len(data["updates"])
            self._last_commit = updates[-1].get("commit") if updates else None
            self._saved_version = self._version()

    def _recover(self, text):
        """Discard only an incomplete trailing update, never invent a completed turn."""
        prefix, tail = text.split(',"updates":[', 1)
        data, updates = json.loads(prefix + "}"), []
        decoder, offset = json.JSONDecoder(), 0

        def top_level_end(start):
            """Find one journal row boundary without treating strings or nested dicts as rows."""
            depth, quoted, escaped = 0, False, False
            for index in range(start, len(tail)):
                char = tail[index]
                if quoted:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        quoted = False
                    continue
                if char == '"':
                    quoted = True
                elif char in "[{":
                    depth += 1
                elif char in "]}":
                    if depth == 0:
                        return None
                    depth -= 1
                    if depth == 0:
                        return index + 1
            return None

        def has_following_commit(start):
            """Reject recovery when a later complete journal row survived the corruption."""
            cursor = start
            while cursor < len(tail):
                while cursor < len(tail) and tail[cursor] in " \r\n\t,":
                    cursor += 1
                if cursor >= len(tail) or tail[cursor] != "{":
                    return False
                end = top_level_end(cursor)
                if end is None:
                    return False
                try:
                    candidate, decoded_end = decoder.raw_decode(tail, cursor)
                except json.JSONDecodeError:
                    candidate, decoded_end = None, cursor
                commit = candidate.get("commit") if isinstance(candidate, dict) else None
                if (
                    decoded_end == end
                    and isinstance(commit, dict)
                    and isinstance(commit.get("sequence"), int)
                    and commit["sequence"] > len(updates)
                    and commit == self._commit_tag(candidate, commit["sequence"])
                ):
                    return True
                cursor = end
            return False

        while offset < len(tail):
            while offset < len(tail) and tail[offset] in " \r\n\t,":
                offset += 1
            try:
                update, offset = decoder.raw_decode(tail, offset)
            except json.JSONDecodeError as exc:
                if has_following_commit(offset):
                    raise ValueError(f"会话中间事务损坏，未修改原文件: {self.path}") from exc
                break
            updates.append(update)
        data["updates"] = updates
        self.recovered_partial_write = True
        return data

    @staticmethod
    def _commit_tag(update, sequence):
        """Check the whole packed update before any of its fields become visible."""
        payload = {key: value for key, value in update.items() if key != "commit"}
        return dict(sequence=sequence, checksum=hashlib.sha256(_json(payload).encode()).hexdigest())

    def _replace(self, data):
        self._replace_file(self.path, data)

    @staticmethod
    def _replace_file(path, data):
        """Publish initialization or a compacted snapshot only after durable writing."""
        temporary = path.with_suffix(".tmp")
        def write():
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                header = {key: value for key, value in data.items() if key != "updates"}
                stream.write(_json(header)[:-1] + ',"updates":[')
                stream.write(",\n".join(_json(row) for row in data["updates"]))
                stream.write("\n]}")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        _write_with_cleanup(path, write)

    def _refresh(self):
        if self._pending_update is None and self._version() != self._saved_version:
            self._read()

    @contextmanager
    def _locked_state(self):
        """Hold the thread/file locks and refresh once before reading or updating state."""
        with self._mutex, self._lock:
            self._refresh()
            yield

    def _apply(self, update):
        self._texts.update(update.get("texts", {}))
        for path, identity in update.get("text_refs", []):
            target = update
            for field in path[:-1]:
                target = target[field]
            target[path[-1]] = self._texts[identity]
        self._prompts.update(update.get("prompts", {}))
        self._metadata.update(update.get("metadata", {}))
        for name, target in (("turns", self._turns), ("jobs", self._jobs)):
            for key, value in update.get(name, {}).items():
                target.setdefault(key, {}).update(value)
                if name == "jobs":
                    if target[key].get("indexed"):
                        self._pending_jobs.discard(key)
                    else:
                        self._pending_jobs.add(key)
        self._usage.update(update.get("usage", {}))
        if "context" in update:
            self._context = update["context"]
        if delta := update.get("context_delta"):
            self._context[delta["start"]:] = delta["ids"]
        for key, record in update.get("messages", {}).items():
            previous = self._records.setdefault(key, {"message": {}})
            if previous["message"]:
                digest = hashlib.sha256(_json(previous["message"]).encode()).hexdigest()
                if self._digests.get(digest) == key:
                    del self._digests[digest]
            previous.update({name: value for name, value in record.items() if name != "message"})
            previous["message"].update(record["message"])
            self._digests[hashlib.sha256(_json(previous["message"]).encode()).hexdigest()] = key
        self._next_id = update.get("next_id", self._next_id)

    def _pack(self, update, *, snapshot=False):
        """Store original input once; explicit paths avoid ambiguous magic JSON objects."""
        texts = dict(self._texts)

        def register(value):
            if isinstance(value, dict):
                for text in value.get("user_inputs", []):
                    texts[hashlib.sha256(text.encode()).hexdigest()] = text
                for item in value.values():
                    register(item)
            elif isinstance(value, list):
                for item in value:
                    register(item)

        register(update)
        known = {text: identity for identity, text in texts.items()}
        links, used = [], set()

        def encode(value, path):
            if isinstance(value, str) and value in known:
                identity = known[value]
                links.append((path, identity))
                used.add(identity)
                return None
            if isinstance(value, dict):
                return {key: encode(item, [*path, key]) for key, item in value.items()}
            if isinstance(value, list):
                return [encode(item, [*path, index]) for index, item in enumerate(value)]
            return value

        packed = encode(update, [])
        packed["texts"] = {key: texts[key] for key in used if snapshot or key not in self._texts}
        packed["text_refs"] = links
        return packed

    def _append(self, update):
        if self._pending_update is not None:
            raise OSError(f"存在尚未确认保存的批次，请先重试: {self.path}")
        update = self._pack(update)
        update["commit"] = self._commit_tag(update, self._count + 1)
        self._pending_update = update
        self._write_update(update)

    def _write_update(self, update):
        """Publish one transaction only after all bytes have been flushed to disk."""
        payload = ((",\n" if self._count else "") + _json(update) + "\n]}").encode()
        offset = self.path.stat().st_size - 3

        def write():
            with self.path.open("r+b") as stream:
                stream.seek(offset)
                stream.write(payload)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())

        _write_with_cleanup(self.path, write)
        self._apply(update)
        self._count += 1
        self._last_commit = update["commit"]
        self._saved_version = self._version()
        self._pending_update = None

    def retry_pending(self):
        """Settle an uncertain previous write before admitting another transaction."""
        with self._mutex, self._lock:
            pending = self._pending_update
            if pending is None:
                return
            with self.path.open("r+b") as stream:
                stream.flush()
                os.fsync(stream.fileno())
            self._read()
            if self._last_commit == pending["commit"]:
                self._pending_update = None
            elif pending["commit"]["sequence"] == self._count + 1:
                self._write_update(pending)
            else:
                raise OSError("会话已有其他写入，未保存批次不能覆盖新的提交")

    def update(self, *, metadata=None, turns=None, jobs=None):
        """Append only changed metadata, turn fields, and perception job fields."""
        with self._locked_state():
            changes = {}
            for name, value, previous in (("metadata", metadata, self._metadata), ("turns", turns, self._turns), ("jobs", jobs, self._jobs)):
                if value:
                    delta = {key: item for key, item in value.items() if item != previous.get(key)}
                    if name != "metadata":
                        delta = {key: {field: item for field, item in row.items() if item != previous.get(key, {}).get(field)}
                                 for key, row in delta.items()}
                    if delta:
                        changes[name] = delta
            if changes:
                self._append(changes)

    def save_context(self, messages, *, turn_id, metadata=None):
        """Serialize the changed suffix; old SDK message objects stay untouched."""
        with self._locked_state():
            prefix = 0
            for message, (previous, _) in zip(messages, self._view):
                if message is not previous:
                    break
                prefix += 1
            start = max(0, prefix - 1)
            view = self._view[:start]
            next_id = self._next_id
            additions, prompts, usage, pending_digests = {}, {}, {}, {}
            for offset, message in enumerate(messages[start:], start=start):
                from redlotus.tools.references import reference_message_data
                raw = ModelMessagesTypeAdapter.dump_python([message], mode="json")[0]
                raw = reference_message_data(raw)
                if instructions := raw.get("instructions"):
                    identity = hashlib.sha256(instructions.encode()).hexdigest()
                    if identity not in self._prompts:
                        prompts[identity] = instructions
                    raw["instructions"] = {"prompt_id": identity}
                digest = hashlib.sha256(_json(raw).encode()).hexdigest()
                key = pending_digests.get(digest, self._digests.get(digest))
                if key is None:
                    if offset < prefix:
                        key = self._view[offset][1]
                    else:
                        key = str(next_id)
                        next_id += 1
                    previous_record = self._records.get(key, {})
                    previous_message = previous_record.get("message", {})
                    additions[key] = dict(
                        turn_id=previous_record.get("turn_id", turn_id),
                        message={field: value for field, value in raw.items()
                                 if field not in previous_message or value != previous_message[field]},
                    )
                    pending_digests[digest] = key
                if summary := _response_usage(message):
                    usage_id = _response_id(message)
                    summary["turn_id"] = self._usage.get(usage_id, {}).get("turn_id", self._records.get(key, {}).get("turn_id", turn_id))
                    if summary != self._usage.get(usage_id):
                        usage[usage_id] = summary
                view.append((message, key))
            changed_meta = {key: value for key, value in (metadata or {}).items() if value != self._metadata.get(key)}
            context = [key for _, key in view]
            shared = 0
            for current, previous in zip(context, self._context):
                if current != previous:
                    break
                shared += 1
            update = dict(messages=additions, prompts=prompts, usage=usage,
                          metadata=changed_meta, next_id=next_id)
            if context != self._context:
                update["context_delta"] = dict(start=shared, ids=context[shared:])
            if additions or prompts or usage or changed_meta or "context_delta" in update:
                self._append(update)
            self._view = view
            return list(self._context)

    def _decode(self, records):
        from redlotus.tools.references import reference_message_data

        rows = []
        for record in records:
            raw = deepcopy(record["message"])
            if isinstance(raw.get("instructions"), dict):
                raw["instructions"] = self._prompts[raw["instructions"]["prompt_id"]]
            rows.append(reference_message_data(raw, restore=True))
        return ModelMessagesTypeAdapter.validate_python(rows)

    def model_messages(self):
        """Restore the current SDK context from its retained message IDs."""
        with self._locked_state():
            return self._decode([self._records[key] for key in self._context])

    def read_turn(self, turn_id):
        """Decode retained main-Agent evidence belonging to one real user turn."""
        with self._locked_state():
            return self._decode([row for row in self._records.values() if row["turn_id"] == turn_id])

    def finish_turn(self, turn_id, details):
        """Assign a completed turn its stable sequence number without recounting replays."""
        with self._locked_state():
            previous = self._turns.get(turn_id)
            number = previous["number"] if previous else self.completed_turns + 1
            turn = dict(details, id=turn_id, number=number, session_id=self.session_id)
            metadata = {"completed_turns": max(number, self.completed_turns)}
            if (self._metadata.get("active_turn") or {}).get("id") == turn_id:
                metadata["active_turn"] = None
            self._append(dict(turns={turn_id: turn}, metadata=metadata))
            return deepcopy(turn)

    def pending_turns(self, after):
        """Return current-session turn evidence after the given consumed position."""
        with self._locked_state():
            return deepcopy(sorted((row for row in self._turns.values() if row["number"] > after), key=lambda row: row["number"]))

    def job(self, identity):
        """Read one persisted perception job without exposing mutable stored state."""
        with self._locked_state():
            return deepcopy(self._jobs.get(identity))

    def turn(self, identity):
        """Read retained turn metadata without loading any other session."""
        with self._locked_state():
            active = self._metadata.get("active_turn") or self._metadata.get("interrupted_turn")
            return deepcopy(self._turns.get(identity) or (active if active and active["id"] == identity else None))

    def usage_responses(self):
        """Return cumulative response usage even after conversation bodies are pruned."""
        with self._locked_state():
            return deepcopy(list(self._usage.values()))

    def pending_jobs(self):
        """Return pending job IDs without rescanning completed production history."""
        with self._locked_state():
            return sorted(self._pending_jobs, key=lambda key: self._jobs[key]["created_at"])

    def record_usage(self, messages, *, role, invocation):
        """Keep per-response counters without saving child or perception transcripts."""
        with self._locked_state():
            usage = {}
            for message in messages:
                if row := _response_usage(message, role=role):
                    key = f"{invocation}:{_response_id(message)}"
                    if self._usage.get(key) != row:
                        usage[key] = row
            if usage:
                self._append(dict(usage=usage))

    def info(self):
        """Expose persisted identity and cumulative state for loading and panels."""
        return {**self.header, **self.metadata, "agent": "coordinator",
                "date": self.header["created_at"][:10], "topic": self._metadata.get("title", self.header["title"]),
                "saved_at": datetime.fromtimestamp(self.path.stat().st_mtime, timezone.utc).isoformat(),
                "message_count": len(self._context)}

    def compact(self, *, keep_turn_ids):
        """Prune only released bodies while retaining context, pending evidence, and totals."""
        with self._locked_state():
            if self._pending_update is not None:
                raise OSError(f"尚未确认保存的批次不能被清理覆盖: {self.path}")
            records = {key: row for key, row in self._records.items() if key in self._context or row["turn_id"] in keep_turn_ids}
            owners = {row["turn_id"] for row in records.values()}
            retained_turns = keep_turn_ids | {key for key, row in self._turns.items() if row.get("turn_id", key) in owners}
            prompts = {row["message"]["instructions"]["prompt_id"] for row in records.values() if isinstance(row["message"].get("instructions"), dict)}
            snapshot = dict(messages=records, prompts={key: self._prompts[key] for key in prompts},
                            context=self._context, metadata=self._metadata, jobs=self._jobs, usage=self._usage,
                            turns={key: row if key in retained_turns else
                                   {field: row[field] for field in ("id", "number", "session_id", "status")}
                                   for key, row in self._turns.items()}, next_id=self._next_id)
            update = self._pack(snapshot, snapshot=True)
            update["commit"] = self._commit_tag(update, 1)
            self._replace({**self.header, "updates": [update]})
            self._read()


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
