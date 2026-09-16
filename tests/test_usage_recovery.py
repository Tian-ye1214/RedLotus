"""Recovery scheduling fixtures are not real-model memory acceptance."""

from redlotus.memory.service import MemoryJob
from memory_helpers import new_memory
from redlotus.core.config import read_locked_json
from redlotus.core.agents import WorkspaceContext
from redlotus.memory.records import PerceptionResult


async def test_reload_at_nineteen_waits_for_the_next_real_turn(tmp_path, monkeypatch):
    memory = new_memory(workspace=WorkspaceContext.from_path(tmp_path))
    for number in range(19):
        event = memory.observations.begin("session", str(number), "scheduling fixture", [])
        event.status = "success"
        memory.observations.finish(event)
    path = memory.session.path
    await memory.close()
    memory = new_memory(workspace=WorkspaceContext.from_path(tmp_path))
    calls = []

    async def produce(service, job):
        calls.append(job.id)
        job.result = PerceptionResult(records=[], reason="No durable information")
        service._save_job(job)

    monkeypatch.setattr("redlotus.memory.service.produce_job", produce)
    try:
        await memory.process_pending(recover=True)
        assert memory.session.path == path and not calls
        assert memory.session.completed_turns == 19
        event = memory.observations.begin("session", "twentieth", "new user input", [])
        event.status = "success"
        memory.observations.finish(event)
        memory.seal_windows()
        await memory.process_pending()
        assert len(calls) == 1 and memory.observations.cursor() == 20
        job = memory.session.job(calls[0])
        assert len(job["window"]["new_turn_ids"]) == 20
        assert job["window"]["overlap_turn_ids"] == []
        await memory.process_pending(recover=True)
        assert len(calls) == 1
    finally:
        await memory.close()


async def test_successful_recovery_preserves_original_job_failure(tmp_path, monkeypatch):
    memory = new_memory(workspace=WorkspaceContext.from_path(tmp_path))
    for number in range(20):
        event = memory.observations.begin("session", str(number), "scheduling fixture", [])
        event.status = "success"
        memory.observations.finish(event)
    memory.seal_windows()
    identity = memory.session.pending_jobs()[0]
    calls = 0

    async def produce(service, job):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first attempt failed")
        job.result = PerceptionResult(records=[], reason="No durable information")
        service._save_job(job)

    monkeypatch.setattr("redlotus.memory.service.produce_job", produce)
    try:
        await memory.process_pending()
        assert memory.session.job(identity)["error"] == "first attempt failed"
        await memory.process_pending(recover=True)
        saved = memory.session.job(identity)
        assert saved["done"] and not saved["error"]
        assert saved["failures"][0]["error"] == "first attempt failed"
    finally:
        await memory.close()
