"""Offline durable-window checks, not model-production acceptance."""

from redlotus.agent_core.memory_service import MemoryJob, MemoryService
from redlotus.infra.persist_utils import read_locked_json
from redlotus.runtime.context import WorkspaceContext
from redlotus.tools.memory.models import PerceptionResult


async def test_frozen_target_never_uses_another_gateways_credentials(tmp_path, monkeypatch):
    import pytest
    from dataclasses import asdict, replace
    from redlotus.ModelGateway.model_factory import ModelTarget

    memory = MemoryService(workspace=WorkspaceContext.from_path(tmp_path))
    initial = ModelTarget.for_role("worker")
    snapshot = asdict(initial)
    snapshot.pop("api_key")
    event = memory.observations.begin("session", "turn", "hello", [])
    job = MemoryJob(id="frozen", events=[event], model_snapshot=snapshot)
    changed = replace(initial, base_url="https://other-gateway.invalid", api_key="other-account")
    monkeypatch.setattr(ModelTarget, "for_role", lambda role: changed)
    with pytest.raises(ValueError, match="网关"):
        memory.target_for_job(job)
    await memory.close()


async def test_sealed_window_is_immutable_and_does_not_include_later_turns(
    tmp_path, monkeypatch
):
    memory = MemoryService(workspace=WorkspaceContext.from_path(tmp_path))
    ids = []
    for index in range(3):
        event = memory.observations.begin("session", str(index), "a real user turn", [])
        event.status = "success"
        memory.observations.finish(event)
        ids.append(event.id)
    memory.seal_windows(flush=True, through=3)
    memory.seal_windows(flush=True, through=3)
    jobs = list(memory.jobs_dir.glob("*.json"))
    assert len(jobs) == 1
    job = MemoryJob.model_validate(read_locked_json(jobs[0]))
    assert job.window.new_turn_ids == ids
    assert "api_key" not in job.model_snapshot
    later = memory.observations.begin(
        "session", "later", "must wait for another window", []
    )
    later.status = "success"
    memory.observations.finish(later)
    calls = []

    async def produce(item):
        calls.append(item.id)
        item.result = PerceptionResult(records=[], reason="Nothing durable")
        memory._save_job(item)

    monkeypatch.setattr(memory, "_produce", produce)
    await memory.process_pending(through=3)
    await memory.process_pending(through=3)
    assert calls == [job.id]
    assert memory.observations.cursor() == 3
    assert memory.observations.window(flush=True).new_turn_ids == [later.id]
    await memory.close()
