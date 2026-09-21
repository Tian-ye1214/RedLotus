"""Conversation identity, input ordering, incremental recovery, and saved-session discovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from textwrap import indent

from filelock import FileLock

from redlotus.sessions.cleanup import _write_with_cleanup


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class SessionJournal:
    """One durable JSON transaction format shared by coordinator and role ledgers."""

    def __init__(self, path, *, lock=None, recover=True, commit_recovery=True, workspace=None):
        self.path = Path(path)
        self.workspace = workspace
        self._lock = lock or FileLock(self.path.with_suffix(".lock"))
        self._mutex = threading.RLock()
        self._recover_partial = recover
        self._commit_recovery = commit_recovery
        self.recovered_partial_write = False
        self._views = {}
        self._roles = {}
        self._pending_update = None
        self._use_lock = None
        self._read()


    def _version(self):
        stat = self.path.stat()
        return stat.st_size, stat.st_mtime_ns


    def _read(self):
        with self._mutex, self._lock:
            previous_views = self._views
            previous_records = getattr(self, "_records", {})
            data, self.recovered_partial_write = self._inspect(self.path.read_bytes())
            updates = data["updates"]
            if self.recovered_partial_write:
                if not self._recover_partial:
                    raise ValueError(f"会话含未完成事务，未修改原文件: {self.path}")
                if self._commit_recovery:
                    self._replace(data)
            self.header = {key: value for key, value in data.items() if key != "updates"}
            self._records, self._prompts, self._metadata, self._turns = {}, {}, {}, {}
            self._jobs, self._usage, self._digests, self._inputs = {}, {}, {}, {}
            self._texts = {}
            self._pending_jobs = set()
            self._contexts, self._views = {"": []}, {}
            self._next_id = 0
            for update in data["updates"]:
                self._apply(update)
            for identity, view in previous_views.items():
                if self._contexts.get(identity) == [key for _, key in view] and all(
                    previous_records.get(key) == self._records.get(key) for _, key in view
                ):
                    self._views[identity] = view
            self._count = len(data["updates"])
            self._last_commit = updates[-1].get("commit") if updates else None
            self._saved_version = self._version()
            raw = self.path.read_bytes()
            self._append_offset = len(raw[:raw.rfind(b"]")].rstrip())


    def _inspect(self, raw):
        """Validate a recovery candidate without changing the file or published state."""
        partial_character = False
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data" or exc.end != len(raw):
                raise
            text = raw[:exc.start].decode("utf-8")
            partial_character = True
        repaired = False
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data, repaired = self._recover(text), True
        else:
            if partial_character:
                raise ValueError(f"完整会话之后存在损坏字节，未修改原文件: {self.path}")
        if (not isinstance(data, dict) or not isinstance(data.get("updates"), list)
                or not all(isinstance(data.get(key), str) for key in ("session_id", "project_id", "title", "created_at"))):
            raise ValueError(f"会话结构无效: {self.path}")
        updates = data["updates"]
        committed = False
        for number, update in enumerate(updates, 1):
            if not isinstance(update, dict):
                raise ValueError(f"会话事务结构无效: {self.path}, transaction={number}")
            commit = update.get("commit")
            if (commit is not None and commit != self._commit_tag(update, number)) or (
                commit is None and committed
            ):
                raise ValueError(f"会话事务校验失败，未修改原文件: {self.path}, transaction={number}")
            committed = committed or commit is not None
        return data, repaired


    def _recover(self, text):
        """Produce a candidate only when no later committed batch would be discarded."""
        boundary = re.search(r',\s*"updates"\s*:\s*\[', text)
        if boundary is None:
            raise ValueError(f"会话头部损坏，未修改原文件: {self.path}")
        prefix, tail = text[:boundary.start()], text[boundary.end():]
        data, updates = json.loads(prefix + "}"), []
        decoder, offset = json.JSONDecoder(), 0

        def has_following_commit(start):
            """Find independently verifiable rows, even after a broken quote or brace."""
            value_end = start
            for match in re.finditer(r"[\[{]", tail[start:]):
                cursor = start + match.start()
                if cursor < value_end:
                    continue
                try:
                    candidate, end = decoder.raw_decode(tail, cursor)
                except json.JSONDecodeError:
                    continue
                before = cursor - 1
                while before >= start and tail[before].isspace():
                    before -= 1
                if before >= start and tail[before] == ":":
                    # A complete field value is data, including nested commit-shaped objects.
                    value_end = end
                    continue
                commit = candidate.get("commit") if isinstance(candidate, dict) else None
                if (
                    isinstance(commit, dict)
                    and isinstance(commit.get("sequence"), int)
                    and commit["sequence"] > len(updates)
                    and commit == self._commit_tag(candidate, commit["sequence"])
                ):
                    return True
            return False

        def incomplete(exc):
            """Recognize a valid JSON prefix cut at EOF, not arbitrary invalid syntax."""
            rest = tail[exc.pos:]
            if not rest or exc.msg.startswith("Unterminated string"):
                return True
            if exc.msg.startswith("Invalid \\u"):
                return bool(re.fullmatch(r"u[0-9a-fA-F]{0,3}", rest))
            if exc.msg == "Expecting value":
                return rest in {"t", "tr", "tru", "f", "fa", "fal", "fals", "n", "nu", "nul", "-"}
            return (exc.pos > 0 and tail[exc.pos - 1].isdigit()
                    and rest in {".", "e", "e+", "e-", "E", "E+", "E-"})

        while offset < len(tail):
            while offset < len(tail) and tail[offset].isspace():
                offset += 1
            if not tail[offset:] or re.fullmatch(r"\]\s*\}?\s*", tail[offset:]):
                break
            if updates:
                if tail[offset:offset + 1] != ",":
                    raise ValueError(f"会话事务边界损坏，未修改原文件: {self.path}")
                offset += 1
                while offset < len(tail) and tail[offset].isspace():
                    offset += 1
            try:
                update, offset = decoder.raw_decode(tail, offset)
            except json.JSONDecodeError as exc:
                if has_following_commit(offset) or not incomplete(exc):
                    raise ValueError(f"会话事务损坏，无法确认仅为尾部半写，未修改原文件: {self.path}") from exc
                break
            updates.append(update)
        data["updates"] = updates
        return data


    @staticmethod
    def _commit_tag(update, sequence):
        """Check the whole packed update before any of its fields become visible."""
        payload = {key: value for key, value in update.items() if key != "commit"}
        return dict(sequence=sequence, checksum=hashlib.sha256(_json(payload).encode()).hexdigest())


    def _replace(self, data):
        self._replace_file(self.path, data, workspace=self.workspace)


    @staticmethod
    def _replace_file(path, data, *, workspace=None):
        """Publish initialization or a compacted snapshot only after durable writing."""
        temporary = path.with_suffix(".tmp")
        def write():
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        _write_with_cleanup(path, write, workspace=workspace)


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
        self._inputs.update(update.get("inputs", {}))
        self._contexts.update(update.get("contexts", {}))
        if "context" in update:
            self._contexts[""] = update["context"]
        if delta := update.get("context_delta"):
            self._contexts.setdefault(update.get("agent_id", ""), [])[delta["start"]:] = delta["ids"]
        for key, record in update.get("messages", {}).items():
            previous = self._records.setdefault(key, {"message": {}})
            if previous["message"]:
                digest = hashlib.sha256(_json(previous["message"]).encode()).hexdigest()
                owner = previous.get("agent_id", "")
                if self._digests.get((owner, digest)) == key:
                    del self._digests[owner, digest]
            previous.update({name: value for name, value in record.items() if name != "message"})
            previous["message"].update(record["message"])
            self._digests[previous.get("agent_id", ""), hashlib.sha256(_json(previous["message"]).encode()).hexdigest()] = key
            self._next_id = max(self._next_id, int(key) + 1)
        self._next_id = max(update.get("next_id", 0), self._next_id)


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
        if additions := {key: texts[key] for key in used if snapshot or key not in self._texts}:
            packed["texts"] = additions
        if links:
            packed["text_refs"] = links
        return packed


    def _write_update(self, update):
        """Publish one transaction only after all bytes have been flushed to disk."""
        formatted = StringIO()
        json.dump(update, formatted, ensure_ascii=False, indent=2)
        batch = ((",\n" if self._count else "\n") + indent(formatted.getvalue(), "    ")).encode()
        payload = batch + b"\n  ]\n}\n"
        offset = self._append_offset

        def write():
            with self.path.open("r+b") as stream:
                stream.seek(offset)
                stream.write(payload)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())

        _write_with_cleanup(self.path, write, workspace=self.workspace)
        self._apply(update)
        self._count += 1
        self._last_commit = update["commit"]
        self._saved_version = self._version()
        self._append_offset = offset + len(batch)
        self._pending_update = None


    def retry_pending(self):
        """Settle an uncertain previous write before admitting another transaction."""
        for child in list(self._roles.values()):
            child.retry_pending()
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
