import unittest
from operations import Budget, budget, remaining
import operations
import os
import subprocess
import sys
import tempfile
from pathlib import Path


class BudgetTests(unittest.TestCase):
    def test_failed_delegation_clears_watchdog_operation(self):
        operations.clear()
        try:
            operations.observe({"type": "tool_start", "sourceSessionID": "child",
                "part": {"id": "nested", "tool": "task", "state": {"status": "running"}}})
            operations.observe({"type": "tool_use", "sourceSessionID": "child",
                "part": {"id": "nested", "tool": "task", "state": {
                    "status": "error", "error": "Nested subagent delegation is disabled"}}})
            self.assertEqual(operations.snapshot(), [])
        finally:
            operations.clear()

    def test_child_progress_refreshes_idle_limit_without_refreshing_other_children(self):
        from unittest.mock import patch
        now = [100.0]
        operations.clear()
        try:
            with patch.object(operations.time, "monotonic", side_effect=lambda: now[0]):
                for id in ("first", "second"):
                    operations.observe({"type": "tool_start", "sourceSessionID": "parent",
                        "part": {"id": id, "tool": "task", "state": {"status": "running", "metadata": {"sessionId": id}}}})
                now[0] += 590
                operations.observe({"type": "step_finish", "sourceSessionID": "first"})
                now[0] += 20
                self.assertEqual([entry["id"] for entry in operations.expired()], ["second"])
                operations.observe({"type": "tool_use", "part": {"id": "second"}})
                now[0] += 580
                self.assertEqual([entry["id"] for entry in operations.expired()], ["first"])
        finally:
            operations.clear()
    def test_shutdown_exception_still_cleans_resource(self):
        from unittest.mock import patch
        cleaned = []
        with patch("operations.subprocess.Popen") as popen:
            process = popen.return_value
            process.communicate.side_effect = KeyboardInterrupt()
            with patch("operations.terminate") as terminate:
                with self.assertRaises(KeyboardInterrupt):
                    operations.run(["tool"], cleanup=lambda: cleaned.append(True))
                terminate.assert_called_once_with(process)
        self.assertEqual(cleaned, [True])

    def test_timeout_reaps_process_and_runs_resource_cleanup(self):
        cleaned = []
        with self.assertRaises(subprocess.TimeoutExpired):
            operations.run([sys.executable, "-c", "import time; time.sleep(30)"],
                           timeout=.1, cleanup=lambda: cleaned.append(True))
        self.assertEqual(cleaned, [True])
        self.assertEqual(operations.snapshot(), [])

    def test_cancel_kills_grandchild_holding_port(self):
        import socket
        import time
        with tempfile.TemporaryDirectory() as root:
            port_file = str(Path(root) / "port")
            child = "import socket,time; s=socket.socket(); s.bind(('127.0.0.1',0)); open(%r,'w').write(str(s.getsockname()[1])); time.sleep(30)" % port_file
            leader = subprocess.Popen([sys.executable, "-c",
                "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',%r]); time.sleep(30)" % child],
                start_new_session=True)
            try:
                for _ in range(100):
                    if Path(port_file).exists():
                        break
                    time.sleep(.01)
                port = int(Path(port_file).read_text())
                operations.terminate(leader, grace=.1)
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", port))
            finally:
                operations.terminate(leader, grace=.1)

    def test_tool_state_distinguishes_subagent_and_silent_test(self):
        operations.clear()
        try:
            operations.observe({"type": "tool_start", "part": {"id": "test", "tool": "bash", "state": {"status": "running"}}})
            operations.observe({"type": "tool_start", "part": {"id": "child", "tool": "task", "state": {"status": "running"}}})
            entries = {e["id"]: e for e in operations.snapshot()}
            self.assertEqual(entries["child"]["kind"], "subagent")
            self.assertGreater(entries["test"]["remaining"], 800)
            operations.observe({"type": "tool_use", "part": {"id": "test"}})
            self.assertEqual(len(operations.snapshot()), 1)
        finally:
            operations.clear()
    def test_retry_uses_remaining_monotonic_budget(self):
        now = [100.0]
        b = Budget(10, clock=lambda: now[0])
        self.assertEqual(b.remaining(20), 10)
        now[0] += 7
        self.assertEqual(b.remaining(20), 3)
        now[0] += 4
        with self.assertRaises(TimeoutError):
            b.remaining()

    def test_nested_budget_cannot_extend_parent(self):
        with budget(1):
            with budget(100):
                self.assertLessEqual(remaining(100), 1)
