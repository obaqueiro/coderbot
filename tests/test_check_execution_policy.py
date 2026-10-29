import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import check_plan
import checks
import claude_runner
from execution_store import ExecutionStore


class CheckExecutionPolicyTests(unittest.TestCase):
    def test_aliases_collapse_without_losing_resource_or_timeout(self):
        first = check_plan.Check("e2e", ["bash", "e2e/run.sh"], resources=["one"])
        alias = check_plan.Check("e2e-completo", first.argv, cwd="./", timeout=2400, resources=["two"])
        result = check_plan.normalize([first, alias], "/workspace")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "e2e")
        self.assertEqual(result[0].timeout, 2400)
        self.assertEqual(set(result[0].resources), {"one", "two", "harness:/workspace"})

    def test_distinct_selections_preserved_and_share_harness_lock(self):
        a = check_plan.Check("full", ["bash", "e2e/run.sh"])
        b = check_plan.Check("focused", ["bash", "e2e/run.sh", "file.spec.ts"])
        result = check_plan.normalize([a, b], "/workspace")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].resources, result[1].resources)
        self.assertEqual(result[0].timeout, 1800)
        self.assertEqual(check_plan.normalize([check_plan.Check("large", a.argv, timeout=9000)], "/workspace")[0].timeout, 3600)

    def test_different_contracts_are_not_deduplicated(self):
        a = check_plan.Check("a", ["bash", "e2e/run.sh"])
        for changes in ({"inputs": ["src"]}, {"env_keys": ["TOKEN"]}, {"reporter": "json"}, {"optional": True}):
            b = check_plan.Check("b", a.argv, **changes)
            self.assertEqual(len(check_plan.normalize([a, b], "/workspace")), 2)

    def test_target_repo_e2e_runs_record_video_once_for_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "e2e").mkdir()
            seen = []
            def run(argv, **kwargs):
                if "env" in kwargs:  # the check itself, not a tool-version probe
                    seen.append((argv, kwargs["env"].get("PW_VIDEO")))
                return subprocess.CompletedProcess(argv, 0, "1 passed", "")
            store = ExecutionStore(root / "data/execution.sqlite")
            with patch.object(checks.evidence, "E2E_DIR", root / "e2e"), \
                 patch.object(checks.operations, "run", side_effect=run), \
                 patch.object(checks.evidence, "harvest_check_run") as harvest:
                checks.execute(check_plan.Check("e2e", ["bash", "e2e/run.sh"], kind="e2e"), root, store, "t")
                checks.execute(check_plan.Check("unit", ["python3", "-m", "unittest"]), root, store, "t")
            self.assertEqual([env for argv, env in seen if argv[0] == "bash"], ["on"])
            self.assertEqual([env for argv, env in seen if argv[0] == "python3"], [None])
            harvest.assert_called_once()

    def test_timeout_growth_preserves_output_and_does_not_hide_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ExecutionStore(root / "data/execution.sqlite")
            check = check_plan.Check("e2e", ["bash", "e2e/run.sh"])
            with patch.object(checks, "tool_versions", return_value={}), patch.object(checks, "check_environment", return_value={}), \
                 patch.object(checks.operations, "run", side_effect=[
                     subprocess.TimeoutExpired(check.argv, 1800, output=b"test output so far"),
                     subprocess.CompletedProcess(check.argv, 1, "real assertion failure", "")]) as run:
                first = checks.execute(check, root, store, "task")
                second = checks.execute(check, root, store, "task")
            self.assertEqual(first["reason"], "execution_timeout")
            self.assertIn("test output so far", Path(first["report"]).read_text())
            self.assertEqual(run.call_args_list[0].kwargs["timeout"], 1800)
            self.assertEqual(run.call_args_list[1].kwargs["timeout"], 3600)
            self.assertNotEqual(second["status"], "pass")

    def test_user_question_excludes_transport_contracts(self):
        output = ('Summary\nATTACH: /private/report.txt\nNEED_USER_INPUT: Authorize a scope revision?\n'
                  '1. Review the revision\n2. Keep work paused\n'
                  'CHECK_PLAN:\n```json\n{"version":1,"checks":[]}\n```\n')
        question = claude_runner.sentinel_question(output)
        self.assertIn("Authorize a scope revision?", question)
        self.assertIn("2. Keep work paused", question)
        self.assertNotIn("CHECK_PLAN", question)
        self.assertNotIn("ATTACH", claude_runner.sentinel_preamble(output))

    def test_timeout_ceiling_blocks_unchanged_reruns_but_source_repair_can_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ExecutionStore(root / "data/execution.sqlite")
            check = check_plan.Check("e2e", ["bash", "e2e/run.sh"])
            with patch.object(checks, "tool_versions", return_value={}), patch.object(checks, "check_environment", return_value={}), \
                 patch.object(checks.operations, "run", side_effect=subprocess.TimeoutExpired(check.argv, 3600)) as run:
                checks.execute(check, root, store, "task")
                checks.execute(check, root, store, "task")
                exhausted = checks.execute(check, root, store, "task")
                self.assertEqual(run.call_count, 2)
                self.assertTrue(exhausted["timeout_budget_exhausted"])
                self.assertEqual(exhausted["status"], "infrastructure")
                (root / "source.py").write_text("repair = True")
                checks.execute(check, root, store, "task")
                self.assertEqual(run.call_count, 3)

    def test_invalid_timeout_rejected(self):
        for value in (float("inf"), float("nan"), True, "1800", -1):
            with self.assertRaises(ValueError):
                check_plan.Check("invalid", ["echo"], timeout=value)


class DuplicateRuns(unittest.TestCase):
    """Running tests is the most expensive thing coderbot does: never twice for one command."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ExecutionStore(self.root / "data/execution.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def run_plan(self, plan, run):
        with patch.object(checks, "tool_versions", return_value={}), \
             patch.object(checks, "check_environment", return_value={}), \
             patch.object(checks.operations, "run", side_effect=run) as launched:
            return checks.execute_plan(plan, self.root, self.store, "task"), launched

    def test_one_command_declared_under_several_ids_runs_once(self):
        argv = ["bash", "e2e/run.sh", "greet.spec.ts"]
        plan = [check_plan.Check("e2e", argv, scope="full"),
                check_plan.Check("focused-e2e", argv, scope="focused", kind="e2e", inputs=["src"]),
                check_plan.Check("e2e-greeting", argv, timeout=2400, inputs=["e2e/tests"])]
        results, launched = self.run_plan(plan, lambda *a, **k: subprocess.CompletedProcess(argv, 0, "ok", ""))
        self.assertEqual(launched.call_count, 1)
        self.assertEqual([r["check"] for r in results], ["e2e", "focused-e2e", "e2e-greeting"])
        self.assertEqual({r["status"] for r in results}, {results[0]["status"]})
        self.assertEqual(results[1]["same_run_as"], "e2e")

    def test_a_redeclared_command_reuses_the_earlier_result(self):
        argv = ["python3", "-m", "unittest"]
        done = lambda *a, **k: subprocess.CompletedProcess(argv, 0, "OK", "")
        self.run_plan([check_plan.Check("unit", argv, timeout=300)], done)
        results, launched = self.run_plan([check_plan.Check("unit-again", argv, timeout=900, scope="focused")], done)
        self.assertEqual(launched.call_count, 0)
        self.assertTrue(results[0]["reused"])

    def test_different_commands_still_run_separately(self):
        plan = [check_plan.Check("a", ["python3", "-m", "unittest", "tests.a"]),
                check_plan.Check("b", ["python3", "-m", "unittest", "tests.b"])]
        _, launched = self.run_plan(plan, lambda argv, **k: subprocess.CompletedProcess(argv, 0, "OK", ""))
        self.assertEqual(launched.call_count, 2)
