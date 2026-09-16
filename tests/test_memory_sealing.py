"""Offline durable-window checks, not model-production acceptance."""

from redlotus.memory.service import MemoryJob
from redlotus.memory.service import MemoryService
from redlotus.core.config import read_locked_json
from redlotus.core.agents import WorkspaceContext
from redlotus.memory.records import PerceptionResult
from redlotus.core.session import SessionFile


def bound_memory(tmp_path):
    workspace = WorkspaceContext.from_path(tmp_path)
    memory = MemoryService(workspace=workspace)
    memory.bind_session(SessionFile.create(tmp_path / "sessions", workspace.project_id, session_id="session"))
    return memory


async def test_frozen_target_never_uses_another_gateways_credentials(tmp_path, monkeypatch):
    import pytest
    from dataclasses import asdict, replace
    from redlotus.core.gateway import ModelTarget

    memory = bound_memory(tmp_path)
    initial = ModelTarget.for_role("worker")
    snapshot = asdict(initial)
    snapshot.pop("api_key")
    event = memory.observations.begin("session", "turn", "hello", [])
    job = MemoryJob(id="frozen", events=[event], model_snapshot=snapshot)
    changed = replace(initial, base_url="https://other-gateway.invalid", api_key="other-account")
    monkeypatch.setattr(ModelTarget, "for_role", lambda role: changed)
    with pytest.raises(ValueError, match="网关"):
        from redlotus.memory.perception import target_for_job
        target_for_job(job, memory._targets)
    await memory.close()


async def test_sealed_window_is_immutable_and_does_not_include_later_turns(
    tmp_path, monkeypatch
):
    memory = bound_memory(tmp_path)
    ids = []
    for index in range(20):
        event = memory.observations.begin("session", str(index), "a real user turn", [])
        event.status = "success"
        memory.observations.finish(event)
        ids.append(event.id)
    memory.seal_windows(through=20)
    memory.seal_windows(through=20)
    jobs = memory.session.pending_jobs()
    assert len(jobs) == 1
    saved = memory.session.job(jobs[0])
    saved["events"] = memory.observations.read(saved.pop("event_ids"))
    job = MemoryJob.model_validate(saved)
    assert job.window.new_turn_ids == ids
    assert "api_key" not in job.model_snapshot
    later = memory.observations.begin(
        "session", "later", "must wait for another window", []
    )
    later.status = "success"
    memory.observations.finish(later)
    calls = []

    async def produce(service, item):
        calls.append(item.id)
        item.result = PerceptionResult(records=[], reason="Nothing durable")
        memory._save_job(item)

    monkeypatch.setattr("redlotus.memory.service.produce_job", produce)
    await memory.process_pending(through=20)
    await memory.process_pending(through=20)
    assert calls == [job.id]
    assert memory.observations.cursor() == 20
    assert memory.observations.window() is None
    assert not memory.session.pending_jobs()
    await memory.close()
