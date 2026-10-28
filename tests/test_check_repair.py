"""Failed/unknown checks repair autonomously; only controller evidence can pass."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main
import check_repair
checks = main.checks
from check_plan import Check
from execution_store import ExecutionStore


def failed(status="infrastructure"):
    return {"status": "indeterminate" if status in ("infrastructure", "unknown") else "fail",
            "checks": [{"check": "unit:backend", "status": status, "report": "missing-pytz.log",
                        "gate": {"status": "indeterminate" if status != "fail" else "fail",
                                 "regressions": [], "preexisting": []}}]}


class CheckRepairTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(main, "_tracked_snapshot", return_value=("head", ""))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_final_failure_starts_repair_without_asking_or_blind_retry(self):
        for status in ("infrastructure", "unknown", "fail"):
            with self.subTest(status=status):
                state = {"state": "E2E", "slug": "task", "item": "task", "session_id": "sid"}
                with patch.object(main.config, "DETERMINISTIC_CHECKS", True), \
                     patch.object(main, "_scope_drift", return_value=False), \
                     patch.object(main, "content_snapshot", return_value="same"), \
                     patch.object(main.final_checks, "run", return_value=failed(status)), \
                     patch.object(main, "save_state"), patch.object(main, "email") as send, \
                     patch.object(main.agent_runner, "resume") as agent:
                    main.do_e2e(state)
                self.assertEqual(state["state"], "REPAIR_CHECKS")
                self.assertEqual(state["check_repair"]["attempt"], 1)
                self.assertNotIn("pending_question", state)
                self.assertIn("implementing a fix", send.call_args.args[2])
                agent.assert_not_called()

    def test_environment_only_repair_delivers_findings_and_rechecks_affected_environment(self):
        state = {"state": "REPAIR_CHECKS", "slug": "task", "item": "task", "session_id": "sid",
                 "base_sha": "a" * 40, "quality_controller_validated": True,
                 "quality_report": {"status": "pass"}, "internal_review_report": {"status": "pass"},
                 "check_repair": {"attempt": 1, "resume": "E2E", "before": "same", "checks": failed()["checks"]}}
        answer = SimpleNamespace(output="Installed declared pytz in backend/.venv; import probe passed.", attachments=[])
        with patch.object(main, "_scope_drift", return_value=False), \
             patch.object(main, "content_snapshot", return_value="same"), \
             patch.object(main.agent_runner, "resume", return_value=answer) as agent, \
             patch.object(main, "handle_result", return_value=False), \
             patch.object(main, "email") as send, patch.object(main, "save_state"), \
             patch.object(main, "trail"), patch.object(main.phase_checkpoint, "store"), \
             patch.object(checks, "invalidate_environment") as invalidate:
            main.do_repair_checks(state)
        self.assertIn("SAME interpreter", agent.call_args.args[1])
        self.assertIn("missing-pytz.log", agent.call_args.args[1])
        self.assertEqual(state["state"], "E2E")
        self.assertNotIn("quality_controller_validated", state)
        self.assertIn("Installed declared pytz", send.call_args.args[2])
        self.assertEqual(invalidate.call_args.args[2], ["unit:backend"])
        self.assertNotIn("final_check_report", state)

    def test_code_repair_requires_reverification_and_review(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "openspec/changes/task").mkdir(parents=True)
            state = {"state": "REPAIR_CHECKS", "slug": "task", "item": "task",
                     "check_repair": {"attempt": 1, "resume": "E2E", "before": "old", "checks": failed()["checks"]},
                     "quality_report": {}, "internal_review_report": {"status": "pass"}}
            with patch.object(main.config, "REPO_PATH", Path(root)), \
                 patch.object(main, "_scope_drift", return_value=False), \
                 patch.object(main, "content_snapshot", return_value="new"), \
                 patch.object(main, "save_state"), patch.object(main, "trail"), \
                 patch.object(main.phase_checkpoint, "store"), patch.object(checks, "invalidate_environment"):
                main._complete_check_repair(state, SimpleNamespace(output="Fixed lint regression"))
            self.assertEqual(state["state"], "VERIFYING")
            self.assertNotIn("quality_report", state)
            # Kept, but reusable only for the exact reviewed content: code changed, so it re-runs.
            self.assertNotIn("reviewed_snapshot", state)

    def test_no_progress_repair_is_bounded_and_slack_choice_resumes_diagnosis(self):
        state = {"state": "E2E", "slug": "task", "item": "task", "session_id": "sid",
                 "check_repair": {"attempt": 3, "findings": "Registry requires unavailable credentials"}}
        with patch.object(main.config, "QUALITY_GATE_MAX_ROUNDS", 3), \
             patch.object(main, "content_snapshot", return_value="same"), \
             patch.object(main, "save_state"), patch.object(main, "email") as send:
            main._begin_check_repair(state, failed(), resume="E2E")
        self.assertEqual(state["state"], "WAIT_REPLY")
        self.assertEqual(state["return_state"], "REPAIR_CHECKS")
        self.assertIn("Registry requires", state["pending_question"])
        self.assertIn("implement a different fix", state["pending_question"])
        self.assertIn("3. Keep waiting", state["pending_question"])
        # Same live context survives restart; user supplies recovery choice through Slack.
        reloaded = json.loads(json.dumps(state))
        text = main.handoffs.remember_decision(reloaded, "question during REPAIR_CHECKS", reloaded["pending_question"])
        self.assertIn("Recommended", text)
        selection = main.handoffs.selected_reply(reloaded, "1")
        prompt = main._reply_prompt(reloaded, "REPAIR_CHECKS", selection["reply"])
        self.assertEqual(reloaded["check_repair"]["attempt"], 1)
        self.assertIn("different fix", prompt)
        self.assertIn("Commit the intended tracked changes", prompt)

    def test_legacy_e2e_reply_gets_repair_prompt_not_bare_answer(self):
        state = {"state": "WAIT_REPLY", "return_state": "E2E", "slug": "task",
                 "final_check_report": failed(), "final_check_round": 3}
        with patch.object(main.config, "DETERMINISTIC_CHECKS", True), \
             patch.object(main, "content_snapshot", return_value="same"):
            prompt = main._reply_prompt(state, "E2E", "Fix it; don't ask me to edit the bot")
        self.assertIn("Diagnose before retrying", prompt)
        self.assertIn("missing-pytz.log", prompt)
        self.assertIn("Fix it; don't ask", prompt)
        self.assertEqual(state["check_repair"]["resume"], "E2E")

    def test_repair_revision_invalidates_only_affected_checks(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "input").write_text("same content")
            store = ExecutionStore(Path(root) / "data/db")
            backend = Check("backend", [sys.executable, "-c", "print('pass')"])
            frontend = Check("frontend", [sys.executable, "-c", "print('pass')"])
            with patch.object(checks, "tool_versions", return_value={}):
                checks.execute(backend, repo, store, "task")
                checks.execute(frontend, repo, store, "task")
                checks.invalidate_environment(store, repo, ["backend"])
                new = checks.execute(backend, repo, store, "task")
                cached = checks.execute(frontend, repo, store, "task")
            self.assertFalse(new["reused"])
            self.assertEqual(new["repetition_reason"], "environment_or_command_changed")
            self.assertTrue(cached["reused"])

    def test_repair_prose_does_not_approve_gate(self):
        state = {"state": "REPAIR_CHECKS", "slug": "task", "item": "task",
                 "check_repair": {"attempt": 1, "resume": "E2E", "before": "same", "checks": failed()["checks"]}}
        with patch.object(main, "_scope_drift", return_value=False), \
             patch.object(main, "content_snapshot", return_value="same"), \
             patch.object(main.phase_checkpoint, "store"), patch.object(checks, "invalidate_environment"), \
             patch.object(main, "save_state"), patch.object(main, "trail"):
            main._complete_check_repair(state, SimpleNamespace(output="All errors are historical, ignore them."))
        self.assertEqual(state["state"], "E2E")
        self.assertNotIn("e2e_passed", state)
        self.assertNotIn("check_waivers", state)

    def test_push_repair_preserves_push_context_and_never_pushes(self):
        state = {"state": "PUSHING", "slug": "task", "item": "task",
                 "push_context": {"continuation": "threads", "output": "RESOLVE: x"}}
        with patch.object(main.config, "DETERMINISTIC_CHECKS", True), \
             patch.object(main.final_checks, "run", return_value=failed()), \
             patch.object(main, "content_snapshot", return_value="same"), \
             patch.object(main, "save_state"), patch.object(main, "email"), patch.object(main, "git") as git:
            main.do_push(state)
        git.assert_not_called()
        self.assertEqual(state["state"], "REPAIR_CHECKS")
        self.assertEqual(state["check_repair"]["resume"], "PUSHING")
        self.assertEqual(state["push_context"]["output"], "RESOLVE: x")

    def test_uncommitted_repair_cannot_advance_to_checks_or_push(self):
        state = {"state": "REPAIR_CHECKS", "slug": "task", "item": "task",
                 "check_repair": {"attempt": 1, "resume": "PUSHING", "before": "same", "checks": failed()["checks"]}}
        with patch.object(main, "_scope_drift", return_value=False), \
             patch.object(main, "_tracked_snapshot", return_value=("head", " M src/repair.py")), \
             patch.object(main, "save_state"), patch.object(checks, "invalidate_environment") as invalidate:
            main._complete_check_repair(state, SimpleNamespace(output="Edited repair; not committed"))
        self.assertEqual(state["state"], "REPAIR_CHECKS")
        self.assertEqual(state["check_repair"]["attempt"], 2)
        self.assertIn("commit only intended", state["check_repair"]["previous"])
        invalidate.assert_not_called()

    def test_archived_evidence_repair_requires_review(self):
        state = {"state": "REPAIR_CHECKS", "slug": "task", "item": "task", "archive_path": "archive/task",
                 "push_context": {"continuation": "evidence"},
                 "check_repair": {"attempt": 1, "resume": "PUSHING", "before": "old",
                                  "checks": failed()["checks"], "evidence": True}}
        with patch.object(main, "content_snapshot", return_value="new"), \
             patch.object(main.phase_checkpoint, "store"), patch.object(checks, "invalidate_environment"), \
             patch.object(main, "save_state"), patch.object(main, "trail"):
            main._complete_check_repair(state, SimpleNamespace(output="Implemented missing demo and video support"))
        self.assertEqual(state["state"], "EVIDENCE_REVIEW")
        self.assertEqual(state["push_context"]["continuation"], "evidence")
        self.assertNotIn("e2e_passed", state)
        prompt = check_repair.prompt(state, state["check_repair"])
        self.assertIn("Implement missing approved functionality", prompt)
        self.assertIn("Do not weaken inventory validation", prompt)

    def test_evidence_push_requires_checks_even_when_default_disabled(self):
        state = {"state": "PUSHING", "slug": "task", "item": "task",
                 "push_context": {"continuation": "evidence"}}
        with patch.object(main.config, "DETERMINISTIC_CHECKS", False), \
             patch.object(main.final_checks, "run", return_value=failed()) as run, \
             patch.object(main, "content_snapshot", return_value="same"), \
             patch.object(main, "save_state"), patch.object(main, "email"), patch.object(main, "git") as git:
            main.do_push(state)
        run.assert_called_once()
        git.assert_not_called()
        self.assertEqual(state["state"], "REPAIR_CHECKS")
