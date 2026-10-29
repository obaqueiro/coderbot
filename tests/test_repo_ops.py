"""RepoOps and repo-mode delivery against real git: a bare origin and a clone, no fakes
under push, HEAD or the dirty check."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import errors
import lifecycle

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main


def run(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class RealCheckout(unittest.TestCase):
    def setUp(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.origin, self.repo = tmp / "origin.git", tmp / "repo"
        run(tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        run(tmp, "clone", "-q", str(self.origin), str(self.repo))
        for name, value in (("user.name", "t"), ("user.email", "t@example.com")):
            run(self.repo, "config", name, value)
        (self.repo / "app.txt").write_text("one\n")
        run(self.repo, "add", "-A")
        run(self.repo, "commit", "-qm", "initial")
        run(self.repo, "push", "-q", "origin", "main")
        run(self.repo, "checkout", "-qb", "task")
        self.ops = lifecycle.RepoOps.checkout(self.repo, "main")

    def commit(self, text):
        (self.repo / "app.txt").write_text(text)
        run(self.repo, "commit", "-qam", text.strip())

    def remote(self, branch="task"):
        return run(self.origin, "rev-parse", f"refs/heads/{branch}")

    def pushed(self):
        try:
            return self.remote()
        except subprocess.CalledProcessError:
            return ""

    def test_push_is_never_forced_and_a_non_fast_forward_is_refused(self):
        self.commit("two\n")
        with patch.object(lifecycle.operations, "tool", wraps=lifecycle.operations.tool) as tool:
            self.ops.push("task")
        argv = tool.call_args.args[0]
        self.assertEqual(argv, ["git", "push", "--quiet", "origin", "HEAD:refs/heads/task"])
        self.assertEqual(self.remote(), self.ops.head())
        # Someone else pushes to the PR branch; our diverged head must not overwrite it.
        theirs = Path(self.enterContext(tempfile.TemporaryDirectory())) / "theirs"
        run(self.repo.parent, "clone", "-q", "-b", "task", str(self.origin), str(theirs))
        run(theirs, "-c", "user.name=r", "-c", "user.email=r@example.com", "commit", "-q", "--allow-empty", "-m", "reviewer")
        run(theirs, "push", "-q", "origin", "task")
        external = self.remote()
        self.commit("three\n")
        with self.assertRaises(errors.CoderbotError):
            self.ops.push("task")
        self.assertEqual(self.remote(), external)

    def test_the_real_dirty_check_and_head(self):
        github = {"state": "OPEN", "headRefName": "task", "baseRefName": "main", "isCrossRepository": False,
                  "number": 7, "url": "https://github.com/acme/project/pull/7", "isDraft": False}
        record = {"branch": "task", "pr_url": github["url"], "pr_number": 7}
        ops = lifecycle.RepoOps.checkout(self.repo, "main", github["url"])
        self.commit("two\n")
        (self.repo / "app.txt").write_text("uncommitted\n")
        with patch.object(lifecycle.RepoOps, "gh", lambda ops, *a: json.dumps({**github, "headRefOid": self.pushed()})):
            with self.assertRaisesRegex(errors.AgentFault, "committed tracked changes"):
                lifecycle.deliver(ops, record, str, str, lambda: None)
            self.assertEqual(self.pushed(), "")  # nothing was pushed
            lifecycle.deliver(ops, record, str, str, lambda: None, ready=False, clean=False)
        self.assertEqual(record["delivered_sha"], run(self.repo, "rev-parse", "HEAD"))
        self.assertEqual((self.repo / "app.txt").read_text(), "uncommitted\n")  # left alone

    def test_the_approved_plan_is_committed_once_for_the_draft_attempt(self):
        change = self.repo / "openspec/changes/plan-x"
        change.mkdir(parents=True)
        (change / "tasks.md").write_text("- [ ] one\n")
        state = {"slug": "plan-x", "branch": "task", "item": "Plan x", "base_sha": run(self.repo, "rev-parse", "HEAD")}
        with patch.object(main.config, "REPO_PATH", self.repo), patch.object(main.config, "DRAFT_PR", True), \
             patch.object(main.config, "BASE_BRANCH", "main"), patch.object(main, "save_state"), \
             patch.object(main.lifecycle, "progress", return_value=[]):  # the draft could not open
            main._rails(state, commit_plan=True)
            (change / "tasks.md").write_text("- [x] one\n")  # the agent's progress, not the plan
            main._rails(state, commit_plan=True)
        subjects = run(self.repo, "log", "--format=%s", "main..task").splitlines()
        self.assertEqual(subjects, ["Add approved OpenSpec change plan-x"])
        self.assertTrue(run(self.repo, "status", "--porcelain"))


if __name__ == "__main__":
    unittest.main()
