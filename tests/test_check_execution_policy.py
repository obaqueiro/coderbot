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
