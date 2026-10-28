"""Discovery uses only synthetic repositories; never reads the live workspace."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from workspace import WorkspaceError, contained, discover, load_manifest, remote_identity


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def repository(path, branch="main", remote="https://github.com/example/test.git"):
    path.mkdir(parents=True)
    git(path, "init", "-b", branch)
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "source.txt").write_text("initial")
    git(path, "add", "source.txt")
    git(path, "commit", "-m", "initial")
    git(path, "remote", "add", "origin", remote)
    git(path, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")
    git(path, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}")
    return path


class WorkspaceDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def manifest(self, value):
        path = self.root / "workspace.json"
        path.write_text(json.dumps(value))
        return path.name

    def test_mixed_children_bases_caches_and_immutable_context(self):
        repository(self.root / "api", "develop")
        repository(self.root / "ui")
        repository(self.root / "node_modules" / "ignored")
        (self.root / "e2e").mkdir()
        context = discover(self.root)
        self.assertEqual([(r.relative_path, r.base_branch) for r in context.repositories],
                         [("api", "develop"), ("ui", "main")])
        self.assertEqual(context.planning_root, self.root)
        with self.assertRaises(AttributeError):
            context.mode = "repo"
        self.assertFalse((self.root / ".git").exists())

    def test_first_level_worktrees_only(self):
        parent = repository(self.root / "parent")
        repository(parent / "child")
        git(parent, "worktree", "add", "-b", "feature", str(self.root / "linked"))
        context = discover(self.root)
        self.assertEqual(len(context.repositories), 2)
        self.assertTrue((self.root / "linked" / ".git").is_file())
        self.assertEqual(context.repository("linked").base_branch, "main")
        self.assertEqual(discover(self.root / "linked").mode, "repo")
        self.assertNotIn("parent/child", [repo.relative_path for repo in context.repositories])
        self.assertTrue(context.repository("parent").owns("source.txt"))

    def test_ancestor_git_not_discovered(self):
        parent = repository(self.root / "ancestor")
        workspace = parent / "workspace"
        workspace.mkdir()
        (workspace / "ordinary").mkdir()
        self.assertEqual(discover(workspace).repositories, ())

    def test_missing_default_never_uses_current_branch(self):
        repo = repository(self.root / "api")
        git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        git(repo, "checkout", "-b", "feature")
        with self.assertRaisesRegex(WorkspaceError, "Cannot determine base"):
            discover(self.root)
        context = discover(self.root, manifest_path=self.manifest({"base_branches": {"api": "main"}}))
        self.assertEqual(context.repository("api").base_branch, "main")
        self.assertEqual(git(repo, "branch", "--show-current"), "feature")

    def test_missing_remote_and_missing_base_actionable(self):
        repo = repository(self.root / "api")
        with self.assertRaisesRegex(WorkspaceError, "not available"):
            discover(self.root, manifest_path=self.manifest({"base_branches": {"api": "missing"}}))
        git(repo, "remote", "remove", "origin")
        with self.assertRaisesRegex(WorkspaceError, "no origin"):
            discover(self.root)

    def test_exclusions_and_legacy_depth_never_enable_recursion(self):
        repository(self.root / "group" / "deep")
        repository(self.root / "ignored")
        context = discover(self.root, manifest_path=self.manifest({"max_depth": 1, "exclude": ["ignored"]}))
        self.assertEqual(context.repositories, ())
        context = discover(self.root, manifest_path=self.manifest({"max_depth": 2, "exclude": ["ignored"]}))
        self.assertEqual(context.repositories, ())
        self.assertEqual(context.manifest.max_depth, 1)

    def test_generated_broken_worktrees_are_never_inspected(self):
        repo = repository(self.root / "api")
        generated = repo / "output/worktrees/2"
        generated.mkdir(parents=True)
        (generated / ".git").write_text("gitdir: /app/output/repo.git/worktrees/2\n")
        grouped = self.root / "other/deep"
        grouped.mkdir(parents=True)
        (grouped / ".git").write_text("gitdir: /missing/repo\n")
        self.assertEqual([r.relative_path for r in discover(self.root).repositories], ["api"])

    def test_provenance_does_not_discover_nested_repositories(self):
        import config
        import repo_provenance
        from unittest.mock import patch
        repository(self.root / "api")
        nested = self.root / "group/nested"
        nested.mkdir(parents=True)
        (nested / ".git").write_text("gitdir: /missing/repo\n")
        with patch.object(config, "TARGET_MODE", "workspace"), \
             patch.object(config, "WORKSPACE_MANIFEST", None):
            self.assertEqual(set(repo_provenance.inspect(self.root)["repositories"]), {"api"})

    def test_external_symlinks_and_manifest_escape(self):
        repository(self.root / "actual")
        (self.root / "alias").symlink_to(self.root / "actual", target_is_directory=True)
        with tempfile.TemporaryDirectory() as external:
            (self.root / "external").symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(WorkspaceError, "outside workspace"):
                contained(self.root, "external/source.txt")
            with self.assertRaises(WorkspaceError):
                discover(self.root, manifest_path=Path(external) / "manifest.json")
        self.assertEqual(len(discover(self.root).repositories), 1)

    def test_repo_adapter_and_invalid_mode(self):
        repo = repository(self.root / "api")
        context = discover(repo, mode="repo")
        self.assertEqual(context.repository(".").path, repo)
        self.assertEqual(discover(repo).mode, "repo")
        with self.assertRaises(WorkspaceError):
            discover(repo, mode="workspace")
        with self.assertRaises(WorkspaceError):
            discover(self.root, mode="guess")

    def test_manifest_validation(self):
        invalid = [{"max_depth": True}, {"exclude": "*"}, {"shared_inputs": ["../secret"]},
                   {"unknown": 1}, {"base_branches": {"api": "bad..branch"}},
                   {"checks": [{"id": "test", "argv": "sh run.sh"}]},
                   {"checks": [{"id": "test", "argv": ["true"], "cwd": "../"}]},
                   {"docker": {"host_root": "relative"}}, {"docker": {"compose_files": ["/outside"]}}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(WorkspaceError):
                load_manifest(self.root, self.manifest(value))
        config = load_manifest(self.root, self.manifest({"checks": [{"id": "unit", "argv": ["true"]}],
                                                       "docker": {"context": "desktop-linux", "host_root": "/host"}}))
        self.assertEqual(json.loads(config.checks_json)[0]["id"], "unit")

    def test_remote_identity_redacts_credentials_and_normalizes_transport(self):
        self.assertEqual(remote_identity("https://user:SECRET@github.com/example/test.git"),
                         remote_identity("git@github.com:example/test.git"))
        self.assertEqual(remote_identity("ssh://git@github.com/example/test.git"), "github.com/example/test")

    def test_config_detects_root_marker_without_mode_setting_and_ignores_old_flag(self):
        repo = repository(self.root / "api")
        git(repo, "worktree", "add", "-b", "feature", str(self.root / "linked"))
        for path, expected in ((self.root, "workspace"), (repo, "repo"), (self.root / "linked", "repo")):
            for old_flag in (None, "repo", "workspace", "obsolete"):
                with self.subTest(path=path, old_flag=old_flag):
                    env = {**os.environ, "CODEBOT_REPO_PATH": str(path),
                           "CODEBOT_DATA_DIR": str(self.root / "test-state")}
                    env.pop("CODEBOT_TARGET_MODE", None)
                    if old_flag is not None:
                        env["CODEBOT_TARGET_MODE"] = old_flag
                    result = subprocess.run([sys.executable, "-c",
                        "import sys; sys.path.insert(0, 'src'); import config; print(config.TARGET_MODE)"],
                        cwd=Path(__file__).resolve().parent.parent, env=env,
                        capture_output=True, text=True, check=True)
                    self.assertEqual(result.stdout.strip(), expected)


if __name__ == "__main__":
    unittest.main()
