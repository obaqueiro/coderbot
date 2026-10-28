"""Independent repair tests: no live agent, provider, data directory or daemon."""
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.selfheal import (Repairs, RepairError, atomic_json, eligible, epoch, inventory,
                              safe_test_environment, snapshot, state_binding, validate_patch, run_process,
                              RepairCancelled, read_json)
from scripts.supervisor import Supervisor, StopRequested, signal_owned, worker_event
import selfheal_events


class SelfHealingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.data = self.root / "data"
        (self.source / "src").mkdir(parents=True)
        (self.source / "tests").mkdir()
        (self.source / "src/bug.py").write_text("def parse(value):\n    return value[0]\n")
        (self.source / "tests/__init__.py").write_text("import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))\n")
        self.repairs = Repairs(self.source, self.data)
        self.incident = {"id": "incident", "epoch": epoch(self.source), "category": "implementation",
                         "error_type": "IndexError", "phase": "EXPLORING", "task_id": "task",
                         "location": "parse", "channel": "email", "provider": "jira", "conversation": "thread"}
        self.state = {"execution_task_id": "task", "state": "WAIT_STUCK", "stuck_return": "EXPLORING"}
        atomic_json(self.data / "state.json", self.state)

    def candidate(self):
        self.incident["epoch"] = epoch(self.source)
        folder, path, record = self.repairs.admit(self.incident)
        (folder / "candidate/src/bug.py").write_text("def parse(value):\n    return value[0] if value else None\n")
        (folder / "candidate/tests/test_selfheal_regression_empty.py").write_text(
            "import unittest\nfrom bug import parse\nclass Empty(unittest.TestCase):\n    def test_empty(self):\n        self.assertIsNone(parse([]))\n")
        return folder, path, record

    def test_end_to_end_candidate_validation_promotion_and_rollback(self):
        original = (self.source / "src/bug.py").read_bytes()
        folder, _, _ = self.candidate()
        changed = self.repairs.validate(folder)
        old_epoch = epoch(self.source)
        new_epoch = self.repairs.promote(folder, changed)
        self.assertNotEqual(old_epoch, new_epoch)
        self.assertIn("if value", (self.source / "src/bug.py").read_text())
        self.repairs.rollback()
        self.assertEqual((self.source / "src/bug.py").read_bytes(), original)
        self.assertFalse((self.source / "tests/test_selfheal_regression_empty.py").exists())

    def test_promotion_preserves_uncommitted_user_work_and_refuses_stale_changes(self):
        (self.source / "src/user.py").write_text("USER_WORK = True\n")
        folder, _, _ = self.candidate()
        changed, _ = validate_patch(inventory(folder / "before"), folder / "candidate")
        self.repairs.validate(folder)
        (self.source / "src/user.py").write_text("NEW_USER_WORK = True\n")
        with self.assertRaisesRegex(RepairError, "changed"):
            self.repairs.promote(folder, changed)
        self.assertEqual((self.source / "src/user.py").read_text(), "NEW_USER_WORK = True\n")

    def test_rollback_never_overwrites_user_edit_after_promotion(self):
        folder, _, _ = self.candidate()
        changed, _ = validate_patch(inventory(folder / "before"), folder / "candidate")
        self.repairs.validate(folder)
        self.repairs.promote(folder, changed)
        (self.source / "src/bug.py").write_text("USER_FIX = True\n")
        with self.assertRaisesRegex(RepairError, "concurrent"):
            self.repairs.rollback()
        self.assertEqual((self.source / "src/bug.py").read_text(), "USER_FIX = True\n")

    def test_interrupted_promotion_is_recoverable_and_idempotent(self):
        folder, _, _ = self.candidate()
        changed, _ = validate_patch(inventory(folder / "before"), folder / "candidate")
        self.repairs.validate(folder)
        actual = self.repairs._replace
        def interrupted(source, destination, **kwargs):
            actual(source, destination, **kwargs)
            raise RuntimeError("crash after first file")
        with patch.object(self.repairs, "_replace", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                self.repairs.promote(folder, changed)
        self.repairs.rollback()
        self.repairs.rollback()
        self.assertEqual(inventory(self.source), inventory(folder / "before"))

    def test_no_automatic_credentials_approval_dependency_or_test_weakening(self):
        for name in ("src/config.py", "scripts/supervisor.py", "tests/test_existing.py", "requirements.txt"):
            with self.subTest(name=name):
                folder = self.root / (name.replace("/", "_") + "candidate")
                before = snapshot(self.source, folder)
                path = folder / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("changed\n")
                with self.assertRaises(RepairError):
                    validate_patch(before, folder)

    def test_persistent_budgets_survive_recreating_supervisor(self):
        self.repairs.admit(self.incident)
        Repairs(self.source, self.data).admit(self.incident)
        with self.assertRaisesRegex(RepairError, "budget"):
            Repairs(self.source, self.data).admit(self.incident)

    def test_operational_errors_and_pending_decisions_are_not_code_repairs(self):
        for error in ("ConnectionError", "PermissionError", "OSError", "ValueError"):
            self.assertFalse(eligible({**self.incident, "error_type": error}))
        self.assertFalse(eligible({**self.incident, "phase": "WAIT_APPROVAL"}))

    def test_snapshot_excludes_live_data_and_credentials(self):
        (self.source / "data").mkdir()
        (self.source / "data/state.json").write_text("live")
        (self.source / ".env").write_text("SECRET=private")
        snapshot(self.source, self.root / "snapshot")
        self.assertFalse((self.root / "snapshot/data").exists())
        self.assertFalse((self.root / "snapshot/.env").exists())
        with patch.dict(os.environ, {"GH_TOKEN": "live-secret", "CODEBOT_SUPERVISOR_DIR": "live"}):
            env = safe_test_environment(self.root / "test-env")
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("CODEBOT_SUPERVISOR_DIR", env)
        self.assertTrue(env["CODEBOT_DATA_DIR"].startswith(str(self.root)))

    def test_provider_neutral_events_retain_opaque_refs(self):
        for channel in ("email", "slack", "telegram-test"):
            for provider in ("jira", "github", "gdoc", "synthetic"):
                self.repairs.event("repair_started", {**self.incident, "channel": channel, "provider": provider})
        events = [json.loads(path.read_text()) for path in (self.repairs.home / "notifications").glob("*.json")]
        self.assertEqual(len(events), 12)
        self.assertTrue(all(event["conversation"] == "thread" for event in events))

    def test_recovery_ticket_does_not_force_a_changed_user_decision(self):
        state = {"execution_task_id": "task", "state": "WAIT_STUCK", "stuck_return": "EXPLORING"}
        ticket = {"version": 1, "incident": "incident", "target": os.environ.get("CODEBOT_REPO_PATH"),
                  "epoch": "new", "task_id": "task", "binding": state_binding(state), "phase": "EXPLORING"}
        atomic_json(self.repairs.home / "recovery.json", ticket)
        with patch.dict(os.environ, {"CODEBOT_SUPERVISOR_DIR": str(self.repairs.home), "CODEBOT_RUNTIME_EPOCH": "new"}):
            self.assertEqual(selfheal_events.recover(state)["state"], "EXPLORING")
            changed = {**state, "pending_question": "New user question"}
            self.assertEqual(selfheal_events.recover(changed), changed)

    def test_operator_shutdown_never_restarts_worker(self):
        supervisor = Supervisor(self.source, self.data, "auto")
        with patch.object(supervisor, "stop"), patch.object(supervisor, "start") as start, \
             patch.object(supervisor, "agent", side_effect=StopRequested), patch.object(supervisor, "notify"):
            with self.assertRaises(StopRequested):
                supervisor.handle(self.incident)
        start.assert_not_called()

    def test_runtime_epoch_changes_computational_keys_not_task_identity(self):
        import phase_checkpoint
        from execution_store import ExecutionStore
        state = {"state": "EXPLORING", "execution_task_id": "task"}
        with patch.dict(os.environ, {"CODEBOT_RUNTIME_EPOCH": "old"}):
            old = phase_checkpoint.key(state, "prompt")
        with patch.dict(os.environ, {"CODEBOT_RUNTIME_EPOCH": "new"}):
            self.assertNotEqual(old, phase_checkpoint.key(state, "prompt"))
        self.assertEqual(ExecutionStore(self.data / "execution.sqlite").task_identity(state, self.source), "task")

    def test_candidate_mutation_after_validation_is_not_promoted(self):
        folder, _, _ = self.candidate()
        changed = self.repairs.validate(folder)
        (folder / "candidate/src/bug.py").write_text("UNVALIDATED = True\n")
        with self.assertRaisesRegex(RepairError, "after validation"):
            self.repairs.promote(folder, changed)

    def test_tests_cannot_modify_candidate_while_passing(self):
        folder, _, _ = self.candidate()
        test = folder / "candidate/tests/test_selfheal_regression_empty.py"
        test.write_text(test.read_text() + "\nfrom pathlib import Path\nPath('src/extra.py').write_text('UNVALIDATED = True')\n")
        with self.assertRaisesRegex(RepairError, "Tests modified"):
            self.repairs.validate(folder)

    def test_regression_cannot_be_skipped_after_repair(self):
        folder, _, _ = self.candidate()
        test = folder / "candidate/tests/test_selfheal_regression_empty.py"
        test.write_text(test.read_text().replace("self.assertIsNone(parse([]))",
            "if parse([]) is None: self.skipTest('hiding fix')"))
        with self.assertRaisesRegex(RepairError, "Regression"):
            self.repairs.validate(folder)

    def test_rollback_uses_journal_not_mutated_candidate_postimages(self):
        folder, _, _ = self.candidate()
        self.repairs.promote(folder, self.repairs.validate(folder))
        (folder / "candidate/src/bug.py").write_text("NOT_DEPLOYED = True\n")
        self.repairs.rollback()
        self.assertEqual(inventory(self.source), inventory(folder / "before"))

    def test_mutated_backup_requires_manual_recovery(self):
        folder, _, _ = self.candidate()
        self.repairs.promote(folder, self.repairs.validate(folder))
        (folder / "before/src/bug.py").write_text("NOT_ORIGINAL = True\n")
        with self.assertRaisesRegex(RepairError, "backup changed"):
            self.repairs.rollback()
        self.assertIn("if value", (self.source / "src/bug.py").read_text())

    def test_protected_approval_function_changes_are_rejected(self):
        (self.source / "src/main.py").write_text("def do_approval_reply(state, reply):\n    return False\n")
        folder, _, _ = self.candidate()
        (folder / "candidate/src/main.py").write_text("def do_approval_reply(state, reply):\n    return True\n")
        with self.assertRaisesRegex(RepairError, "protected approval"):
            self.repairs.validate(folder)

    def test_snapshot_rejects_symlinked_source_root(self):
        external = self.root / "external"
        external.mkdir()
        (self.source / "scripts").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(RepairError, "symlink"):
            inventory(self.source)

    def test_worker_and_supervisor_state_bindings_match(self):
        state = {**self.state, "branch": "feature", "conversation_version": 3,
                 "workspace": {"identity": "workspace", "mode": "workspace", "root": "/target", "checks": []}}
        self.assertEqual(selfheal_events.state_binding(state), state_binding(state))
        self.assertNotEqual(state_binding(state), state_binding({**state, "conversation_version": 4}))

    def test_recovery_is_one_time_and_never_resumes_approval(self):
        ticket = {"version": 1, "incident": "one", "target": os.environ.get("CODEBOT_REPO_PATH"),
                  "epoch": "new", "task_id": "task", "binding": state_binding(self.state), "phase": "EXPLORING"}
        atomic_json(self.repairs.home / "recovery.json", ticket)
        with patch.dict(os.environ, {"CODEBOT_SUPERVISOR_DIR": str(self.repairs.home), "CODEBOT_RUNTIME_EPOCH": "new"}):
            recovered = selfheal_events.recover(self.state)
            repeated = {**self.state, "runtime_recovery_incident": recovered["runtime_recovery_incident"]}
            self.assertEqual(selfheal_events.recover(repeated), repeated)
            selfheal_events.acknowledge(recovered)
            self.assertFalse((self.repairs.home / "recovery.json").exists())
            approval = {**self.state, "stuck_return": "WAIT_APPROVAL"}
            atomic_json(self.repairs.home / "recovery.json", {**ticket, "phase": "WAIT_APPROVAL", "binding": state_binding(approval)})
            self.assertEqual(selfheal_events.recover(approval), approval)

    def fake_agent(self, folder, record):
        (folder / "candidate/src/bug.py").write_text("def parse(value):\n    return value[0] if value else None\n")
        (folder / "candidate/tests/test_selfheal_regression_empty.py").write_text(
            "import unittest\nfrom bug import parse\nclass Empty(unittest.TestCase):\n    def test_empty(self):\n        self.assertIsNone(parse([]))\n")

    def test_supervisor_validates_promotes_and_requires_durable_phase_progress(self):
        supervisor = Supervisor(self.source, self.data, "auto")
        def start():
            supervisor.boot = "new-boot"
            supervisor.worker = Mock()
            supervisor.worker.poll.return_value = None
            atomic_json(self.repairs.home / "events/progress.json", {"boot": "new-boot", "epoch": epoch(self.source),
                "task_id": "task", "kind": "phase_completed", "origin": "EXPLORING", "phase": "WAIT_APPROVAL"})
        with patch.object(supervisor, "agent", side_effect=self.fake_agent), \
             patch.object(supervisor, "start", side_effect=start), patch.object(supervisor, "stop"), patch.object(supervisor, "notify"):
            supervisor.handle(self.incident)
        self.assertEqual(read_json(self.repairs.home / "promotion.json")["status"], "committed")
        self.assertEqual(read_json(self.data / "state.json"), self.state)

    def test_propose_does_not_promote_or_resume_task(self):
        supervisor = Supervisor(self.source, self.data, "propose")
        original = inventory(self.source)
        with patch.object(supervisor, "agent", side_effect=self.fake_agent), patch.object(supervisor, "start") as start, \
             patch.object(supervisor, "stop"), patch.object(supervisor, "notify"):
            supervisor.handle(self.incident)
        start.assert_called_once()
        self.assertEqual(inventory(self.source), original)
        self.assertFalse((self.repairs.home / "promotion.json").exists())

    def test_observation_failure_rolls_back_without_resetting_external_receipts(self):
        supervisor = Supervisor(self.source, self.data, "auto")
        original = inventory(self.source)
        receipt = self.data / "accepted-operation.json"
        receipt.write_text('"remote-PR-accepted"')
        def start():
            supervisor.worker = Mock()
            supervisor.worker.poll.return_value = 1
        def stop():
            supervisor.worker = None
        with patch.object(supervisor, "agent", side_effect=self.fake_agent), patch.object(supervisor, "start", side_effect=start), \
             patch.object(supervisor, "stop", side_effect=stop), patch.object(supervisor, "notify"):
            supervisor.handle(self.incident)
        self.assertEqual(inventory(self.source), original)
        self.assertEqual(receipt.read_text(), '"remote-PR-accepted"')
        self.assertFalse((self.repairs.home / "recovery.json").exists())

    def test_pause_cancels_repair_without_restart(self):
        supervisor = Supervisor(self.source, self.data, "auto")
        with patch.object(supervisor, "agent", side_effect=RepairCancelled("paused")), \
             patch.object(supervisor, "stop"), patch.object(supervisor, "start") as start, patch.object(supervisor, "notify"):
            supervisor.handle(self.incident)
        start.assert_not_called()
        self.assertFalse((self.repairs.home / "promotion.json").exists())

    def test_stale_incident_does_not_call_agent(self):
        supervisor = Supervisor(self.source, self.data, "auto")
        with patch.object(supervisor, "stop"), patch.object(supervisor, "start"), patch.object(supervisor, "notify"), \
             patch.object(supervisor, "agent") as agent:
            supervisor.handle({**self.incident, "binding": "old-decision"})
        agent.assert_not_called()

    def test_restart_budget_persists_across_supervisor_processes(self):
        for _ in range(3):
            self.assertTrue(Supervisor(self.source, self.data, "observe").restart_allowed())
        self.assertFalse(Supervisor(self.source, self.data, "observe").restart_allowed())

    def test_pid_reuse_never_signals_unrelated_process(self):
        with patch("scripts.supervisor.process_table", return_value={42: {"ticks": "new"}}), \
             patch("scripts.supervisor.os.kill") as kill:
            signal_owned({42: {"ticks": "old"}}, signal.SIGKILL)
        kill.assert_not_called()

    def test_corrupt_worker_telemetry_cannot_crash_supervisor(self):
        path = self.root / "worker.json"
        for value in ("truncated", "[]", "null"):
            path.write_text(value)
            self.assertEqual(worker_event(path), {})

    @unittest.skipUnless(sys.platform == "linux", "GNU stat/container healthcheck fixture")
    def test_supervisor_healthcheck_ignores_intentionally_stopped_worker(self):
        worker = self.data / "heartbeat"
        worker.write_text("stopped")
        os.utime(worker, (1, 1))
        atomic_json(self.repairs.home / "heartbeat.json", {"time": 1})
        result = subprocess.run(["bash", str(Path(__file__).resolve().parent.parent / "scripts/healthcheck.sh")],
            capture_output=True, text=True, env={**os.environ, "CODEBOT_DATA_DIR": str(self.data),
                                                "CODEBOT_SELF_HEAL": "observe", "CODEBOT_HEARTBEAT_HARD": "9000"})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_process_timeout_and_pause_kill_owned_group(self):
        for cancel in (None, lambda: True):
            with self.subTest(cancel=bool(cancel)):
                expected = RepairCancelled if cancel else subprocess.TimeoutExpired
                with self.assertRaises(expected):
                    run_process([sys.executable, "-c", "import time; time.sleep(60)"], self.root,
                                safe_test_environment(self.root / "process"), 0.05, self.root / "process.log", cancelled=cancel)

    @unittest.skipUnless(sys.platform == "linux", "Linux subreaper ownership fixture")
    def test_linux_worker_shutdown_reaps_detached_tools_in_separate_process(self):
        (self.source / "src/main.py").write_text(
            "import os, subprocess, sys, time\nfrom pathlib import Path\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'], start_new_session=True)\n"
            "Path(os.environ['CODEBOT_DATA_DIR'], 'child.pid').write_text(str(child.pid))\n"
            "time.sleep(60)\n")
        # Never make the test runner itself a subreaper or signal its children.
        code = (
            "import ctypes, os, time\nfrom pathlib import Path\nfrom scripts.supervisor import Supervisor\n"
            "assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0\n"
            f"supervisor = Supervisor({str(self.source)!r}, {str(self.data)!r}, 'observe')\n"
            "supervisor.start()\n"
            "try:\n"
            "    deadline = time.monotonic() + 5\n"
            "    while not (supervisor.data / 'child.pid').exists():\n"
            "        assert time.monotonic() < deadline\n"
            "        time.sleep(0.02)\n"
            "    child = int((supervisor.data / 'child.pid').read_text())\n"
            "finally:\n"
            "    supervisor.stop()\n"
            "assert not Path('/proc', str(child)).exists(), 'detached tool survived'\n"
        )
        result, output = run_process([sys.executable, "-c", code], Path(__file__).resolve().parent.parent,
                                     safe_test_environment(self.root / "lifecycle"), 20, self.root / "lifecycle.log")
        self.assertEqual(result, 0, output)

    def test_paused_supervisor_does_not_start_worker_on_boot(self):
        supervisor = Supervisor(self.source, self.data, "observe")
        atomic_json(self.repairs.home / "control.json", {"action": "pause"})
        with patch("scripts.supervisor.sys.platform", "darwin"), patch("scripts.supervisor.time.sleep", side_effect=StopRequested), \
             patch.object(supervisor, "stop"), patch.object(supervisor, "start") as start, patch.object(supervisor, "notify"):
            with self.assertRaises(StopRequested):
                supervisor.run()
        start.assert_not_called()

    def test_opencode_adapter_keeps_provider_settings_without_task_context(self):
        folder, _, record = self.candidate()
        supervisor = Supervisor(self.source, self.data, "auto")
        settings = {"providers": {"fixture": {"settings": {"apiKey": "{env:MODEL_API_KEY}"}}}, "mcp": {"servers": {"task": {}}}}
        with patch.dict(os.environ, {"CODEBOT_AGENT": "opencode", "OPENCODE_MODEL": "fixture/model",
                                     "MODEL_API_KEY": "model-secret", "GH_TOKEN": "task-secret",
                                     "OPENCODE_CONFIG_CONTENT": json.dumps(settings)}), \
             patch("scripts.supervisor.run_process", side_effect=[(0, "--standalone"), (0, "done")]) as run:
            supervisor.agent(folder, record)
        args = run.call_args.args
        self.assertEqual(args[0][:3], ["opencode", "--standalone", "run"])
        self.assertNotIn("--session", args[0])
        self.assertNotIn("GH_TOKEN", args[2])
        self.assertNotIn("mcp", json.loads(args[2]["OPENCODE_CONFIG_CONTENT"]))
        self.assertEqual(args[2]["MODEL_API_KEY"], "model-secret")


if __name__ == "__main__":
    unittest.main()
