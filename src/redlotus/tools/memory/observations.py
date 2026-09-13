from __future__ import annotations

import hashlib

from filelock import FileLock, Timeout

from redlotus.infra.persist_utils import (
    file_lock,
    save_locked_json,
    atomic_write_json,
    iso_utc_now,
    read_locked_json,
)
from redlotus.runtime.context import WorkspaceContext
from redlotus.infra.paths import project_data_dir, migrate_project_data
from redlotus.config.app_config import settings
from redlotus.tools.memory.models import ObservedTurn, WindowManifest


class ObservationStore:
    def __init__(
        self,
        workspace: WorkspaceContext,
        *,
        window_turns: int | None = None,
        overlap_turns: int | None = None,
    ):
        config = settings()["memory_perception"]
        window_turns = config["window_turns"] if window_turns is None else window_turns
        overlap_turns = (
            config["overlap_turns"] if overlap_turns is None else overlap_turns
        )
        if not 0 <= overlap_turns < window_turns:
            raise ValueError("Memory overlap must be smaller than its window")
        self.workspace = workspace
        migrate_project_data(workspace)
        self.root = project_data_dir(workspace) / "memory"
        self.turns = self.root / "turns"
        self.order_path = self.root / "event_order.json"
        self.cursor_path = self.root / "perception_state.json"
        self.window_turns, self.overlap_turns = window_turns, overlap_turns
        self.active: dict[str, FileLock] = {}

    def begin(
        self, session_id: str, turn_id: str, text: str, reference_ids: list[str]
    ) -> ObservedTurn:
        identity = hashlib.sha256(
            f"{self.workspace.project_id}\0{session_id}\0{turn_id}".encode()
        ).hexdigest()[:32]
        self.turns.mkdir(parents=True, exist_ok=True)
        lock = FileLock(
            self.turns / f"{identity}.active.lock", timeout=0, thread_local=False
        )
        lock.acquire()
        self.active[identity] = lock
        event = ObservedTurn(
            id=identity,
            project_id=self.workspace.project_id,
            session_id=session_id,
            turn_id=turn_id,
            user_inputs=[text],
            reference_ids=reference_ids,
        )
        self.save(event)
        return event

    def save(self, event: ObservedTurn) -> None:
        save_locked_json(self.turns / f"{event.id}.json", event.model_dump(mode="json"))

    def finish(self, event: ObservedTurn) -> None:
        event.finished_at = iso_utc_now()
        try:
            self.save(event)
            self.append(event.id)
        finally:
            self.active.pop(event.id).release()

    def append(self, event_id: str) -> None:
        with file_lock(self.order_path):
            order = self.order()
            if event_id not in order:
                order.append(event_id)
                atomic_write_json(self.order_path, order)

    def order(self) -> list[str]:
        return read_locked_json(self.order_path) if self.order_path.exists() else []

    def cursor(self) -> int:
        if not self.cursor_path.exists():
            return 0
        return int(read_locked_json(self.cursor_path)["consumed"])

    def read(self, ids: list[str]) -> list[ObservedTurn]:
        return [
            ObservedTurn.model_validate(read_locked_json(self.turns / f"{key}.json"))
            for key in ids
        ]

    def window(
        self, *, flush: bool = False, migration: bool = False
    ) -> WindowManifest | None:
        order, cursor = self.order(), self.cursor()
        overlap = order[max(0, cursor - self.overlap_turns) : cursor]
        needed = self.window_turns - len(overlap)
        fresh = order[cursor : cursor + needed]
        if not fresh or (len(fresh) < needed and not flush):
            return None
        identity = hashlib.sha256(
            f"{self.workspace.project_id}\0{cursor}\0{fresh[-1]}".encode()
        ).hexdigest()[:32]
        events = self.read([*overlap, *fresh])
        refs = list(
            dict.fromkeys(ref for event in events for ref in event.reference_ids)
        )
        return WindowManifest(
            id=identity,
            project_id=self.workspace.project_id,
            new_turn_ids=fresh,
            overlap_turn_ids=overlap,
            reference_ids=refs,
            start_position=cursor,
            end_position=cursor + len(fresh),
            reason="migration"
            if migration
            else "flush"
            if len(fresh) < needed
            else "window",
        )

    def commit(self, window: WindowManifest) -> None:
        save_locked_json(
            self.cursor_path, dict(consumed=window.end_position, last_window=window.id)
        )

    def recover(self) -> None:
        """Recover ended turns and released crash claims without inventing successful outcomes."""
        known = set(self.order())
        paths = sorted(
            self.turns.glob("*.json"),
            key=lambda path: read_locked_json(path).get("created_at", ""),
        )
        for path in paths:
            if path.stem in known:
                continue
            try:
                with FileLock(self.turns / f"{path.stem}.active.lock", timeout=0):
                    event = ObservedTurn.model_validate(read_locked_json(path))
                    if event.status == "running":
                        event.status = "unverified"
                        event.error = "Previous process ended before the turn outcome was recorded."
                    if event.finished_at is None:
                        event.finished_at = event.created_at
                    self.save(event)
                    self.append(event.id)
            except Timeout:
                continue

    def close(self) -> None:
        for lock in self.active.values():
            lock.release()
        self.active.clear()
