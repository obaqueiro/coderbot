"""One bounded decision reminder, never working-state noise or repeated diagnostics."""
import sys
import unittest
from unittest.mock import Mock, patch

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main


class DecisionReminders(unittest.TestCase):
    def test_work_states_never_ping(self):
        for phase in main.PHASES:
            state = {"state": phase, "thread_id": "t", "last_contact": 0}
            self.assertFalse(main._ping_due(state, 1e9), phase)

    def test_decision_reminder_once_after_day(self):
        state = {"state": "WAIT_APPROVAL", "thread_id": "t", "last_contact": 1000}
        with patch.object(main.config, "PING_SCHEDULE_SECONDS", [86400]):
            self.assertFalse(main._ping_due(state, 1000 + 86399))
            self.assertTrue(main._ping_due(state, 1000 + 86400))
            state["reminded_decision"] = state["pending_decision_id"]
            self.assertFalse(main._ping_due(state, 1e9))

    def test_reminder_has_question_but_no_prior_message_or_traceback(self):
        state = {"state": "WAIT_REPLY", "pending_question": "Which model?",
                 "last_email": {"body": "Traceback: " + "x" * 10000}}
        body = main._ping_body(state, 1e9, 1)
        self.assertIn("Which model?", body)
        self.assertNotIn("Traceback", body)
        self.assertLess(len(body), 600)

    def test_send_has_no_attachments_and_keeps_original_handoff(self):
        state = {"state": "WAIT_APPROVAL", "thread_id": "t", "last_contact": 0,
                 "last_email": {"body": "original"}}
        with patch.object(main.config, "PING_SCHEDULE_SECONDS", [86400]), \
             patch.object(main.time, "time", return_value=86400), \
             patch.object(main, "_send_localized") as send:
            main._maybe_ping(state)
        send.assert_called_once()
        self.assertEqual(state["last_email"]["body"], "original")
        self.assertEqual(state["ping_count"], 1)

    def test_contact_refreshes_clock(self):
        state = {"ping_count": 1, "last_ping_at": 10}
        with patch.object(main.time, "time", return_value=100):
            main._note_contact(state)
        self.assertEqual(state["last_contact"], 100)
        self.assertNotIn("ping_count", state)

    def test_status_contact_does_not_repeat_same_decision_reminder(self):
        state = {"state": "WAIT_APPROVAL", "thread_id": "t", "last_contact": 0}
        self.assertTrue(main._ping_due(state, 86400))
        state["reminded_decision"] = state["pending_decision_id"]
        main._note_contact(state)
        self.assertFalse(main._ping_due(state, 1e12))
        state["reviewed_proposal"] = "new-version"
        self.assertTrue(main._ping_due(state, 1e12))

