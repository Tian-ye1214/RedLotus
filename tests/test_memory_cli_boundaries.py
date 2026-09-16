"""Regression coverage for CLI memory boundaries and retained tool output."""

from pathlib import Path

import pytest

from redlotus.core.agents import AgentRunPolicy, WorkspaceContext, workspace_context
from redlotus.core.session import SessionFile
from redlotus.memory.perception import MemoryJob
from redlotus.memory.records import LongTermMemory, MemoryRecord
from redlotus.memory.service import MemoryService
from redlotus.tools.registry import _model_result, tool_result_succeeded
from redlotus.tools.toolkit import BasicToolkit


async def test_unbind_session_drops_old_runtime_and_blocks_old_job_retry(
    tmp_path, monkeypatch
):
    """Removing a session must make its pending job unreachable to CLI retry."""
    workspace = WorkspaceContext.from_path(tmp_path / "project")
    memory = MemoryService(workspace=workspace)
    memory.long_term = LongTermMemory(tmp_path / "global")
    session = SessionFile.create(
        tmp_path / "sessions", workspace.project_id, session_id="old-session"
    )
    try:
        memory.bind_session(session)
        memory.reset_injection_snapshot("captured prompt snapshot")
        event = await memory.begin_turn(
            "old-session", "turn", "old input", references=[]
        )
        memory._save_job(MemoryJob(id="old-job", events=[event]))
        memory._processor({"error": "old memory failure"})
        memory._context_notices.append(["old notice"])
        memory.last_error = "old memory failure"
        memory._pending_end = 7
        memory._targets["old-job"] = object()
        memory._background = object()
        memory._background_running = True

        memory.unbind_session()

        assert memory.session is None
        assert memory.current is None
        assert memory.observations.session is None
        assert memory.evidence.session is None
        assert memory._background is None
        assert not memory._background_running
        assert memory._pending_end == 0
        assert memory._targets == {}
        assert memory.last_error == ""
        assert memory.take_context_notices() == []
        assert memory.injection_for_session() == "captured prompt snapshot"

        async def old_job_must_not_run(*args, **kwargs):
            raise AssertionError("unbound session retried an old memory job")

        monkeypatch.setattr(memory, "_execute", old_job_must_not_run)
        memory.schedule_processing()
        await memory.process_pending(recover=True)
    finally:
        await memory.close()


async def test_unbind_session_keeps_global_and_project_memory(tmp_path):
    """Session teardown must not delete persisted memory belonging to its workspace."""
    workspace = WorkspaceContext.from_path(tmp_path / "project")
    memory = MemoryService(workspace=workspace)
    memory.long_term = LongTermMemory(tmp_path / "global")
    session = SessionFile.create(
        tmp_path / "sessions", workspace.project_id, session_id="old-session"
    )
    project_record = MemoryRecord(
        id="project-record",
        project_id=workspace.project_id,
        goal="project detail",
        content="keep project memory",
        last_change_id="project-save",
    )
    global_record = MemoryRecord(
        id="global-record",
        project_id=workspace.project_id,
        scope="global",
        goal="global preference",
        content="keep global memory",
        last_change_id="global-save",
    )
    try:
        memory.bind_session(session)
        memory.store.save([project_record, global_record])
        memory.long_term.path.parent.mkdir(parents=True, exist_ok=True)
        memory.long_term.path.write_text("persisted core memory", encoding="utf-8")

        memory.unbind_session()

        assert memory.store.get(project_record.id).content == "keep project memory"
        assert memory.store.get(global_record.id).content == "keep global memory"
        assert memory.long_term.path.read_text(encoding="utf-8") == "persisted core memory"
    finally:
        await memory.close()


@pytest.mark.parametrize(
    ("method", "identity"),
    [
        ("read_memory", "invalid id!"),
        ("read_memory", "missing-record"),
        ("read_episode", "invalid id!"),
        ("read_episode", "missing-record"),
    ],
)
async def test_memory_reader_returns_recoverable_error_for_unreadable_ids(
    tmp_path, method, identity
):
    """Bad tool IDs must report a tool error instead of breaking the model call."""
    memory = MemoryService(workspace=WorkspaceContext.from_path(tmp_path / "project"))
    try:
        result = await getattr(memory.reader, method)(identity)
        assert isinstance(result, str)
        assert result.startswith("Error:")
    finally:
        await memory.close()


async def test_memory_reader_reports_unavailable_when_owner_is_not_authorized(tmp_path):
    """A non-owner receives a recoverable tool result for both direct-read tools."""
    memory = MemoryService(
        workspace=WorkspaceContext.from_path(tmp_path / "project"),
        owner_memory_allowed=False,
    )
    try:
        assert (await memory.reader.read_memory("known-record")).startswith("Error:")
        assert (await memory.reader.read_episode("known-record")).startswith("Error:")
    finally:
        await memory.close()


@pytest.mark.parametrize("method", ["read_memory", "read_episode"])
async def test_memory_reader_propagates_unexpected_storage_errors(
    tmp_path, monkeypatch, method
):
    """Only expected authorization and lookup errors are converted to tool results."""
    memory = MemoryService(workspace=WorkspaceContext.from_path(tmp_path / "project"))

    def unavailable(identity):
        raise RuntimeError(f"storage failed for {identity}")

    monkeypatch.setattr(memory.store, "get", unavailable)
    try:
        with pytest.raises(RuntimeError, match="storage failed"):
            await getattr(memory.reader, method)("record")
    finally:
        await memory.close()


def test_long_tool_result_stays_in_owning_workspace_workdatabase(tmp_path, monkeypatch):
    """A retained result stays readable through the owner project's normal file tool."""
    owner = WorkspaceContext.from_path(tmp_path / "owner-project")
    foreign = WorkspaceContext.from_path(tmp_path / "foreign-project")
    foreign.root.mkdir()
    monkeypatch.setattr("redlotus.core.agents._workspace", foreign.root)
    original = "tool output\nExit code: 1\n" + "detail\n" * 100

    with workspace_context(owner):
        preview = _model_result(original, AgentRunPolicy(3, 40, 60))

    assert preview.startswith("Error: tool reported a business failure.")
    assert not tool_result_succeeded(preview)
    path = Path(preview.rsplit("Full original tool result: ", 1)[1])
    assert path.is_relative_to(owner.root / "WorkDatabase" / "tool_results")
    assert not path.is_relative_to(foreign.root)
    assert path.read_text(encoding="utf-8") == original

    toolkit = BasicToolkit(None, workspace=owner)
    assert toolkit.read_file(path.relative_to(owner.root).as_posix()) == original
