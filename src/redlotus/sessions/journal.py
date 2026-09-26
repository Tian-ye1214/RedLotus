"""Conversation identity, input ordering, incremental recovery, and saved-session discovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from textwrap import indent

from filelock import FileLock, Timeout

from redlotus.sessions.cleanup import _write_with_cleanup
from redlotus.sessions.context import _CANCELLING_WRITE


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@contextmanager
def _journal_lock(lock):
    """Let the owner loop wait for contended checkpoint locks outside its writer."""
    cancelled = _CANCELLING_WRITE.get()
    was_cancelled = cancelled is not None and cancelled()
    with lock.acquire(blocking=cancelled is None):
        if not was_cancelled and cancelled is not None and cancelled():
            raise Timeout(lock.lock_file)
        yield lock


class SessionJournal:
    """One durable JSON transaction format shared by coordinator and role ledgers."""

    def __init__(self, path, *, lock=None, recover=True, commit_recovery=True, workspace=None):
        self.path = Path(path)
        self.workspace = workspace
        self._lock = lock or FileLock(self.path.with_suffix(".lock"))
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
        with _journal_lock(self._lock):
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
            self._jobs, self._usage, self._inputs = {}, {}, {}
            self._texts, self._resume_links, self._pause_requests, self._message_offsets = {}, {}, {}, {}
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
        """Hold the journal lock and refresh once before reading or updating state."""
        with _journal_lock(self._lock):
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
        for state in update.get("metadata", {}).values():
            if isinstance(state, dict) and (request := state.get("request")) and isinstance(request, dict):
                if (identity := request.get("id")) and (turn := state.get("turn_id")):
                    self._resume_links[turn] = self._pause_requests.setdefault(identity, turn)
        for name, target in (("turns", self._turns), ("jobs", self._jobs)):
            for key, value in update.get(name, {}).items():
                target.setdefault(key, {}).update(value)
                if name == "jobs":
                    if target[key].get("indexed") or target[key].get("superseded_by_turn_count_version"):
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
        self._message_offsets.update(update.get("message_offsets", {}))
        for key, record in update.get("messages", {}).items():
            owner = record.get("turn_id", self._records.get(key, {}).get("turn_id"))
            previous = self._records.setdefault(key, {"message": {}, "source_index": self._message_offsets.get(owner, 0)})
            previous.update({name: value for name, value in record.items() if name != "message"})
            previous["message"].update(record["message"])
            self._message_offsets[owner] = max(self._message_offsets.get(owner, 0), previous["source_index"] + 1)
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
        with _journal_lock(self._lock):
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


    def _completion_projection(self, completed=None, records=None):
        """Project completion and memory eligibility without changing audit identities."""
        turns, records = dict(self._turns), self._records if records is None else records
        if completed:
            turns[completed["id"]] = {**turns.get(completed["id"], {}), **completed,
                                      "final_response_completed": True}
        links, bodies = {turn.get("turn_id", key): turn["logical_turn_id"] for key, turn in turns.items() if turn.get("logical_turn_id")}, {}
        links.update(self._resume_links)
        for record in records.values():
            owner, message = record.get("turn_id"), record["message"]
            if record.get("agent_id"):
                continue
            bodies.setdefault(owner, []).append(message)
            for part in message.get("parts", []):
                for item in part.get("content", []) if isinstance(part.get("content"), list) else []:
                    if isinstance(item, dict) and (item.get("metadata") or {}).get("origin") == "runtime_control":
                        try:
                            control = json.loads(item.get("content", ""))
                        except (ValueError, TypeError):
                            continue
                        if isinstance(control, dict) and control.get("command") == "resume" and control.get("turn_id"):
                            links[owner] = control["turn_id"]
        def root(identity):
            seen = set()
            while identity in links and links[identity] not in seen:
                seen.add(identity)
                identity = links[identity]
            return identity
        groups = {}
        for identity, turn in turns.items():
            groups.setdefault(root(turn.get("turn_id", identity)), []).append(identity)
        sealed = {key for job in self._jobs.values() if job.get("window") and (job.get("done") or job.get("indexed")
                  or {"sealed_at", "records_committed_at"}.intersection(job.get("timings", {}))) for key in job["window"].get("new_turn_ids", [])}
        updates, confirmed, incomplete = {}, [], bool(self._metadata.get("turn_count_incomplete") or
            self._metadata.get("turn_count_version") != 2 and self._metadata.get("completed_turns", 0) > len(turns))
        for logical, ids in groups.items():
            aliases = list(dict.fromkeys([logical, *(turns[key].get("turn_id", key) for key in ids)]))
            candidates, ready = [], False
            for identity in ids:
                turn = turns[identity]
                history = bodies.get(turn.get("turn_id", identity), [])
                final = any(message.get("kind") == "response" and (message.get("metadata") or {}).get("origin") not in {"context_summary", "execution_status", "runtime_control"}
                            and any(part.get("part_kind") == "text" for part in message.get("parts", []))
                            and not any(part.get("part_kind") == "tool-call" for part in message.get("parts", [])) for message in history)
                contradiction = any((message.get("metadata") or {}).get("origin") == "execution_status" for message in history)
                success = turn.get("status") == "success" and turn.get("number", 0) > 0 and not contradiction
                receipt = (turn.get("final_response_completed") or turn.get("completion_number") or
                           self._metadata.get("turn_count_version") != 2 and success and (turn.get("turn_id") or identity in sealed))
                if receipt:
                    candidates.append(identity)
                    ready |= bool(final and "user_inputs" in turn)
                elif turn.get("status") not in {"cancelled", "failed", "needs_input", "running"}:
                    incomplete = True
                updates[identity] = dict(logical_turn_id=logical, audit_turn_ids=aliases, audit_event_ids=ids,
                                         completion_number=None, evidence_ready=False)
            if candidates:
                representative = next((key for key in candidates if turns[key].get("completion_number")), candidates[-1])
                confirmed.append(representative)
                updates[representative].update(evidence_ready=ready, final_response_completed=True)
        confirmed.sort(key=lambda key: (not turns[key].get("completion_number"),
                                        turns[key].get("completion_number") or turns[key].get("number", len(turns) + 1)))
        for ordinal, identity in enumerate(confirmed, 1):
            updates[identity]["completion_number"] = ordinal
        incomplete |= bool(set(bodies) - {turn.get("turn_id", key) for key, turn in turns.items()} - set(links.values()))
        cursors = {key: self._metadata.get(key, 0) for key in ("perception_consumed", "perception_reserved")}
        reopened = min((updates[key]["completion_number"] - 1 for key in confirmed if updates[key]["evidence_ready"]
                        and not self._turns.get(key, {}).get("evidence_ready") and not self._turns.get(key, {}).get("memory_covered")),
                       default=max(cursors.values()))
        return updates, dict(completed_turns=len(confirmed), turn_count_version=2, turn_count_incomplete=incomplete,
                             **{key: min(value, reopened) for key, value in cursors.items()})

    def correct_turn_counts(self):
        """Use one locked transaction for legacy counts, eligibility and job recovery."""
        with self._locked_state():
            if self.recovered_partial_write and not self._commit_recovery:
                self._commit_recovery = True
                self._read()
            if self._metadata.get("turn_count_version") == 2 or self.header.get("role", "coordinator") != "coordinator":
                return False
            turns, metadata = self._completion_projection()
            covered, jobs = set(), {}
            for identity, job in self._jobs.items():
                if not (window := job.get("window")):
                    continue
                committed = bool(job.get("done") or job.get("indexed") or "records_committed_at" in job.get("timings", {}))
                ids = window.get("new_turn_ids", []) if committed else []
                covered.update(ids)
                jobs[identity] = dict(turn_count_migration=dict(committed=committed, covered_turn_ids=ids))
                if not committed:
                    jobs[identity]["superseded_by_turn_count_version"] = 2
            turns = {key: dict(turn, memory_covered=bool(covered.intersection(turn["audit_event_ids"]))) for key, turn in turns.items()}
            consumed = min((turn["completion_number"] - 1 for turn in turns.values()
                            if turn["completion_number"] and turn["evidence_ready"] and not turn["memory_covered"]),
                           default=metadata["completed_turns"])
            metadata.update(perception_consumed=consumed, perception_reserved=consumed,
                            legacy_turn_count={key: self._metadata.get(key, 0) for key in
                                               ("completed_turns", "perception_consumed", "perception_reserved")})
            self._append(dict(turns=turns, jobs=jobs, metadata=metadata))
            return True
