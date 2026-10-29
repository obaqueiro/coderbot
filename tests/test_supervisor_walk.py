"""End-to-end walk of the real supervisor: a fake worker with a buggy module, a fake
`claude` writing a canned patch plus RepairResult, and a file notifier
(tests/supervisor_walk). Each scenario runs scripts/supervisor.py in a subprocess
against a temporary checkout and data directory; nothing touches the real data/."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from scripts.selfheal import inventory, read_json

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests/supervisor_walk"
BUG = "def parse(value):\n    return value[0]\n"


class SupervisorWalk(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.source, self.data, self.bin = root / "source", root / "data", root / "bin"
        for name, target in (("worker.py", "src/main.py"), ("notifier.py", "src/repair_notifications.py")):
            (self.source / target).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(FIXTURE / name, self.source / target)
        shutil.copyfile(ROOT / "src/selfheal_events.py", self.source / "src/selfheal_events.py")
        (self.source / "src/bug.py").write_text(BUG)
        (self.source / "tests").mkdir()
        (self.source / "tests/__init__.py").write_text(
            "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))\n")
        (self.source / "tests/test_bug.py").write_text(
            "import unittest\nfrom bug import parse\nclass Bug(unittest.TestCase):\n"
            "    def test_first(self):\n        self.assertEqual(parse([5]), 5)\n")
        self.bin.mkdir()
        (self.bin / "claude").write_text(f"#!{sys.executable}\n" + (FIXTURE / "claude").read_text())
        (self.bin / "claude").chmod(0o755)
        self.data.mkdir()
        (self.data / "state.json").write_text(json.dumps(
            {"execution_task_id": "task", "item": "Fix", "state": "EXPLORING", "thread_id": "thread"}))
        self.original = inventory(self.source)
        self.process = None

    def supervise(self, mode, **env):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("CODEBOT_", "WALK_")) and key != "CLAUDE_CONFIG_PATH"}
        environment.update(PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}", HOME=str(self.data / "home"),
                           CODEBOT_AGENT="claude", PYTHONDONTWRITEBYTECODE="1", **env)
        self.log = (self.data / "supervisor.log").open("w")
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts/supervisor.py"), "--source", str(self.source),
             "--data", str(self.data), "--mode", mode], env=environment, stdout=self.log, stderr=subprocess.STDOUT,
            start_new_session=True)
        self.addCleanup(self.shutdown)

    def shutdown(self):
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
        self.log.close()
        for pid in (self.data / "workers").glob("*") if (self.data / "workers").exists() else ():
            with self.assertRaises(ProcessLookupError, msg="orphan worker " + pid.name):
                os.kill(int(pid.name), 0)

    def notified(self):
        try:
            return [json.loads(line) for line in (self.data / "notified.jsonl").read_text().splitlines()]
        except FileNotFoundError:
            return []

    def kinds(self):
        return [event["kind"] for event in self.notified()]

    def until(self, condition, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            self.assertIsNone(self.process.poll(), "supervisor exited:\n" + (self.data / "supervisor.log").read_text())
            time.sleep(0.2)
        self.fail(f"timed out; notified {self.kinds()}\n" + (self.data / "supervisor.log").read_text()[-4000:])

    def state(self):
        return read_json(self.data / "state.json", {})

    def agent_calls(self):
        path = self.bin / "calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def test_observe_reports_the_incident_without_calling_the_agent(self):
        self.supervise("observe")
        self.until(lambda: "incident_detected" in self.kinds())
        [event] = [event for event in self.notified() if event["kind"] == "incident_detected"]
        self.assertIn("IndexError", event["detail"])
        self.assertEqual(self.agent_calls(), [])
        self.assertEqual(inventory(self.source), self.original)

    def test_propose_validates_a_candidate_and_leaves_the_source_byte_identical(self):
        self.supervise("propose")
        self.until(lambda: "repair_proposed" in self.kinds())
        self.assertEqual(len(self.agent_calls()), 1)
        self.assertEqual(inventory(self.source), self.original)
        self.assertEqual(self.state()["state"], "WAIT_STUCK")
        self.assertFalse((self.data / "supervisor/promotion.json").exists())

    def test_auto_promotes_resumes_the_task_and_announces_the_rollback(self):
        self.supervise("auto")
        self.until(lambda: "repair_recovered" in self.kinds())
        self.assertEqual(self.state()["state"], "WAIT_APPROVAL")  # the recovery ticket resumed EXPLORING
        self.assertTrue((self.data / "supervisor/recovery.consumed").exists())
        self.assertIn("value[0] if value", (self.source / "src/bug.py").read_text())
        [promoted] = [event for event in self.notified() if event["kind"] == "repair_promoted"]
        self.assertIn("parse() indexes an empty list", promoted["detail"])
        self.assertIn("src/bug.py +", promoted["detail"])
        self.assertIn("--control rollback " + promoted["incident"], promoted["detail"])
        self.assertEqual(read_json(self.data / "supervisor/promotion.json")["status"], "committed")

    def test_failed_observation_rolls_back_byte_identical(self):
        self.supervise("auto", WALK_PATCH="regress")
        self.until(lambda: "repair_failed" in self.kinds())
        self.until(lambda: inventory(self.source) == self.original)
        self.assertEqual(read_json(self.data / "supervisor/promotion.json")["status"], "rolled_back")
        self.assertIn("repair_promoted", self.kinds())

    def test_operator_rollback_restores_a_committed_promotion(self):
        self.supervise("auto")
        promoted = self.until(lambda: [e for e in self.notified() if e["kind"] == "repair_recovered"])[0]
        subprocess.run([sys.executable, str(ROOT / "scripts/supervisor.py"), "--data", str(self.data),
                        "--control", "rollback", promoted["incident"]], check=True, timeout=30)
        self.until(lambda: "repair_rolled_back" in self.kinds())
        self.until(lambda: inventory(self.source) == self.original)
        self.assertEqual(read_json(self.data / "supervisor/promotion.json")["status"], "rolled_back")

    def test_rollback_conflict_pauses_and_alerts_instead_of_crash_looping(self):
        self.supervise("auto", WALK_PATCH="gate")
        self.until(lambda: (self.data / "gated").exists())  # the promoted code is running
        (self.source / "src/bug.py").write_text("USER_EDIT = True\n")  # an operator edits the live tree
        (self.data / "go").touch()
        self.until(lambda: "repair_conflict" in self.kinds())
        self.until(lambda: read_json(self.data / "supervisor/control.json", {}).get("action") == "pause")
        self.assertEqual((self.source / "src/bug.py").read_text(), "USER_EDIT = True\n")
        self.assertEqual(read_json(self.data / "supervisor/promotion.json")["status"], "conflict")
        time.sleep(2)
        self.assertIsNone(self.process.poll())  # still supervising, worker held stopped
        self.assertEqual(self.kinds().count("repair_conflict"), 1)
        workers = len(list((self.data / "workers").glob("*")))
        subprocess.run([sys.executable, str(ROOT / "scripts/supervisor.py"), "--data", str(self.data),
                        "--control", "resolve"], check=True, timeout=30)
        self.assertEqual(read_json(self.data / "supervisor/promotion.json")["status"], "resolved")
        self.until(lambda: len(list((self.data / "workers").glob("*"))) > workers)  # the worker runs again

    def test_restart_storm_trips_the_breaker_instead_of_pausing(self):
        self.supervise("observe", WALK_STORM="1")
        self.until(lambda: read_json(self.data / "supervisor/control.json", {}).get("action") == "cooldown", 120)
        control = read_json(self.data / "supervisor/control.json")
        self.assertEqual(control["trips"], 1)
        self.assertIn("worker_unavailable", self.until(self.kinds))
        beat = read_json(self.data / "supervisor/heartbeat.json")["time"]
        self.until(lambda: read_json(self.data / "supervisor/heartbeat.json")["time"] > beat, 30)

    def test_fatal_startup_exit_alerts_at_once_without_burning_restarts(self):
        self.supervise("observe", WALK_FATAL="1")
        self.until(lambda: read_json(self.data / "supervisor/control.json", {}).get("action") == "cooldown")
        self.assertEqual(read_json(self.data / "supervisor/restarts.json")["used"], 1)
        [alert] = [event for event in self.notified() if event["kind"] == "worker_unavailable"]
        self.assertIn("GH_TOKEN is missing", alert["detail"])


if __name__ == "__main__":
    unittest.main()
