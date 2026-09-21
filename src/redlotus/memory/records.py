"""Memory records, core Markdown and current-session observations."""

from __future__ import annotations
import asyncio
import json
from dataclasses import asdict
from pydantic_ai.messages import BinaryContent, ImageUrl, ModelMessagesTypeAdapter, TextContent
from redlotus.runtime.config import settings
from redlotus.runtime.resources import (
    iso_utc_now,
    memory_dir,
    atomic_write_text,
    file_lock,
    project_data_dir,
)
from redlotus.runtime.network import ModelInputPolicy
from redlotus.core.agents import Outcome
from redlotus.tools.references import ReferenceStore
from redlotus.tools.registry import tool_result_succeeded


from typing import Literal
from pydantic import BaseModel, Field, computed_field
import re
import shutil
from pathlib import Path
import hashlib


class MemoryContent(BaseModel):
    """Fields shared by LLM drafts and persisted memory, defined once."""

    scope: Literal["project", "global"] = "project"
    kind: Literal["episode", "semantic", "requested"] = "episode"
    projection: Literal["none", "profile", "experience"] = "none"
    subject: str = ""
    goal: str
    content: str = ""
    decisions: list[str] = Field(default_factory=list)
    attempts: list[str] = Field(default_factory=list)
    result: str = ""
    unresolved: list[str] = Field(default_factory=list)
    status: Outcome = "unverified"
    source_turn_ids: list[str] = Field(default_factory=list)
    reference_ids: list[str] = Field(default_factory=list)
    behavior_evidence_ids: list[str] = Field(default_factory=list)

    def text(self) -> str:
        return "\n".join(
            part
            for part in [
                self.goal,
                self.content,
                *self.decisions,
                *self.attempts,
                self.result,
                *self.unresolved,
            ]
            if part
        )


class MemoryRecord(MemoryContent):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    project_id: str
    origin: Literal["automatic", "explicit", "legacy"] = "automatic"
    state: Literal["active", "deleted", "superseded"] = "active"
    evidence: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=iso_utc_now)
    updated_at: str = Field(default_factory=iso_utc_now)
    version: int = 1
    last_change_id: str = ""
    source_updated_at: str = ""
    request_created_at: str = ""
    source_memory_ids: list[str] = Field(default_factory=list)
    reference_sources: dict[str, str] = Field(default_factory=dict)

    @computed_field
    @property
    def occurrence_count(self) -> int:
        """Count independent user turns, never duplicate excerpts or overlap."""
        return len({identity.rsplit(":u", 1)[0] for identity in self.behavior_evidence_ids})


class MemoryDraft(MemoryContent):
    action: Literal["create", "update", "delete"] = "create"
    target_id: str | None = None
    core_old_text: str = ""
    source_turn_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    search_id: str = ""
    promotion_basis: str = ""

    def validate_behavior(self, sources, previous=None, *, related=()):
        """Only genuine user evidence can support the recorded behavior count."""
        known = set(previous.behavior_evidence_ids) if previous else set()
        known.update(identity for record in related if record.scope == "project" and record.state == "active"
                     for identity in record.behavior_evidence_ids)
        for identity in set(self.behavior_evidence_ids) - known:
            evidence = sources.get(identity, {})
            if (evidence.get("kind") != "user" or not evidence.get("verified")
                or evidence.get("event_id") not in self.source_turn_ids):
                valid = sorted(known | {key for key, source in sources.items()
                                      if source.get("kind") == "user" and source.get("verified")
                                      and source.get("event_id") in self.source_turn_ids})
                raise ValueError(json.dumps({"error": "memory_behavior_evidence", "invalid": identity, "valid": valid}))

    def validated_scope(self, requested_scope):
        if requested_scope != "auto" and self.scope != requested_scope:
            raise ValueError(json.dumps({"error": "memory_requested_scope", "expected": requested_scope, "actual": self.scope}))
        return self

    def validated_sources(self, current_ids, new_ids, reference_ids, previous=None):
        historical = set(previous.source_turn_ids) if previous else set()
        if set(self.source_turn_ids) - set(current_ids) - historical or not set(
            self.source_turn_ids
        ) & set(new_ids):
            raise ValueError(json.dumps({"error": "memory_turn_sources", "current": list(current_ids), "new": list(new_ids), "actual": self.source_turn_ids}))
        old_refs = set(previous.reference_ids) if previous else set()
        if set(self.reference_ids) - set(reference_ids) - old_refs:
            raise ValueError(json.dumps({"error": "memory_reference_sources", "allowed": list(reference_ids), "actual": self.reference_ids}))
        return self.model_copy(
            update={
                "source_turn_ids": [
                    key for key in self.source_turn_ids if key in current_ids
                ]
            }
        )


class PerceptionResult(BaseModel):
    records: list[MemoryDraft] = Field(default_factory=list)
    reason: str
    request_authorized: bool = False


class WindowManifest(BaseModel):
    id: str
    project_id: str
    new_turn_ids: list[str]
    overlap_turn_ids: list[str] = Field(default_factory=list)
    reference_ids: list[str] = Field(default_factory=list)
    start_position: int
    end_position: int
    reason: Literal["window", "flush", "migration"] = "window"


class ObservedTurn(BaseModel):
    id: str
    project_id: str
    session_id: str
    turn_id: str
    created_at: str = Field(default_factory=iso_utc_now)
    finished_at: str | None = None
    status: Literal[
        "running", "success", "failed", "cancelled", "needs_input", "unverified"
    ] = "running"
    user_inputs: list[str] = Field(default_factory=list)
    reference_ids: list[str] = Field(default_factory=list)
    evidence_paths: list[str] = Field(default_factory=list)
    requested_record_ids: list[str] = Field(default_factory=list)
    error: str = ""
    origin: Literal["user", "legacy", "migration"] = "user"
    inline_messages: list[dict] = Field(default_factory=list)


SECTIONS = ("用户画像", "可复用经验")
EMPTY_MEMORY = "# MEMORY\n\n## 用户画像\n\n## 可复用经验\n"
CREDENTIAL_PATTERN = re.compile(
    r"(?i)(?:\b(?:api[_ -]?key|access[_ -]?token|password|secret|密码|密钥)\s*[:=：]\s*\S+"
    r"|\bsk-[A-Za-z0-9_-]{12,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|Bearer\s+[A-Za-z0-9_.-]{12,})"
)


class LongTermMemory:
    """Editable core profile; complete semantic records belong to LanceDB."""

    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else memory_dir()
        self.path = self.directory / "MEMORY.md"

    def read(self):
        with file_lock(self.path):
            if not self.path.exists():
                sections = {}
                for name, heading in (("USER.md", "用户偏好"), ("SOUL.md", "经验")):
                    old = self.directory / name
                    if old.exists():
                        backup = self.directory / "migration_backup" / name
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        if not backup.exists():
                            shutil.copy2(old, backup)
                        sections[heading] = re.sub(
                            r"^#.*\n", "", old.read_text(encoding="utf-8"), count=1
                        ).strip()
                atomic_write_text(
                    self.path,
                    self._render("# MEMORY", sections)
                    if any(sections.values())
                    else EMPTY_MEMORY,
                )
            return self.path.read_text(encoding="utf-8")

    @staticmethod
    def _parse(body):
        parts = re.split(r"^## ([^\n]+)$", body, flags=re.M)
        if len(parts) == 1:
            raise ValueError("MEMORY.md requires section headings")
        aliases = {"用户偏好": "用户画像", "经验": "可复用经验"}
        sections = {
            aliases.get(name.strip(), name.strip()): text.strip()
            for name, text in zip(parts[1::2], parts[2::2])
        }
        for name in SECTIONS:
            sections.setdefault(name, "")
        return parts[0].rstrip(), sections

    @staticmethod
    def _render(prefix, sections):
        return (
            prefix.rstrip()
            + "\n\n"
            + "\n\n".join(
                f"## {name}\n\n{text}".rstrip() for name, text in sections.items()
            )
            + "\n"
        )

    def get_injection(self):
        return "<core_memory>\n" + self.read() + "\n</core_memory>"

    def apply_record(self, record, previous=None, *, core_old_text=""):
        self.read()
        with file_lock(self.path):
            original = self.path.read_text(encoding="utf-8")
            prefix, sections = self._parse(original)
            marker = re.compile(
                r"<!-- memory:"
                + re.escape(record.id)
                + r" -->\n(.*?)\n<!-- /memory -->",
                re.S,
            )
            match = marker.search(original)
            content = record.content or record.result or record.goal
            if match and previous and record.origin != "explicit":
                old = previous.content or previous.result or previous.goal
                if match.group(1).strip() not in (old.strip(), content.strip()):
                    return False
            sections = {
                name: marker.sub("", text).strip() for name, text in sections.items()
            }
            if record.state == "active" and record.projection != "none":
                if CREDENTIAL_PATTERN.search(content):
                    raise ValueError("Credentials cannot enter core memory")
                name = "用户画像" if record.projection == "profile" else "可复用经验"
                text = sections[name]
                block = f"<!-- memory:{record.id} -->\n{content}\n<!-- /memory -->"
                if core_old_text and text.count(core_old_text) == 1:
                    sections[name] = text.replace(core_old_text, block, 1)
                elif core_old_text and not match and content not in text:
                    raise ValueError("Core memory changed; old text no longer matches")
                elif content not in text:
                    sections[name] = (text + "\n\n" + block).strip()
            elif core_old_text and not match:
                if sum(text.count(core_old_text) for text in sections.values()) > 1:
                    raise ValueError("Core memory text to remove is ambiguous")
                sections = {
                    name: text.replace(core_old_text, "", 1).strip()
                    for name, text in sections.items()
                }
            updated = self._render(prefix, sections)
            if updated != original:
                atomic_write_text(self.path, updated)
            return True

    def legacy_content(self):
        body = self.read()
        if not re.search(r"^## (用户偏好|项目简况|经验)$", body, re.M):
            return ""
        backup = self.directory / "migration_backup/MEMORY-v1.md"
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            backup.write_text(body, encoding="utf-8")
        return body

    def finish_legacy_migration(self, expected):
        with file_lock(self.path):
            prefix, sections = self._parse(self.path.read_text(encoding="utf-8"))
            _, old = self._parse(expected)
            if sections.get("项目简况") == old.get("项目简况"):
                sections.pop("项目简况", None)
            atomic_write_text(self.path, self._render(prefix, sections))

    async def list_memory(self):
        return await asyncio.to_thread(self.read)

    async def snapshot(self):
        body = await self.list_memory()
        return {
            "memory": dict(
                path=self.path,
                body=body,
                chars=len(body),
                empty=body.strip() == EMPTY_MEMORY.strip(),
            )
        }

    async def clear_all(self):
        def clear():
            with file_lock(self.path):
                atomic_write_text(self.path, EMPTY_MEMORY)

        await asyncio.to_thread(clear)


class ObservationStore:
    def __init__(self, workspace, *, window_turns=None, overlap_turns=None):
        config = settings()["memory_perception"]
        self.window_turns = config["window_turns"] if window_turns is None else window_turns
        self.overlap_turns = config["overlap_turns"] if overlap_turns is None else overlap_turns
        if not 0 <= self.overlap_turns < self.window_turns:
            raise ValueError("Memory overlap must be smaller than its window")
        self.workspace = workspace
        self.root = project_data_dir(workspace) / "memory"
        self.session = None

    def bind(self, session):
        """Select an existing session without consuming or producing any memory."""
        if session.project_id != self.workspace.project_id:
            raise ValueError("会话不属于当前项目")
        self.session = session

    def begin(self, session_id, turn_id, text, reference_ids):
        """Register one real user turn; tool and urgent activity update this same event."""
        if self.session is None or self.session.session_id != session_id:
            raise ValueError("感知尚未绑定当前会话")
        identity = hashlib.sha256(f"{session_id}\0{turn_id}".encode()).hexdigest()[:32]
        event = ObservedTurn(id=identity, project_id=self.workspace.project_id,
                             session_id=session_id, turn_id=turn_id,
                             user_inputs=[text], reference_ids=reference_ids)
        self.save(event)
        return event

    def save(self, event):
        """Persist active input and reference associations inside the session file."""
        previous = self.session.turn(event.id)
        value = event.model_dump(mode="json")
        if previous and "number" in previous:
            self.session.update(turns={event.id: {**value, "number": previous["number"]}})
        else:
            self.session.update(metadata={"active_turn": value})

    def finish(self, event):
        """Count exactly one finished outer turn, retaining its actual outcome."""
        event.finished_at = iso_utc_now()
        self.session.finish_turn(event.id, event.model_dump(mode="json"))

    def order(self):
        return [row["id"] for row in self.session.pending_turns(0)] if self.session else []

    def cursor(self):
        return self.session.metadata.get("perception_consumed", 0) if self.session else 0

    def reserved_cursor(self):
        return self.session.metadata.get("perception_reserved", 0) if self.session else 0

    def reserve(self, window):
        self.session.update(metadata={"perception_reserved": window.end_position})

    def read(self, ids):
        return [ObservedTurn.model_validate(self.session.turn(key)) for key in ids]

    def window(self, *, through=None, start=None):
        """Select twenty new finished turns plus context-only overlap from this session."""
        if self.session is None:
            return None
        cursor = self.cursor() if start is None else start
        through = self.session.completed_turns if through is None else through
        end = cursor + self.window_turns
        if through < end:
            return None
        rows = self.session.pending_turns(max(0, cursor - self.overlap_turns))
        events = [row for row in rows if row["number"] <= end]
        fresh = [row["id"] for row in events if row["number"] > cursor]
        overlap = [row["id"] for row in events if row["number"] <= cursor]
        identity = hashlib.sha256(f"{self.session.session_id}\0{cursor}\0{end}".encode()).hexdigest()[:32]
        return WindowManifest(id=identity, project_id=self.workspace.project_id,
                              new_turn_ids=fresh, overlap_turn_ids=overlap,
                              reference_ids=list(dict.fromkeys(ref for row in events for ref in row["reference_ids"])),
                              start_position=cursor, end_position=end)

    def commit(self, window):
        self.session.update(metadata={"perception_consumed": max(self.cursor(), window.end_position)})


class EvidenceReader:
    def __init__(self, references: ReferenceStore):
        self.references = references
        self.session = None

    async def collect(
        self, events: list[ObservedTurn]
    ) -> tuple[list[dict], dict, list]:
        packets = {
            event.id: {
                **event.model_dump(
                    exclude={"project_id", "turn_id", "inline_messages"}
                ),
                "operations": [],
            }
            for event in events
        }
        sources = {}
        refs = {
            key: await self.references.parse(self.references.load(key))
            for key in dict.fromkeys(
                key for event in events for key in event.reference_ids
            )
        }
        for event in events:
            for index, text in enumerate(event.user_inputs):
                sources[f"{event.id}:u{index}"] = dict(
                    event_id=event.id,
                    kind="user" if event.origin == "user" else "legacy_user",
                    text=text,
                    verified=event.origin == "user",
                )
        messages = []
        for event in events:
            history = await asyncio.to_thread(self.session.read_turn, event.turn_id)
            for index, message in enumerate(history):
                if (getattr(message, "metadata", None) or {}).get("origin") == "context_summary":
                    continue
                messages.append((event, str(index), message))
        for event, message_id, message in messages:
            for index, part in enumerate(message.parts):
                kind = getattr(part, "part_kind", "")
                content = getattr(part, "content", getattr(part, "args", ""))
                source_id = f"{event.id}:{message_id}:{index}"
                if isinstance(content, list):
                    texts = []
                    for asset_index, item in enumerate(content):
                        if isinstance(item, str):
                            texts.append(item)
                        elif isinstance(item, ImageUrl):
                            images = packets[event.id].setdefault("image_urls", [])
                            if not any(image["url"] == item.url for image in images):
                                images.append(asdict(item))
                        elif (
                            isinstance(item, TextContent)
                            and (item.metadata or {}).get("origin") == "runtime_control"
                        ):
                            identity = f"{source_id}:{asset_index}"
                            control = dict(
                                id=identity,
                                event_id=event.id,
                                kind="control-return",
                                tool="runtime",
                                text=item.content,
                                verified=True,
                            )
                            packets[event.id]["operations"].append(control)
                            sources[identity] = control
                        elif isinstance(item, BinaryContent):
                            identifier = item.identifier or ""
                            known = identifier[:32]
                            if known in refs:
                                continue
                            reference = await self.references.import_binary(
                                item, source=f"trace:{source_id}:{asset_index}",
                                policy=ModelInputPolicy.for_role(settings()["memory_perception"]["model_role"]),
                            )
                            refs[reference.id] = reference
                            packets[event.id]["reference_ids"] = list(
                                dict.fromkeys(
                                    [*packets[event.id]["reference_ids"], reference.id]
                                )
                            )
                    content = "\n".join(texts)
                elif not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False, default=str)
                if kind == "user-prompt":
                    continue  # Only original user_inputs can authorize preferences.
                tool = getattr(part, "tool_name", "")
                verified = kind == "tool-return" and tool_result_succeeded(
                    getattr(part, "content", None)
                )
                if tool in (
                    "remember",
                    "update_memory",
                    "delete_memory",
                    "read_memory",
                    "search_memory",
                    "list_memory",
                    "search_episodes",
                    "read_episode",
                    "execute_task_with_manager",
                    "execute_task_with_worker",
                    "final_result",
                ):
                    verified = False
                if kind in ("text", "tool-call", "tool-return", "retry-prompt"):
                    evidence = dict(
                        id=source_id,
                        event_id=event.id,
                        kind=kind,
                        tool=tool,
                        text=content,
                        verified=verified,
                    )
                    packets[event.id]["operations"].append(evidence)
                    sources[source_id] = evidence
        return list(packets.values()), sources, list(refs.values())
