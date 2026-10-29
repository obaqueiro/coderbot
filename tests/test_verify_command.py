"""Explicit verification skips planning, never checks or repository safety."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import command_text
import config
from execution_store import ExecutionStore

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main


class VerifyCommandTests(unittest.TestCase):
    def test_unchanged_verify_reuses_verified_result_without_another_agent_turn(self):
        state = {"state": "WAIT_REPLY", "slug": "feature", "item": "task", "session_id": "session",
                 "quality_controller_validated": True,
                 "quality_snapshot": "same", "reviewed_snapshot": "same",
                 "internal_review_report": {"status": "pass", "critical": 0, "important": 0},
                 "quality_report": {"status": "pass", "commands": ["focused: pass"],
                                    "openspec": "pass", "tasks": "1/1"}}
        with tempfile.TemporaryDirectory() as root:
            tasks = Path(root) / "openspec/changes/feature/tasks.md"
            tasks.parent.mkdir(parents=True)
            tasks.write_text("- [x] Work\n")
            with patch.object(config, "REPO_PATH", Path(root)), patch.object(main, "save_state"), \
                 patch.object(main.phase_checkpoint, "store", return_value=ExecutionStore(Path(root) / "db")), \
                 patch.object(main, "content_snapshot", return_value="same"), patch.object(main, "trail"), \
                 patch.object(main.agent_runner, "resume") as agent:
                main._request_verification(state)
                main.do_verify(state)
            self.assertEqual(state["state"], "E2E")
            agent.assert_not_called()

    def test_live_verify_interrupts_without_consuming_command_or_mutating_state(self):
        with tempfile.TemporaryDirectory() as root:
            tasks = Path(root) / "openspec/changes/feature/tasks.md"
            tasks.parent.mkdir(parents=True)
            tasks.write_text("- [x] Work\n")
            state = {"state": "REPLANNING", "item": "task", "slug": "feature", "session_id": "session", "thread_id": "thread"}
            before = dict(state)
            with patch.object(config, "REPO_PATH", Path(root)), patch.object(main, "_current_state", state), \
                 patch.object(main.agent_runner, "turn_snapshot", return_value={"active": True}), \
                 patch.object(main.operations, "snapshot", return_value=[]), \
                 patch.object(main.gmail_client, "poll_verify", return_value=("message", "thread", "VERIFY", False, "")), \
                 patch.object(main.gmail_client, "mark_processed") as consumed, \
                 patch.object(main.turn_control, "request_kick", return_value=True) as kick:
                main._work_active.set()
                try:
                    self.assertTrue(main._status_supervisor_once())
                finally:
                    main._work_active.clear()
            kick.assert_called_once()
            consumed.assert_not_called()
            self.assertEqual(state, before)

    def test_parser_requires_explicit_verify_word(self):
        self.assertEqual(command_text.parse_command("VERIFY"), ("VERIFY", "", ""))
        self.assertEqual(command_text.parse_command("VERIFY codebot-test: commit abc123 already has the fixes"),
                         ("VERIFY", "codebot-test", "commit abc123 already has the fixes"))
        self.assertIsNone(command_text.parse_command("verifying"))
        self.assertEqual(command_text.parse_command("VERIFY --restore-plan 93ea27a")[2], "--restore-plan 93ea27a")

    def test_replanning_can_move_to_verify_without_changing_work_or_faking_approval(self):
        for channel in ("slack", "email"):
            with self.subTest(channel=channel), tempfile.TemporaryDirectory() as root:
                tasks = Path(root) / "openspec/changes/feature/tasks.md"
                tasks.parent.mkdir(parents=True)
                tasks.write_text("- [x] Existing implementation\n")
                state = {"state": "REPLANNING", "item": "task", "slug": "feature", "session_id": "session",
                         "thread_id": "thread", "branch": "task-branch", "base_sha": "base",
                         "replan": {"feedback_id": "old"}, "coverage_report": {"invalid": True},
                         "pending_question": "Approve again?", "return_state": "IMPLEMENTING"}
                with patch.object(config, "REPO_PATH", Path(root)), patch.object(config, "COMM_CHANNEL", channel), \
                     patch.object(main.phase_checkpoint, "store", return_value=ExecutionStore(Path(root) / "execution.sqlite")), \
                     patch.object(main, "save_state"), patch.object(main, "email"), \
                     patch.object(main, "_load_holds", return_value=[]), \
                     patch.object(main.gmail_client, "poll_command", return_value=(
                         "message", "thread", "VERIFY", False, "Use commit abc123")), \
                     patch.object(main.gmail_client, "mark_processed") as consumed, \
                     patch.object(main, "git") as git:
                    self.assertTrue(main.check_commands(state))
                self.assertEqual(state["state"], "VERIFYING")
                self.assertEqual(state["verification_requested_from"], "REPLANNING")
                self.assertIn("Use commit abc123", main._verify_prompt(state))
                self.assertEqual(state["branch"], "task-branch")
                self.assertEqual(state["session_id"], "session")
                self.assertNotIn("approved_proposal", state)
                self.assertNotIn("replan", state)
                self.assertNotIn("coverage_report", state)
                self.assertEqual(tasks.read_text(), "- [x] Existing implementation\n")
                git.assert_not_called()
                consumed.assert_called_once_with("message")

    def test_verify_survives_reload_without_replaying_old_transitions(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            tasks = repo / "openspec/changes/feature/tasks.md"
            tasks.parent.mkdir(parents=True)
            tasks.write_text("- [x] Work\n")
            database = ExecutionStore(repo / "execution.sqlite")
            state = {"state": "REPLANNING", "item": "task", "slug": "feature",
                     "session_id": "session", "thread_id": "thread"}
            planning = database.record("feedback", state, repo, "coverage-old",
                                       {"text": "Missing approved requirements"}, status="planning")
            repairing = database.record("feedback", state, repo, "repair-old",
                                        {"text": "Previous correction"}, status="repairing")
            pending = database.record("feedback", state, repo, "new-feedback",
                                      {"text": "New user feedback"})
            state["replan"] = {"feedback_id": planning["id"]}
            state["active_feedback_id"] = repairing["id"]
            with patch.object(config, "REPO_PATH", repo), patch.object(config, "STATE_PATH", repo / "state.json"), \
                 patch.object(main.phase_checkpoint, "store", return_value=database):
                main._request_verification(state)
                for _ in range(2):
                    loaded = main.load_state()
                    self.assertEqual(loaded["state"], "VERIFYING")
                    self.assertNotIn("replan", loaded)
                    self.assertNotIn("active_feedback_id", loaded)
                    main.save_state(loaded)
            for row in (planning, repairing):
                saved = database.get("feedback", row["id"])
                self.assertEqual(saved["status"], "superseded")
                self.assertEqual(saved["data"]["outcome"], "verify-requested")
                self.assertEqual(saved["data"]["text"], row["data"]["text"])
            self.assertEqual(database.get("feedback", pending["id"])["status"], "pending")

    def test_invalid_requests_leave_state_untouched(self):
        for extra in ({}, {"archive_path": "openspec/changes/archive/feature"}):
            state = {"state": "REPLANNING", "item": "task", "slug": "feature", "session_id": "session", **extra}
            before = dict(state)
            with tempfile.TemporaryDirectory() as root, patch.object(config, "REPO_PATH", Path(root)):
                with self.assertRaises(ValueError):
                    main._request_verification(state)
            self.assertEqual(state, before)

    def test_e2e_runs_final_checks_without_coverage_audit(self):
        state = {"state": "E2E", "item": "task", "slug": "feature", "approved_proposal": "approved",
                 "coverage_report": {"requirements": [{"status": "missing"}]}}
        with patch.object(main.final_checks, "run", return_value={"status": "pass", "checks": []}) as checks, \
             patch.object(main.phase_checkpoint, "store"), patch.object(main, "save_state"), \
             patch.object(main, "announce_milestone"), patch.object(main.agent_runner, "run") as agent:
            main.do_e2e(state)
        self.assertEqual(state["state"], "ARCHIVING")
        checks.assert_called_once()
        agent.assert_not_called()
