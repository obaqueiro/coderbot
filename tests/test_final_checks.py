from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from check_plan import Check
from execution_store import ExecutionStore
import final_checks


class FinalCheckTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("final_checks.snapshot", return_value="fixture-content")
        patcher.start()
        self.addCleanup(patcher.stop)
    def test_push_failure_preserves_continuation_before_any_push(self):
        import main
        state = {"state": "PUSHING", "branch": "b", "item": "t",
                 "push_context": {"continuation": "threads", "output": "RESOLVE: x"}}
        with patch.object(main.config, "DETERMINISTIC_CHECKS", True), \
             patch.object(main.final_checks, "run", return_value={"status": "indeterminate", "checks": []}), \
             patch.object(main, "save_state"), patch.object(main, "email"), \
             patch.object(main, "content_snapshot", return_value="same"), \
             patch.object(main, "git") as git:
            main.do_push(state)
        git.assert_not_called()
        self.assertEqual(state["push_context"]["output"], "RESOLVE: x")
    def test_only_new_regressions_block_final_gate(self):
        with tempfile.TemporaryDirectory() as root:
            store = ExecutionStore(Path(root) / "db")
            failed = {"status": "fail", "failures": {"old": "old", "new": "new"}}
            with patch("final_checks.check_plan.discover", return_value=[Check("unit", ["test"])]), \
                 patch("final_checks.checks.execute_plan", return_value=[failed]), \
                 patch("final_checks.baseline_result", return_value={"status": "fail", "failures": {"old": "old"}}):
                report = final_checks.run({"base_sha": "a" * 40}, root, store)
            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["checks"][0]["gate"]["regressions"], ["new"])
    def test_confirmed_preexisting_failures_do_not_block_regression_gate(self):
        with tempfile.TemporaryDirectory() as root:
            store = ExecutionStore(Path(root) / "db")
            failed = {"status": "fail", "failures": {f"test_{i}": "AssertionError: baseline" for i in range(18)}}
            with patch("final_checks.check_plan.discover", return_value=[Check("e2e", ["run"])]), \
                 patch("final_checks.checks.execute_plan", return_value=[failed]), \
                 patch("final_checks.baseline_result", return_value=failed):
                report = final_checks.run({"branch": "b", "base_sha": "a" * 40}, root, store)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["suite_status"], "fail")
            self.assertEqual(len(report["checks"][0]["gate"]["preexisting"]), 18)
    def test_without_harness_still_executes_complete_unit_suite(self):
        with tempfile.TemporaryDirectory() as root:
            store = ExecutionStore(Path(root) / "db")
            plan = [Check("unit", ["python", "test"])]
            with patch("final_checks.check_plan.discover", return_value=plan), \
                 patch("final_checks.checks.execute_plan", return_value=[{"status": "pass"}]) as run:
                result = final_checks.run({"branch": "b"}, root, store)
            self.assertEqual(result["status"], "pass")
            run.assert_called_once()

    def test_preexisting_cheap_failure_cannot_create_success_from_partial_results(self):
        plan = [Check("unit", ["test"]), Check("full", ["playwright", "test"], kind="e2e")]
        failed = {"status": "fail", "failures": {"old": "baseline"}}
        with tempfile.TemporaryDirectory() as root:
            store = ExecutionStore(Path(root) / "db")
            with patch("final_checks.check_plan.discover", return_value=plan), \
                 patch("checks.execute_plan", return_value=[failed]), \
                 patch("final_checks.baseline_result", return_value=failed):
                report = final_checks.run({"base_sha": "a" * 40}, root, store)
        self.assertEqual(report["status"], "indeterminate")
        self.assertEqual(report["stage"], "needs_focused_e2e")

    def test_real_focused_pass_preserves_failed_historical_audit_without_running_it(self):
        focused = Check("focused", ["playwright", "test", "feature.spec.ts"], kind="e2e", scope="focused")
        full = Check("full", ["playwright", "test"], kind="e2e")
        failed = {"status": "fail", "failures": {"old": "baseline"}}
        with tempfile.TemporaryDirectory() as root:
            store = ExecutionStore(Path(root) / "db")
            state = {"base_sha": "a" * 40, "reported_check_plan": [focused.to_dict()]}
            store.put("check_run", task_id=store.task_identity(state, root), status="complete",
                      data={"check": full.to_dict(), "result": {**failed, "report": "old-audit.log"}})
            with patch("final_checks.check_plan.discover", return_value=[full]), \
                 patch("checks.execute_plan", return_value=[{"status": "pass", "test_count": 1}]) as run, \
                 patch("final_checks.baseline_result", return_value=failed) as baseline:
                report = final_checks.run(state, root, store)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["suite_status"], "fail")
        self.assertEqual([r["check"] for r in report["checks"]], ["focused"])
        self.assertEqual(report["audit_checks"][0]["failures"], {"old": "baseline"})
        self.assertEqual(report["audit_checks"][0]["report"], "old-audit.log")
        self.assertNotIn("gate", report["audit_checks"][0])
        self.assertEqual(report["verification_scope"], "focused_e2e")
        run.assert_called_once()
        baseline.assert_not_called()

    def test_same_id_focused_registration_replaces_discovered_full_command(self):
        full = Check("e2e", ["playwright", "test"], kind="e2e")
        focused = Check("e2e", ["playwright", "test", "feature.spec.ts"], kind="e2e", scope="focused")
        with tempfile.TemporaryDirectory() as root:
            store = ExecutionStore(Path(root) / "db")
            state = {"reported_check_plan": [focused.to_dict()]}
            store.put("check_run", task_id=store.task_identity(state, root), status="complete",
                      data={"check": full.to_dict(), "result": {"check": "e2e", "status": "pass",
                          "report": "old-source-audit.log", "gate": {"status": "pass"}}})
            with patch("final_checks.check_plan.discover", return_value=[full]), \
                 patch("checks.execute_plan", return_value=[{"status": "pass", "test_count": 2}]) as run:
                report = final_checks.run(state, root, store)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["suite_status"], "indeterminate")
        self.assertTrue(report["audit_checks"][0]["historical"])
        self.assertNotIn("gate", report["audit_checks"][0])
        self.assertEqual(run.call_args.args[0][0].argv, focused.argv)

    def test_focused_failure_is_never_excused_by_a_historical_baseline(self):
        focused = Check("e2e", ["playwright", "test", "feature.spec.ts"], kind="e2e", scope="focused")
        failed = {"status": "fail", "failures": {"feature": "assertion"}, "test_count": 2}
        with tempfile.TemporaryDirectory() as root:
            with patch("final_checks.check_plan.discover", return_value=[focused]), \
                 patch("checks.execute_plan", return_value=[failed]), patch("final_checks.baseline_result") as baseline:
                report = final_checks.run({"base_sha": "a" * 40}, root, ExecutionStore(Path(root) / "db"))
        self.assertNotEqual(report["status"], "pass")
        self.assertEqual(report["checks"][0]["failures"], failed["failures"])
        baseline.assert_not_called()

    def test_source_drift_blocks_even_when_every_check_passes(self):
        with tempfile.TemporaryDirectory() as root:
            with patch("final_checks.snapshot", side_effect=["before", "after"]), \
                 patch("final_checks.check_plan.discover", return_value=[Check("unit", ["test"])]), \
                 patch("checks.execute_plan", return_value=[{"status": "pass"}]):
                report = final_checks.run({}, root, ExecutionStore(Path(root) / "db"))
        self.assertEqual(report["status"], "indeterminate")
        self.assertTrue(report["source_changed"])

    def test_implementation_output_registers_browser_checks_without_executing_them(self):
        import json
        import main
        from types import SimpleNamespace
        result = SimpleNamespace(session_id="session", attachments=[], question=None,
            output="CHECK_PLAN: " + json.dumps({"version": 1, "checks": [Check("full", ["playwright", "test"]).to_dict()]}))
        state = {"state": "IMPLEMENTING", "item": "task"}
        with patch.object(main.config, "TARGET_MODE", "repo"), patch.object(main.checks, "execute_plan") as run, \
             patch.object(main, "_maybe_request_credential", return_value=False), patch.object(main, "_transcript_append"):
            main.handle_result(state, result, "IMPLEMENTING")
        run.assert_not_called()
        self.assertEqual(state["reported_check_plan"][0]["id"], "full")

    def test_approval_scope_ignores_checkboxes_not_changed_requirements(self):
        import proposal_package
        files = {"tasks.md": "- [ ] Implement\n", "specs/feature/spec.md": "Required behavior"}
        approved = proposal_package.scope_fingerprint(files)
        files["tasks.md"] = "- [x] Implement\n"
        self.assertEqual(proposal_package.scope_fingerprint(files), approved)
        files["specs/feature/spec.md"] = "Different behavior"
        self.assertNotEqual(proposal_package.scope_fingerprint(files), approved)

    def test_skipped_checks_never_claim_the_raw_suite_is_green(self):
        value = final_checks.report({"stage": "complete", "checks": [
            {"check": "optional", "status": "skipped", "gate": {"status": "pass"}}]}, "/repo")
        self.assertEqual(value["status"], "pass")
        self.assertEqual(value["suite_status"], "skipped")

    def test_missing_selection_report_preserves_actionable_registration_details(self):
        issue = {"check": "e2e-compartido", "argv": ["bash", "e2e/run.sh"],
                 "reason": "unfiltered_e2e_registered_as_focused"}
        report = final_checks.report({"stage": "needs_focused_e2e", "checks": [], "selection_issues": [issue]}, "/repo")
        self.assertEqual(report["status"], "indeterminate")
        self.assertEqual(report["checks"][0]["selection_issues"], [issue])
