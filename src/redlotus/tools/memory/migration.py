"""Import legacy sources as observations; only the LLM may create new memory."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import re
from pathlib import Path

from redlotus.infra.persist_utils import save_locked_json, iso_utc_now
from redlotus.tools.memory.models import ObservedTurn
from redlotus.tools.memory.observations import ObservationStore


def migrate_record_files(store, project_root: Path, global_root: Path):
    """Move the previous branch's formal JSON records into LanceDB after backing them up."""
    from redlotus.tools.memory.models import MemoryRecord

    for root in (project_root, global_root):
        marker = root / "migration_backup/records_v3.json"
        if marker.exists():
            continue
        source = root / "records"
        if not source.is_dir():
            continue
        backup = marker.parent / "records-json-v2"
        if not backup.exists():
            shutil.copytree(source, backup)
        known = {row.id for row in store.all(active_only=False)}
        records = [
            MemoryRecord.model_validate_json(path.read_text(encoding="utf-8"))
            for path in source.glob("*.json")
        ]
        store.save([row for row in records if row.id not in known])
        save_locked_json(marker, dict(ids=[row.id for row in records]))


async def migrate_observations(
    store: ObservationStore, *, rag_config: dict | None = None
) -> None:
    from pydantic_ai.messages import ModelMessagesTypeAdapter, UserPromptPart
    from redlotus.tools.conversation_log import read_saved_model_messages_file
    from redlotus.tools.memory.message_text import split_messages_into_turns

    backup = store.root / "migration_backup"
    marker = backup / "observations_v2.json"
    if marker.exists():
        return
    backup.mkdir(parents=True, exist_ok=True)
    for name in ("episodes", "pending", "index_state.json"):
        source, target = store.root / name, backup / name
        if source.exists() and not target.exists():
            if source.is_dir():
                await asyncio.to_thread(shutil.copytree, source, target)
            else:
                await asyncio.to_thread(shutil.copy2, source, target)
    existing = []
    for path in store.turns.glob("*.json"):
        raw = json.loads(path.read_text(encoding="utf-8"))
        existing.append(ObservedTurn.model_validate(raw))
    covered = {
        str(Path(path).resolve()) for event in existing for path in event.evidence_paths
    }
    by_turn = {event.turn_id: event.id for event in existing}
    imported, mapping = [], {}
    source_paths = {
        path.name: path
        for root in (store.root.parent, store.workspace.root / ".redlotus")
        for path in root.glob("*_ModelMessages.json")
    }
    for path in sorted(source_paths.values()):
        if str(path.resolve()) in covered:
            continue
        journal = path.with_name(path.name.replace("_ModelMessages.json", ".jsonl"))
        if journal.exists():
            continue
        messages, meta = await asyncio.to_thread(read_saved_model_messages_file, path)
        if meta.get("agent") != "coordinator":
            continue
        copied = backup / path.name
        if not copied.exists():
            await asyncio.to_thread(shutil.copy2, path, copied)
        for index, turn in enumerate(split_messages_into_turns(messages)):
            identity = hashlib.sha256(f"{path}:{index}".encode()).hexdigest()[:32]
            user_parts = [
                part
                for message in turn
                for part in message.parts
                if isinstance(part, UserPromptPart)
            ]
            created = (
                user_parts[0].timestamp.isoformat() if user_parts else iso_utc_now()
            )
            text = [
                part.content for part in user_parts if isinstance(part.content, str)
            ]
            event = ObservedTurn(
                id=identity,
                project_id=store.workspace.project_id,
                session_id=str(path),
                turn_id=identity,
                user_inputs=text,
                created_at=created,
                finished_at=created,
                status="unverified",
                origin="legacy",
                evidence_paths=[str(path)],
                inline_messages=ModelMessagesTypeAdapter.dump_python(turn, mode="json"),
            )
            store.save(event)
            imported.append(identity)
        covered.add(str(path.resolve()))
    for path in (store.root / "episodes").glob("*.json"):
        raw = json.loads(path.read_text(encoding="utf-8"))
        event_id = by_turn.get(raw.get("turn_id"))
        if event_id:
            mapping[path.stem] = event_id
            continue
        if all(
            str(Path(source).resolve()) in covered for source in raw.get("evidence", [])
        ) and raw.get("evidence"):
            continue
        identity = hashlib.sha256(("legacy-episode:" + str(path)).encode()).hexdigest()[
            :32
        ]
        created = raw.get("created_at") or iso_utc_now()
        event = ObservedTurn(
            id=identity,
            project_id=store.workspace.project_id,
            session_id=str(path),
            turn_id=identity,
            created_at=created,
            finished_at=created,
            status="unverified",
            origin="legacy",
            user_inputs=[
                "既有历史情景（需重新归纳，不能据此推断新的用户偏好）："
                + json.dumps(raw, ensure_ascii=False)
            ],
            evidence_paths=raw.get("evidence", []),
        )
        store.save(event)
        imported.append(identity)
        mapping[path.stem] = identity
    from redlotus.config.app_config import settings
    from redlotus.RAG.storage_path import resolve_lancedb_dir

    config = (
        rag_config
        if rag_config is not None
        else settings().get("short_term_memory", {})
    )
    database = Path(resolve_lancedb_dir(config.get("db_path", "data/rag_lancedb/stm")))
    table_name = config.get("table_name", "conversation_turns")
    table_path = database / (table_name + ".lance")
    unassigned = []
    if table_path.is_dir():
        destination = database / "migration_backup" / table_path.name
        if not destination.exists():
            await asyncio.to_thread(shutil.copytree, table_path, destination)

        def read_rows():
            import lancedb

            return (
                lancedb.connect(str(database))
                .open_table(table_name)
                .to_arrow()
                .to_pylist()
            )

        grouped = {}
        for row in await asyncio.to_thread(read_rows):
            source = str(row.get("source") or "")
            path = Path(source.split("#", 1)[0])
            owner = (
                next(
                    (
                        parent.parent
                        for parent in path.parents
                        if parent.name == ".redlotus"
                    ),
                    None,
                )
                if path.is_absolute()
                else None
            )
            if owner is None:
                unassigned.append(
                    {key: value for key, value in row.items() if key != "vector"}
                )
                continue
            if (
                owner.resolve() != store.workspace.root
                or str(path.resolve()) in covered
            ):
                continue
            key = re.sub(r"#c\d+$", "", source)
            grouped.setdefault(key, []).append(row)
        for source, rows in grouped.items():
            identity = hashlib.sha256(("legacy-index:" + source).encode()).hexdigest()[
                :32
            ]
            text = "\n\n".join(
                str(row.get("text", ""))
                for row in sorted(rows, key=lambda row: row.get("source", ""))
            )
            event = ObservedTurn(
                id=identity,
                project_id=store.workspace.project_id,
                session_id=source,
                turn_id=identity,
                status="unverified",
                origin="legacy",
                finished_at=iso_utc_now(),
                user_inputs=["旧索引中保留的历史内容：\n" + text],
                evidence_paths=[source],
            )
            store.save(event)
            imported.append(identity)
    if unassigned:
        await asyncio.to_thread(
            save_locked_json, backup / "unassigned_legacy_vectors.json", unassigned
        )
    await asyncio.to_thread(
        save_locked_json,
        marker,
        dict(created_at=iso_utc_now(), imported=imported, legacy_mapping=mapping),
    )


def migrate_pending_jobs(memory):
    """Import the previous runtime's requests, receipts and successful production cache."""
    from datetime import datetime, timezone
    from redlotus.agent_core.memory_service import MemoryJob
    from redlotus.tools.memory.models import PerceptionResult, WindowManifest

    root = memory.observations.root
    backup = root / "migration_backup/jobs-v2"
    marker = backup / "imported.json"
    if marker.exists():
        return
    backup.mkdir(parents=True, exist_ok=True)
    for name in ("requests", "production", "receipts"):
        source, destination = root / name, backup / name
        if source.exists() and not destination.exists():
            shutil.copytree(source, destination)

    def read(path, default):
        return (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
        )

    records = {record.id: record for record in memory.store.all(active_only=False)}

    def import_job(job, production, receipt=None):
        if memory._job_path(job).exists():
            return
        path = production / f"{job.id}.json"
        cache = read(path, {})
        if cache:
            job.result = PerceptionResult.model_validate(cache["result"])
            job.sources = cache["sources"]
            job.reference_ids = cache["reference_ids"]
            if job.window:
                job.created_at = (
                    datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            identities = [
                draft.target_id
                or hashlib.sha256(f"{job.id}:{index}".encode()).hexdigest()[:32]
                for index, draft in enumerate(job.result.records)
            ]
            job.bases = {key: records[key] for key in identities if key in records}
        if receipt is not None:
            job.done, job.records = True, receipt.get("ids", [])
        memory._save_job(job)

    imported = []
    for path in sorted((backup / "requests").glob("*.json")):
        request = read(path, {})
        event = ObservedTurn.model_validate(request["event"])
        if event.project_id != memory.workspace.project_id:
            continue  # Preserve the backup without assigning another project's request.
        job = MemoryJob(
            id=request["id"],
            events=[event],
            request=request["request"],
            scope=request["scope"],
            created_at=event.created_at,
        )
        import_job(
            job, backup / "production", read(backup / "receipts" / path.name, None)
        )
        imported.append(job.id)

    # Only the unconsumed cursor can have an unfinished window. A prior short flush
    # may have fewer events than are present now; recover its exact stable window ID.
    order = memory.observations.order()
    while (cursor := memory.observations.cursor()) < len(order):
        overlap = order[max(0, cursor - memory.observations.overlap_turns) : cursor]
        end_limit = min(
            len(order), cursor + memory.observations.window_turns - len(overlap)
        )
        for end in range(end_limit, cursor, -1):
            identity = hashlib.sha256(
                f"{memory.workspace.project_id}\0{cursor}\0{order[end - 1]}".encode()
            ).hexdigest()[:32]
            receipt = read(backup / "receipts" / f"{identity}.json", None)
            if (
                not (backup / "production" / f"{identity}.json").exists()
                and receipt is None
            ):
                continue
            events = memory.observations.read([*overlap, *order[cursor:end]])
            window = WindowManifest(
                id=identity,
                project_id=memory.workspace.project_id,
                start_position=cursor,
                end_position=end,
                new_turn_ids=order[cursor:end],
                overlap_turn_ids=overlap,
                reason="flush" if end < end_limit else "window",
            )
            job = MemoryJob(id=identity, events=events, window=window)
            import_job(job, backup / "production", receipt)
            imported.append(identity)
            if receipt is not None:
                memory.observations.commit(window)
            break
        else:
            break
        if receipt is None:
            break

    global_backup = memory.long_term.directory / "migration_backup"
    old_core = read(global_backup / "core_input_v2.json", None)
    if old_core:
        source = global_backup / "core_source.md"
        if not source.exists():
            source.write_text(old_core["legacy"], encoding="utf-8")
        event = ObservedTurn.model_validate(old_core["event"])
        job = MemoryJob(
            id=event.id,
            events=[event],
            scope="global",
            created_at=event.created_at,
            request="迁移原有记忆：项目与知识存长期记录，画像和通用经验保留核心投影。",
        )
        receipt = read(global_backup / "core_records_v2.json", None)
        import_job(job, global_backup / "core_production", receipt)
        if receipt is not None:
            save_locked_json(global_backup / "core_records_v3.json", {"done": True})
    save_locked_json(marker, {"jobs": imported})
