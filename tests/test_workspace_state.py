"""Workspace state and durable namespaces never touch production state."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from execution_store import ExecutionStore
from workspace import discover
from workspace_state import TargetMismatch, bind_and_validate, repository_record
from tests.test_workspace import git, repository


class WorkspaceStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = repository(self.root / "api")
        self.workspace = discover(self.root)

    def feature(self):
        state = bind_and_validate({"state": "IDLE"}, self.workspace)
        state.update(state="IMPLEMENTING", item="Feature", slug="feature")
        return repository_record(state, self.workspace, "api", reason="API change")

    def test_blank_initializes_without_task_identity_or_git_changes(self):
        state = {"state": "IDLE"}
        result = bind_and_validate(state, self.workspace)
        self.assertEqual(state, {"state": "IDLE"})
        self.assertEqual(result["workspace"]["repositories"], {})
        self.assertNotIn("execution_task_id", result)
        self.assertEqual(git(self.repo, "branch", "--show-current"), "main")

    def test_active_legacy_state_not_silently_adopted(self):
        with self.assertRaisesRegex(TargetMismatch, "no workspace identity"):
            bind_and_validate({"state": "IMPLEMENTING", "item": "old"}, self.workspace)
        with self.assertRaises(TargetMismatch):
            bind_and_validate({"state": "IDLE", "execution_task_id": "old"}, self.workspace)

    def test_repo_mode_legacy_unchanged(self):
        state = {"state": "IMPLEMENTING", "item": "old", "branch": "feature"}
        self.assertEqual(bind_and_validate(state, discover(self.repo, mode="repo")), state)

    def test_other_target_or_mode_rejected(self):
        state = self.feature()
        other = self.root / "other"
        other.mkdir()
        with self.assertRaisesRegex(TargetMismatch, "different target"):
            bind_and_validate(state, discover(other))
        with self.assertRaises(TargetMismatch):
            bind_and_validate(state, discover(self.repo, mode="repo"))

    def test_missing_or_changed_remote_repository_rejected_without_mutation(self):
        state = self.feature()
        original = json.dumps(state)
        git(self.repo, "remote", "set-url", "origin", "https://github.com/example/other.git")
        with self.assertRaisesRegex(TargetMismatch, "identity changed"):
            bind_and_validate(state, discover(self.root))
        self.assertEqual(json.dumps(state), original)
        self.repo.rename(self.root / "gone")
        with self.assertRaisesRegex(TargetMismatch, "missing"):
            bind_and_validate(state, discover(self.root))

    def test_restart_preserves_partial_progress_and_adds_candidates(self):
        state = self.feature()
        record = state["workspace"]["repositories"]["api"]
        record.update(branch="bot-feature", base_sha="base", pr_url="https://github.com/example/test/pull/1",
                      publication={"status": "complete"}, merge={"status": "waiting"})
        repository(self.root / "ui")
        restored = bind_and_validate(json.loads(json.dumps(state)), discover(self.root))
        self.assertEqual(restored["workspace"]["repositories"]["api"], record)
        self.assertIn("ui", restored["workspace"]["inventory"])
        self.assertNotIn("ui", restored["workspace"]["repositories"])

    def test_dependency_remote_drift_also_rejected(self):
        state = bind_and_validate({"state": "IDLE"}, self.workspace)
        state["workspace"]["verification_repositories"] = ["api"]
        git(self.repo, "remote", "set-url", "origin", "https://github.com/example/other.git")
        with self.assertRaises(TargetMismatch):
            bind_and_validate(state, discover(self.root))

    def test_base_or_checkpoint_or_schema_drift_rejected(self):
        state = self.feature()
        git(self.repo, "branch", "develop")
        git(self.repo, "update-ref", "refs/remotes/origin/develop", "HEAD")
        git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop")
        with self.assertRaisesRegex(TargetMismatch, "base branch"):
            bind_and_validate(state, discover(self.root))
        state["workspace"]["schema_version"] = 99
        with self.assertRaisesRegex(TargetMismatch, "schema"):
            bind_and_validate(state, self.workspace)

    def test_stable_feature_and_independent_repository_namespaces(self):
        state = self.feature()
        repository(self.root / "ui", remote="https://github.com/example/ui.git")
        context = discover(self.root)
        store = ExecutionStore(self.root / "execution.sqlite")
        task = store.task_identity(state, context.root)
        state = repository_record(state, context, "ui", reason="UI change")
        self.assertEqual(task, store.task_identity(state, context.root))
        api, ui = context.repository("api"), context.repository("ui")
        self.assertNotEqual(store.repository_identity(state, context.root, api),
                            store.repository_identity(state, context.root, ui))
        id = store.record_repository("checkpoint", state, context, api, "publication",
                                     {"pr_url": "accepted"}, status="complete")
        row = store.get("checkpoint", id)
        self.assertEqual(row["parent_id"], task)
        self.assertEqual(row["task_id"], store.repository_identity(state, context.root, api))

    def test_runtime_load_rejects_mismatch_before_durable_migration(self):
        import main
        state = self.feature()
        state["workspace"]["root"] = "/other-target"
        path = self.root / "state.json"
        path.write_text(json.dumps(state))
        with patch.object(main.config, "STATE_PATH", path), \
             patch.object(main.config, "REPO_PATH", self.root), \
             patch.object(main.config, "TARGET_MODE", "workspace"), \
             patch.object(main.config, "WORKSPACE_MANIFEST", None), \
             patch.object(main.task_records, "migrate") as migrate:
            with self.assertRaises(TargetMismatch):
                main.load_state()
        migrate.assert_not_called()

    def test_startup_rejects_mismatch_before_any_external_effect(self):
        import main
        path = self.root / "state.json"
        state = self.feature()
        state["workspace"]["root"] = "/other-target"
        original = json.dumps(state)
        path.write_text(original)
        with patch.object(main.config, "STATE_PATH", path), \
             patch.object(main.config, "REPO_PATH", self.root), \
             patch.object(main.config, "TARGET_MODE", "workspace"), \
             patch.object(main.config, "WORKSPACE_MANIFEST", None), \
             patch.object(main, "validate_managed_runtime") as runtime, \
             patch.object(main, "task_source", Mock()) as backlog, \
             patch.object(main, "gmail_client", Mock()) as channel:
            with self.assertRaisesRegex(SystemExit, "different target"):
                main.main()
        runtime.assert_not_called()
        backlog.validate.assert_not_called()
        channel.start.assert_not_called()
        self.assertEqual(path.read_text(), original)


if __name__ == "__main__":
    unittest.main()
