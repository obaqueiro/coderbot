"""Heartbeat, single-instance lock and the container healthcheck script."""
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main

main.config.STATE_PATH = pathlib.Path(tempfile.mkdtemp()) / "state.json"
ROOT = pathlib.Path(__file__).resolve().parent.parent


class SingleInstanceLock(unittest.TestCase):
    def test_second_holder_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(main.config, "DATA_DIR", pathlib.Path(tmp)):
            main._acquire_single_instance_lock()
            first = main._lock_handle
            try:
                with self.assertRaisesRegex(SystemExit, "refusing to start"):
                    main._acquire_single_instance_lock()
            finally:
                first.close()
            main._lock_handle = None
            main._acquire_single_instance_lock()  # released with the handle
            main._lock_handle.close()


class Heartbeat(unittest.TestCase):
    def test_heartbeat_written_only_while_tick_is_young(self):
        with tempfile.TemporaryDirectory() as tmp:
            beat = pathlib.Path(tmp) / "heartbeat"
            with patch.object(main.config, "HEARTBEAT_PATH", beat), \
                 patch.object(main.config, "HEARTBEAT_MAX_TICK_SECONDS", 100), \
                 patch.object(main.time, "sleep", side_effect=[None, StopIteration]):
                main._liveness["tick_started"] = time.time()
                with self.assertRaises(StopIteration):
                    main._heartbeat_loop()
            self.assertTrue(beat.exists())
            beat.unlink()
            with patch.object(main.config, "HEARTBEAT_PATH", beat), \
                 patch.object(main.config, "HEARTBEAT_MAX_TICK_SECONDS", 100), \
                 patch.object(main.time, "sleep", side_effect=StopIteration):
                main._liveness["tick_started"] = time.time() - 1000
                with self.assertRaises(StopIteration):
                    main._heartbeat_loop()
            self.assertFalse(beat.exists())

    def test_status_reports_heartbeat_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            beat = pathlib.Path(tmp) / "heartbeat"
            beat.write_text("1")
            with patch.object(main.config, "HEARTBEAT_PATH", beat):
                self.assertIsNotNone(main._heartbeat_age())
            with patch.object(main.config, "HEARTBEAT_PATH", pathlib.Path(tmp) / "missing"):
                self.assertIsNone(main._heartbeat_age())


class HealthcheckScript(unittest.TestCase):
    def run_check(self, age: int | None, **env):
        with tempfile.TemporaryDirectory() as tmp:
            if age is not None:
                beat = pathlib.Path(tmp) / "heartbeat"
                beat.write_text("x")
                os.utime(beat, (time.time() - age, time.time() - age))
            proc = subprocess.run(
                ["bash", str(ROOT / "scripts/healthcheck.sh")], capture_output=True, text=True,
                env={**os.environ, "CODEBOT_DATA_DIR": tmp, "PATH": os.environ["PATH"], **env})
            return proc.returncode, proc.stderr

    @unittest.skipIf(subprocess.run(["stat", "-c", "%Y", "/"], capture_output=True).returncode != 0,
                     "GNU stat not available")
    def test_thresholds(self):
        self.assertEqual(self.run_check(None)[0], 0)  # no heartbeat yet: starting
        self.assertEqual(self.run_check(10)[0], 0)
        rc, err = self.run_check(500, CODEBOT_HEARTBEAT_STALE="360", CODEBOT_HEARTBEAT_HARD="9000")
        self.assertEqual(rc, 1)
        self.assertIn("stale", err)

    def test_data_dir_default_is_container_path(self):
        source = (ROOT / "scripts/healthcheck.sh").read_text()
        self.assertIn('DATA_DIR="${CODEBOT_DATA_DIR:-/app/data}"', source)
        self.assertNotIn("/Users/", source)


if __name__ == "__main__":
    unittest.main()


class GracefulShutdown(unittest.TestCase):
    """SIGTERM (docker stop, Spot interruption) must interrupt a running child and make
    main() return without touching state.json."""

    def tearDown(self):
        import signal
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.default_int_handler)

    def test_sigterm_kills_running_subprocess(self):
        import signal
        import threading
        main._install_shutdown_handlers()
        threading.Timer(0.3, os.kill, args=(os.getpid(), signal.SIGTERM)).start()
        started = time.monotonic()
        with self.assertRaises(main.ShutdownRequested) as ctx:
            subprocess.run(["sleep", "30"], capture_output=True, timeout=60)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(str(ctx.exception), "SIGTERM")

    def test_main_exits_cleanly_without_saving_state(self):
        import signal
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(main.config, "DATA_DIR", pathlib.Path(tmp)), \
                patch.object(main.config, "USER_EMAIL", "u@x"), \
                patch.object(main.config, "TASK_SOURCE", "gdoc"), \
                patch.object(main.config, "DOC_ID", "doc"), \
                patch.object(main.config, "REPO_PATH", ROOT), \
                patch.object(main, "validate_managed_runtime"), \
                patch.object(main.task_source, "SOURCES", ("gdoc",)), \
                patch.object(main, "load_state", return_value={"state": "IDLE"}), \
                patch.object(main, "save_state") as save, \
                patch.object(main, "_heartbeat_loop"), \
                patch.object(main, "_status_supervisor_loop"), patch.object(main, "_probe_runtime"), \
                patch.object(main, "check_commands", return_value=False), \
                patch.object(main, "_maybe_ping"), \
                patch.object(main.time, "sleep", side_effect=AssertionError("the tick failed and the loop backed off")), \
                patch.dict(main.PHASES, {"IDLE": lambda st: os.kill(os.getpid(), signal.SIGTERM)}):
            main.main()  # returns instead of raising
            save.assert_not_called()
            self.assertIsNone(main._lock_handle)
            main._acquire_single_instance_lock()  # lock was released on the way out
            main._lock_handle.close()
            main._lock_handle = None
