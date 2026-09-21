from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import ImageUrl, ModelRetry, ToolReturn, capture_run_messages
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import FunctionToolset

from redlotus.memory.records import (
    CREDENTIAL_PATTERN,
    MemoryRecord,
    ObservedTurn,
    PerceptionResult,
    WindowManifest,
)
from redlotus.memory.store import MemoryStore
from redlotus.prompts.prompt import (
    load_prompt,
    window_prompt_content,
    with_runtime_context,
)
from redlotus.runtime.config import get_agent_usage_limits, settings
from redlotus.runtime.network import ModelTarget, create_model
from redlotus.runtime.resources import WorkspaceContext, iso_utc_now
from redlotus.sessions.context import SubagentSpec
from redlotus.tools.references import ReferenceFile, ReferenceStore


class PerceptionTiming(AbstractCapability):
    def __init__(self, record):
        self.record = record

    async def wrap_model_request(self, ctx, request_context, handler):
        started_at, started = iso_utc_now(), time.perf_counter()
        response = None
        try:
            response = await handler(request_context)
            return response
        finally:
            if self.record:
                self.record(
                    dict(
                        started_at=started_at,
                        finished_at=iso_utc_now(),
                        seconds=time.perf_counter() - started,
                        usage=asdict(response.usage) if response else None,
                    )
                )


class MemoryPerception:
    """A factory-owned memory task using the configured sub-Agent model."""

    def __init__(
        self,
        workspace: WorkspaceContext,
        factory,
        registry,
        *, create_agent,
    ):
        self.create_agent = create_agent
        self.workspace, self.factory, self.registry = (
            workspace,
            factory,
            registry,
        )

    async def produce(
        self,
        payload: dict,
        references: list[ReferenceFile],
        *,
        on_usage: Callable | None = None,
        on_search: Callable | None = None,
        on_call: Callable | None = None,
        target: ModelTarget | None = None,
        config: dict | None = None,
        instructions: str | None = None,
        on_messages: Callable | None = None,
    ) -> PerceptionResult:
        payload = deepcopy(payload)
        config = deepcopy(config or settings()["memory_perception"])
        target = target or ModelTarget.for_role(config["model_role"])
        instructions = instructions or load_prompt("memory_perception_system.md")
        spec = SubagentSpec(
            payload["session_id"],
            None,
            self.workspace,
            role="perception",
        )

        async def execute():
            store = MemoryStore(self.workspace)
            reference_store = ReferenceStore(self.workspace)
            existing = {
                row["id"]: MemoryRecord.model_validate(row)
                for row in payload.get("existing_records", [])
            }
            current_ids = set(payload["new_turn_ids"]) | set(
                payload.get("overlap_turn_ids", [])
            )
            searches = []
            sources = {
                operation["id"]: operation["text"]
                for event in payload["events"]
                for operation in event.get("operations", [])
            }
            user_sources = {}
            for event in payload["events"]:
                event["user_evidence_ids"] = [f"{event['id']}:u{index}" for index in range(len(event["user_inputs"]))]
                user_sources.update((identity, dict(
                    event_id=event["id"], kind="user", verified=event.get("origin", "user") == "user",
                )) for identity in event["user_evidence_ids"])
                sources.update(zip(event["user_evidence_ids"], event["user_inputs"]))
            references_by_id = {ref.id: ref for ref in references}

            async def search(query, scope):
                revision = await asyncio.to_thread(store.revision, scope)
                rows = await store.search(query, scope)
                retrieval_error = store.retrieval_error
                changed = revision != await asyncio.to_thread(store.revision, scope)
                existing.update((row.id, row) for row in rows)
                receipt = dict(
                        query=query,
                        scope=scope,
                        revision=revision,
                        ids=[row.id for row in rows],
                        retrieval_error=retrieval_error or ("Memory changed during search." if changed else ""),
                )
                receipt["id"] = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()[:32]
                searches.append(receipt)
                if on_search:
                    on_search(rows, receipt)
                return dict(
                    search_id=receipt["id"],
                    records=[row.model_dump(mode="json") for row in rows],
                    retrieval_error=receipt["retrieval_error"],
                )

            async def search_memory(
                query: str = "", scope: Literal["project", "global"] = "global",
                id: str | None = None,
            ) -> dict:
                """Search existing memories or read a known record before proposing production.

                Search global L2 before every L2 insert or update. Similar records must be
                updated under their existing IDs; a retrieval error is not evidence of absence.

                Args:
                    query: The full semantic query describing the memory to match.
                    scope: project for current-project L1, or global for owner-authorized L2.
                    id: An existing memory ID to read instead of searching.

                Returns:
                    Full records and search evidence, or an explicit retrieval error."""
                if id is not None:
                    row = await read_memory(id)
                    if row["scope"] != scope:
                        raise ModelRetry(json.dumps({"error": "memory_wrong_scope", "expected": scope, "actual": row["scope"]}))
                    return row
                return await search(query, scope)

            async def read_memory(id: str) -> dict:
                """Read a known permitted memory ID, including an explicitly supplied project record."""
                if id not in existing:
                    try:
                        row = await asyncio.to_thread(store.get, id)
                    except (KeyError, ValueError) as exc:
                        raise ModelRetry(json.dumps({"error": "memory_not_found", "id": id})) from exc
                    existing[id] = row
                    if on_search:
                        on_search(
                            [row],
                            dict(operation="read", query=id, scope=row.scope, ids=[id]),
                        )
                return existing[id].model_dump(mode="json")

            async def read_evidence(id: str) -> dict:
                """Read a complete source from this fixed perception window.

                Args:
                    id: An evidence ID provided in the current window.

                Returns:
                    The source ID and full evidence text; unknown IDs are rejected."""
                if id not in sources:
                    raise ModelRetry(json.dumps({"error": "memory_evidence_id", "id": id}))
                return dict(id=id, text=sources[id])

            async def read_reference(id: str, part: int = 0):
                """Read original text or media from a reference attached to this perception window.

                Args:
                    id: A reference ID associated with the current window.
                    part: The zero-based reference part to retrieve.

                Returns:
                    The original part, source locator and total part count; unavailable parts are rejected."""
                if id not in references_by_id:
                    raise ModelRetry(json.dumps({"error": "memory_reference_id", "id": id}))
                ref = await reference_store.parse(references_by_id[id])
                if not 0 <= part < len(ref.parts):
                    raise ModelRetry(json.dumps({"error": "memory_reference_part", "part": part}))
                item = ref.parts[part]
                if item.kind == "text":
                    return dict(
                        id=id,
                        name=ref.name,
                        locator=item.locator,
                        part=part,
                        parts=len(ref.parts),
                        text=item.text,
                    )
                return ToolReturn(
                    return_value=dict(
                        id=id, name=ref.name, part=part, parts=len(ref.parts)
                    ),
                    content=ref.model_copy(update={"parts": [item]}).to_prompt(),
                )

            def validate(result):
                proposed = set()
                for draft in result.records:
                    key = (draft.scope, draft.target_id) if draft.target_id else (
                        draft.scope, (draft.subject or draft.goal).strip().casefold()
                    )
                    if (draft.scope == "global" or draft.target_id) and key in proposed:
                        raise ModelRetry(json.dumps({"error": "memory_duplicate_proposal", "target": key}))
                    proposed.add(key)
                production = [row for row in result.records if row.action != "delete"]
                if production and not searches:
                    raise ModelRetry(json.dumps({"error": "memory_search_first", "required_tool": "search_memory"}))
                if any(
                    not any(
                        query["scope"] == row.scope
                        for query in searches
                    )
                    for row in production
                ):
                    raise ModelRetry(json.dumps({"error": "memory_search_scope", "required_scopes": sorted({row.scope for row in production})}))
                for draft in result.records:
                    if draft.action != "create" and draft.target_id not in existing and not draft.core_old_text:
                        raise ModelRetry(json.dumps({"error": "memory_read_target", "id": draft.target_id, "required_tool": "search_memory"}))
                    if draft.scope == "global" and draft.action != "delete":
                        receipt = next((query for query in searches if query["id"] == draft.search_id), None)
                        if not receipt or receipt["scope"] != "global" or receipt["retrieval_error"]:
                            raise ModelRetry(json.dumps({"error": "memory_l_two_search", "required_tool": "search_memory", "scope": "global"}))
                        if draft.action == "create" and draft.subject.strip() and any(
                            row.scope == "global" and row.state == "active"
                            and row.subject.strip().casefold() == draft.subject.strip().casefold()
                            for row in existing.values()
                        ):
                            raise ModelRetry(json.dumps({"error": "memory_duplicate", "subject": draft.subject, "required_action": "update"}))
                    if payload.get("operation") in ("update", "delete") and (
                        draft.action != payload["operation"] or draft.target_id != payload["target_id"]
                    ):
                        raise ModelRetry(json.dumps({"error": "memory_target_operation", "expected_action": payload["operation"], "expected_id": payload["target_id"]}))
                    if payload.get("operation") == "remember" and draft.action == "delete":
                        raise ModelRetry(json.dumps({"error": "memory_remember_operation", "actual_action": draft.action}))
                allowed = [
                    row
                    for row in result.records
                    if row.action == "delete"
                    or not CREDENTIAL_PATTERN.search(row.subject + "\n" + row.text())
                ]
                if len(allowed) != len(result.records):
                    result.records = allowed
                    result.reason = "Credentials cannot enter persistent memory; other eligible facts can be saved separately."
                    if not allowed:
                        result.request_authorized = False
                if (
                    payload["mode"] == "explicit_request"
                    and not result.records
                    and any(
                        CREDENTIAL_PATTERN.search(text)
                        for event in payload["events"]
                        for text in event["user_inputs"]
                    )
                ):
                    result.request_authorized = False
                    result.reason = "Credentials cannot enter persistent memory."
                if (
                    payload["mode"] == "explicit_request"
                    and result.request_authorized
                    and not result.records
                ):
                    raise ModelRetry(json.dumps({"error": "memory_explicit_record", "records": []}))
                try:
                    result.records = [
                        row.validated_scope(
                            payload.get("requested_scope", "auto")
                        ).validated_sources(
                            current_ids,
                            payload["new_turn_ids"],
                            [ref.id for ref in references],
                            existing.get(row.target_id),
                        )
                        for row in result.records
                    ]
                    for draft in result.records:
                        draft.validate_behavior(user_sources, existing.get(draft.target_id), related=existing.values())
                except ValueError as exc:
                    raise ModelRetry(str(exc)) from exc
                return result

            async def run():
                tools = [search_memory, read_evidence, read_reference]
                agent = self.create_agent(
                    target,
                    instructions=instructions,
                    output_type=PerceptionResult,
                    role="perception",
                    usage_category="auxiliary",
                    capabilities=[PerceptionTiming(on_call)],
                    toolsets=[FunctionToolset(tools)],
                )
                agent.output_validator(validate)
                with capture_run_messages() as messages:
                    try:
                        result = await agent.run(
                            with_runtime_context(
                                [window_prompt_content(payload), *(
                                    ImageUrl(**image)
                                    for event in payload["events"]
                                    for image in event.get("image_urls", [])
                                )]
                            ),
                            usage_limits=get_agent_usage_limits(),
                        )
                    finally:
                        if on_messages:
                            on_messages(messages)
                if on_usage:
                    await asyncio.to_thread(
                        on_usage, {"model": target.name, **asdict(result.usage)}
                    )
                return result.output

            try:
                return await run()
            finally:
                await store.close()

        agent_id = await self.registry.ensure_agent(spec.session_id, "perception")
        return await self.registry.run(
            factory=lambda: (
                self.factory.run(spec, execute) if self.factory else execute()
            ),
            agent_id=agent_id,
            turn_id=None,
        )


class MemoryJob(BaseModel):
    id: str
    created_at: str = Field(default_factory=iso_utc_now)
    events: list[ObservedTurn]
    window: WindowManifest | None = None
    request: str | None = None
    scope: str = "auto"
    operation: str | None = None
    target_id: str | None = None
    result: PerceptionResult | None = None
    sources: dict = Field(default_factory=dict)
    reference_ids: list[str] = Field(default_factory=list)
    bases: dict[str, MemoryRecord] = Field(default_factory=dict)
    core_snapshot: str | None = None
    model_snapshot: dict = Field(default_factory=dict)
    perception_config: dict = Field(default_factory=dict)
    prompt_snapshot: str = ""
    timings: dict[str, str] = Field(default_factory=dict)
    searches: list[dict] = Field(default_factory=list)
    model_calls: list[dict] = Field(default_factory=list)
    elapsed: dict[str, float] = Field(default_factory=dict)
    usage: list[dict] = Field(default_factory=list)
    records: list[str] = Field(default_factory=list)
    done: bool = False
    error: str = ""
    failures: list[dict] = Field(default_factory=list)
    blocked_recipe: str = ""


def target_for_job(job, targets):
    if job.id in targets:
        return targets[job.id]
    config = job.perception_config or settings()["memory_perception"]
    current = ModelTarget.for_role(config["model_role"])
    if not job.model_snapshot:
        return current
    snapshot = dict(job.model_snapshot)
    if snapshot["base_url"] != current.base_url:
        raise ValueError(
            "待恢复感知的网关与当前配置不同；请恢复原网关配置后重试，不能混用其他网关的凭据。"
        )
    if snapshot["protocol"] != current.protocol:
        same_target = (
            snapshot["name"] == current.name
            and snapshot["timeout"] == current.timeout
            and json.loads(snapshot["options_json"]) == json.loads(current.options_json)
        )
        previous = ModelTarget(**snapshot, api_key=current.api_key)
        if not same_target or type(create_model(previous)) is not type(create_model(current)):
            raise ValueError("待恢复感知的模型协议或参数已变化；不能当作标识格式更正恢复。")
        snapshot["protocol"] = current.protocol
    return ModelTarget(**snapshot, api_key=current.api_key)


async def produce_job(service, job):
    config = job.perception_config or settings()["memory_perception"]
    job.timings["preparing_at"] = iso_utc_now()
    service._save_job(job)
    packets, job.sources, references = await service.evidence.collect(job.events)
    job.reference_ids = [ref.id for ref in references]
    # Tombstones prevent forgotten facts being recreated; active records are retrieved by the Agent.
    inactive = await asyncio.to_thread(service.store.all, active_only=False)
    job.bases.update((row.id, row) for row in inactive if row.state != "active")
    if job.target_id:
        target_record = await asyncio.to_thread(service.store.get, job.target_id)
        job.bases[target_record.id] = target_record
    job.core_snapshot = await service.long_term.list_memory()
    payload = dict(
        mode="migration"
        if job.events[0].origin == "migration"
        else "explicit_request"
        if job.request is not None
        else "perception",
        previous_error=job.error,
        project=str(service.workspace.root),
        session_id=service.session.session_id,
        events=packets,
        existing_records=[
            row.model_dump(mode="json") for row in job.bases.values()
        ],
        new_turn_ids=job.window.new_turn_ids
        if job.window
        else [event.id for event in job.events],
        overlap_turn_ids=job.window.overlap_turn_ids if job.window else [],
        core_memory=service.long_term.get_injection(inactive, body=job.core_snapshot),
        explicit_request=job.request,
        requested_scope=job.scope,
        operation=job.operation,
        target_id=job.target_id,
        memory_cleared_at={
            scope: service._cleared_at(scope) for scope in ("project", "global")
        },
        references=[
            dict(
                id=ref.id,
                name=ref.name,
                source=ref.source,
                media_type=ref.media_type,
            )
            for ref in references
        ],
    )

    def retrieved(rows, search):
        job.bases.update((row.id, row) for row in rows)
        job.searches.append(search)
        service._save_job(job)

    def usage(value):
        job.usage.append(value)
        service._save_job(job)

    def model_call(value):
        job.model_calls.append(value)
        service._save_job(job)

    target = target_for_job(job, service._targets)
    job.timings["model_started_at"] = iso_utc_now()
    service._save_job(job)
    result = await service.perception.produce(
        payload,
        references,
        on_usage=usage,
        on_search=retrieved,
        on_call=model_call,
        target=target,
        config=config,
        instructions=job.prompt_snapshot or None,
        on_messages=lambda messages: service.session.record_usage(
            messages, role="perception", invocation=job.id
        ),
    )
    if job.request is not None and not result.request_authorized:
        job.result, job.done = result, True
        service._save_job(job)
        return
    if job.request is not None and not result.records:
        raise ValueError(result.reason)
    job.result = result
    job.timings["produced_at"] = iso_utc_now()
    service._save_job(job)
