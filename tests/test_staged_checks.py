import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import check_plan
import staged_checks
import prompts
import claude_runner
from execution_store import ExecutionStore


class StagedChecksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ExecutionStore(self.root / "data/db")
        self.state = {"execution_task_id": "task"}
        self.plan = [check_plan.Check("unit", ["echo", "unit"]),
                     check_plan.Check("focused", ["playwright", "test", "feature.spec.ts"], scope="focused", kind="e2e"),
                     check_plan.Check("full", ["playwright", "test"], kind="e2e")]
        for target, value in (("checks.tool_versions", {}), ("checks.check_environment", {})):
            p = patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def execute(self, selected, *args, **kwargs):
        return [{"check": c.id, "status": "pass", "test_count": 1} for c in selected]

    def test_default_gate_runs_cheap_and_focused_not_full(self):
        with patch("checks.execute_plan", side_effect=self.execute) as run:
            result = staged_checks.run(self.state, self.plan, self.root, self.store)
        self.assertEqual([[c.id for c in call.args[0]] for call in run.call_args_list], [["unit"], ["focused"]])
        self.assertEqual(result["stage"], "complete")
        self.assertEqual(result["audit_checks"][0]["status"], "not_run")
        self.assertFalse(result["audit_checks"][0]["required"])

    def test_explicit_audit_keeps_cheap_focused_full_order(self):
        with patch("checks.execute_plan", side_effect=self.execute) as run:
            result = staged_checks.run(self.state, self.plan, self.root, self.store, audit=True)
        self.assertEqual([[c.id for c in call.args[0]] for call in run.call_args_list], [["unit"], ["focused"], ["full"]])
        self.assertEqual(result["stage"], "complete")

    def test_exhausted_audits_and_unknown_history_do_not_block_focused_gate(self):
        for i in range(2):
            self.store.put("operation", task_id="task", identity="full-e2e-launch", data={"launch": i})
            self.store.put("check_run", task_id="task", status="complete", data={
                "check": self.plan[2].to_dict(), "result": {"check": "full", "status": "unknown",
                    "exit_code": 1, "failures": {}, "report": f"audit-{i}.log", "content": "old-source"}})
        before = self.store.list("check_run", "task")
        with patch("checks.execute_plan", side_effect=self.execute) as run:
            result = staged_checks.run(dict(self.state), self.plan, self.root, ExecutionStore(self.store.path))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result["stage"], "complete")
        self.assertEqual([r["status"] for r in result["audit_checks"]], ["unknown", "unknown"])
        self.assertTrue(all(r["historical"] and not r["required"] for r in result["audit_checks"]))
        self.assertEqual(self.store.list("check_run", "task"), before)
        self.assertEqual(len(self.store.list("operation", "task", identity="full-e2e-launch")), 2)

    def test_selected_e2e_with_legacy_full_label_is_required_focused_execution(self):
        selected = check_plan.Check("legacy", ["playwright", "test", "feature.spec.ts"], kind="e2e")
        with patch("checks.execute_plan", side_effect=self.execute) as run:
            result = staged_checks.run(self.state, [self.plan[0], selected], self.root, self.store)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0][0].scope, "focused")
        self.assertNotIn("audit_task", run.call_args.kwargs)
        self.assertEqual(result["stage"], "complete")

    def test_fast_failure_prevents_browser_execution(self):
        with patch("checks.execute_plan", return_value=[{"status": "fail"}]) as run:
            result = staged_checks.run(self.state, self.plan, self.root, self.store)
        run.assert_called_once()
        self.assertEqual(result["stage"], "unit_functional")

    def test_missing_focused_plan_does_not_run_full(self):
        with patch("checks.execute_plan", side_effect=self.execute) as run:
            result = staged_checks.run(self.state, [self.plan[0], self.plan[2]], self.root, self.store)
        run.assert_called_once()
        self.assertEqual(result["stage"], "needs_focused_e2e")

    def test_failed_or_empty_focused_e2e_prevents_full(self):
        for failed in ({"status": "unknown"}, {"status": "pass", "zero_tests": True}, {"status": "skipped"}):
            with self.subTest(failed=failed), patch("checks.execute_plan", side_effect=[[{"status": "pass"}], [failed]]) as run:
                result = staged_checks.run(self.state, self.plan, self.root, self.store)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(result["stage"], "focused_e2e")

    def test_relabeling_unfiltered_suite_as_focused_cannot_run_audit(self):
        invalid = check_plan.Check("fake-focus", ["playwright", "test"], scope="focused", kind="e2e")
        with patch("checks.execute_plan", side_effect=self.execute) as run:
            result = staged_checks.run(self.state, [self.plan[0], invalid, self.plan[2]], self.root, self.store)
        run.assert_called_once()
        self.assertEqual(result["stage"], "needs_focused_e2e")

    def test_posix_harness_shell_aliases_collapse(self):
        (self.root / "e2e").mkdir()
        (self.root / "e2e/run.sh").write_text("#!/bin/sh\necho test\n")
        checks = [check_plan.Check("a", ["sh", "e2e/run.sh"]), check_plan.Check("b", ["bash", "e2e/run.sh"])]
        self.assertEqual(len(check_plan.normalize(checks, self.root)), 1)

    def test_aliases_reuse_actual_results_across_separate_plans_and_ids(self):
        import checks
        path = self.root / "e2e/run.sh"
        path.parent.mkdir()
        path.write_text("#!/bin/sh\necho done\n")
        checks.execute(check_plan.Check("first", ["sh", "e2e/run.sh"]), self.root, self.store, "task", audit_task="task")
        result = checks.execute(check_plan.Check("second", ["bash", "e2e/run.sh"]), self.root, self.store, "task", audit_task="task")
        self.assertTrue(result["reused"])
        self.assertEqual(result["check"], "second")
        self.assertEqual(len(self.store.list("operation", "task", identity="full-e2e-launch")), 1)

    def test_audit_budget_survives_state_reload_and_content_changes(self):
        plan = [check_plan.Check("focused", ["python3", "-c", 'print(\'{"failures":{},"ran":1}\')', "feature.spec.ts"],
                                scope="focused", kind="e2e", reporter="json"),
                check_plan.Check("full", ["echo", "full"], kind="e2e")]
        staged_checks.run(self.state, plan, self.root, self.store, audit=True)
        reused = staged_checks.run(dict(self.state), plan, self.root, ExecutionStore(self.store.path), audit=True)
        self.assertTrue(all(r["reused"] for r in reused["checks"]))
        (self.root / "source").write_text("second")
        staged_checks.run(self.state, plan, self.root, self.store, audit=True)
        (self.root / "source").write_text("third")
        result = staged_checks.run(self.state, plan, self.root, self.store, audit=True)
        self.assertEqual(result["stage"], "full_e2e_budget_exhausted")
        self.assertEqual(len(self.store.list("operation", "task", identity="full-e2e-launch")), 2)

    def test_multiple_full_checks_cannot_exceed_budget_in_one_batch(self):
        import checks
        plan = [check_plan.Check(str(i), ["echo", str(i)], kind="e2e") for i in range(3)]
        result = checks.execute_plan(plan, self.root, self.store, "task", audit_task="task")
        self.assertEqual(sum(r["status"] == "pass" for r in result), 2)
        self.assertEqual(sum(r.get("reason") == "full_e2e_budget_exhausted" for r in result), 1)

    def test_bookkeeping_does_not_invalidate_actual_check_cache(self):
        import checks
        check = check_plan.Check("full", ["echo", "full"], kind="e2e")
        checks.execute(check, self.root, self.store, "task", audit_task="task")
        path = self.root / "openspec/changes/feature/tasks.md"
        path.parent.mkdir(parents=True)
        path.write_text("- [x] Verified\n")
        self.assertTrue(checks.execute(check, self.root, self.store, "task", audit_task="task")["reused"])

    def test_reserved_crashed_launches_are_not_forgotten_on_restart(self):
        import checks
        for _ in range(2):
            self.store.put("operation", task_id="task", identity="full-e2e-launch", data={})
        self.store.interrupt_running("task")
        result = checks.execute(check_plan.Check("full", ["echo", "full"], kind="e2e"), self.root,
                                ExecutionStore(self.store.path), "task", audit_task="task")
        self.assertEqual(result["reason"], "full_e2e_budget_exhausted")

    def test_environment_revision_invalidates_cache_without_resetting_budget(self):
        import checks
        check = check_plan.Check("full", ["echo", "full"], kind="e2e")
        checks.execute(check, self.root, self.store, "task", audit_task="task")
        checks.invalidate_environment(self.store, self.root, ["full"])
        self.assertFalse(checks.execute(check, self.root, self.store, "task", audit_task="task")["reused"])
        checks.invalidate_environment(self.store, self.root, ["full"])
        self.assertEqual(checks.execute(check, self.root, self.store, "task", audit_task="task")["reason"], "full_e2e_budget_exhausted")

    def test_binary_executable_and_shell_flags_are_not_shell_aliases(self):
        path = self.root / "binary"
        path.write_bytes(b"\xff\x00not a script")
        check_plan.normalize([check_plan.Check("binary", [str(path)])], self.root)
        script = self.root / "run.sh"
        script.write_text("#!/bin/sh\necho test\n")
        plan = [check_plan.Check("a", ["sh", "-e", "run.sh"]), check_plan.Check("b", ["bash", "run.sh"])]
        self.assertEqual(len(check_plan.normalize(plan, self.root)), 2)

    def test_wrapped_browser_checks_are_not_cheap(self):
        (self.root / "package.json").write_text('{"scripts":{"test":"playwright test"}}')
        for argv in (["npm", "test"], ["env", "docker", "compose", "run", "e2e-tests"]):
            self.assertTrue(check_plan.is_e2e(check_plan.Check("wrapper", argv, kind="unit"), self.root))

    def test_echoed_filename_is_not_positive_focused_execution_evidence(self):
        plan = [check_plan.Check("fake", ["echo", "feature.spec.ts"], kind="e2e", scope="focused"), self.plan[2]]
        result = staged_checks.run(self.state, plan, self.root, self.store)
        self.assertEqual(result["stage"], "focused_e2e")
        self.assertEqual(self.store.list("operation", "task", identity="full-e2e-launch"), [])

    def test_policy_is_in_working_phase_and_headless_contracts(self):
        for text in (prompts.ENVIRONMENT, prompts.IMPLEMENT, prompts.VERIFY, prompts.PROPOSE,
                     prompts.RESOLVE_CONFLICTS, claude_runner.SENTINEL_CONTRACT):
            self.assertIn("Staged testing policy", text)
            self.assertIn("not a mandatory feature gate", text)
            self.assertIn("unit and functional", text)
            self.assertNotIn("full suite if the conflicts touched shared code", text)

    def test_minimal_code_policy_reaches_working_and_headless_prompts(self):
        for text in (prompts.ENVIRONMENT, prompts.PROPOSE, claude_runner.SENTINEL_CONTRACT):
            self.assertIn("as little code as possible", text)
            self.assertIn("net code", text)

    def test_diagnostic_plan_does_not_erase_registered_full_suite(self):
        check_plan.remember(self.state, 'CHECK_PLAN: {"version":1,"checks":[{"id":"full","argv":["playwright","test"]}]}')
        check_plan.remember(self.state, 'CHECK_PLAN: {"version":1,"checks":[{"id":"unit","argv":["pytest"]}]}')
        self.assertEqual({c["id"] for c in self.state["reported_check_plan"]}, {"full", "unit"})
