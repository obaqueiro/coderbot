"""Syntax validation must not execute or gate the browser harness."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import check_plan
import checks
import staged_checks
from execution_store import ExecutionStore


class ShellSyntaxChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "e2e").mkdir()
        (self.root / "e2e/run.sh").write_text("#!/bin/sh\necho executed > harness-ran\n# playwright test\n")
        for name in ("cleanup.sh", "verify-http.sh", "export-evidence.sh"):
            (self.root / "e2e" / name).write_text("#!/bin/sh\necho executed\n")
        self.store = ExecutionStore(self.root / "data/db")
        self.syntax = check_plan.Check("entorno-sintaxis", ["bash", "-n", "e2e/run.sh",
            "e2e/cleanup.sh", "e2e/verify-http.sh", "e2e/export-evidence.sh"], scope="focused")

    def test_exact_live_command_is_neither_e2e_nor_harness(self):
        self.assertFalse(check_plan.is_e2e(self.syntax, self.root))
        self.assertFalse(check_plan.is_harness(self.syntax, self.root))
        normalized = check_plan.normalize([self.syntax], self.root)[0]
        self.assertEqual(normalized.timeout, 900)
        self.assertEqual(normalized.resources, [])

    def test_shell_option_forms_only_before_script_or_command_count(self):
        for argv in (["sh", "-n", "e2e/run.sh"], ["/bin/bash", "-en", "e2e/run.sh"],
                     ["bash", "-e", "-n", "--", "e2e/run.sh"],
                     ["env", "COMPOSE_PROFILES=verification", "bash", "-n", "e2e/run.sh"],
                     ["bash", "--noprofile", "--norc", "-n", "e2e/run.sh"],
                     ["bash", "-n", "-c", "playwright test"]):
            with self.subTest(argv=argv):
                self.assertFalse(check_plan.is_e2e(check_plan.Check("syntax", argv), self.root))
        for argv in (["bash", "e2e/run.sh", "-n"], ["bash", "-c", "playwright test", "-n"],
                     ["bash", "-n", "+n", "e2e/run.sh"], ["bash", "e2e/run.sh"],
                     ["bash", "-in", "e2e/run.sh"],
                     ["bash", "--norc", "-n", "+n", "e2e/run.sh"],
                     ["bash", "-e", "e2e/run.sh", "feature.spec.ts"]):
            with self.subTest(argv=argv):
                self.assertTrue(check_plan.is_e2e(check_plan.Check("execute", argv), self.root))

    def test_syntax_runs_before_focused_checks_and_full_only_on_explicit_audit(self):
        focused = check_plan.Check("focused", ["playwright", "test", "feature.spec.ts"], scope="focused", kind="e2e")
        full = check_plan.Check("full", ["bash", "e2e/run.sh"], kind="e2e")
        def execute(selected, *args, **kwargs):
            return [{"check": c.id, "status": "pass", "test_count": 1} for c in selected]
        for audit in (False, True):
            with self.subTest(audit=audit), patch("checks.execute_plan", side_effect=execute) as run:
                result = staged_checks.run({"execution_task_id": "task"}, [self.syntax, focused, full],
                                           self.root, self.store, audit=audit)
            self.assertEqual(result["stage"], "complete")
            self.assertEqual([[c.id for c in call.args[0]] for call in run.call_args_list],
                             [["entorno-sintaxis"], ["focused"]] + ([["full"]] if audit else []))

    def test_real_syntax_check_neither_launches_harness_nor_spends_audit_budget(self):
        full_syntax = check_plan.Check(self.syntax.id, self.syntax.argv)
        with patch("checks.tool_versions", return_value={}), patch("checks.check_environment", return_value={}):
            result = checks.execute(full_syntax, self.root, self.store, "task", audit_task="task")
            repeated = checks.execute(full_syntax, self.root, self.store, "task", audit_task="task")
        self.assertEqual(result["status"], "pass")
        self.assertTrue(repeated["reused"])
        self.assertFalse((self.root / "harness-ran").exists())
        self.assertEqual(self.store.list("operation", "task", identity="full-e2e-launch"), [])

    def test_invalid_focused_feedback_names_existing_registration(self):
        invalid = check_plan.Check("e2e-compartido", ["bash", "e2e/run.sh"], scope="focused")
        result = staged_checks.run({"execution_task_id": "task"}, [invalid], self.root, self.store)
        self.assertEqual(result["stage"], "needs_focused_e2e")
        self.assertEqual(result["selection_issues"][0]["check"], "e2e-compartido")
        self.assertEqual(result["selection_issues"][0]["argv"], ["bash", "e2e/run.sh"])
        self.assertFalse((self.root / "harness-ran").exists())
