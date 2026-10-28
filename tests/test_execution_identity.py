from pathlib import Path
import subprocess
import tempfile
import unittest

from execution_identity import environment_identity, snapshot


class IdentityTests(unittest.TestCase):
    def test_workspace_respects_child_ignores_but_keeps_source_and_dependencies(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            repo = workspace / "app"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / ".gitignore").write_text("next-env.d.ts\n*.tsbuildinfo\ncoverage/\n")
            (repo / "source.ts").write_text("source")
            (repo / "package-lock.json").write_text("dependencies")
            before = snapshot(workspace)
            (repo / "next-env.d.ts").write_text("generated")
            (repo / "tsconfig.tsbuildinfo").write_text("cache")
            (repo / "coverage").mkdir()
            (repo / "coverage/report.json").write_text("report")
            (repo / "private.key").write_text("must not enter identity")
            (repo / "token.json").write_text("credential")
            self.assertEqual(snapshot(workspace), before)
            scoped = snapshot(workspace, ["app/source.ts"])
            (repo / "package-lock.json").write_text("changed dependencies")
            self.assertNotEqual(snapshot(workspace), before)
            self.assertEqual(snapshot(workspace, ["app/source.ts"]), scoped)
            before = snapshot(workspace)
            (repo / "source.ts").write_text("changed source")
            self.assertNotEqual(snapshot(workspace), before)
            before = snapshot(workspace)
            (workspace / "shared.sh").write_text("shared source")
            self.assertNotEqual(snapshot(workspace), before)

    def test_workspace_keeps_tracked_files_even_when_ignored_and_detects_deletion(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            repo = workspace / "app"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "source.ts").write_text("tracked")
            subprocess.run(["git", "add", "source.ts"], cwd=repo, check=True)
            (repo / ".gitignore").write_text("source.ts\n")
            before = snapshot(workspace)
            (repo / "source.ts").write_text("changed")
            self.assertNotEqual(snapshot(workspace), before)
            before = snapshot(workspace)
            (repo / "source.ts").unlink()
            self.assertNotEqual(snapshot(workspace), before)

    def test_environment_command_and_tool_versions_invalidate_without_secret_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            kwargs = {"cwd": root, "env_keys": ["TOKEN"], "environ": {"TOKEN": "secret"},
                      "tools": {"node": "22"}, "key_path": Path(root) / "identity.key"}
            first = environment_identity(["npm", "test"], **kwargs)
            self.assertEqual(first, environment_identity(["npm", "test"], **kwargs))
            self.assertNotIn("secret", first)
            kwargs["environ"] = {"TOKEN": "changed"}
            self.assertNotEqual(first, environment_identity(["npm", "test"], **kwargs))
            self.assertEqual((Path(root) / "identity.key").stat().st_mode & 0o777, 0o600)

    def test_content_changes_without_status_change_and_document_scope(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (repo / "app.py").write_text("first")
            (repo / "README.md").write_text("doc")
            before = snapshot(repo, ["app.py"])
            (repo / "app.py").write_text("second")
            after = snapshot(repo, ["app.py"])
            self.assertNotEqual(before, after)
            (repo / "README.md").write_text("new doc")
            self.assertEqual(snapshot(repo, ["app.py"]), after)
            (repo / "extra.py").write_text("new input")
            self.assertNotEqual(snapshot(repo), snapshot(repo, ["app.py"]))

    def test_sensitive_files_and_cache_are_not_snapshot_inputs(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            before = snapshot(repo)
            (repo / ".env").write_text("TOKEN=secret")
            (repo / "node_modules").mkdir()
            (repo / "node_modules/file.js").write_text("cache")
            self.assertEqual(before, snapshot(repo))
