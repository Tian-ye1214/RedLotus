"""Count actual outer turns within one session, independent of model/tool events."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from redlotus.core.session import SessionFile
from redlotus.core.agents import WorkspaceContext
from redlotus.memory.records import ObservationStore


class SessionWindowTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.workspace = WorkspaceContext.from_path(self.root)
        self.session = SessionFile.create(self.root / "sessions", self.workspace.project_id)
        self.observations = ObservationStore(self.workspace, window_turns=20, overlap_turns=3)
        self.observations.bind(self.session)

    def add_turn(self, number):
        event = self.observations.begin(self.session.session_id, f"turn-{number}", f"input-{number}", [])
        event.status = "success"
        self.observations.finish(event)
        return event.id

    def test_sixty_turns_seal_exactly_three_disjoint_windows(self):
        ids, windows = [], []
        for number in range(1, 61):
            ids.append(self.add_turn(number))
            window = self.observations.window(start=self.observations.reserved_cursor())
            if window:
                windows.append(window)
                self.observations.reserve(window)
        self.assertEqual(len(windows), 3)
        self.assertEqual([w.new_turn_ids for w in windows], [ids[:20], ids[20:40], ids[40:60]])
        self.assertEqual([w.overlap_turn_ids for w in windows], [[], ids[17:20], ids[37:40]])
        self.assertEqual(self.observations.reserved_cursor(), 60)
        self.assertEqual(list((self.root / "sessions").rglob("*.json")), [self.session.path])

    def test_load_nineteen_then_twentieth_turn_triggers_first_window(self):
        for number in range(19):
            self.add_turn(number)
        restored = ObservationStore(self.workspace, window_turns=20, overlap_turns=3)
        restored.bind(SessionFile.load(self.session.path))
        self.assertIsNone(restored.window())
        self.observations = restored
        self.add_turn(19)
        self.assertEqual(len(restored.window().new_turn_ids), 20)

    def test_new_session_never_reads_previous_session_turns(self):
        for number in range(21):
            self.add_turn(number)
        fresh = SessionFile.create(self.root / "sessions", self.workspace.project_id)
        self.observations.bind(fresh)
        self.assertEqual(self.observations.order(), [])
        self.assertIsNone(self.observations.window())

    def test_urgent_and_repeated_status_saves_do_not_increment_turns(self):
        event = self.observations.begin(self.session.session_id, "one", "initial", [])
        for number in range(30):
            event.user_inputs.append(f"urgent-{number}")
            self.observations.save(event)
        self.assertEqual(self.session.completed_turns, 0)
        event.status = "success"
        self.observations.finish(event)
        self.assertEqual(self.session.completed_turns, 1)
        self.assertIsNone(self.observations.window())


if __name__ == "__main__":
    unittest.main()
