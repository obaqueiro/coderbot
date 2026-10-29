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


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        if Path("/proc").is_dir():
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        else:
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    except (OSError, IndexError):
        return False  # gone between the checks
    return bool(state.strip()) and not state.strip().startswith("Z")  # a zombie has exited


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
        self.process = self.log = None
        self.addCleanup(self.shutdown)

    def supervise(self, mode, *, stall=False, **env):
        """Start scripts/supervisor.py (a second call is a restart on the same source/data).
        stall=True runs it through tests/supervisor_walk/stall_promotion.py."""
        self.assertTrue(self.process is None or self.process.poll() is not None, "supervisor already running")
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("CODEBOT_", "WALK_")) and key != "CLAUDE_CONFIG_PATH"}
        environment.update(PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}", HOME=str(self.data / "home"),
                           CODEBOT_AGENT="claude", PYTHONDONTWRITEBYTECODE="1", **env)
        if stall:
            environment["CODEBOT_DATA_DIR"] = str(self.data)
        if self.log:
            self.log.close()
        self.log = (self.data / "supervisor.log").open("a")
        self.process = subprocess.Popen(
            [sys.executable, "-X", "faulthandler", *([str(FIXTURE / "stall_promotion.py")] if stall else []), str(ROOT / "scripts/supervisor.py"),
             "--source", str(self.source), "--data", str(self.data), "--mode", mode],
            env=environment, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)

    def shutdown(self):
        ignored = False
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                ignored = True
                os.kill(self.process.pid, signal.SIGABRT)  # faulthandler: every thread's stack into the log
                time.sleep(2)
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except PermissionError:
                    pass
                self.process.wait()
        if self.log:
            self.log.close()
        if ignored:
            for pid in self.pids():
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.fail("the supervisor ignored SIGTERM for 30 s:\n" + (self.data / "supervisor.log").read_text()[-6000:])
        orphans = self.orphans()
        if orphans:
            processes = subprocess.run(["ps", "-o", "pid,ppid,pgid,stat,etime,command", "-p", ",".join(map(str, orphans))],
                                       capture_output=True, text=True).stdout
            self.fail(f"orphan worker/agent processes {orphans}:\n{processes}\nexit={self.process.returncode}\n"
                      + (self.data / "supervisor.log").read_text()[-3000:])

    def pids(self):
        """Every worker and repair-agent process the walk ever started."""
        workers = (self.data / "workers").glob("*") if (self.data / "workers").exists() else ()
        agents = [self.bin / name for name in ("agent.pid", "agent-child.pid") if (self.bin / name).exists()]
        return [int(path.name) for path in workers] + [int(path.read_text()) for path in agents]

    def orphans(self, timeout=10):
        """The walk's processes still alive after a bounded wait (a killed process may need a
        moment to exit and be reaped; zombies count as gone)."""
        deadline = time.monotonic() + timeout
        while True:
            living = [pid for pid in self.pids() if alive(pid)]
            if not living or time.monotonic() >= deadline:
                return living
            time.sleep(0.1)

    def crash(self):
        """kill -9 of the supervisor and, as when its container dies, of everything it ran."""
        os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait()
        for pid in self.pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.assertEqual(self.orphans(), [])

    def healthy(self, paused=False):
        """The supervisor heartbeat is fresh; in the image also the container healthcheck,
        which reports a paused supervisor unhealthy (alive, not killed)."""
        self.assertLess(time.time() - read_json(self.data / "supervisor/heartbeat.json")["time"], 30)
        if sys.platform == "linux":  # GNU stat
            result = subprocess.run(["bash", str(ROOT / "scripts/healthcheck.sh")], capture_output=True, text=True,
                                    env={**os.environ, "CODEBOT_DATA_DIR": str(self.data), "CODEBOT_SELF_HEAL": "auto"})
            self.assertEqual(result.returncode, 1 if paused else 0, result.stderr)
            if paused:
                self.assertIn("paused", result.stderr)

    def workers(self):
        return len(list((self.data / "workers").glob("*"))) if (self.data / "workers").exists() else 0

    def incident(self):
        [record] = [read_json(path) for path in (self.data / "supervisor/incidents").glob("*.json")]
        return record

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


    # ---- S10: crashes, stops and drift around a repair
    def test_kill_9_mid_promotion_restores_the_old_source_at_restart(self):
        self.supervise("auto", stall=True)
        self.until(lambda: (self.data / "stalled").exists())  # src/bug.py copied, the regression test not yet
        self.assertNotEqual(inventory(self.source), self.original)
        self.assertEqual(read_json(self.data / "supervisor/promotion.json")["status"], "promoting")
        self.crash()
        self.supervise("observe")
        self.until(lambda: "repair_rolled_back" in self.kinds())
        self.assertEqual(inventory(self.source), self.original)  # byte-identical old, never mixed
        self.assertEqual(read_json(self.data / "supervisor/promotion.json")["status"], "rolled_back")
        self.assertFalse((self.data / "supervisor/recovery.json").exists())
        self.assertEqual(self.incident()["status"], "rolled_back")  # the journal no longer says snapshotted
        workers = self.workers()
        self.until(lambda: "incident_detected" in self.kinds())  # the old code runs again and reports the bug
        self.assertGreaterEqual(self.workers(), workers)
        self.assertEqual(self.state()["state"], "WAIT_STUCK")
        self.healthy()

    def test_kill_9_after_a_committed_promotion_keeps_the_new_source(self):
        self.supervise("auto")
        self.until(lambda: "repair_recovered" in self.kinds())
        [folder] = (self.data / "supervisor/repairs").glob("*/*")
        repaired = read_json(folder / "validated.json")["inventory"]
        self.assertEqual(inventory(self.source), repaired)
        self.crash()
        workers = self.workers()
        self.supervise("observe")
        self.until(lambda: self.workers() > workers)
        self.assertEqual(inventory(self.source), repaired)  # byte-identical new, never mixed
        self.assertEqual(read_json(self.data / "supervisor/promotion.json")["status"], "committed")
        self.assertEqual(self.incident()["status"], "recovered")
        self.assertNotIn("repair_rolled_back", self.kinds())
        self.healthy()

    def test_pause_during_the_repair_promotes_nothing_and_resume_restarts_the_worker(self):
        (self.bin / "agent-mode").write_text("hang")
        self.supervise("auto")
        self.until(lambda: (self.bin / "agent-child.pid").exists())
        subprocess.run([sys.executable, str(ROOT / "scripts/supervisor.py"), "--data", str(self.data),
                        "--control", "pause"], check=True, timeout=30)
        self.until(lambda: "repair_paused" in self.kinds())
        self.assertEqual(self.orphans(), [])  # agent, its child and the worker are all gone
        self.assertEqual(inventory(self.source), self.original)
        self.assertFalse((self.data / "supervisor/promotion.json").exists())
        self.assertEqual(self.incident()["status"], "human_required")
        self.healthy(paused=True)  # paused, not dead
        workers = self.workers()
        subprocess.run([sys.executable, str(ROOT / "scripts/supervisor.py"), "--data", str(self.data),
                        "--control", "resume"], check=True, timeout=30)
        self.until(lambda: self.workers() > workers)  # the worker resumes on the old code
        self.assertEqual(self.state()["state"], "WAIT_STUCK")
        self.assertEqual(inventory(self.source), self.original)
        self.assertEqual(len(self.agent_calls()), 1)

    def test_stop_during_the_repair_exits_cleanly_and_the_worker_resumes_after_restart(self):
        (self.bin / "agent-mode").write_text("hang")
        self.supervise("auto")
        self.until(lambda: (self.bin / "agent-child.pid").exists())
        self.process.send_signal(signal.SIGTERM)
        self.assertEqual(self.process.wait(timeout=60), 0)
        self.assertEqual(self.orphans(), [])
        self.assertEqual(inventory(self.source), self.original)
        self.assertFalse((self.data / "supervisor/promotion.json").exists())
        workers = self.workers()
        self.supervise("observe")
        self.until(lambda: self.workers() > workers)
        self.until(lambda: "incident_detected" in self.kinds())  # the interrupted incident is reported, not lost
        self.assertEqual(self.state()["state"], "WAIT_STUCK")
        self.assertEqual(inventory(self.source), self.original)
        self.healthy()

    def test_hung_repair_agent_is_time_bounded_and_leaves_no_process(self):
        (self.bin / "agent-mode").write_text("hang")
        self.supervise("auto", CODEBOT_REPAIR_AGENT_TIMEOUT="5")
        workers = self.until(lambda: (self.bin / "agent-child.pid").exists() and self.workers())
        [failed] = self.until(lambda: [e for e in self.notified() if e["kind"] == "repair_failed"], 60)
        self.assertIn("no result within 5 s", failed["detail"])
        self.assertNotIn("Repair this Coderbot", failed["detail"])  # never the agent's argv or prompt
        agent = [int((self.bin / name).read_text()) for name in ("agent.pid", "agent-child.pid")]
        self.until(lambda: not any(alive(pid) for pid in agent), 15)
        self.until(lambda: self.workers() > workers)  # the old code runs again
        self.assertEqual(len(self.agent_calls()), 1)  # a timed-out agent is not retried
        self.assertEqual(inventory(self.source), self.original)
        self.assertFalse((self.data / "supervisor/promotion.json").exists())
        self.assertEqual(self.incident()["status"], "human_required")
        self.healthy()

    def test_live_tree_edit_during_the_repair_is_restored_and_paused_without_promotion(self):
        (self.bin / "agent-mode").write_text("wait")
        self.supervise("auto")
        self.until(lambda: (self.bin / "agent.pid").exists())
        (self.source / "src/bug.py").write_text("LIVE_EDIT = True\n")  # someone edits the live tree
        (self.source / "src/extra.py").write_text("NEW = 1\n")
        (self.bin / "go").touch()
        [paused] = self.until(lambda: [e for e in self.notified() if e["kind"] == "repair_paused"])
        self.assertIn("src/bug.py", paused["detail"])
        self.assertEqual(read_json(self.data / "supervisor/control.json")["action"], "pause")
        self.assertEqual(inventory(self.source), self.original)  # restored from the snapshot
        [folder] = (self.data / "supervisor/repairs").glob("*/*")
        self.assertEqual((folder / "drift/src/bug.py").read_text(), "LIVE_EDIT = True\n")  # the edit is kept
        self.assertEqual((folder / "drift/src/extra.py").read_text(), "NEW = 1\n")
        self.assertFalse((self.data / "supervisor/promotion.json").exists())
        self.assertNotIn("repair_promoted", self.kinds())
        self.assertEqual(self.incident()["status"], "human_required")
        workers = self.workers()
        time.sleep(2)
        self.assertEqual(self.workers(), workers)  # held stopped while paused
        self.assertEqual(self.orphans(), [])
        self.healthy(paused=True)


if __name__ == "__main__":
    unittest.main()
