import pytest

from redlotus.memory.service import MemoryJob
from memory_helpers import new_memory
from redlotus.core.agents import WorkspaceContext
from redlotus.memory.records import MemoryDraft
from redlotus.memory.records import MemoryRecord
from redlotus.memory.records import ObservedTurn
from redlotus.memory.records import PerceptionResult


@pytest.mark.parametrize("old_scope,new_scope", [("project", "global"), ("global", "project")])
async def test_explicit_scope_correction_updates_same_record(tmp_path, monkeypatch, old_scope, new_scope):
    service = new_memory(workspace=WorkspaceContext.from_path(tmp_path))
    previous = MemoryRecord(id="existing", project_id="original-project", scope=old_scope, kind="requested", origin="explicit", projection="profile" if old_scope == "global" else "none", goal="Existing fact", content="A fact with a corrected scope", created_at="2020-01-01T00:00:00Z", updated_at="2020-01-01T00:00:00Z")
    event = ObservedTurn(id="event", project_id=service.workspace.project_id, session_id="session", turn_id="turn", user_inputs=["Correct the scope"], status="success")
    draft = MemoryDraft(action="update", target_id=previous.id, scope=new_scope, kind="requested", projection="none", goal=previous.goal, content=previous.content, source_turn_ids=[event.id])
    job = MemoryJob(id="scope-correction", request="Correct the scope", scope=new_scope, events=[event], bases={previous.id: previous}, result=PerceptionResult(records=[draft], reason="Explicit correction", request_authorized=True))
    service.current = event
    saved = []
    monkeypatch.setattr(service.store, "get", lambda identity: previous)
    monkeypatch.setattr(service.store, "save", lambda records: saved.extend(records))
    if old_scope == "global":
        service.long_term.apply_record(previous)
    try:
        await service._apply(job)
        assert job.records == [previous.id]
        assert saved[0].scope == new_scope
        if new_scope == "project":
            assert saved[0].project_id == service.workspace.project_id
            assert previous.id not in service.long_term.read()
    finally:
        await service.close()
