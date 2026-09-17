"""Lightweight project session-index discovery behavior."""

import errno
import json
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart

from redlotus.core import config, session as session_module
from redlotus.core.agents import WorkspaceContext, workspace_context
from redlotus.core.cli_commands import list_workspace_snapshots
from redlotus.core.session import SessionFile


def _count_session_loads(monkeypatch):
    original = SessionFile.load
    calls = []

    def counted(path, **kwargs):
        calls.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(SessionFile, "load", counted)
    return calls


def test_scan_info_reuses_unchanged_cached_metadata_without_loading_session(
    tmp_path, monkeypatch
):
    session = SessionFile.create(tmp_path, "project", session_id="cached", title="初始标题")

    first = SessionFile.scan_info(tmp_path)
    calls = _count_session_loads(monkeypatch)
    second = SessionFile.scan_info(tmp_path)

    assert [(entry.path, entry.info["title"]) for entry in first] == [
        (session.path, "初始标题")
    ]
    assert [(entry.path, entry.info["title"]) for entry in second] == [
        (session.path, "初始标题")
    ]
    assert calls == []


def test_scan_info_refreshes_only_a_session_changed_outside_the_cache(
    tmp_path, monkeypatch
):
    session = SessionFile.create(tmp_path, "project", session_id="changed", title="旧标题")
    SessionFile.scan_info(tmp_path)
    session.update(metadata={"title": "新标题"})

    calls = _count_session_loads(monkeypatch)
    entries = SessionFile.scan_info(tmp_path)

    assert calls == [session.path]
    assert entries[0].info["title"] == "新标题"


def test_scan_info_cache_contains_only_discovery_metadata_not_message_bodies(tmp_path):
    session = SessionFile.create(tmp_path, "project", session_id="private", title="私密任务")
    secret = "FULL PROMPT BODY MUST NOT ENTER THE INDEX"
    session.save_context(
        [ModelRequest(parts=[UserPromptPart(secret)])], turn_id="turn-private"
    )

    SessionFile.scan_info(tmp_path)
    cached = (tmp_path / "index.json").read_text(encoding="utf-8")
    payload = json.loads(cached)

    assert secret not in cached
    entry = payload["sessions"]["private/model_messages.json"]
    assert set(entry["info"]) == {
        "session_id",
        "project_id",
        "title",
        "saved_at",
        "completed_turns",
        "status",
    }


def test_scan_info_rebuilds_a_corrupt_cache_and_does_not_create_empty_roots(tmp_path):
    session = SessionFile.create(tmp_path, "project", session_id="rebuild")
    (tmp_path / "index.json").write_text("not json", encoding="utf-8")

    rebuilt = SessionFile.scan_info(tmp_path)
    missing_root = tmp_path / "does-not-exist"

    assert [entry.path for entry in rebuilt] == [session.path]
    assert json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))["version"] == 1
    assert SessionFile.scan_info(missing_root) == []
    assert not missing_root.exists()


def test_scan_info_prunes_deleted_sessions_from_an_existing_cache(tmp_path):
    session = SessionFile.create(tmp_path, "project", session_id="deleted")
    SessionFile.scan_info(tmp_path)
    session.path.unlink()

    assert SessionFile.scan_info(tmp_path) == []
    assert json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))["sessions"] == {}


def test_snapshot_list_keeps_one_corrupt_session_as_an_individual_unloadable_row(
    tmp_path,
):
    healthy = SessionFile.create(tmp_path, "project", session_id="healthy")
    broken = SessionFile.create(tmp_path, "project", session_id="broken")
    broken.update(metadata={"title": "first"})
    broken.update(metadata={"title": "second"})
    corrupted = broken.path.read_bytes().replace(
        b'"title":"first"', b'"title":!"first"', 1
    )
    broken.path.write_bytes(corrupted)

    snapshots = list_workspace_snapshots(root=tmp_path, include_unloadable=True)

    assert healthy.path in [snapshot.path for snapshot in snapshots]
    corrupt = next(snapshot for snapshot in snapshots if snapshot.path == broken.path)
    assert not corrupt.is_loadable
    assert corrupt.status == "损坏"


def test_scan_info_lists_recoverable_partial_without_rewriting_it(
    tmp_path,
):
    healthy = SessionFile.create(tmp_path, "project", session_id="healthy")
    partial = SessionFile.create(tmp_path, "project", session_id="partial")
    partial.update(metadata={"title": "complete"})
    torn = partial.path.read_bytes()[:-3] + b',\n{"metadata":{"title":"partial'
    partial.path.write_bytes(torn)

    entries = {entry.path: entry for entry in SessionFile.scan_info(tmp_path)}

    assert partial.path.read_bytes() == torn
    assert entries[healthy.path].info["session_id"] == "healthy"
    assert entries[partial.path].info["session_id"] == "partial"
    assert not entries[partial.path].error
    snapshots = list_workspace_snapshots(root=tmp_path, include_unloadable=True)
    listed = next(snapshot for snapshot in snapshots if snapshot.path == partial.path)
    assert listed.is_loadable

    restored = SessionFile.load(partial.path)

    assert restored.recovered_partial_write
    assert partial.path.read_bytes() != torn


def test_scan_info_does_not_trigger_recovery_cleanup_in_another_active_workspace(
    tmp_path, monkeypatch
):
    first_root, second_root = tmp_path / "first", tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    partial = SessionFile.create(first_root, "first", session_id="partial")
    partial.update(metadata={"title": "complete"})
    torn = partial.path.read_bytes()[:-3] + b',\n{"metadata":{"title":"partial'
    partial.path.write_bytes(torn)
    untouched = SessionFile.create(second_root, "second", session_id="untouched")
    untouched_bytes = untouched.path.read_bytes()
    replacements, cleanup_calls = [], []
    original_replace = session_module.os.replace

    def disk_full_replace(source, target):
        if Path(target) == partial.path:
            replacements.append(target)
            raise OSError(errno.ENOSPC, "disk full")
        return original_replace(source, target)

    def unexpected_cleanup(*args):
        cleanup_calls.append(args)
        raise AssertionError("scan must not begin storage cleanup")

    monkeypatch.setattr(session_module.os, "replace", disk_full_replace)
    monkeypatch.setattr(config, "retry_after_storage_cleanup", unexpected_cleanup)
    with workspace_context(WorkspaceContext.from_path(second_root)):
        entries = {entry.path: entry for entry in SessionFile.scan_info(first_root)}

    assert replacements == []
    assert cleanup_calls == []
    assert partial.path.read_bytes() == torn
    assert entries[partial.path].info["session_id"] == "partial"
    assert not entries[partial.path].error
    assert untouched.path.read_bytes() == untouched_bytes


def test_load_without_recovery_rejects_a_recoverable_partial_without_rewriting_it(
    tmp_path,
):
    partial = SessionFile.create(tmp_path, "project", session_id="partial")
    partial.update(metadata={"title": "complete"})
    torn = partial.path.read_bytes()[:-3] + b',\n{"metadata":{"title":"partial'
    partial.path.write_bytes(torn)

    with pytest.raises(ValueError, match="会话含未完成事务"):
        SessionFile.load(partial.path, recover=False)

    assert partial.path.read_bytes() == torn
