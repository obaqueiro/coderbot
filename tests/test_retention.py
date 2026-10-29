"""H10: bounded disk growth and a non-blocking runtime probe."""
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import retention
from execution_store import ExecutionStore

ROOT = Path(__file__).resolve().parents[1]


class Sweep(unittest.TestCase):
    def test_old_unreferenced_entries_go_and_the_rest_stay(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            old = time.time() - (retention.KEEP_DAYS + 1) * 86400
            entries = {}
            for name in ("outbox/checks/run-old", "outbox/checks/run-new", "diagnostics/old.txt",
                         "workspaces/held-task", "workspaces/stale-task", "outbox/evidence.webm"):
                path = data / name
                path.parent.mkdir(parents=True, exist_ok=True)
                (path.mkdir() if "." not in path.name else path.write_text("x"))
                entries[name] = path
                if "new" not in name:
                    os.utime(path, (old, old))
            stale_temp = data / ".state.json.abc.tmp"
            stale_temp.write_text("{")
            os.utime(stale_temp, (old, old))
            store = ExecutionStore(data / "execution.sqlite")
            store.put("checkpoint", task_id="done", data={}, status="complete")
            store.put("checkpoint", task_id="live", data={}, status="running")
            with store.connection() as db:
                db.execute("UPDATE checkpoint SET updated=?", (old,))
            with patch.object(retention.operations, "tool") as tool:
                removed = retention.sweep(data, data, keep=[entries["workspaces/held-task"]], store=store)
            tool.assert_called_once()
            self.assertEqual(tool.call_args.args[0], ["git", "worktree", "prune"])
            self.assertEqual(removed, 4)
            self.assertFalse(entries["outbox/checks/run-old"].exists())
            self.assertFalse(entries["diagnostics/old.txt"].exists())
            self.assertFalse(entries["workspaces/stale-task"].exists())
            self.assertFalse(stale_temp.exists())
            for kept in ("outbox/checks/run-new", "workspaces/held-task", "outbox/evidence.webm"):
                self.assertTrue(entries[kept].exists(), kept)
            self.assertEqual([r["task_id"] for r in store.list("checkpoint", "live")], ["live"])
            self.assertEqual(store.list("checkpoint", "done"), [])

    def test_runs_at_most_once_a_day_and_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(retention, "sweep", side_effect=RuntimeError("disk")) as sweep:
                retention.maybe_sweep(Path(tmp), Path(tmp))
                retention.maybe_sweep(Path(tmp), Path(tmp))
            sweep.assert_called_once()


class Probe(unittest.TestCase):
    def test_pins_match_the_dockerfile(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        for package, name in (("@anthropic-ai/claude-code", "claude"), ("opencode-ai", "opencode"),
                              ("@fission-ai/openspec", "openspec")):
            self.assertIn(f"{package}@{retention.PINNED[name]}", dockerfile)

    def test_mismatches_are_reported_not_raised(self):
        def tool(argv, **_):
            if argv[0] == "openspec":
                raise FileNotFoundError("openspec")
            out = {"--version": "2.0.0 (Claude Code)", "--help": "--resume --model"}[argv[1]]
            return subprocess.CompletedProcess(argv, 0, out, "")
        with patch.object(retention.operations, "tool", side_effect=tool):
            problems = retention.probe("claude")
        self.assertTrue(any(re.search(r"claude: version '2.0.0', pinned", p) for p in problems))
        self.assertTrue(any(p.startswith("openspec: not runnable") for p in problems))
        self.assertTrue(any("--setting-sources" in p for p in problems))
        self.assertFalse(any(p.startswith("opencode") for p in problems))


if __name__ == "__main__":
    unittest.main()
