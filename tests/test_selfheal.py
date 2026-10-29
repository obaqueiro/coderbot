"""Independent repair tests: no live agent, provider, data directory or daemon."""
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from scripts.selfheal import (Repairs, Runbooks, RepairError, atomic_json, eligible, epoch, inventory,
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

    def typed_candidate(self, **fields):
        self.incident = {**self.incident, "version": 2, "class": "agent", "code": "IndexError",
                         "frames": ["src/main.py:9:run", "src/bug.py:2:parse"], **fields}
        return self.candidate()

    def test_reproduction_must_raise_the_incident_exception_through_its_frame(self):
        folder, _, _ = self.typed_candidate()
        self.assertEqual(self.repairs.validate(folder), ["src/bug.py", "tests/test_selfheal_regression_empty.py"])
        for number, (fields, test) in enumerate((
                ({}, "self.assertEqual(parse([1]), 2)"),  # fails, but not by the incident
                ({"code": "TypeError"}, "self.assertIsNone(parse([]))"),
                ({"frames": ["src/other.py:1:load"]}, "self.assertIsNone(parse([]))"))):
            with self.subTest(fields=fields):
                (self.repairs.home / "budget.json").unlink(missing_ok=True)
                folder, _, _ = self.typed_candidate(task_id=str(number), **fields)
                regression = folder / "candidate/tests/test_selfheal_regression_empty.py"
                regression.write_text(regression.read_text().replace("self.assertIsNone(parse([]))", test))
                with self.assertRaisesRegex(RepairError, "reproduce|Regression"):
                    self.repairs.validate(folder)

    def test_oversized_repairs_are_rejected_before_tests_run(self):
        folder, _, _ = self.candidate()
        (folder / "candidate/src/bug.py").write_text("def parse(value):\n    return value[0] if value else None\n"
                                                    + "X = 1\n" * 500)
        with patch.object(self.repairs, "suite") as suite, self.assertRaisesRegex(RepairError, "too large"):
            self.repairs.validate(folder)
        suite.assert_not_called()

    def test_standing_failures_on_old_code_do_not_burn_a_candidate(self):
        (self.source / "tests/test_standing.py").write_text(
            "import unittest\nfrom bug import parse\nclass Standing(unittest.TestCase):\n"
            "    def test_broken(self):\n        self.fail('old')\n"
            "    def test_first(self):\n        self.assertEqual(parse([5]), 5)\n")
        folder, _, _ = self.candidate()
        self.repairs.validate(folder)
        [cached] = (self.repairs.home / "baseline").glob("*.json")
        self.assertEqual(len(read_json(cached)["failures"]), 1)
        shutil.rmtree(folder / "reproduction")
        with patch.object(self.repairs, "suite", wraps=self.repairs.suite) as suite:
            self.repairs.validate(folder)  # cached for this runtime epoch
        self.assertEqual(suite.call_count, 1)
        shutil.rmtree(folder / "reproduction")
        (folder / "candidate/src/bug.py").write_text("def parse(value):\n    return None\n")  # a new failure
        with self.assertRaisesRegex(RepairError, "suite must pass"):
            self.repairs.validate(folder)

    def test_protected_files_and_approval_code_are_refused_before_admission(self):
        bug = {**self.incident, "version": 2, "class": "agent", "code": "KeyError"}
        self.assertFalse(eligible({**bug, "frames": ["src/config.py:3:<module>"]}))
        self.assertFalse(eligible({**bug, "frames": ["src/main.py:3:do_approval_reply"]}))
        with self.assertRaisesRegex(RepairError, "not eligible"):
            self.repairs.admit({**bug, "frames": ["src/main.py:3:do_merge_reply"]})
        self.assertFalse((self.repairs.home / "budget.json").exists())

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
        def stop():
            supervisor.worker = None
        with patch.object(supervisor, "agent", side_effect=self.fake_agent), \
             patch.object(supervisor, "start", side_effect=lambda: setattr(supervisor, "worker", Mock())) as start, \
             patch.object(supervisor, "stop", side_effect=stop), patch.object(supervisor, "notify"):
            supervisor.handle(self.incident)
        # S7: up again while the candidate is validated, and after the proposal on the old code.
        self.assertEqual(start.call_count, 2)
        self.assertIsNotNone(supervisor.worker)
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

    def test_exhausted_restarts_back_off_and_probe_instead_of_pausing_forever(self):
        supervisor = Supervisor(self.source, self.data, "observe")
        control = self.repairs.home / "control.json"
        with patch.object(supervisor, "notify"):
            supervisor.trip("startup outage")
        first = read_json(control)
        self.assertEqual((first["action"], first["trips"]), ("cooldown", 1))
        self.assertTrue(supervisor.paused())
        atomic_json(control, {**first, "until": 0})  # cooldown over
        supervisor.breaker_tick()
        self.assertEqual(read_json(control)["action"], "probe")
        self.assertFalse(supervisor.paused())
        self.assertEqual(read_json(self.repairs.home / "restarts.json")["used"], 0)
        with patch.object(supervisor, "notify"):
            supervisor.trip("still down")
        second = read_json(control)
        self.assertEqual(second["trips"], 2)
        self.assertGreater(second["until"] - time.time(), 550)  # 10 min: the backoff doubled

    def test_corrupt_supervisor_state_is_quarantined_not_fatal(self):
        path = self.repairs.home / "restarts.json"
        path.write_text("{torn")
        self.assertEqual(read_json(path, {"used": 0}), {"used": 0})
        self.assertFalse(path.exists())
        self.assertTrue(list(self.repairs.home.glob("restarts.json.corrupt-*")))

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

    def test_live_tree_drift_during_repair_is_restored_kept_aside_and_paused(self):
        supervisor = Supervisor(self.source, self.data, "auto")
        original = inventory(self.source)
        def escaping_agent(folder, record):
            self.fake_agent(folder, record)
            (self.source / "src/bug.py").write_text("ESCAPED = True\n")  # wrote the live tree, not the candidate
            (self.source / "src/new.py").write_text("NEW = True\n")
        with patch.object(supervisor, "agent", side_effect=escaping_agent), patch.object(supervisor, "stop"), \
             patch.object(supervisor, "start") as start, patch.object(supervisor, "notify"):
            supervisor.handle(self.incident)
        start.assert_not_called()
        self.assertEqual(inventory(self.source), original)
        self.assertEqual(read_json(self.repairs.home / "control.json"), {"action": "pause"})
        self.assertFalse((self.repairs.home / "promotion.json").exists())
        [folder] = (self.repairs.home / "repairs").glob("*/*")
        self.assertEqual((folder / "drift/src/bug.py").read_text(), "ESCAPED = True\n")
        self.assertEqual((folder / "drift/src/new.py").read_text(), "NEW = True\n")
        [event] = [read_json(p) for p in (self.repairs.home / "notifications").glob("*.json")
                   if read_json(p)["kind"] == "repair_paused"]
        self.assertIn("src/bug.py, src/new.py", event["detail"])

    def test_repair_agent_timeout_alert_carries_neither_argv_nor_prompt(self):
        supervisor = Supervisor(self.source, self.data, "auto")
        folder = self.repairs.home / "repairs/x/1"
        (folder / "candidate").mkdir(parents=True)
        with patch.dict(os.environ, {"CODEBOT_AGENT": "claude", "CODEBOT_REPAIR_AGENT_TIMEOUT": "7"}), \
             patch("scripts.supervisor.run_process", side_effect=lambda argv, *a, **k: (_ for _ in ()).throw(
                 subprocess.TimeoutExpired(argv, 7))) as run:
            with self.assertRaises(RepairError) as raised:
                supervisor.agent(folder, self.incident)
        self.assertEqual(run.call_args.args[3], 7)
        self.assertEqual(str(raised.exception), "Repair agent failed: no result within 7 s; see private agent log")

    def test_boot_rollback_of_an_interrupted_promotion_closes_its_incident_record(self):
        folder, path, record = self.candidate()
        changed = self.repairs.validate(folder)
        self.repairs.promote(folder, changed)
        atomic_json(path, {**record, "status": "observing"})  # then the supervisor was killed
        supervisor = Supervisor(self.source, self.data, "observe")
        with patch("scripts.supervisor.sys.platform", "darwin"), patch("scripts.supervisor.time.sleep", side_effect=StopRequested), \
             patch.object(supervisor, "stop"), patch.object(supervisor, "start"), patch.object(supervisor, "notify"), \
             patch.object(supervisor.runbooks, "run"), patch.object(supervisor, "restart_allowed", return_value=True):
            atomic_json(self.repairs.home / "control.json", {"action": "pause"})  # keep the loop to one pass
            with self.assertRaises(StopRequested):
                supervisor.run()
        self.assertEqual(inventory(self.source), inventory(folder / "before"))
        self.assertEqual(read_json(path)["status"], "rolled_back")
        kinds = [read_json(p)["kind"] for p in (self.repairs.home / "notifications").glob("*.json")]
        self.assertIn("repair_rolled_back", kinds)

    def test_a_stop_during_a_process_wait_survives_killpg_eperm(self):
        # macOS returns EPERM from killpg when the group holds only a zombie; a PermissionError
        # there replaced StopRequested, notify() swallowed it and SIGTERM was lost for good.
        stops = iter([False, True])
        def cancelled():
            if next(stops):
                raise StopRequested()
            return False
        with patch("scripts.selfheal.os.killpg", side_effect=PermissionError(1, "Operation not permitted")):
            with self.assertRaises(StopRequested):
                run_process([sys.executable, "-c", "import time; time.sleep(1.5)"], self.root, {"PATH": os.environ["PATH"]},
                            30, self.root / "wait.log", cancelled=cancelled)

    def test_stop_signal_while_spawning_the_worker_is_deferred_until_it_is_tracked(self):
        import scripts.supervisor as module
        supervisor = Supervisor(self.source, self.data, "observe")
        process = Mock(stdout=[])
        def spawn(*args, **kwargs):
            module.stop_signal(signal.SIGTERM, None)  # SIGTERM between fork and assignment
            return process
        handlers = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
        self.addCleanup(signal.signal, signal.SIGTERM, handlers[0])
        self.addCleanup(signal.signal, signal.SIGINT, handlers[1])
        self.addCleanup(module.SPAWNING.update, active=False, stop=False)
        with patch("scripts.supervisor.subprocess.Popen", side_effect=spawn):
            with self.assertRaises(StopRequested):
                supervisor.start()
        self.assertIs(supervisor.worker, process)  # so the caller's stop() kills and reaps it

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


class SupervisorHygiene(unittest.TestCase):
    def test_liveness_tolerates_long_poll_intervals_and_bad_values(self):
        import importlib
        import scripts.supervisor as supervisor_module
        for value, expected in (("300", 900), ("", 360), ("2m", 360), ("30", 180)):
            with patch.dict(os.environ, {"CODEBOT_POLL_INTERVAL": value}):
                self.assertEqual(importlib.reload(supervisor_module).LIVENESS_SECONDS, expected)
        with patch.dict(os.environ, {"CODEBOT_AGENT_TIMEOUT": "7200"}):
            self.assertEqual(importlib.reload(supervisor_module).OBSERVATION_SECONDS, 8100)
        self.assertGreaterEqual(importlib.reload(supervisor_module).OBSERVATION_SECONDS, 900)


class RepairAgentScope(unittest.TestCase):
    def test_repair_agent_cannot_publish_commit_or_read_live_secrets(self):
        import scripts.supervisor as supervisor_module
        denied = supervisor_module.REPAIR_DENIED_CLAUDE
        for rule in ("Bash(git push:*)", "Bash(git commit:*)", "Bash(gh:*)", "Bash(docker:*)",
                     "Read(/app/.env)"):
            self.assertIn(rule, denied)

    def test_repair_agent_reads_its_candidate_but_no_other_live_data(self):
        import scripts.supervisor as supervisor_module
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            folder = data / "supervisor/repairs/inc-1"
            (folder / "candidate").mkdir(parents=True)
            (data / "supervisor/repairs/inc-0").mkdir()
            (data / "state.json").write_text("{}")
            (data / "supervisor/control.json").write_text("{}")
            denied = supervisor_module.repair_denied_reads(data, folder)
        self.assertIn(f"Read({data / 'state.json'})", denied)
        self.assertIn(f"Read({data / 'supervisor/control.json'}/**)", denied)
        self.assertIn(f"Read({data / 'supervisor/repairs/inc-0'}/**)", denied)
        self.assertFalse(any("inc-1" in rule for rule in denied))


class RunbookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repairs = Repairs(self.root / "source", self.root / "data")
        self.runbooks = Runbooks(self.repairs)
        for name, value in (("CODEBOT_REPO_PATH", str(self.root / "repo")), ("HOME", str(self.root / "home"))):
            manager = patch.dict(os.environ, {name: value})
            manager.start()
            self.addCleanup(manager.stop)
        manager = patch("scripts.selfheal.git_running", return_value=False)
        self.git_running = manager.start()
        self.addCleanup(manager.stop)

    def aged(self, path, text="x", age=3600):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        os.utime(path, (time.time() - age, time.time() - age))
        return path

    def events(self, kind):
        return [event for event in (json.loads(path.read_text()) for path in (self.repairs.home / "notifications").glob("*.json"))
                if event["kind"] == kind]

    def test_stale_index_lock_is_removed_only_when_no_git_runs_and_dry_run_reports(self):
        lock = self.aged(self.root / "repo/.git/index.lock")
        fresh = self.aged(self.root / "repo/child/.git/index.lock", age=10)
        self.git_running.return_value = True
        self.assertEqual(self.runbooks.run(act=True), [])
        self.git_running.return_value = False
        self.assertEqual(self.runbooks.run(act=False), ["git_index_lock"])
        self.assertTrue(lock.exists())
        self.assertEqual(self.runbooks.run(act=True), [])  # the same finding is not repeated within hours
        (self.repairs.home / "runbooks.json").unlink()
        self.assertEqual(self.runbooks.run(act=True), ["git_index_lock"])
        self.assertFalse(lock.exists())
        self.assertTrue(fresh.exists())
        self.assertEqual(len(self.events("runbook_dry_run")), 1)
        self.assertEqual(len(self.events("runbook_applied")), 1)

    def test_corrupt_claude_config_is_restored_from_the_newest_valid_backup(self):
        config = self.aged(self.root / "home/.claude.json", "{torn")
        self.aged(self.root / "home/.claude/backups/.claude.json.backup.1", '{"old": 1}')
        self.aged(self.root / "home/.claude/backups/.claude.json.backup.2", '{"new": 2}')
        self.aged(self.root / "home/.claude/backups/.claude.json.backup.3", "{torn")
        self.assertIn("claude_config", self.runbooks.run(act=True))
        self.assertEqual(json.loads(config.read_text()), {"new": 2})
        self.assertTrue(list((self.root / "home").glob(".claude.json.corrupt-*")))

    def test_retention_prunes_only_owned_history_and_more_under_disk_pressure(self):
        old = self.aged(self.root / "data/diagnostics/old.md", age=20 * 86400)
        recent = self.aged(self.root / "data/diagnostics/recent.md", age=2 * 86400)
        evidence = self.aged(self.root / "data/outbox/evidence/video.mp4", age=40 * 86400)
        checks = self.aged(self.root / "data/outbox/checks/run/report.json", age=20 * 86400).parent
        os.utime(checks, (time.time() - 20 * 86400,) * 2)
        self.runbooks.run(act=True)
        self.assertFalse(old.exists() or checks.exists())
        self.assertTrue(recent.exists() and evidence.exists())
        self.runbooks.run({"version": 2, "class": "transient", "code": "OSError", "errno": "ENOSPC"}, act=True)
        self.assertFalse(recent.exists())
        self.assertTrue(evidence.exists())

    def test_retention_keeps_a_proposed_candidate_awaiting_review(self):
        proposed = self.aged(self.repairs.home / "repairs/inc-1/candidate/src/x.py", age=40 * 86400).parent.parent
        stale = self.aged(self.repairs.home / "repairs/inc-2/candidate/src/x.py", age=40 * 86400).parent.parent
        for folder in (proposed, stale):
            os.utime(folder, (time.time() - 40 * 86400,) * 2)
        (self.repairs.home / "incidents").mkdir(parents=True, exist_ok=True)
        (self.repairs.home / "incidents/inc-1.json").write_text(json.dumps(
            {"status": "human_required", "candidate": str(proposed.parent)}))
        self.runbooks.run(act=True)
        self.assertTrue(proposed.exists())
        self.assertFalse(stale.exists())

    def test_outage_is_deferred_not_repaired_and_a_failing_runbook_never_raises(self):
        supervisor = Supervisor(self.repairs.source, self.repairs.data, "observe")
        outage = {"id": "x", "version": 2, "class": "transient", "code": "ConnectionError", "phase": "EXPLORING"}
        with patch.object(supervisor, "notify"), patch.object(supervisor, "repair") as repair:
            supervisor.handle(outage)
        repair.assert_not_called()
        self.assertEqual([event["runbook"] for event in self.events("runbook_dry_run")], ["outage_backoff"])
        self.assertFalse(self.events("incident_detected"))
        with patch.object(self.runbooks, "registry", {"broken": (Mock(side_effect=OSError("boom")), None)}):
            self.assertEqual(self.runbooks.run(act=True), [])
        self.assertEqual(len(self.events("runbook_failed")), 1)


class TypedIncidents(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def raised(self, code, filename):
        namespace = {}
        exec(compile(code, str(filename), "exec"), namespace)
        try:
            namespace["step"]()
        except BaseException as error:
            return error

    def test_worker_incident_carries_class_code_and_bounded_relative_frames(self):
        error = self.raised("def step():\n    return None + 1\n", selfheal_events.SOURCE_ROOT / "src/fake_bug.py")
        fields = selfheal_events.incident_fields(error)
        self.assertEqual((fields["version"], fields["class"], fields["code"]), (2, "agent", "TypeError"))
        self.assertEqual(fields["frames"][-1], "src/fake_bug.py:2:step")
        self.assertFalse(any(str(selfheal_events.SOURCE_ROOT) in frame for frame in fields["frames"]))
        deep = selfheal_events.incident_fields(self.raised(
            "def step(depth=40):\n    return step(depth - 1) if depth else None + 1\n", self.root / "deep.py"))
        self.assertEqual(len(deep["frames"]), 16)
        disk = selfheal_events.incident_fields(OSError(28, "No space left on device", "/app/data/x"))
        self.assertEqual((disk["class"], disk["errno"], disk["resource"]), ("transient", "ENOSPC", "/app/data/x"))
        with patch.dict(os.environ, {"CODEBOT_SUPERVISOR_DIR": str(self.root)}):
            selfheal_events.incident({"state": "EXPLORING"}, error, binding="b")
        [event] = [json.loads(path.read_text()) for path in (self.root / "events").glob("*.json")]
        self.assertEqual((event["kind"], event["version"], event["code"], event["binding"]), ("incident", 2, "TypeError", "b"))

    def test_only_code_bugs_inside_src_reach_the_llm_repair(self):
        bug = {"version": 2, "class": "agent", "code": "TypeError", "phase": "EXPLORING",
               "frames": ["src/main.py:10:run", "src/bug.py:2:parse"]}
        self.assertTrue(eligible(bug))
        self.assertFalse(eligible({**bug, "frames": ["lib/python3.12/json/decoder.py:3:decode"]}))
        self.assertFalse(eligible({**bug, "class": "transient", "code": "OSError"}))
        self.assertFalse(eligible({**bug, "class": "fatal"}))
        self.assertFalse(eligible({**bug, "code": "AgentFault"}))
        legacy = {"category": "implementation", "error_type": "TypeError", "phase": "EXPLORING", "location": "parse"}
        self.assertTrue(eligible(legacy))

    def fixture(self, files):
        source = self.root / "source"
        for name, text in files.items():
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            (source / name).write_text(text)
        shutil.copy2(Path(selfheal_events.__file__), source / "src/selfheal_events.py")
        return Supervisor(source, self.root / "data", "observe")

    def crash_of(self, supervisor):
        supervisor.start()
        supervisor.worker.wait(timeout=30)
        supervisor.stop()
        return supervisor.crash()

    def test_bootstrap_records_import_and_configuration_failures(self):
        supervisor = self.fixture({"src/main.py": "import broken\n", "src/broken.py": "def parse(:\n"})
        crash = self.crash_of(supervisor)
        self.assertEqual((crash["code"], crash["class"], crash["phase"]), ("SyntaxError", "agent", "STARTUP"))
        self.assertEqual(crash["frames"][-1], "src/broken.py:1:<module>")
        self.assertTrue(eligible(crash))
        self.assertIsNone(supervisor.crash())  # consumed once
        (supervisor.source / "src/main.py").write_text("raise SystemExit('GH_TOKEN is missing')\n")
        self.assertEqual(self.crash_of(supervisor)["class"], "fatal")
        (supervisor.source / "src/main.py").write_text("import sys\nsys.exit(0)\n")
        self.assertIsNone(self.crash_of(supervisor))
