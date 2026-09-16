"""Single-file storage must recover SDK history and preserve unconsumed evidence."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart


class SessionFileTests(unittest.TestCase):
    def setUp(self):
        from redlotus.core.session import SessionFile

        self.directory = TemporaryDirectory(dir=Path(__file__).resolve().parents[1] / "tests")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = SessionFile.create(self.root, "project", session_id="same-session")

    def dialogue(self, question, answer):
        return [ModelRequest(parts=[UserPromptPart(question)]), ModelResponse(parts=[TextPart(answer)])]

    def test_saves_one_json_and_appends_without_rewriting_prefix(self):
        first = self.dialogue("FIRST_LONG_INPUT_" * 3000, "one")
        self.store.save_context(first, turn_id="t1", metadata={"tasks": [{"id": "a", "status": "completed"}]})
        before = self.store.path.read_bytes()
        self.store.save_context([*first, *self.dialogue("second", "two")], turn_id="t2")
        after = self.store.path.read_bytes()
        self.assertEqual(list(self.root.rglob("*.json")), [self.store.path])
        self.assertEqual(self.store.path.name, "model_messages.json")
        self.assertTrue(after.startswith(before[:-3]))
        self.assertLess(len(after) - len(before), 3000)
        json.loads(after)

    def test_append_cost_does_not_grow_with_the_context_index(self):
        history = [message for number in range(500) for message in self.dialogue(str(number), "answer")]
        self.store.save_context(history, turn_id="old")
        before = self.store.path.stat().st_size
        self.store.save_context([*history, *self.dialogue("new", "answer")], turn_id="new")
        self.assertLess(self.store.path.stat().st_size - before, 3000)

    def test_sdk_metadata_updates_do_not_duplicate_message_bodies(self):
        text = "IMMUTABLE_BODY_" * 3000
        request = ModelRequest(parts=[UserPromptPart(text)])
        self.store.save_context([request], turn_id="same")
        before = self.store.path.stat().st_size
        request.run_id = "sdk-run-identity"
        self.store.save_context([request], turn_id="same")
        self.assertLess(self.store.path.stat().st_size - before, 1000)
        self.assertEqual(self.store.path.read_text(encoding="utf-8").count(text), 1)

    def test_turn_evidence_references_the_same_user_text_as_sdk_history(self):
        from redlotus.core.session import SessionFile

        text = "SHARED_ORIGINAL_INPUT_" * 3000
        self.store.update(metadata={"active_turn": {"id": "event", "user_inputs": [text]}})
        self.store.save_context(self.dialogue(text, "answer"), turn_id="turn")
        self.store.finish_turn("event", {"status": "success", "user_inputs": [text]})
        self.store.update(metadata={"active_turn": None})
        assert self.store.path.read_text(encoding="utf-8").count(text) == 1
        restored = SessionFile.load(self.store.path)
        assert restored.model_messages()[0].parts[0].content == text
        assert restored.pending_turns(0)[0]["user_inputs"] == [text]

    def test_restores_identity_tasks_and_sdk_messages(self):
        from redlotus.core.session import SessionFile

        self.store.save_context(self.dialogue("continue", "ok"), turn_id="t1", metadata={"tasks": ["finished"]})
        self.store.finish_turn("t1", {"status": "success", "user_inputs": ["continue"]})
        loaded = SessionFile.load(self.store.path)
        self.assertEqual(loaded.session_id, "same-session")
        self.assertEqual(loaded.metadata["tasks"], ["finished"])
        self.assertEqual(loaded.completed_turns, 1)
        self.assertEqual(loaded.model_messages()[-1].parts[0].content, "ok")
        self.assertEqual(loaded.pending_turns(0)[0]["id"], "t1")

    def test_context_compaction_retains_only_required_turn_evidence(self):
        first = self.dialogue("unconsumed source", "result")
        self.store.save_context(first, turn_id="t1")
        self.store.finish_turn("t1", {"status": "success"})
        self.store.save_context(self.dialogue("newer source", "newer result"), turn_id="t2")
        self.store.finish_turn("t2", {"status": "success"})
        self.store.compact(keep_turn_ids={"t1"})
        self.assertEqual(len(self.store.read_turn("t1")), 2)
        self.store.compact(keep_turn_ids=set())
        self.assertEqual(self.store.read_turn("t1"), [])
        self.assertEqual(self.store.completed_turns, 2)
        self.assertEqual(self.store.model_messages()[-1].parts[0].content, "newer result")

    def test_repeated_finish_does_not_add_another_real_turn(self):
        self.store.finish_turn("t1", {"status": "success"})
        self.store.finish_turn("t1", {"status": "success"})
        self.assertEqual(self.store.completed_turns, 1)

    def test_another_instance_sees_completed_and_consumed_positions(self):
        from redlotus.core.session import SessionFile

        second = SessionFile.load(self.store.path)
        self.store.finish_turn("twentieth", {"status": "success"})
        self.store.update(metadata={"perception_reserved": 20, "perception_consumed": 20})
        assert second.completed_turns == 1
        assert second.metadata["perception_reserved"] == 20
        assert second.metadata["perception_consumed"] == 20

    def test_recovers_last_complete_update_after_a_torn_append(self):
        from redlotus.core.session import SessionFile

        self.store.save_context(self.dialogue("kept", "saved"), turn_id="t1")
        complete = self.store.path.read_bytes()
        self.store.path.write_bytes(complete[:-3] + b',\n{"messages":{"incomplete":')
        restored = SessionFile.load(self.store.path)
        self.assertEqual(restored.model_messages()[-1].parts[0].content, "saved")
        self.assertTrue(restored.recovered_partial_write)
        json.loads(self.store.path.read_text(encoding="utf-8"))

    def test_stable_instructions_are_stored_once(self):
        history = []
        instructions = "SESSION_FIXED_SKILLS_AND_MEMORY_" * 300
        for number in range(3):
            messages = self.dialogue(str(number), "answer")
            messages[0].instructions = instructions
            history.extend(messages)
            self.store.save_context(history, turn_id=str(number))
        self.assertEqual(self.store.path.read_text(encoding="utf-8").count(instructions), 1)
        self.assertEqual(self.store.model_messages()[0].instructions, instructions)

    def test_shared_response_keeps_its_original_turn_and_usage(self):
        first = self.dialogue("first", "one")
        self.store.save_context(first, turn_id="t1")
        self.store.save_context([*first, *self.dialogue("second", "two")], turn_id="t2")
        self.assertEqual([row["turn_id"] for row in self.store.usage_responses()], ["t1", "t2"])
        self.assertEqual(len(self.store.read_turn("t1")), 2)

    def test_updated_message_does_not_reuse_an_obsolete_digest(self):
        from copy import deepcopy

        history = self.dialogue("first", "draft")
        original = deepcopy(history)
        self.store.save_context(history, turn_id="t1")
        history[-1].parts[0].content = "completed"
        self.store.save_context(history, turn_id="t1")
        self.store.save_context(original, turn_id="t2")
        self.assertEqual(self.store.model_messages()[-1].parts[0].content, "draft")

    def test_replaying_a_pruned_turn_does_not_increment_count(self):
        self.store.finish_turn("t1", {"status": "success", "user_inputs": ["old input"]})
        self.store.compact(keep_turn_ids=set())
        self.store.finish_turn("t1", {"status": "success"})
        self.assertEqual(self.store.completed_turns, 1)

    def test_registered_picture_reuses_original_snapshot_after_restore(self):
        import base64
        import os
        from unittest.mock import patch
        from pydantic_ai import BinaryContent

        data = bytes(range(256)) * 300
        references = self.root / "references"
        snapshot = references / "blobs" / "picture" / "source.png"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(data)
        manifest = references / "manifests" / ("a" * 32 + ".json")
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({"parts": [{"path": str(snapshot)}]}), encoding="utf-8")
        request = ModelRequest(parts=[UserPromptPart([
            "read the attached picture", BinaryContent(data=data, media_type="image/png", identifier="a" * 32 + "-0")
        ])])
        with patch("redlotus.core.config.references_dir", return_value=references):
            self.store.save_context([request], turn_id="picture")
            stored = self.store.path.read_text(encoding="utf-8")
            self.assertNotIn(base64.urlsafe_b64encode(data).decode(), stored)
            self.assertEqual(self.store.model_messages()[0].parts[0].content[1].data, data)
        self.assertEqual(list((references / "blobs").rglob("*.*")), [snapshot])


if __name__ == "__main__":
    unittest.main()
