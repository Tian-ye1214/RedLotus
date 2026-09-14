from __future__ import annotations

import asyncio
import json
import mimetypes
from pathlib import Path

from pydantic_ai.messages import BinaryContent, ModelMessagesTypeAdapter, TextContent

from redlotus.config.app_config import settings
from redlotus.infra.paths import project_data_dir
from redlotus.ModelGateway.input_policy import ModelInputPolicy
from redlotus.references.store import ReferenceStore
from redlotus.runtime.tool_telemetry import tool_result_succeeded
from redlotus.tools.memory.models import ObservedTurn


class EvidenceReader:
    def __init__(self, references: ReferenceStore):
        self.references = references

    async def collect(
        self, events: list[ObservedTurn]
    ) -> tuple[list[dict], dict, list]:
        by_turn = {event.turn_id: event for event in events}
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
        paths = set()
        for event in events:
            for path in event.evidence_paths:
                journal = Path(path.replace("_ModelMessages.json", ".jsonl"))
                legacy = self.references.workspace.root / ".redlotus"
                if not journal.is_file() and journal.is_relative_to(legacy):
                    journal = project_data_dir(
                        self.references.workspace
                    ) / journal.relative_to(legacy)
                if journal.suffix == ".jsonl" and journal.is_file():
                    paths.add(journal)
            for index, text in enumerate(event.user_inputs):
                sources[f"{event.id}:u{index}"] = dict(
                    event_id=event.id,
                    kind="user" if event.origin == "user" else "legacy_user",
                    text=text,
                    verified=event.origin == "user",
                )
        messages = []
        for event in events:
            for index, raw in enumerate(event.inline_messages):
                messages.append(
                    (
                        event,
                        f"legacy-{index}",
                        ModelMessagesTypeAdapter.validate_python([raw])[0],
                    )
                )
        for path in sorted(paths):
            lines = await asyncio.to_thread(path.read_text, encoding="utf-8")
            for line in lines.splitlines():
                row = json.loads(line)
                event = by_turn.get(row.get("meta", {}).get("turn_id"))
                if (
                    event is None
                    or row.get("meta", {}).get("origin") == "context_summary"
                    or (
                        event.origin == "user"
                        and row.get("meta", {}).get("agent") != "coordinator"
                    )
                ):
                    continue
                messages.append(
                    (
                        event,
                        row["event_id"],
                        ModelMessagesTypeAdapter.validate_python([row["message"]])[0],
                    )
                )
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
                            try:
                                registered = self.references.load(known)
                            except (ValueError, FileNotFoundError):
                                registered = None
                            if registered is not None:
                                reference = await self.references.parse(registered)
                            else:
                                extension = (
                                    mimetypes.guess_extension(item.media_type) or ".bin"
                                )
                                reference = await self.references.import_bytes(
                                    item.data,
                                    name="media" + extension,
                                    source=f"trace:{source_id}:{asset_index}",
                                    policy=ModelInputPolicy.for_role(
                                        settings()["memory_perception"]["model_role"]
                                    ),
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
