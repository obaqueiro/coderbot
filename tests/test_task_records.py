import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from execution_store import ExecutionStore
import repo_provenance
import task_records


class TaskRecordsTests(unittest.TestCase):
    def test_migration_preserves_question_blocker_and_identity_across_workspace(self):
        with tempfile.TemporaryDirectory() as root:
            store = ExecutionStore(Path(root) / "db")
            state = {"item": "feature", "branch": "feature", "session_id": "session",
                     "pr_url": "https://example/pr/1", "pending_question": "Which model?",
                     "return_state": "ARCHIVING", "stuck_return": "WAIT_REPLY",
                     "stuck_error": "failure", "state": "WAIT_STUCK"}
            original = dict(state)
            task_records.migrate(state, store, root)
            restored = json.loads(json.dumps(state))
            reopened = ExecutionStore(Path(root) / "db")
            task_records.migrate(restored, reopened, root + "/isolated")
            for key, value in original.items():
                self.assertEqual(restored[key], value)
            self.assertEqual(len(reopened.list("feedback", restored["execution_task_id"])), 1)
            self.assertNotIn("recovery_id", restored)

    def test_contact_event_never_overwrites_phase(self):
        with tempfile.TemporaryDirectory() as root:
            store = ExecutionStore(Path(root) / "db")
            state = {"item": "feature", "thread_id": "thread", "state": "IMPLEMENTING"}
            task_records.contact(store, state, root, "status1", "thread", at=100)
            task_records.contact(store, state, root, "foreign", "other", at=200)
            state["state"] = "VERIFYING"
            task_records.apply_contacts(store, state, root)
            self.assertEqual(state["state"], "VERIFYING")
            self.assertEqual(state["last_contact"], 100)

    def test_durable_planning_transition_recovers_without_fsm_write(self):
        with tempfile.TemporaryDirectory() as root:
            store = ExecutionStore(Path(root) / "db")
            state = {"item": "feature", "state": "WAIT_REVIEW", "slug": "feature", "pr_url": "https://example/pr/1"}
            store.record("feedback", state, root, "message", {"text": "Need selector", "assessment": {}}, status="planning")
            task_records.migrate(state, store, root)
            self.assertEqual(state["state"], "REPLANNING")
            self.assertEqual(state["replan"]["feedback"], "Need selector")
            self.assertEqual(state["pr_url"], "https://example/pr/1")

    def test_provenance_preserves_staged_unstaged_and_untracked_files(self):
        with tempfile.TemporaryDirectory() as root:
            def git(*args):
                return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            git("init")
            path = Path(root) / "file with spaces.txt"
            path.write_text("base")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "base")
            path.write_text("staged")
            git("add", ".")
            path.write_text("unstaged")
            untracked = Path(root) / "new file.txt"
            untracked.write_text("new")
            before = git("diff", "--cached", "--binary").stdout
            value = repo_provenance.inspect(root)
            self.assertEqual(value["files"][path.name]["status"], "MM")
            self.assertEqual(value["files"][untracked.name]["status"], "??")
            self.assertEqual(git("diff", "--cached", "--binary").stdout, before)
            self.assertEqual(path.read_text(), "unstaged")
