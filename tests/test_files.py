"""File review conflict and atomic publication regressions."""

import threading

import pytest

import redlotus.runtime.files as _core_config
from redlotus.documents.review import PendingReviewStore


@pytest.fixture
def review(tmp_path):
    path = tmp_path / "document.txt"
    path.write_text("first\nkeep\nlast\n", encoding="utf-8")
    store = PendingReviewStore(threading.Lock())
    store.activate(lambda: None)
    store.write(path, path.name, lambda previous: "FIRST\nkeep\nLAST\n")
    return store, path


@pytest.mark.parametrize("external", ["USER EDIT\n", None])
def test_second_agent_write_rejects_external_changes(review, external):
    store, path = review
    if external is None:
        path.unlink()
    else:
        path.write_text(external, encoding="utf-8")
    with pytest.raises(ValueError, match="修改"):
        store.write(path, path.name, lambda previous: (previous or "") + "agent\n")
    assert (path.read_text(encoding="utf-8") if path.exists() else None) == external


def test_partial_rejection_then_write_still_restores_original(review):
    store, path = review
    store.decide(store.get(str(path)), 0, True)
    store.write(path, path.name, lambda previous: previous.replace("LAST", "SECOND"))
    entry = store.get(str(path))
    for hunk in entry.hunks:
        store.decide(entry, hunk.index, True)
    assert path.read_text(encoding="utf-8") == "first\nkeep\nlast\n"


def test_new_empty_file_allows_a_second_agent_write(tmp_path):
    store = PendingReviewStore(threading.Lock())
    store.activate(lambda: None)
    path = tmp_path / "new.txt"
    store.write(path, path.name, lambda previous: "")
    store.write(path, path.name, lambda previous: "content")
    assert path.read_text(encoding="utf-8") == "content"
    store.decide(store.get(str(path)), 0, True)
    assert not path.exists()


def test_atomic_write_and_rejection_failure_leave_file_and_review_intact(review, monkeypatch):
    store, path = review
    entry = store.get(str(path))
    before = path.read_bytes()
    def fail_replace(*args):
        raise OSError("Disk unavailable")
    monkeypatch.setattr(_core_config.os, "replace", fail_replace)
    with pytest.raises(OSError):
        store.write(path, path.name, lambda previous: "destroyed")
    with pytest.raises(OSError):
        store.decide(entry, 0, True)
    assert path.read_bytes() == before
    assert not entry.decisions
    assert store.get(str(path)) is entry
    assert [p.name for p in path.parent.iterdir()] == [path.name]
