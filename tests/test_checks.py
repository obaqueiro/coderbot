from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from check_plan import Check
from checks import execute
from execution_store import ExecutionStore


class CheckRunnerTests(unittest.TestCase):
    def test_scheduler_serializes_exclusive_resources(self):
        from checks import execute_plan
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            store = ExecutionStore(Path(root) / "data/db")
            plan = [Check(str(i), [sys.executable, "-c", f"import time; time.sleep(.1); print({i})"],
                          resources=["port:8111"]) for i in range(2)]
            results = execute_plan(plan, repo, store, "t")
            # Operation start times are stored before lock acquisition; use the
            # wall time bound plus distinct reports to establish serialized runs.
            self.assertGreater(max(r["finished"] for r in results) - min(r["started"] for r in results), .2)
            self.assertEqual([r["check"] for r in results], ["0", "1"])
    def test_reuse_and_invalidation_for_content_and_environment(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root, \
             patch("checks.tool_versions", return_value={"executable": sys.executable, "version": "test"}):
            repo = Path(root) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "app.py").write_text("a")
            store = ExecutionStore(Path(root) / "data/db")
            check = Check("unit", [sys.executable, "-c", "print('ok')"], inputs=["app.py"], env_keys=["SETTING"])
            execute(check, repo, store, "t")
            self.assertTrue(execute(check, repo, store, "t")["reused"])
            (repo / "app.py").write_text("b")
            self.assertEqual(execute(check, repo, store, "t")["repetition_reason"], "content_changed")
            with patch.dict("os.environ", {"SETTING": "new"}):
                self.assertEqual(execute(check, repo, store, "t")["repetition_reason"], "environment_or_command_changed")

    def test_runs_real_process_and_retains_structured_result_and_report(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            store = ExecutionStore(Path(root) / "data/db")
            check = Check("unit", [sys.executable, "-c", "print('passed')"])
            result = execute(check, repo, store, "task")
            self.assertEqual(result["status"], "pass")
            self.assertIn("passed", Path(result["report"]).read_text())
            self.assertEqual(store.reusable_check("task", result["identity"])["data"]["result"], result)

    def test_junit_and_json_failures_are_identifiable(self):
        from check_results import parse_output
        xml = '<testsuite><testcase classname="X" name="a"><failure message="bad">trace</failure></testcase></testsuite>'
        self.assertEqual(parse_output(xml, 1, "junit")["failures"], {"X:a": "bad\ntrace"})
        self.assertEqual(parse_output('{"failures":{"a":"bad"}}', 1, "json")["status"], "fail")

    def test_positive_execution_counts_require_recognized_reporter_output(self):
        from check_results import parse_output
        for output, reporter, expected in (("  2 passed\n", "playwright", 2),
                ('{"failures": {}, "ran": 3}', "json", 3),
                ('<testsuite><testcase name="a"/></testsuite>', "junit", 1)):
            self.assertEqual(parse_output(output, 0, reporter)["test_count"], expected)
        self.assertNotIn("test_count", parse_output("feature.spec.ts", 0, "text"))

    def test_partial_browser_failure_inventory_cannot_be_attributed_as_baseline_only(self):
        from check_results import parse_output
        output = "  1) [chromium] feature.spec.ts:2:3 › scenario\nAssertionError: bad\n  2 failed\n"
        self.assertEqual(parse_output(output, 1, "playwright")["status"], "unknown")
