"""Scheduling regressions; live model acceptance is recorded separately."""

from redlotus.runtime.context import WorkspaceContext
from redlotus.tools.memory.observations import ObservationStore


def add_turns(store, start, end):
    ids = []
    for index in range(start, end):
        event = store.begin("session", str(index), f"user {index}", [])
        event.status = "success"
        store.finish(event)
        ids.append(event.id)
    return ids


def test_twenty_new_turns_are_required_after_overlap(tmp_path):
    store = ObservationStore(
        WorkspaceContext.from_path(tmp_path), window_turns=20, overlap_turns=3
    )
    first_ids = add_turns(store, 0, 20)
    first = store.window()
    assert first.new_turn_ids == first_ids
    store.commit(first)
    next_ids = add_turns(store, 20, 39)
    assert store.window() is None
    next_ids += add_turns(store, 39, 40)
    second = store.window()
    assert second.new_turn_ids == next_ids
    assert second.overlap_turn_ids == first_ids[-3:]


def test_flush_cannot_consume_events_after_its_boundary(tmp_path):
    store = ObservationStore(
        WorkspaceContext.from_path(tmp_path), window_turns=20, overlap_turns=3
    )
    first_ids = add_turns(store, 0, 4)
    add_turns(store, 4, 7)
    window = store.window(flush=True, through=4)
    assert window.new_turn_ids == first_ids
    store.commit(window)
    assert store.window(flush=True, through=4) is None
    assert len(store.window(flush=True).new_turn_ids) == 3


def test_completed_cursor_never_rewinds_or_drops_reservation(tmp_path):
    store = ObservationStore(
        WorkspaceContext.from_path(tmp_path), window_turns=20, overlap_turns=3
    )
    add_turns(store, 0, 40)
    first = store.window()
    store.reserve(first)
    assert store.reserved_cursor() == 20
    second = store.window(start=20)
    store.reserve(second)
    store.commit(first)
    store.commit(second)
    store.commit(first)
    assert store.cursor() == 40
    assert store.reserved_cursor() == 40
