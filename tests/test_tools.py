"""File review transaction faults, exercised against real isolated files."""

import threading

import pytest


@pytest.fixture
def reviewed_file(tmp_path):
    from redlotus.tools.interaction import PendingReviewStore

    path = tmp_path / "review.txt"
    path.write_text("a\nkeep\nz\n", encoding="utf-8")
    store = PendingReviewStore(threading.Lock())
    store.activate(lambda: None)
    store.write(path, path.name, lambda _: "A\nkeep\nZ\n")
    return path, store


@pytest.mark.parametrize("decision", [None, False, True])
def test_review_refuses_external_edit_before_second_write(reviewed_file, decision):
    path, store = reviewed_file
    entry = store.get(str(path))
    if decision is not None:
        store.decide(entry, 0, decision)
    owner_text = path.read_text(encoding="utf-8") + "owner edit\n"
    path.write_text(owner_text, encoding="utf-8")
    with pytest.raises(ValueError, match="之外"):
        store.write(path, path.name, lambda previous: previous + "agent edit\n")
    assert path.read_text(encoding="utf-8") == owner_text
    assert store.get(str(path)) is entry
    with pytest.raises(ValueError, match="之外"):
        store.decide(entry, 1, True)
    assert path.read_text(encoding="utf-8") == owner_text


def test_second_write_keeps_accepted_changes_as_the_new_baseline(reviewed_file):
    path, store = reviewed_file
    store.decide(store.get(str(path)), 0, False)
    store.write(path, path.name, lambda previous: previous + "later\n")
    entry = store.get(str(path))
    for hunk in entry.hunks:
        store.decide(entry, hunk.index, True)
    assert path.read_text(encoding="utf-8") == "A\nkeep\nz\n"


def test_reject_all_after_partial_rejection_and_another_write(reviewed_file):
    path, store = reviewed_file
    store.decide(store.get(str(path)), 0, True)
    store.write(path, path.name, lambda previous: previous + "later\n")
    entry = store.get(str(path))
    for hunk in entry.hunks:
        store.decide(entry, hunk.index, True)
    assert path.read_text(encoding="utf-8") == "a\nkeep\nz\n"


def test_review_refuses_external_deletion_of_an_empty_original(tmp_path):
    from redlotus.tools.interaction import PendingReviewStore

    path = tmp_path / "empty.txt"
    path.touch()
    store = PendingReviewStore(threading.Lock())
    store.activate(lambda: None)
    store.write(path, path.name, lambda _: "new")
    entry = store.get(str(path))
    store.decide(entry, 0, True)
    path.unlink()
    with pytest.raises(ValueError, match="之外"):
        store.decide(entry, 0, False)
    assert not path.exists()
    with pytest.raises(ValueError, match="之外"):
        store.write(path, path.name, lambda _: "replacement")
    assert not path.exists()


@pytest.mark.parametrize("operation", ["write", "reject"])
def test_failed_write_preserves_file_and_review_state(reviewed_file, monkeypatch, operation):
    from pathlib import Path

    path, store = reviewed_file
    entry = store.get(str(path))
    original = path.read_bytes()
    original_write = Path.write_text

    def fail_write(target, content, *args, **kwargs):
        original_write(target, content[:1], *args, **kwargs)
        raise OSError("injected disk failure")

    monkeypatch.setattr(Path, "write_text", fail_write)
    with pytest.raises(OSError, match="disk failure"):
        if operation == "write":
            store.write(path, path.name, lambda _: "replacement")
        else:
            store.decide(entry, 0, True)
    assert path.read_bytes() == original
    assert store.get(str(path)) is entry and not entry.decisions


def test_rejecting_new_file_removes_only_the_reviewed_version(tmp_path):
    from redlotus.tools.interaction import PendingReviewStore

    path = tmp_path / "created.txt"
    store = PendingReviewStore(threading.Lock())
    store.activate(lambda: None)
    store.write(path, path.name, lambda _: "new")
    entry = store.get(str(path))
    assert store.decide(entry, 0, True)
    assert not path.exists()
    assert store.decide(entry, 0, True)
