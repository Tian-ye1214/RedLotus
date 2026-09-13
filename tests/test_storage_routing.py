import json
import os
from pydantic_ai.messages import ModelRequest, UserPromptPart

from redlotus.config.app_config import settings
from redlotus.infra import paths
from redlotus.references.store import ReferenceStore
from redlotus.runtime.context import WorkspaceContext, workspace_context
from redlotus.tools.conversation_log import ConversationLog
from redlotus.workspace.workspace import conversations_root, list_workspace_snapshots


async def test_session_and_reference_files_are_separate_from_memory(
    tmp_path, monkeypatch
):
    config = settings()
    state, sessions, references, runtime = (
        tmp_path / name for name in ("state", "sessions", "references", "runtime")
    )
    config["storage"] = dict(
        state_dir=str(state),
        sessions_dir=str(sessions),
        references_dir=str(references),
        compression_dir=str(sessions / "compression"),
        runtime_dir=str(runtime),
    )
    selected = tmp_path / "config.json"
    selected.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(selected))
    monkeypatch.delenv("REDLOTUS_DATA_DIR")
    workspace = WorkspaceContext.from_path(tmp_path / "project")
    trace = ConversationLog("coordinator", "2026-09-14", "routing", workspace=workspace)
    await trace.save([ModelRequest(parts=[UserPromptPart("original input")])])
    assert paths.user_data_dir() == state
    assert trace.model_messages_path().is_relative_to(sessions)
    assert ReferenceStore(workspace).root == references
    assert (
        paths.project_data_dir(workspace) == state / "projects" / workspace.project_id
    )
    assert paths.memory_dir() == state / "LongTermMemory"
    assert paths.user_skills_dir() == runtime / "skills"
    with workspace_context(workspace):
        assert conversations_root() == sessions / workspace.project_id
        assert list_workspace_snapshots()
    assert not list(state.rglob("*_ModelMessages.json"))


def test_storage_defaults_preserve_existing_layout(monkeypatch, tmp_path):
    monkeypatch.setenv("REDLOTUS_DATA_DIR", str(tmp_path))
    workspace = WorkspaceContext.from_path(tmp_path / "project")
    with workspace_context(workspace):
        assert conversations_root() == tmp_path / "projects" / workspace.project_id
    assert ReferenceStore(workspace).root == tmp_path / "references"


def test_snapshot_discovery_preserves_old_location(tmp_path):
    current, previous = tmp_path / "current", tmp_path / "previous"
    for root in (current, previous):
        root.mkdir()
        (root / "coordinator_ModelMessages.json").write_text(
            json.dumps({"meta": {"agent": "coordinator"}, "model_messages": []}),
            encoding="utf-8",
        )
    snapshots = list_workspace_snapshots(root=current, legacy_root=previous)
    assert {item.path.parent for item in snapshots} == {current, previous}


def test_config_detects_content_change_with_same_timestamp(tmp_path, monkeypatch):
    config = settings()
    config["models"]["worker"]["max_tokens"] = 393216
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(path))
    assert settings()["models"]["worker"]["max_tokens"] == 393216
    stamp = path.stat()
    config["models"]["worker"]["max_tokens"] = 131072
    path.write_text(json.dumps(config), encoding="utf-8")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert settings()["models"]["worker"]["max_tokens"] == 131072
