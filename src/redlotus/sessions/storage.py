"""Conversation identity, input ordering, incremental recovery, and saved-session discovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelResponse,
    TextContent,
    ToolSearchCallPart,
)

from redlotus.runtime.resources import (
    conversations_root,
)
from redlotus.sessions.journal import SessionJournal, _json


def _response_id(message):
    """Keep the message identity used by existing compression checkpoints."""
    return ":".join(str(value or "") for value in (
        message.provider_name, message.model_name, message.provider_response_id, message.timestamp.isoformat()
    ))


def _is_sdk_capability_load_receipt(message):
    """Recognize Pydantic AI's local deferred-capability bookkeeping response."""
    if message.model_name or message.provider_name or message.usage.has_values() or len(message.parts) != 1:
        return False
    part = message.parts[0]
    return isinstance(part, ToolSearchCallPart) and part.tool_call_id.startswith("auto_load_")


def _response_usage(message, **metadata):
    """Project actual model response counters; control receipts carry no model usage."""
    if (
        not isinstance(message, ModelResponse)
        or (message.metadata or {}).get("origin") == "execution_status"
        or _is_sdk_capability_load_receipt(message)
    ):
        return None
    return dict(metadata, model_name=message.model_name, provider_name=message.provider_name,
                provider_response_id=message.provider_response_id,
                category=(message.metadata or {}).get("usage_category", "unknown"),
                timestamp=message.timestamp.isoformat(), usage=asdict(message.usage))


@dataclass(frozen=True)
class SessionScanInfo:
    path: Path
    info: dict
    error: str = ""


class SessionFile(SessionJournal):
    """Append completed updates; compact only when retained evidence changes."""


    @classmethod
    def create(cls, root, project_id, *, session_id=None, title="", workspace=None):
        identity = session_id or uuid4().hex
        root = Path(root).resolve()
        path = root / identity / "model_messages.json"
        if not path.resolve().is_relative_to(root):
            raise ValueError("Session ID must remain inside the session directory")
        header = dict(session_id=identity, project_id=project_id, title=title, input_accounting=1,
                      created_at=datetime.now(timezone.utc).isoformat())
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(path.with_suffix(".lock")):
            if path.exists():
                raise FileExistsError(path)
            cls._replace_file(path, {**header, "updates": []}, workspace=workspace)
        return cls(path, workspace=workspace)

    @classmethod
    def load(cls, path, *, lock=None, recover=True, commit_recovery=True, workspace=None):
        return cls(path, lock=lock, recover=recover, commit_recovery=commit_recovery, workspace=workspace)

    @staticmethod
    def _scan_record(info):
        if not isinstance(info, dict):
            return None
        status = info.get("status") or (
            "active" if info.get("active_turn") else "interrupted"
            if info.get("interrupted_turn") else "completed"
            if info.get("completed_turns", 0) else "new"
        )
        record = {
            "session_id": info.get("session_id"), "project_id": info.get("project_id"),
            "title": info.get("topic", info.get("title", "")), "saved_at": info.get("saved_at"),
            "completed_turns": info.get("completed_turns", 0), "status": status,
        }
        try:
            if (
                not all(isinstance(record[key], str) for key in ("session_id", "project_id", "title", "saved_at"))
                or isinstance(record["completed_turns"], bool)
                or not isinstance(record["completed_turns"], int)
                or record["completed_turns"] < 0
                or record["status"] not in {"active", "interrupted", "completed", "new"}
            ):
                return None
            datetime.fromisoformat(record["saved_at"])
        except (TypeError, ValueError):
            return None
        return record

    @classmethod
    def scan_info(cls, root=None) -> list[SessionScanInfo]:
        """Discover saved sessions through a rebuildable metadata-only cache."""
        root = Path(root or conversations_root()).resolve()
        if not root.is_dir():
            return []
        index = root / "index.json"
        try:
            cached = json.loads(index.read_text(encoding="utf-8"))
            cached = cached["sessions"] if cached.get("version") == 1 and isinstance(cached["sessions"], dict) else {}
        except (OSError, TypeError, ValueError, KeyError, AttributeError):
            cached = {}
        rows, scanned = {}, []
        for path in sorted(root.glob("*/model_messages.json")):
            try:
                stat = path.stat()
            except OSError:
                continue
            signature = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            key = path.relative_to(root).as_posix()
            previous = cached.get(key)
            if isinstance(previous, dict) and previous.get("signature") == signature:
                if set(previous) == {"signature", "error"} and previous["error"] is True:
                    rows[key] = previous
                    scanned.append(SessionScanInfo(path, {}, "会话文件不可读取"))
                    continue
                info = cls._scan_record(previous.get("info", {}))
                if set(previous) == {"signature", "info"} and info:
                    rows[key] = {"signature": signature, "info": info}
                    scanned.append(SessionScanInfo(path, info))
                    continue
            try:
                info = cls._scan_record(cls.load(path, commit_recovery=False).info())
                if info is None:
                    raise ValueError("会话元数据无效")
                stat = path.stat()
                rows[key] = {"signature": {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}, "info": info}
                scanned.append(SessionScanInfo(path, info))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                rows[key] = {"signature": signature, "error": True}
                scanned.append(SessionScanInfo(path, {}, str(exc)))
        if rows != cached:
            try:
                # Session-file locks are released before this unrelated cache lock is attempted.
                with FileLock(index.with_suffix(".lock"), timeout=0):
                    from redlotus.runtime.resources import atomic_write_json
                    atomic_write_json(index, {"version": 1, "sessions": rows})
            except (OSError, Timeout):
                pass
        return scanned

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
    def role(self):
        return self.header.get("role", "coordinator")

    def role_file(self, role, *, create=True):
        """Open one sibling journal per role; each journal separates Agent instances."""
        if not re.fullmatch(r"[a-z][a-z0-9_]*", role):
            raise ValueError(f"Invalid session role: {role}")
        if role == self.role:
            return self
        with self._mutex:
            if role in self._roles:
                return self._roles[role]
            path = self.path.with_name(f"model_messages.{role}.json")
            if role == "coordinator":
                path = self.path.with_name("model_messages.json")
            with FileLock(path.with_suffix(".lock")) as lock:
                if not path.exists():
                    if not create:
                        return None
                    header = {key: self.header[key] for key in ("session_id", "project_id", "title", "created_at")}
                    self._replace_file(path, dict(header, role=role, updates=[]), workspace=self.workspace)
                child = SessionFile.load(path, lock=lock, workspace=self.workspace)
            if child.session_id != self.session_id or child.project_id != self.project_id or child.role != role:
                raise ValueError(f"Role journal belongs to another session: {path}")
            self._roles[role] = child
            return child

    def role_messages(self, role, *, agent_id=""):
        """Read role context without creating files or rewriting a legacy session."""
        child = self.role_file(role, create=False)
        if child is not None and (messages := child.model_messages(agent_id=agent_id)):
            return messages
        if role == "manager" and self.metadata.get("manager_context"):
            from redlotus.tools.references import reference_message_data
            return ModelMessagesTypeAdapter.validate_python(reference_message_data(
                self.metadata["manager_context"], restore=True, workspace=self.workspace,
            ))
        return []

    def _split_legacy_roles(self):
        """Publish role data durably before removing mixed legacy data from the parent."""
        if self.role != "coordinator":
            return
        moved = {key: row for key, row in self._usage.items() if row.get("role", self.role) != self.role}
        legacy = self._metadata.get("manager_context")
        if not moved and not legacy:
            return
        for role in {row["role"] for row in moved.values()}:
            child = self.role_file(role)
            with child._locked_state():
                rows = {key: {field: value for field, value in row.items() if field != "role"}
                        for key, row in moved.items() if row["role"] == role and key not in child._usage}
                if rows:
                    child._append(dict(usage=rows))
        if legacy:
            from redlotus.sessions.context import make_agent_id
            manager_id = make_agent_id(self.session_id, "manager", "planning")
            manager = self.role_file("manager")
            if not manager.model_messages(agent_id=manager_id):
                manager.save_context(self.role_messages("manager", agent_id=manager_id),
                                     turn_id=None, agent_id=manager_id)
        metadata, usage = self._metadata, self._usage
        self._metadata = {key: value for key, value in metadata.items() if key != "manager_context"}
        self._usage = {key: value for key, value in usage.items() if key not in moved}
        try:
            self.compact(keep_turn_ids=set(self._turns) | {row["turn_id"] for row in self._records.values()})
        except BaseException:
            self._metadata, self._usage = metadata, usage
            raise

    @property
    def metadata(self):
        with self._locked_state():
            return deepcopy(self._metadata)

    @property
    def completed_turns(self):
        with self._locked_state():
            return self._metadata.get("completed_turns", 0)












    def _append(self, update):
        if self._pending_update is not None:
            raise OSError(f"存在尚未确认保存的批次，请先重试: {self.path}")
        self._split_legacy_roles()
        update = self._pack({key: value for key, value in update.items()
                             if value or key in {"context", "contexts"}})
        update["commit"] = self._commit_tag(update, self._count + 1)
        self._pending_update = update
        self._write_update(update)



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

    def save_context(self, messages, *, turn_id, metadata=None, agent_id="", invocation=None):
        """Serialize the changed suffix; old SDK message objects stay untouched."""
        with self._locked_state():
            previous_view = self._views.get(agent_id, [])
            previous_context = self._contexts.get(agent_id, [])
            prefix = 0
            for message, (previous, _) in zip(messages, previous_view):
                if message is not previous:
                    break
                prefix += 1
            start = max(0, prefix - 1)
            view = previous_view[:start]
            next_id = self._next_id
            additions, prompts, usage, pending_digests = {}, {}, {}, {}
            for offset, message in enumerate(messages[start:], start=start):
                from redlotus.tools.references import reference_message_data
                raw = ModelMessagesTypeAdapter.dump_python([message], mode="json")[0]
                raw = reference_message_data(raw, workspace=self.workspace)
                if instructions := raw.get("instructions"):
                    identity = hashlib.sha256(instructions.encode()).hexdigest()
                    if identity not in self._prompts:
                        prompts[identity] = instructions
                    raw["instructions"] = {"prompt_id": identity}
                digest = hashlib.sha256(_json(raw).encode()).hexdigest()
                key = pending_digests.get(digest, self._digests.get((agent_id, digest)))
                if key is None:
                    if offset < prefix:
                        key = previous_view[offset][1]
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
                    if agent_id:
                        additions[key]["agent_id"] = agent_id
                    if invocation:
                        additions[key]["invocation"] = invocation
                    pending_digests[digest] = key
                usage.update(self._usage_updates([message],
                    turn_id=self._records.get(key, {}).get("turn_id", turn_id),
                    agent_id=agent_id, invocation=invocation,
                ))
                view.append((message, key))
            changed_meta = {key: value for key, value in (metadata or {}).items() if value != self._metadata.get(key)}
            context = [key for _, key in view]
            shared = 0
            for current, previous in zip(context, previous_context):
                if current != previous:
                    break
                shared += 1
            update = dict(messages=additions, prompts=prompts, usage=usage,
                          metadata=changed_meta)
            if agent_id:
                update["agent_id"] = agent_id
            if context != previous_context:
                update["context_delta"] = dict(start=shared, ids=context[shared:])
            if additions or prompts or usage or changed_meta or "context_delta" in update:
                self._append(update)
            self._views[agent_id] = view
            return list(self._contexts.get(agent_id, []))

    def _decode(self, records):
        from redlotus.tools.references import reference_message_data

        rows = []
        for record in records:
            raw = deepcopy(record["message"])
            if isinstance(raw.get("instructions"), dict):
                raw["instructions"] = self._prompts[raw["instructions"]["prompt_id"]]
            rows.append(reference_message_data(raw, restore=True, workspace=self.workspace))
        return ModelMessagesTypeAdapter.validate_python(rows)

    def model_messages(self, *, agent_id=""):
        """Restore the current SDK context from its retained message IDs."""
        with self._locked_state():
            return self._decode([self._records[key] for key in self._contexts.get(agent_id, [])])

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
            rows = {(row.get("role", self.role), key): dict(row, role=row.get("role", self.role))
                    for key, row in self._usage.items()}
        if self.role == "coordinator":
            for path in self.path.parent.glob("model_messages.*.json"):
                child = self.role_file(path.name[len("model_messages."):-len(".json")], create=False)
                with child._locked_state():
                    rows.update(((child.role, key), dict(row, role=child.role)) for key, row in child._usage.items())
        return deepcopy(list(rows.values()))

    def record_input(self, input_id, message):
        """Count admitted user content once, without retaining another body copy."""
        from math import ceil

        from redlotus.sessions.context import _estimate_text_tokens

        text = message.original_text if message.original_text is not None else message.text
        attachments = message.attachments
        text += "".join(part if isinstance(part, str) else part.content
                        for part in attachments if isinstance(part, str) or isinstance(part, TextContent))
        entries = {f"input:{input_id}": dict(
            tokens=ceil(_estimate_text_tokens(text)),
            unmetered=sum(not isinstance(part, (str, TextContent)) for part in attachments),
        )}
        for ref in message.references:
            entries[f"reference:{ref.sha256}"] = dict(
                tokens=ceil(_estimate_text_tokens("".join(part.text for part in ref.parts if part.kind == "text"))),
                unmetered=sum(part.kind != "text" for part in ref.parts) if ref.parts else 1,
            )
        with self._locked_state():
            if f"input:{input_id}" in self._inputs:
                return
            additions = {key: value for key, value in entries.items() if key not in self._inputs}
            if additions:
                self._append(dict(inputs=additions))

    def input_usage(self):
        """Expose measured content and coverage independently of API usage."""
        with self._locked_state():
            return dict(
                input_tokens=sum(row["tokens"] for row in self._inputs.values()),
                unmetered_attachments=sum(row["unmetered"] for row in self._inputs.values()),
                incomplete_sessions=int(not self.header.get("input_accounting")
                                        or bool(self._usage) and not self._inputs),
            )

    def pending_jobs(self):
        """Return pending job IDs without rescanning completed production history."""
        with self._locked_state():
            return sorted(self._pending_jobs, key=lambda key: self._jobs[key]["created_at"])

    def _usage_updates(self, messages, **metadata):
        """Share response identity between checkpoints, auxiliary calls and save retries."""
        changes = {}
        for message in messages:
            if row := _response_usage(message):
                identity = (message.metadata or {}).get("usage_request_id") or ":".join(str(value or "") for value in (
                    message.provider_name, message.model_name, message.provider_response_id or message.timestamp.isoformat(),
                ))
                legacy = _response_id(message)
                key = next((key for key in self._usage if key == legacy or key.endswith(":" + legacy)), identity)
                row = {**metadata, **self._usage.get(key, {}), **row}
                if row != self._usage.get(key):
                    changes[key] = row
        return changes

    def record_usage(self, messages, *, role, invocation, agent_id=None):
        """Keep per-response counters without saving child or perception transcripts."""
        if role != self.role:
            return self.role_file(role).record_usage(messages, role=role, invocation=invocation, agent_id=agent_id)
        with self._locked_state():
            usage = self._usage_updates(messages, invocation=invocation, agent_id=agent_id)
            if usage:
                self._append(dict(usage=usage))

    def info(self):
        """Expose persisted identity and cumulative state for loading and panels."""
        return {**self.header, **self.metadata, "agent": "coordinator",
                "date": self.header["created_at"][:10], "topic": self._metadata.get("title", self.header["title"]),
                "saved_at": datetime.fromtimestamp(self.path.stat().st_mtime, timezone.utc).isoformat(),
                "message_count": len(self._contexts.get("", [])), "input_usage": self.input_usage()}

    def compact(self, *, keep_turn_ids, release_turn_id=None, keep_agent_ids=()):
        """Prune only released bodies while retaining context, pending evidence, and totals."""
        with self._locked_state():
            if self._pending_update is not None:
                raise OSError(f"尚未确认保存的批次不能被清理覆盖: {self.path}")
            contexts = {agent: context for agent, context in self._contexts.items()
                        if release_turn_id is None or agent in keep_agent_ids or not any(
                            self._records[key]["turn_id"] == release_turn_id for key in context
                        )}
            context_ids = {key for context in contexts.values() for key in context}
            records = {key: row for key, row in self._records.items() if key in context_ids or row["turn_id"] in keep_turn_ids}
            owners = {row["turn_id"] for row in records.values()}
            retained_turns = keep_turn_ids | {key for key, row in self._turns.items() if row.get("turn_id", key) in owners}
            prompts = {row["message"]["instructions"]["prompt_id"] for row in records.values() if isinstance(row["message"].get("instructions"), dict)}
            snapshot = dict(messages=records, prompts={key: self._prompts[key] for key in prompts},
                            contexts=contexts, metadata=self._metadata, jobs=self._jobs, usage=self._usage, inputs=self._inputs,
                            turns={key: row if key in retained_turns else
                                   {field: row[field] for field in ("id", "number", "session_id", "status")}
                                   for key, row in self._turns.items()})
            update = self._pack(snapshot, snapshot=True)
            update["commit"] = self._commit_tag(update, 1)
            self._replace({**self.header, "updates": [update]})
            self._read()










