"""Resets never lose uncommitted work silently, and never skip the reset itself."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import config

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main


def run(repo, *args, check=True):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=check)


class Repo(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        run(self.repo, "init", "-q", "-b", "main")
        run(self.repo, "config", "user.email", "t@example.com")
        run(self.repo, "config", "user.name", "Test")
        (self.repo / "app.py").write_text("base\n")
        run(self.repo, "add", ".")
        run(self.repo, "commit", "-q", "-m", "base")
        for name, value in (("REPO_PATH", self.repo), ("BASE_BRANCH", "main"),
                            ("DATA_DIR", self.root / "data")):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)

    def clean(self):
        return run(self.repo, "status", "--porcelain").stdout.strip() == ""


class PreserveBeforeReset(Repo):
    def test_dirty_tree_is_stashed_and_tree_ends_clean(self):
        (self.repo / "app.py").write_text("human edit\n")
        (self.repo / "new.py").write_text("untracked\n")
        notes = main._reset_to_base_branch("hold task-x")
        self.assertTrue(self.clean())
        self.assertTrue(any("git stash 'codebot: hold task-x'" in n for n in notes), notes)
        stash = run(self.repo, "stash", "list").stdout
        self.assertIn("codebot: hold task-x", stash)
        run(self.repo, "stash", "pop")
        self.assertEqual((self.repo / "app.py").read_text(), "human edit\n")
        self.assertEqual((self.repo / "new.py").read_text(), "untracked\n")

    def test_clean_tree_reports_nothing(self):
        self.assertEqual(main._reset_to_base_branch(), [])
        self.assertEqual(run(self.repo, "stash", "list").stdout, "")

    def test_stash_failure_falls_back_to_backup_copy(self):
        (self.repo / "app.py").write_text("human edit\n")
        real = main._git_quiet
        with patch.object(main, "_git_quiet", side_effect=lambda *a: False if a[0] == "stash" else real(*a)):
            notes = main._reset_to_base_branch("abort y")
        self.assertTrue(self.clean())
        backups = list((self.root / "data" / "backups").glob("abort-y-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "app.py").read_text(), "human edit\n")
        self.assertTrue((backups[0] / "changes.patch").read_bytes())
        self.assertTrue(any(str(backups[0]) in n for n in notes), notes)

    def test_preservation_crash_never_blocks_the_reset(self):
        (self.repo / "app.py").write_text("edit\n")
        with patch.object(main, "_uncommitted_paths", side_effect=RuntimeError("boom")):
            notes = main._reset_to_base_branch()
        self.assertTrue(self.clean())
        self.assertTrue(any("could not preserve" in n for n in notes), notes)

    def test_mid_merge_conflict_is_preserved_and_reset_completes(self):
        run(self.repo, "checkout", "-q", "-b", "feature")
        (self.repo / "app.py").write_text("feature\n")
        run(self.repo, "commit", "-qam", "feature")
        run(self.repo, "checkout", "-q", "main")
        (self.repo / "app.py").write_text("main\n")
        run(self.repo, "commit", "-qam", "main")
        run(self.repo, "merge", "feature", check=False)
        self.assertTrue((self.repo / ".git" / "MERGE_HEAD").exists())
        notes = main._reset_to_base_branch("conflict")
        self.assertTrue(self.clean())
        self.assertFalse((self.repo / ".git" / "MERGE_HEAD").exists())
        self.assertTrue(notes)  # stashed or backed up, and said so

    def test_evidence_is_not_stashed(self):
        (self.repo / "demo.mp4").write_bytes(b"video")
        self.assertEqual(main._reset_to_base_branch(), [])


class CommitPendingWork(Repo):
    def test_credential_files_stay_out_of_the_hold_commit(self):
        run(self.repo, "checkout", "-q", "-b", "task")
        (self.repo / "app.py").write_text("work\n")
        (self.repo / ".env").write_text("API_KEY=x\n")
        (self.repo / "server.pem").write_text("key\n")
        (self.repo / ".env.example").write_text("API_KEY=\n")
        (self.repo / "feature.py").write_text("new\n")
        notes = main._commit_pending_work({"branch": "task"})
        committed = run(self.repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
        self.assertEqual(sorted(committed), [".env.example", "app.py", "feature.py"])
        self.assertTrue(any(".env" in n and "credentials" in n for n in notes), notes)
        self.assertTrue((self.repo / ".env").exists())


class SecretNames(unittest.TestCase):
    def test_names(self):
        for path in (".env", "config/.env.local", "a/b.pem", "x.key", "id_rsa", "id_ed25519.pub",
                     "credentials.json", "sub/token.json", "service-account-prod.json", "c.p12"):
            self.assertTrue(main._looks_like_secret(path), path)
        for path in (".env.example", ".env.sample", "keys.py", "README.md", "src/token.py",
                     "environment.ts", "monkey.txt"):
            self.assertFalse(main._looks_like_secret(path), path)



class ManyUntracked(Repo):
    def test_many_untracked_files_without_evidence_use_no_pathspec(self):
        for i in range(50):
            (self.repo / f"f{i}.txt").write_text(str(i))
        calls = []
        real = main._git_quiet

        def spy(*args):
            calls.append(args)
            return real(*args)

        with patch.object(main, "_git_quiet", side_effect=spy):
            main._reset_to_base_branch("many")
        stash = next(c for c in calls if c[0] == "stash")
        self.assertNotIn("--", stash)
        self.assertTrue(self.clean())
        self.assertIn("codebot: many", run(self.repo, "stash", "list").stdout)

    def test_git_quiet_never_raises_on_oserror(self):
        with patch.object(main.operations, "tool", side_effect=OSError("Argument list too long")):
            self.assertFalse(main._git_quiet("status"))


if __name__ == "__main__":
    unittest.main()
