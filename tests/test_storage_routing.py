import json
import os
import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart

from redlotus.core.config import settings
from redlotus.core import config as paths
from redlotus.tools.references import ReferenceStore
from redlotus.core.agents import WorkspaceContext
from redlotus.core.agents import workspace_context
from redlotus.core.session import SessionFile
from redlotus.core.session import conversations_root
from redlotus.core.cli_commands import list_workspace_snapshots


async def test_session_and_reference_files_are_separate_from_memory(
    tmp_path, monkeypatch
):
    config = settings()
    state = tmp_path / "state"
    config["storage"].update(
        state_dir=str(state),
        project_dir=".redlotus",
        sessions_dir=".redlotus/sessions",
        project_logs_dir=".redlotus/logs",
        references_dir="WorkDatabase/references",
        runtime_dir="WorkDatabase/runtime",
    )
    selected = tmp_path / "config.json"
    selected.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(selected))
    monkeypatch.delenv("REDLOTUS_DATA_DIR")
    workspace = WorkspaceContext.from_path(tmp_path / "project")
    trace = SessionFile.create(paths.session_data_dir(workspace), workspace.project_id)
    trace.save_context([ModelRequest(parts=[UserPromptPart("original input")])], turn_id="one")
    assert paths.user_data_dir() == state
    assert trace.path.is_relative_to(workspace.root / ".redlotus/sessions")
    assert ReferenceStore(workspace).root == workspace.root / "WorkDatabase/references"
    assert paths.project_data_dir(workspace) == workspace.root / ".redlotus"
    assert paths.memory_dir() == state / "LongTermMemory"
    assert paths.user_skills_dir(workspace) == workspace.root / "WorkDatabase/runtime/skills"
    with workspace_context(workspace):
        assert conversations_root() == workspace.root / ".redlotus/sessions"
        assert list_workspace_snapshots()
    assert not list(state.rglob("model_messages.json"))


def test_empty_project_session_path_is_rejected(monkeypatch, tmp_path):
    config = settings()
    config["storage"]["sessions_dir"] = ""
    config["storage"]["references_dir"] = ""
    selected = tmp_path / "defaults.json"
    selected.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("REDLOTUS_CONFIG_FILE", str(selected))
    monkeypatch.setenv("REDLOTUS_DATA_DIR", str(tmp_path))
    workspace = WorkspaceContext.from_path(tmp_path / "project")
    with workspace_context(workspace):
        with pytest.raises(paths.ConfigError, match="sessions_dir"):
            conversations_root()


def test_snapshot_discovery_only_offers_the_new_single_file_format(tmp_path):
    current, previous = tmp_path / "current", tmp_path / "previous"
    for root in (current, previous):
        root.mkdir()
        (root / "coordinator_ModelMessages.json").write_text(
            json.dumps({"meta": {"agent": "coordinator"}, "model_messages": []}),
            encoding="utf-8",
        )
    session = SessionFile.create(current, "project")
    snapshots = list_workspace_snapshots(root=current)
    assert [item.path for item in snapshots] == [session.path]


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
