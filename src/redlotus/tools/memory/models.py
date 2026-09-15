"""The boundary between observed events, LLM-produced changes and stored memory."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from redlotus.infra.persist_utils import iso_utc_now

Outcome = Literal["success", "failed", "cancelled", "needs_input", "unverified"]


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
    status: Outcome = Field(
        default="unverified",
        description="Outcome of the user's goal, not the completion of an Agent turn. Required steps still failed or unverified cannot be success.",
    )
    source_turn_ids: list[str] = Field(default_factory=list)
    reference_ids: list[str] = Field(default_factory=list)

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


class MemoryDraft(MemoryContent):
    action: Literal["create", "update", "delete"] = "create"
    target_id: str | None = None
    core_old_text: str = ""
    source_turn_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)

    def validated_scope(self, requested_scope):
        if requested_scope != "auto" and self.scope != requested_scope:
            raise ValueError(
                f"本次请求只允许 scope={requested_scope}；不要混入其他范围或其他记忆提议。"
            )
        return self

    def validated_sources(self, current_ids, new_ids, reference_ids, previous=None):
        historical = set(previous.source_turn_ids) if previous else set()
        if set(self.source_turn_ids) - set(current_ids) - historical or not set(
            self.source_turn_ids
        ) & set(new_ids):
            raise ValueError(
                "source_turn_ids 必须来自提供的事件，且至少包含一个新增事件；保留旧出处时只能引用目标记录已有的出处。"
            )
        old_refs = set(previous.reference_ids) if previous else set()
        if set(self.reference_ids) - set(reference_ids) - old_refs:
            raise ValueError(
                "reference_ids 只能逐字使用已提供的引用 ID；文件路径和文件名不是引用 ID，没有引用时使用空数组。"
            )
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
