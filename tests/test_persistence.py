"""Persisted state: atomic writes, quarantined corruption, one task-key schema."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import persistence


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        persistence.quarantined.clear()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(persistence.quarantined.clear)

    def test_concurrent_writers_never_share_a_temp_name_or_leave_one(self):
        target = self.dir / "state.json"
        threads = [threading.Thread(target=persistence.write_json, args=(target, {"n": n}))
                   for n in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertIn(json.loads(target.read_text())["n"], range(20))
        self.assertEqual(os.listdir(self.dir), ["state.json"])

    def test_corrupt_file_is_quarantined_not_emptied(self):
        target = self.dir / "holds.json"
        target.write_text('[{"thread_id": "t1"')
        self.assertEqual(persistence.read_json(target, []), [])
        self.assertFalse(target.exists())
        [(path, moved)] = persistence.quarantined
        self.assertEqual(Path(moved).read_text(), '[{"thread_id": "t1"')
        persistence.write_json(target, [])
        self.assertTrue(Path(moved).exists(), "the next save must not destroy the corrupt copy")

    def test_missing_file_is_the_default_and_other_errors_propagate(self):
        self.assertEqual(persistence.read_json(self.dir / "absent.json", []), [])
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                persistence.read_json(self.dir / "x.json", [])

    def test_state_reader_is_pure_and_maps_removed_states_forward(self):
        target = self.dir / "state.json"
        self.assertIsNone(persistence.read_state(target))
        target.write_text(json.dumps({"state": "OLD_PHASE", "item": "x"}))
        with patch.dict(persistence.FORWARD_STATES, {"OLD_PHASE": "VERIFYING"}):
            self.assertEqual(persistence.read_state(target), {"state": "VERIFYING", "item": "x"})
        self.assertEqual(json.loads(target.read_text())["state"], "OLD_PHASE")
        target.write_text("[]")
        self.assertEqual(persistence.read_state(target), {"state": "IDLE"})
        self.assertEqual(len(persistence.quarantined), 1)

    def test_schema_keeps_the_scope_fingerprint_with_a_held_task(self):
        # HOLD saves TASK_KEYS and CONTINUE restores them: a fingerprint left behind made
        # the resumed task report scope drift (and leaked into the next task).
        self.assertIn("approved_scope_fingerprint", persistence.TASK_KEYS)
        self.assertEqual(len(persistence.TASK_KEYS), len(set(persistence.TASK_KEYS)))
        self.assertFalse(set(persistence.TASK_KEYS) & set(persistence.GLOBAL_KEYS))


class Holds(unittest.TestCase):
    def test_corrupt_holds_are_quarantined_and_reported_once(self):
        import main
        with tempfile.TemporaryDirectory() as tmp:
            holds = Path(tmp) / "holds.json"
            holds.write_text("{not json")
            persistence.quarantined.clear()
            with patch.object(main.config, "HOLDS_PATH", holds), \
                 patch.object(main.config, "COMM_CHANNEL", "slack"), \
                 patch.object(main.gmail_client, "announce") as announce:
                self.assertEqual(main._load_holds(), [])
                main._save_holds([])
                main._report_quarantined()
                main._report_quarantined()
            announce.assert_called_once()
            self.assertEqual(len(list(Path(tmp).glob("holds.json.corrupt-*"))), 1)
            self.assertEqual(persistence.quarantined, [])


if __name__ == "__main__":
    unittest.main()


class ForwardStates(unittest.TestCase):
    def test_deleted_phases_resume_in_verification_from_every_pointer(self):
        import persistence
        state = persistence.forward({"state": "WAIT_STUCK", "stuck_return": "RECOVERING",
                                     "return_state": "EVIDENCE_REVIEW", "check_repair": {"resume": "RECOVERING"},
                                     "pending_decision": {"origin": "EVIDENCE_REVIEW"}})
        self.assertEqual((state["state"], state["stuck_return"], state["return_state"]),
                         ("WAIT_STUCK", "VERIFYING", "VERIFYING"))
        self.assertEqual(state["check_repair"]["resume"], "VERIFYING")
        self.assertEqual(state["pending_decision"]["origin"], "VERIFYING")
        self.assertEqual(persistence.forward({"state": "RECOVERING"})["state"], "VERIFYING")
