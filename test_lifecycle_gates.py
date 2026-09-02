"""State-machine tests for persisted verification and internal-review gates."""
import json
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

with patch.dict(sys.modules, {"gdoc_client": Mock(), "gmail_client": Mock()}):
    import main


def result(output, question=None):
    return SimpleNamespace(
        session_id="session-2", output=output, question=question, attachments=[])


QUALITY_PASS = (
    'QUALITY_GATE: {"status":"pass","commands":["python -m unittest: pass"],'
    '"openspec":"pass","tasks":"7/7"}'
)
REVIEW_PASS = (
    'INTERNAL_REVIEW: {"status":"pass","critical":0,"important":0,'
    '"tests":["python -m unittest: pass"]}'
)


class GateParserTests(unittest.TestCase):
    def test_quality_gate_accepts_one_valid_final_contract(self):
        parsed, reason = main.parse_quality_gate("verification notes\n" + QUALITY_PASS + "\n")

        self.assertEqual(parsed["tasks"], "7/7")
        self.assertIsNone(reason)

    def test_quality_gate_rejects_invalid_contracts(self):
        invalid = {
            "missing": "verification notes",
            "not final": QUALITY_PASS + "\ntrailing text",
            "multiple": QUALITY_PASS + "\n" + QUALITY_PASS,
            "marker in prose": "The format is QUALITY_GATE: example\n" + QUALITY_PASS,
            "malformed": "QUALITY_GATE: {not-json}",
            "failed": QUALITY_PASS.replace('"status":"pass"', '"status":"fail"'),
            "empty commands": QUALITY_PASS.replace(
                '["python -m unittest: pass"]', "[]"),
            "non-string command": QUALITY_PASS.replace(
                '["python -m unittest: pass"]', "[42]"),
            "openspec failed": QUALITY_PASS.replace(
                '"openspec":"pass"', '"openspec":"fail"'),
            "incomplete tasks": QUALITY_PASS.replace('"tasks":"7/7"', '"tasks":"6/7"'),
            "nonnumeric tasks": QUALITY_PASS.replace('"tasks":"7/7"', '"tasks":"all/all"'),
            "extra field": QUALITY_PASS[:-1] + ',"extra":true}',
        }

        for name, output in invalid.items():
            with self.subTest(name=name):
                parsed, reason = main.parse_quality_gate(output)
                self.assertIsNone(parsed)
                self.assertIsInstance(reason, str)
                self.assertTrue(reason)

    def test_internal_review_accepts_clean_final_contract(self):
        parsed, reason = main.parse_internal_review("review notes\n" + REVIEW_PASS)

        self.assertEqual(parsed["important"], 0)
        self.assertIsNone(reason)

    def test_internal_review_rejects_invalid_contracts(self):
        invalid = {
            "missing": "review notes",
            "not final": REVIEW_PASS + "\ntrailing text",
            "multiple": REVIEW_PASS + "\n" + REVIEW_PASS,
            "marker in prose": "The format is INTERNAL_REVIEW: example\n" + REVIEW_PASS,
            "malformed": "INTERNAL_REVIEW: {not-json}",
            "failed": REVIEW_PASS.replace('"status":"pass"', '"status":"fail"'),
            "critical": REVIEW_PASS.replace('"critical":0', '"critical":1'),
            "important": REVIEW_PASS.replace('"important":0', '"important":2'),
            "boolean count": REVIEW_PASS.replace('"critical":0', '"critical":false'),
            "empty tests": REVIEW_PASS.replace('["python -m unittest: pass"]', "[]"),
            "non-string test": REVIEW_PASS.replace('["python -m unittest: pass"]', "[42]"),
            "extra field": REVIEW_PASS[:-1] + ',"extra":true}',
        }

        for name, output in invalid.items():
            with self.subTest(name=name):
                parsed, reason = main.parse_internal_review(output)
                self.assertIsNone(parsed)
                self.assertIsInstance(reason, str)
                self.assertTrue(reason)


class LifecycleGateTests(unittest.TestCase):
    def base_state(self, **updates):
        state = {
            "state": "IMPLEMENTING",
            "slug": "api-version",
            "branch": "codebot-api-version",
            "item": "Support another API version",
            "session_id": "session-1",
            "has_e2e_harness": True,
        }
        state.update(updates)
        return state

    def test_implementation_advances_to_verification(self):
        state = self.base_state()
        with patch.object(main.agent_runner, "resume", return_value=result("implemented")), \
             patch.object(main, "_scrub_evidence_from_repo", return_value=[]):
            main.do_implement(state)

        self.assertEqual(state["state"], "VERIFYING")
        self.assertEqual(state["verify_round"], 0)

    def test_verification_passes_to_internal_review(self):
        state = self.base_state(state="VERIFYING", verify_round=1)
        instructions = json.dumps({
            "state": "all_done",
            "progress": {"total": 7, "complete": 7, "remaining": 0},
        })
        checks = [
            subprocess.CompletedProcess([], 0, "valid", ""),
            subprocess.CompletedProcess([], 0, instructions, ""),
        ]
        with patch.object(main.agent_runner, "resume", return_value=result(QUALITY_PASS)) as resume, \
             patch.object(main.subprocess, "run", side_effect=checks) as process:
            main.do_verify(state)

        self.assertIn("verification phase", resume.call_args.args[1])
        self.assertEqual(state["state"], "INTERNAL_REVIEW")
        self.assertNotIn("verify_round", state)
        self.assertEqual(
            process.call_args_list[0].args[0],
            ["openspec", "validate", "api-version", "--strict", "--no-interactive"])
        self.assertEqual(
            process.call_args_list[1].args[0],
            ["openspec", "instructions", "apply", "--change", "api-version", "--json"])

    def test_verification_independent_checks_fail_through_normal_retry(self):
        valid = subprocess.CompletedProcess([], 0, json.dumps({
            "state": "all_done",
            "progress": {"total": 7, "complete": 7, "remaining": 0},
        }), "")
        failures = {
            "validate command": [subprocess.CompletedProcess([], 1, "", "invalid")],
            "instructions command": [
                subprocess.CompletedProcess([], 0, "valid", ""),
                subprocess.CompletedProcess([], 1, "", "failed"),
            ],
            "malformed instructions": [
                subprocess.CompletedProcess([], 0, "valid", ""),
                subprocess.CompletedProcess([], 0, "not-json", ""),
            ],
            "wrong state": [
                subprocess.CompletedProcess([], 0, "valid", ""),
                subprocess.CompletedProcess([], 0, json.dumps({
                    "state": "ready",
                    "progress": {"total": 7, "complete": 7, "remaining": 0},
                }), ""),
            ],
            "remaining tasks": [
                subprocess.CompletedProcess([], 0, "valid", ""),
                subprocess.CompletedProcess([], 0, json.dumps({
                    "state": "all_done",
                    "progress": {"total": 7, "complete": 6, "remaining": 1},
                }), ""),
            ],
            "inconsistent totals": [
                subprocess.CompletedProcess([], 0, "valid", ""),
                subprocess.CompletedProcess([], 0, json.dumps({
                    "state": "all_done",
                    "progress": {"total": 7, "complete": 6, "remaining": 0},
                }), ""),
            ],
        }
        for name, checks in failures.items():
            with self.subTest(name=name):
                state = self.base_state(state="VERIFYING", verify_round=0)
                with patch.object(main.agent_runner, "resume", return_value=result(QUALITY_PASS)), \
                     patch.object(main.subprocess, "run", side_effect=checks):
                    main.do_verify(state)
                self.assertEqual(state["state"], "VERIFYING")
                self.assertEqual(state["verify_round"], 1)

    def test_clean_review_routes_by_e2e_capability(self):
        for has_harness, expected in ((True, "E2E"), (False, "ARCHIVING")):
            with self.subTest(has_harness=has_harness):
                state = self.base_state(
                    state="INTERNAL_REVIEW", review_gate_round=1,
                    has_e2e_harness=has_harness)
                with patch.object(main.agent_runner, "resume", return_value=result(REVIEW_PASS)):
                    main.do_internal_review(state)
                self.assertEqual(state["state"], expected)
                self.assertNotIn("review_gate_round", state)

    def test_invalid_gate_retries_then_emails_and_waits(self):
        state = self.base_state(state="VERIFYING", verify_round=0)
        with patch.object(main.config, "QUALITY_GATE_MAX_ROUNDS", 2, create=True), \
             patch.object(main.agent_runner, "resume",
                          side_effect=[result("bad"), result("still bad")]), \
             patch.object(main, "email") as email:
            main.do_verify(state)
            self.assertEqual(state["state"], "VERIFYING")
            self.assertEqual(state["verify_round"], 1)
            main.do_verify(state)

        self.assertEqual(state["state"], "WAIT_REPLY")
        self.assertEqual(state["return_state"], "VERIFYING")
        self.assertEqual(state["verify_round"], 2)
        email.assert_called_once()

    def test_gate_reply_is_parsed_instead_of_bypassing_gate(self):
        state = self.base_state(
            state="WAIT_REPLY", return_state="VERIFYING", verify_round=3)
        with patch.object(main.agent_runner, "resume",
                          return_value=result("still malformed")) as resume, \
             patch.object(main.config, "QUALITY_GATE_MAX_ROUNDS", 3, create=True):
            main._handle_reply(state, "please rerun the checks")

        self.assertIn("please rerun the checks", resume.call_args.args[1])
        self.assertEqual(state["state"], "VERIFYING")
        self.assertEqual(state["verify_round"], 1)
        self.assertNotIn("return_state", state)

    def test_e2e_repair_with_tracked_change_returns_to_verification(self):
        state = self.base_state(state="E2E", e2e_round=0)
        with patch.object(main.evidence, "run_suite", return_value=(False, "failure")), \
             patch.object(main, "git", side_effect=["head-1", "", "head-2", ""]), \
             patch.object(main.agent_runner, "resume", return_value=result("fixed")):
            main.do_e2e(state)

        self.assertEqual(state["state"], "VERIFYING")
        self.assertEqual(state["verify_round"], 0)

    def test_e2e_repair_snapshot_is_saved_before_agent_resume(self):
        state = self.base_state(state="E2E", e2e_round=0)
        events = []

        def save(saved):
            events.append(("save", saved["e2e_repair_head"], saved["e2e_repair_status"]))

        def resume(*args):
            events.append(("resume",))
            return result("fixed")

        with patch.object(main.evidence, "run_suite", return_value=(False, "failure")), \
             patch.object(main, "git", side_effect=["head-1", "", "head-2", ""]), \
             patch.object(main, "save_state", side_effect=save), \
             patch.object(main.agent_runner, "resume", side_effect=resume):
            main.do_e2e(state)

        self.assertEqual(events[0], ("save", "head-1", ""))
        self.assertEqual(events[1], ("resume",))

    def test_e2e_repair_without_tracked_change_reruns_e2e(self):
        state = self.base_state(state="E2E", e2e_round=0)
        with patch.object(main.evidence, "run_suite", return_value=(False, "failure")), \
             patch.object(main, "git", side_effect=["head-1", " M tracked", "head-1", " M tracked"]), \
             patch.object(main.agent_runner, "resume", return_value=result("no code change")):
            main.do_e2e(state)

        self.assertEqual(state["state"], "E2E")

    def test_e2e_restart_with_changed_repair_enters_verification_before_suite(self):
        state = self.base_state(
            state="E2E", e2e_round=1, e2e_repair_head="head-1", e2e_repair_status="")
        with patch.object(main, "git", side_effect=["head-2", ""]), \
             patch.object(main.evidence, "run_suite") as run_suite:
            main.do_e2e(state)

        run_suite.assert_not_called()
        self.assertEqual(state["state"], "VERIFYING")
        self.assertNotIn("e2e_repair_head", state)
        self.assertNotIn("e2e_repair_status", state)

    def test_e2e_restart_with_unchanged_repair_reruns_suite(self):
        state = self.base_state(
            state="E2E", e2e_round=1, e2e_repair_head="head-1",
            e2e_repair_status=" M tracked")
        with patch.object(main, "git", side_effect=["head-1", " M tracked"]), \
             patch.object(main.evidence, "run_suite", return_value=(True, "ok")) as run_suite:
            main.do_e2e(state)

        run_suite.assert_called_once_with()
        self.assertEqual(state["state"], "ARCHIVING")

    def test_e2e_paths_enter_archiving(self):
        no_harness = self.base_state(state="E2E", has_e2e_harness=False)
        passed = self.base_state(state="E2E", has_e2e_harness=True)
        with patch.object(main.evidence, "run_suite", return_value=(True, "ok")):
            main.do_e2e(no_harness)
            main.do_e2e(passed)

        self.assertEqual(no_harness["state"], "ARCHIVING")
        self.assertEqual(passed["state"], "ARCHIVING")

    def test_e2e_exhaustion_reply_without_repair_snapshot_reruns_e2e(self):
        state = self.base_state(
            state="WAIT_REPLY", return_state="E2E", e2e_round=6)
        with patch.object(main.agent_runner, "resume", return_value=result("try again")):
            main._handle_reply(state, "service is back")

        self.assertEqual(state["state"], "E2E")
        self.assertEqual(state["e2e_round"], 0)
        self.assertNotIn("return_state", state)

    def test_abort_cleanup_removes_new_context_from_old_state_safely(self):
        state = self.base_state(
            state="VERIFYING", verify_round=2, review_gate_round=1,
            archive_round=2, archive_path="openspec/changes/archive/example",
            e2e_repair_head="abc", e2e_repair_status=" M app.py",
            push_context={"continuation": "review"}, archive_error="failed")
        with patch.object(main, "_reset_to_base_branch", return_value=[]), \
             patch.object(main, "email"):
            main._abort_and_reset(state, "stop")

        self.assertEqual(state["state"], "IDLE")
        for key in ("verify_round", "review_gate_round", "archive_round", "archive_path",
                    "e2e_repair_head", "e2e_repair_status"):
            self.assertNotIn(key, state)
        self.assertNotIn("push_context", state)
        self.assertNotIn("archive_error", state)


if __name__ == "__main__":
    unittest.main()
