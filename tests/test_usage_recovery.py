"""Recovery scheduling fixtures are not real-model memory acceptance."""

from redlotus.agent_core.memory_service import MemoryJob, MemoryService
from redlotus.infra.persist_utils import read_locked_json
from redlotus.runtime.context import WorkspaceContext
from redlotus.tools.memory.models import PerceptionResult


async def test_recovering_twentieth_turn_seals_once_without_another_input(
    tmp_path, monkeypatch
):
    memory = MemoryService(workspace=WorkspaceContext.from_path(tmp_path))
    for number in range(20):
        event = memory.observations.begin(
            "session", str(number), "scheduling fixture", []
        )
        if number < 19:
            event.status = "success"
            memory.observations.finish(event)
    memory.observations.close()
    calls = []

    async def produce(job):
        calls.append(job.id)
        job.result = PerceptionResult(records=[], reason="No durable information")
        memory._save_job(job)

    monkeypatch.setattr(memory, "_produce", produce)
    try:
        assert len(memory.observations.order()) == 19
        await memory.process_pending(recover=True)
        assert len(calls) == 1
        assert memory.observations.cursor() == 20
        job = MemoryJob.model_validate(
            read_locked_json(next(memory.jobs_dir.glob("*.json")))
        )
        assert len(job.window.new_turn_ids) == 20 and not job.window.overlap_turn_ids
        assert job.events[-1].status == "unverified"
        await memory.process_pending(recover=True)
        assert len(calls) == 1
    finally:
        await memory.close()


async def test_successful_recovery_preserves_original_job_failure(
    tmp_path, monkeypatch
):
    memory = MemoryService(workspace=WorkspaceContext.from_path(tmp_path))
    event = memory.observations.begin("session", "ended", "scheduling fixture", [])
    event.status = "success"
    memory.observations.finish(event)
    memory.seal_windows(flush=True)
    path = next(memory.jobs_dir.glob("*.json"))
    calls = 0

    async def produce(job):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first attempt failed")
        job.result = PerceptionResult(records=[], reason="No durable information")
        memory._save_job(job)

    monkeypatch.setattr(memory, "_produce", produce)
    try:
        await memory.process_pending()
        assert read_locked_json(path)["error"] == "first attempt failed"
        await memory.process_pending(recover=True)
        saved = read_locked_json(path)
        assert saved["done"] and not saved["error"]
        assert saved["failures"][0]["error"] == "first attempt failed"
    finally:
        await memory.close()
