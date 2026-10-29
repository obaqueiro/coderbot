"""H9: no state save behind network I/O, a lock-free signal handler, and a lateral
Claude process that never shares the main ~/.claude.json."""
import json
import signal
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_runner
import claude_runner
import main
import turn_control


def _in_thread(function, seconds=5):
    outcome = {}

    def target():
        try:
            outcome["value"] = function()
        except BaseException as error:  # noqa: BLE001
            outcome["error"] = error
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    return None if thread.is_alive() else outcome


class Locks(unittest.TestCase):
    def test_save_state_does_not_wait_for_the_status_lock(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(main.config, "STATE_PATH", Path(tmp) / "state.json"):
            held, release = threading.Event(), threading.Event()

            def status_thread_doing_io():
                with main._status_command_lock:
                    held.set()
                    release.wait(10)
            threading.Thread(target=status_thread_doing_io, daemon=True).start()
            held.wait(5)
            try:
                self.assertIsNotNone(_in_thread(lambda: main.save_state({"state": "IDLE"})),
                                     "save_state blocked behind the status lock")
            finally:
                release.set()
            self.assertEqual(json.loads((Path(tmp) / "state.json").read_text()), {"state": "IDLE"})

    def test_progress_publish_does_not_hold_the_status_lock(self):
        seen = []

        def free():
            if not main._status_command_lock.acquire(timeout=1):
                return False
            main._status_command_lock.release()
            return True
        with patch.object(main.gmail_client, "update_progress",
                          side_effect=lambda *_: seen.append(_in_thread(free, 3))):
            main._publish_progress({"thread_id": "t", "state": "IMPLEMENTING", "item": "x"})
        self.assertEqual(seen, [{"value": True}])

    def test_signal_handler_takes_no_lock(self):
        main._shutdown.clear()
        with turn_control._lock:  # the main thread was inside register/release
            outcome = _in_thread(lambda: main._request_shutdown(signal.SIGTERM, None), 3)
        self.assertIsNotNone(outcome, "the handler deadlocked on a lock")
        self.assertIsInstance(outcome["error"], main.ShutdownRequested)
        self.assertIsNone(_in_thread(lambda: main._request_shutdown(signal.SIGTERM, None))
                          .get("error"), "a second signal must not interrupt the cleanup")
        main._shutdown.clear()


class LateralConfigDir(unittest.TestCase):
    def run_as(self, role, env):
        token = turn_control.role.set(role)
        try:
            with tempfile.TemporaryDirectory() as tmp, \
                    patch.object(claude_runner.config, "DATA_DIR", Path(tmp)), \
                    patch.object(claude_runner.secret_store, "environment", return_value=dict(env)), \
                    patch.object(claude_runner.operations, "run") as run:
                claude_runner._run(["claude", "--tools", ""], "hi")
                return run.call_args.kwargs["env"], Path(tmp)
        finally:
            turn_control.role.reset(token)

    def test_lateral_claude_gets_its_own_config_dir_when_auth_is_in_the_env(self):
        env, data = self.run_as("conversation", {"CLAUDE_CODE_OAUTH_TOKEN": "t"})
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(data / "claude-lateral"))
        work, _ = self.run_as("work", {"CLAUDE_CODE_OAUTH_TOKEN": "t"})
        self.assertNotIn("CLAUDE_CONFIG_DIR", work)
        # File-based OAuth would race the main process's token refresh: share instead.
        shared, _ = self.run_as("conversation", {})
        self.assertNotIn("CLAUDE_CONFIG_DIR", shared)

    def test_lateral_session_from_the_shared_dir_starts_fresh(self):
        missing = RuntimeError('claude exited 1: stdout="No conversation found with session ID: old"')
        fresh = claude_runner.ClaudeResult("new", "hello")
        with patch.object(agent_runner.config, "AGENT", "claude"), \
                patch.object(agent_runner.claude_runner, "_invoke", side_effect=[missing, fresh]) as invoke:
            agent_runner.converse("hi", "old")
        self.assertIn("--resume", invoke.call_args_list[0].args[0])
        self.assertNotIn("--resume", invoke.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
