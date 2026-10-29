"""State-machine tests for persisted verification and internal-review gates."""
import json
import subprocess
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main

# Never let a test that reaches save_state() write the real data/state.json (it would
# poison a live deployment sharing this checkout).
main.config.STATE_PATH = pathlib.Path(tempfile.mkdtemp()) / "state.json"


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

    def test_quality_gate_accepts_optional_preexisting_failures(self):
        contract = ('QUALITY_GATE: {"status":"pass","commands":["npm run lint: fail (100 errors)"],'
                    '"preexisting":["npm run lint: same 100 errors on main"],'
                    '"openspec":"pass","tasks":"7/7"}')
        parsed, reason = main.parse_quality_gate("ok\n" + contract)
        self.assertIsNone(reason)
        self.assertEqual(parsed["preexisting"], ["npm run lint: same 100 errors on main"])
        parsed, reason = main.parse_quality_gate(contract.replace(
            '"preexisting":["npm run lint: same 100 errors on main"],', '"preexisting":[],'))
        self.assertIsNone(reason)
        for bad in ('"preexisting":"lint",', '"preexisting":[""],', '"preexisting":[1],'):
            parsed, reason = main.parse_quality_gate(contract.replace(
                '"preexisting":["npm run lint: same 100 errors on main"],', bad))
            self.assertIsNone(parsed, bad)
            self.assertIn("preexisting", reason)

    def test_failed_gate_reason_names_the_failing_commands(self):
        output = ('QUALITY_GATE: {"status":"fail","commands":["npm run lint: fail (100 errors)",'
                  '"npm run build: pass","git status --short: clean"],'
                  '"openspec":"pass","tasks":"7/7"}')
        parsed, reason = main.parse_quality_gate(output)
        self.assertIsNone(parsed)
        self.assertEqual(reason, "quality gate status is not pass "
                                 "(failing: npm run lint: fail (100 errors))")

    def test_gate_report_is_readable_with_failing_checks_first_and_agent_notes(self):
        output = ("Lint errors are all in legacy files.\n"
                  'QUALITY_GATE: {"status":"fail","commands":["npm run build: pass",'
                  '"npm run lint: fail (100 errors)","git status --short: clean"],'
                  '"preexisting":["pytest: 1 error (also on main)"],"openspec":"pass","tasks":"6/7"}')
        report = main._gate_report(output, "QUALITY_GATE")
        lines = report.splitlines()
        self.assertEqual(lines[0], "Checks the agent reported (failing first):")
        self.assertEqual(lines[1], "- npm run lint: fail (100 errors)")
        self.assertEqual(lines[2], "- pytest: 1 error (also on main) [preexisting]")
        self.assertIn("- npm run build: pass", lines[3:5])
        self.assertIn("- git status --short: clean", lines[3:5])
        self.assertIn('Other contract fields: openspec="pass", tasks="6/7"', report)
        self.assertIn("Agent's notes:\nLint errors are all in legacy files.", report)
        self.assertNotIn("QUALITY_GATE:", report)  # the raw JSON is not what the user reads

    def test_gate_report_without_contract_falls_back_to_agent_output(self):
        report = main._gate_report("I could not run anything because X.", "QUALITY_GATE")
        self.assertEqual(report, "Agent's notes:\nI could not run anything because X.")
        report = main._gate_report("QUALITY_GATE: {oops", "QUALITY_GATE")
        self.assertIn("Contract line as emitted (malformed): QUALITY_GATE: {oops", report)

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
    def setUp(self):
        # Gate tests exercise transition logic; milestone sends have their own coverage.
        patcher = patch.object(main, "announce_milestone")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(main, "content_snapshot", return_value="fixture")
        patcher.start()
        self.addCleanup(patcher.stop)

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
             patch.object(main.task_phases, "inventory", return_value=["Approved task"]), \
             patch.object(main, "_run_checked", return_value="{}"), \
             patch.object(main.task_phases, "validate_progress", return_value={
                 "total": 7, "complete": 7, "implementation": [], "final_checks": []}), \
             patch.object(main, "_scrub_evidence_from_repo", return_value=[]):
            main.do_implement(state)

        self.assertEqual(state["state"], "VERIFYING")
        self.assertEqual(state.get("verify_round", 0), 0)

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

        self.assertIn("verify OpenSpec change", resume.call_args.args[1])
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
        for has_harness, expected in ((True, "E2E"), (False, "E2E")):
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

    def test_rejected_gate_round_feeds_reason_and_report_into_retry_and_stuck_email(self):
        failing = ('Ran everything.\nQUALITY_GATE: {"status":"fail","commands":'
                   '["npm run lint: fail (100 errors)"],"openspec":"pass","tasks":"7/7"}')
        state = self.base_state(state="VERIFYING", verify_round=0)
        with patch.object(main.config, "QUALITY_GATE_MAX_ROUNDS", 2, create=True), \
             patch.object(main.config, "BASE_BRANCH", "develop", create=True), \
             patch.object(main.agent_runner, "resume",
                          side_effect=[result(failing), result(failing)]) as resume, \
             patch.object(main, "email") as email:
            main.do_verify(state)
            first_prompt = resume.call_args.args[1]
            main.do_verify(state)
            second_prompt = resume.call_args.args[1]

        self.assertNotIn("REJECTED", first_prompt)
        self.assertIn("against base commit develop", first_prompt)
        self.assertIn("rejected (round 1 of 2)", second_prompt)
        self.assertIn("quality gate status is not pass", second_prompt)
        self.assertIn("- npm run lint: fail (100 errors)", second_prompt)
        self.assertTrue(second_prompt.endswith(first_prompt))  # feedback is prepended only
        self.assertEqual(state["state"], "WAIT_REPLY")
        body = email.call_args.args[2]
        self.assertIn("Latest reason: quality gate status is not pass "
                      "(failing: npm run lint: fail (100 errors))", body)
        self.assertIn("diagnostic file", body)
        self.assertIn("Checks the agent reported (failing first)", body)
        self.assertIn("Agent's notes:\nRan everything.", body)
        self.assertIn("pre-existing on the base branch", body)

    def test_passing_gate_clears_feedback_and_first_round_gets_no_feedback(self):
        state = self.base_state(state="VERIFYING", verify_round=0,
                                verify_round_feedback={"reason": "stale", "report": "old"})
        self.assertEqual(main._gate_feedback(state, "verify_round"), "")  # round 0: ignored
        with patch.object(main.agent_runner, "resume", return_value=result(QUALITY_PASS)), \
             patch.object(main, "_run_checked", side_effect=[
                 "", json.dumps({"state": "all_done",
                                 "progress": {"total": 7, "complete": 7, "remaining": 0}})]):
            main.do_verify(state)
        self.assertEqual(state["state"], "INTERNAL_REVIEW")
        self.assertNotIn("verify_round_feedback", state)

    def test_gate_reply_is_parsed_instead_of_bypassing_gate(self):
        state = self.base_state(
            state="WAIT_REPLY", return_state="VERIFYING", verify_round=3)
        with patch.object(main.agent_runner, "run", return_value=Mock(output='{"action":"answer"}')), \
             patch.object(main.agent_runner, "resume",
                          return_value=result("still malformed")) as resume, \
             patch.object(main, "email"), \
             patch.object(main.config, "QUALITY_GATE_MAX_ROUNDS", 3, create=True):
            main._handle_reply(state, "please rerun the checks")

        self.assertIn("please rerun the checks", resume.call_args.args[1])
        self.assertEqual(state["state"], "VERIFYING")
        self.assertEqual(state["verify_round"], 1)
        self.assertNotIn("return_state", state)

    def test_e2e_exhaustion_reply_without_repair_snapshot_reruns_e2e(self):
        state = self.base_state(
            state="WAIT_REPLY", return_state="E2E", e2e_round=6)
        with patch.object(main.agent_runner, "run", return_value=Mock(output='{"action":"answer"}')), \
             patch.object(main, "email"), \
             patch.object(main.agent_runner, "resume", return_value=result("try again")):
            main._handle_reply(state, "service is back")

        self.assertEqual(state["state"], "E2E")
        self.assertEqual(state.get("e2e_round", 0), 0)
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


class SelfHealingCodeReviewSwitch(unittest.TestCase):
    def _seed(self, enabled):
        state = {"has_e2e_harness": True, "e2e_kind": "playwright", "has_code_review": False}
        with patch.object(main.config, "SELF_HEAL_CODE_REVIEW", enabled), \
             patch.object(main.task_source, "ensure_item", return_value=True) as ensure:
            main._seed_self_healing_items(state)
        return ensure

    def test_seeds_code_review_item_by_default(self):
        ensure = self._seed(True)
        ensure.assert_called_once_with(main.CODE_REVIEW_ITEM)

    def test_off_skips_code_review_item(self):
        ensure = self._seed(False)
        ensure.assert_not_called()
