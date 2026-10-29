"""Existing PR handoffs publish task commits through the shared seam and verify the actual GitHub head."""
import unittest
from unittest.mock import patch, Mock
import sys

import errors
from tests.fake_github import FakeGitHub

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main


class PRPublication(unittest.TestCase):
    def setUp(self):
        for name in ("save_state", "_pr_title"):
            patcher = patch.object(main, name, return_value="title")
            patcher.start()
            self.addCleanup(patcher.stop)

    def state(self, github, **extra):
        return {"pr_url": github.add(111, extra.get("remote_branch", "task"), sha="a" * 40), "branch": "task", **extra}

    def test_existing_pr_publishes_head_without_force_then_confirms_remote(self):
        with FakeGitHub(head="b" * 40) as github:
            state = self.state(github)
            main._deliver(state, "body")
        self.assertEqual(github.pushes, ["task"])
        self.assertEqual(github.calls[0][:3], ("pr", "view", "111"))  # validated before the push
        self.assertEqual(state["delivered_sha"], "b" * 40)
        self.assertEqual(state["repository"]["delivered_sha"], "b" * 40)

    def test_wrong_target_or_fork_pr_never_pushes(self):
        for changes in ({"head": "other"}, {"base": "other-base"}, {"cross": True}):
            with self.subTest(changes=changes), FakeGitHub() as github:
                state = self.state(github)
                github.prs[111].update(changes)
                with self.assertRaisesRegex(errors.Fatal, "no longer matches"):
                    main._deliver(state, "body")
                self.assertEqual(github.pushes, [])

    def test_closed_pr_never_pushes(self):
        with FakeGitHub() as github:
            state = self.state(github)
            github.prs[111]["state"] = "CLOSED"
            with self.assertRaisesRegex(errors.Fatal, "CLOSED"):
                main._deliver(state, "body")
        self.assertEqual(github.pushes, [])

    def test_remote_branch_mapping_is_respected(self):
        with FakeGitHub() as github:
            state = self.state(github, remote_branch="original-task")
            main._deliver(state, "body")
        self.assertEqual(github.pushes, ["original-task"])

    def test_push_rejection_and_head_mismatch_never_claim_delivery(self):
        with FakeGitHub() as github:
            state = self.state(github)
            github.fail_push = RuntimeError("rejected")
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                main._deliver(state, "body")
            self.assertNotIn("delivered_sha", state)
            github.fail_push = None
            with patch.object(github, "push"):  # GitHub never sees the new head
                github.local_head = "b" * 40
                with self.assertRaisesRegex(errors.Transient, "does not match"):
                    main._deliver(state, "body")
        self.assertNotIn("delivered_sha", state)

    def test_uncommitted_tracked_changes_are_never_published(self):
        with FakeGitHub() as github:
            state = self.state(github)
            github.dirty = " M src/app.py"
            with self.assertRaisesRegex(errors.AgentFault, "committed tracked changes"):
                main._deliver(state, "body")
        self.assertEqual(github.pushes, [])
