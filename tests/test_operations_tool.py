"""operations.tool: the one wrapper for short git/gh/CLI children."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import errors
import operations
import turn_control


class Tool(unittest.TestCase):
    def test_invalid_utf8_is_replaced_and_bytes_mode_is_exact(self):
        code = "import sys; sys.stdout.buffer.write(b'ok\\xff\\n')"
        self.assertEqual(operations.tool([sys.executable, "-c", code]).stdout, "ok�\n")
        self.assertEqual(operations.tool([sys.executable, "-c", code], text=False).stdout, b"ok\xff\n")

    def test_stderr_keeps_a_bounded_tail_and_stdout_is_whole(self):
        code = ("import sys; sys.stdout.write('o' * 300000); "
                "sys.stderr.write('x' * 200000 + 'END')")
        result = operations.tool([sys.executable, "-c", code])
        self.assertEqual(len(result.stdout), 300000)
        self.assertEqual(len(result.stderr), operations.STDERR_TAIL)
        self.assertTrue(result.stderr.endswith("END"))

    def test_missing_executable_is_a_fatal_file_not_found(self):
        with self.assertRaises(FileNotFoundError) as caught:
            operations.tool(["definitely-not-installed-coderbot"])
        self.assertIsInstance(caught.exception, errors.MissingExecutable)
        self.assertEqual(errors.kind(caught.exception), errors.FATAL)
        self.assertEqual(errors.code(caught.exception), "MissingExecutable")

    def test_timeout_kills_the_whole_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "grandchild.pid"
            code = (f"import subprocess, sys, time; p = subprocess.Popen([sys.executable, '-c', "
                    f"'import time; time.sleep(30)']); open({str(marker)!r}, 'w').write(str(p.pid)); "
                    "time.sleep(30)")
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                operations.tool([sys.executable, "-c", code], timeout=1)
            self.assertLess(time.monotonic() - started, 15)
            pid = int(marker.read_text())
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(.1)
            else:
                self.fail("grandchild survived the group kill")

    def test_never_prompts_and_never_inherits_stdin(self):
        code = "import os, sys; print(os.environ['GIT_TERMINAL_PROMPT'], repr(sys.stdin.read()))"
        self.assertEqual(operations.tool([sys.executable, "-c", code]).stdout.strip(), "0 ''")
        self.assertEqual(operations.tool([sys.executable, "-c", "import sys; print(sys.stdin.read())"],
                                         input="hi").stdout.strip(), "hi")

    def test_kick_does_not_interrupt_a_tool_call(self):
        self.assertFalse(turn_control.request_kick())
        result = operations.tool([sys.executable, "-c", "print('done')"])
        self.assertEqual(result.stdout.strip(), "done")
        self.assertFalse(turn_control._processes)

    def test_check_raises_called_process_error(self):
        with self.assertRaises(subprocess.CalledProcessError):
            operations.tool([sys.executable, "-c", "raise SystemExit(3)"], check=True)

    def test_killed_git_leaves_no_fresh_index_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / ".git" / "index.lock"
            lock.parent.mkdir()
            lock.write_text("")
            operations._drop_stale_index_lock(["git", "commit"], tmp, time.time() + 60)
            self.assertTrue(lock.exists(), "a lock older than the call belongs to someone else")
            operations._drop_stale_index_lock(["git", "commit"], tmp, time.time() - 60)
            self.assertFalse(lock.exists())


if __name__ == "__main__":
    unittest.main()
