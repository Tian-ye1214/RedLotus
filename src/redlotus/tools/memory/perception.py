from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime
from typing import Callable

from pydantic import BaseModel, Field
from pydantic_ai import ModelRetry, PromptedOutput, capture_run_messages

from redlotus.ModelGateway.agent_factory import create_agent
from redlotus.ModelGateway.model_factory import ModelTarget
from redlotus.prompt import with_runtime_context, load_prompt
from redlotus.ModelGateway.ModelChecker import get_effective_max_context_async
from redlotus.config.app_config import get_agent_usage_limits, settings
from redlotus.ModelGateway.input_policy import ModelInputPolicy
from redlotus.runtime.lifecycle import (
    AgentRegistry,
)
from redlotus.runtime.subagents import SubagentFactory, SubagentSpec
from redlotus.runtime.context import WorkspaceContext
from redlotus.references.models import ReferenceFile
from redlotus.tools.memory.models import PerceptionResult, MemoryRecord
from redlotus.tools.memory.ltm import CREDENTIAL_PATTERN
from redlotus.tools.conversation_log import ConversationLog


class PerceivedFragment(BaseModel):
    observations: list[str] = Field(default_factory=list)
    source_turn_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    reference_ids: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


class MemoryPerception:
    """A factory-owned memory task using the configured sub-Agent model."""

    def __init__(
        self,
        workspace: WorkspaceContext,
        factory: SubagentFactory,
        registry: AgentRegistry,
    ):
        self.workspace, self.factory, self.registry = (
            workspace,
            factory,
            registry,
        )

    async def produce(
        self,
        job_id: str,
        payload: dict,
        references: list[ReferenceFile],
        *,
        on_fragment: Callable | None = None,
        fragments: list[dict] | None = None,
        on_usage: Callable | None = None,
    ) -> PerceptionResult:
        config = settings()["memory_perception"]
        target = ModelTarget.for_role(config["model_role"])
        instructions = load_prompt("memory_perception_system.md")
        context_limit = await get_effective_max_context_async(
            target.name, role=config["model_role"], context=target.context
        )
        fragment_prompt = load_prompt("memory_perception_fragment.md")
        overhead = max(len(instructions), len(fragment_prompt)) + len(
            json.dumps(PerceptionResult.model_json_schema())
        )
        budget = min(
            int(context_limit * config["input_context_ratio"]),
            context_limit - int(target.settings.get("max_tokens") or 0) - overhead,
        )
        if budget <= config["fragment_min_chars"]:
            raise ValueError(
                "Perception context cannot fit its instructions and configured output budget."
            )
        policy = ModelInputPolicy.from_limits(target.limits)
        material = self._material(
            payload, references, budget, policy, config["fragment_min_chars"]
        )
        spec = SubagentSpec(
            f"memory:{self.workspace.project_id}",
            None,
            self.workspace,
            role="perception",
        )

        async def execute():
            trace = ConversationLog(
                "perception",
                datetime.now().strftime("%Y-%m-%d"),
                job_id,
                workspace=self.workspace,
            )
            existing = {
                row["id"]: MemoryRecord.model_validate(row)
                for row in payload.get("existing_records", [])
            }
            current_ids = set(payload["new_turn_ids"]) | set(
                payload.get("overlap_turn_ids", [])
            )

            def validate(result):
                allowed = [
                    row
                    for row in result.records
                    if row.action == "delete"
                    or not CREDENTIAL_PATTERN.search(row.subject + "\n" + row.text())
                ]
                if len(allowed) != len(result.records):
                    result.records = allowed
                    result.reason = (
                        "凭据不会进入持久记忆；其他符合要求的内容可独立保存。"
                    )
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
                    result.reason = "凭据不会进入持久记忆。"
                try:
                    result.records = [
                        row.validated_sources(
                            current_ids,
                            payload["new_turn_ids"],
                            [ref.id for ref in references],
                            existing.get(row.target_id),
                        )
                        for row in result.records
                    ]
                except ValueError as exc:
                    raise ModelRetry(str(exc)) from exc
                return result

            async def run(content, output_type):
                prompt = (
                    fragment_prompt
                    if output_type is PerceivedFragment
                    else instructions
                )
                agent = create_agent(
                    target,
                    instructions=prompt,
                    output_type=PromptedOutput(output_type),
                    role="perception",
                )
                if output_type is PerceptionResult:
                    agent.output_validator(validate)
                with capture_run_messages() as messages:
                    try:
                        result = await agent.run(
                            with_runtime_context(content),
                            usage_limits=get_agent_usage_limits(),
                        )
                    finally:
                        await trace.save(
                            messages,
                            extra={
                                "kind": "perception",
                                "job_id": job_id,
                                "model_role": config["model_role"],
                            },
                        )
                if on_usage:
                    await asyncio.to_thread(
                        on_usage, {"model": target.name, **asdict(result.usage)}
                    )
                return result.output

            if len(material) == 1:
                return await run(material[0], PerceptionResult)
            collected = list(fragments or [])
            for index, batch in enumerate(material):
                if index >= len(collected):
                    result = await run(batch, PerceivedFragment)
                    collected.append(result.model_dump())
                    if on_fragment:
                        await asyncio.to_thread(on_fragment, collected)
            context = {key: value for key, value in payload.items() if key != "events"}
            return await run(
                json.dumps(
                    {**context, "read_fragments": collected}, ensure_ascii=False
                ),
                PerceptionResult,
            )

        agent_id = await self.registry.ensure_agent(spec.session_id, "perception")
        return await self.registry.run(
            factory=lambda: self.factory.run(spec, execute),
            agent_id=agent_id,
            turn_id=None,
        )

    @staticmethod
    def _material(payload, references, budget, policy, minimum_fragment_chars):
        header = json.dumps(
            {k: v for k, v in payload.items() if k != "events"}, ensure_ascii=False
        )
        groups, current = [], [header]
        used, files, size = len(header), 0, 0
        for event in payload.get("events", []):
            text = json.dumps(event, ensure_ascii=False)
            # Split only model input, not the resulting memory; every segment retains its source id.
            step = max(minimum_fragment_chars, budget - len(header))
            for offset in range(0, len(text), step):
                part = (
                    f"【原始事件 {event['id']} / 片段 {offset // step + 1}】\n"
                    + text[offset : offset + step]
                )
                if len(current) > 1 and used + len(part) > budget:
                    groups.append(current)
                    current, used, files, size = [header], len(header), 0, 0
                current.append(part)
                used += len(part)
        for reference in references:
            content = reference.to_prompt()
            count = sum(len(item) for item in content if isinstance(item, str))
            maximum = policy.max_request_bytes or policy.max_file_bytes
            if len(current) > 1 and (
                files >= policy.max_files
                or size + reference.byte_size > maximum
                or used + count > budget
            ):
                groups.append(current)
                current, used, files, size = [header], len(header), 0, 0
            current.extend(content)
            files += 1
            size += reference.byte_size
            used += count
        groups.append(current)
        return groups
