"""A failed append cannot discard an earlier commit or invent a completed turn."""

import errno
import json
from unittest.mock import patch

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from redlotus.core.session import SessionFile
from redlotus.core.cli_commands import list_workspace_snapshots


def dialogue(question):
    return [ModelRequest(parts=[UserPromptPart(question)]), ModelResponse(parts=[TextPart('已读取')])]


def test_partial_chinese_tail_recovers_previous_commit_and_can_append(tmp_path):
    store = SessionFile.create(tmp_path, 'project')
    store.save_context(dialogue('完整的第一轮'), turn_id='one')
    complete = store.path.read_bytes()
    store.path.write_bytes(complete[:-3] + b',\n{"metadata":{"title":"' + '成'.encode()[:2])

    restored = SessionFile.load(store.path)
    assert restored.model_messages()[0].parts[0].content == '完整的第一轮'
    restored.save_context(dialogue('恢复后的新输入'), turn_id='two')
    assert SessionFile.load(store.path).model_messages()[0].parts[0].content == '恢复后的新输入'
    json.loads(store.path.read_bytes())


def test_checksum_rejects_complete_but_damaged_last_transaction(tmp_path):
    store = SessionFile.create(tmp_path, 'project')
    store.update(metadata={'title': 'first'})
    store.update(metadata={'title': 'later'})
    raw = store.path.read_bytes().replace(b'"title":"later"', b'"title":"other"')
    store.path.write_bytes(raw)

    restored = SessionFile.load(store.path)
    assert restored.metadata['title'] == 'first'
    assert restored.recovered_partial_write


def test_middle_corruption_is_reported_without_rewriting_file(tmp_path):
    store = SessionFile.create(tmp_path, 'project')
    for title in ('first', 'second', 'third'):
        store.update(metadata={'title': title})
    damaged = store.path.read_bytes().replace(b'"title":"second"', b'"title":"broken"')
    store.path.write_bytes(damaged)

    with pytest.raises(ValueError, match='事务|transaction|checksum'):
        SessionFile.load(store.path)
    assert store.path.read_bytes() == damaged


def test_malformed_middle_json_is_not_mistaken_for_an_incomplete_tail(tmp_path):
    store = SessionFile.create(tmp_path, 'project')
    for title in ('first', 'second', 'third'):
        store.update(metadata={'title': title})
    damaged = store.path.read_bytes().replace(b'"title":"second"', b'"title":broken')
    store.path.write_bytes(damaged)
    with pytest.raises(ValueError):
        SessionFile.load(store.path)
    assert store.path.read_bytes() == damaged


def test_failed_sync_never_advances_memory_and_retry_does_not_duplicate(tmp_path):
    store = SessionFile.create(tmp_path, 'project')
    store.update(metadata={'title': 'first'})
    with patch('redlotus.core.session.os.fsync', side_effect=PermissionError('read only')):
        with pytest.raises(OSError):
            store.update(metadata={'title': 'second'})
    assert store._metadata['title'] == 'first'
    store.retry_pending()
    restored = SessionFile.load(store.path)
    assert restored.metadata['title'] == 'second'
    assert len(json.loads(store.path.read_bytes())['updates']) == 2


def test_failed_commit_remains_invisible_to_readers_and_failed_retry(tmp_path):
    store = SessionFile.create(tmp_path, 'project')
    store.update(metadata={'title': 'committed'})
    with patch('redlotus.core.session.os.fsync', side_effect=PermissionError('read only')):
        with pytest.raises(OSError):
            store.update(metadata={'title': 'unconfirmed'})
        assert store.metadata['title'] == 'committed'
        with pytest.raises(OSError):
            store.retry_pending()
        assert store.metadata['title'] == 'committed'
    store.retry_pending()
    assert store.metadata['title'] == 'unconfirmed'


def test_finish_turn_commits_counter_and_active_clear_together(tmp_path):
    store = SessionFile.create(tmp_path, 'project')
    store.update(metadata={'active_turn': {'id': 't1', 'status': 'unverified'}})
    store.finish_turn('t1', {'status': 'success', 'user_inputs': ['一个问题']})

    last = json.loads(store.path.read_bytes())['updates'][-1]
    assert last['metadata']['completed_turns'] == 1
    assert last['metadata']['active_turn'] is None
    assert 't1' in last['turns']
    store.finish_turn('t1', {'status': 'success'})
    assert store.completed_turns == 1


def test_one_unreadable_session_does_not_hide_healthy_sessions(tmp_path):
    healthy = SessionFile.create(tmp_path, 'project', session_id='healthy')
    broken = SessionFile.create(tmp_path, 'project', session_id='broken')
    broken.path.write_bytes(b'not a session')

    snapshots = list_workspace_snapshots(root=tmp_path)
    assert [snapshot.path for snapshot in snapshots] == [healthy.path]
    assert broken.path.read_bytes() == b'not a session'
def test_pending_batch_cannot_be_replaced_by_another_update(tmp_path, monkeypatch):
    import os
    from redlotus.core.session import SessionFile

    session = SessionFile.create(tmp_path, "project")
    def fail(_):
        raise PermissionError("not writable")
    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(PermissionError):
        session.update(metadata={"first": "保留"})
    with pytest.raises(OSError):
        session.update(metadata={"second": "不能覆盖"})
    assert "second" not in session._pending_update.get("metadata", {})


def test_active_session_use_locks_are_independent_and_released(tmp_path):
    from filelock import FileLock, Timeout
    from redlotus.core.session import SessionFile

    first = SessionFile.create(tmp_path, "project")
    second = SessionFile.load(first.path)
    first.acquire_use()
    second.acquire_use()
    locks = list(first.path.parent.glob(".use-*.lock"))
    assert len(locks) == 2
    for path in locks:
        with pytest.raises(Timeout):
            FileLock(path).acquire(timeout=0)
    first.release_use()
    assert len(list(first.path.parent.glob(".use-*.lock"))) == 1
    second.release_use()
    assert not list(first.path.parent.glob(".use-*.lock"))


def test_failed_initial_creation_does_not_publish_half_a_session(tmp_path, monkeypatch):
    with patch('redlotus.core.session.os.fsync', side_effect=PermissionError('read only')):
        with pytest.raises(OSError):
            SessionFile.create(tmp_path, 'project', session_id='initial')
    assert not (tmp_path / 'initial' / 'model_messages.json').exists()
    restored = SessionFile.create(tmp_path, 'project', session_id='initial')
    assert restored.completed_turns == 0
