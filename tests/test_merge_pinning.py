"""The merge is pinned to the PR head the user reviewed; every failure mode falls back safely."""
import json
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main

PR = "https://github.com/acme/project/pull/42"
SHA = "a" * 40
OTHER = "b" * 40


def done(rc=0, stderr=""):
    return subprocess.CompletedProcess([], rc, "", stderr)


class Pinning(unittest.TestCase):
    def state(self, **extra):
        return {"state": "WAIT_MERGE", "item": "task", "slug": "s", "branch": "b",
                "session_id": "sid", "pr_url": PR, **extra}

    def merge(self, state, *, head=SHA, runs=(done(),)):
        publication = (patch.object(main, "_pr_publication", side_effect=head)
                       if isinstance(head, BaseException) or (isinstance(head, list))
                       else patch.object(main, "_pr_publication", return_value={"headRefOid": head}))
        with patch.object(main, "trail"), \
             patch.object(main, "_pr_merge_state", return_value=("OPEN", "MERGEABLE")), \
             patch.object(main, "unresolved_review_threads", return_value=[]), \
             patch.object(main, "_conversation_delivery_blocked", return_value=False), \
             patch.object(main, "content_snapshot", return_value="x"), \
             publication, \
             patch.object(main, "email") as email, \
             patch.object(main, "_finish_task") as finish, \
             patch.object(main.subprocess, "run", side_effect=list(runs)) as run:
            main.do_merge_reply(state, "merge")
        return email, finish, run

    def test_legacy_state_merges_without_pin(self):
        email, finish, run = self.merge(self.state())
        self.assertNotIn("--match-head-commit", run.call_args.args[0])
        finish.assert_called_once()

    def test_unchanged_head_merges_pinned(self):
        email, finish, run = self.merge(self.state(delivered_sha=SHA))
        self.assertEqual(run.call_args.args[0][-2:], ["--match-head-commit", SHA])
        finish.assert_called_once()
        email.assert_not_called()

    def test_moved_head_blocks_and_adopts_the_new_head(self):
        state = self.state(delivered_sha=SHA, reviewed_pr_snapshot="x")
        email, finish, run = self.merge(state, head=OTHER)
        run.assert_not_called()
        finish.assert_not_called()
        self.assertIn("changed since review", email.call_args.args[1])
        self.assertEqual(state["delivered_sha"], OTHER)
        self.assertNotIn("reviewed_pr_snapshot", state)
        self.assertEqual(state["state"], "WAIT_MERGE")

    def test_unreadable_head_still_merges_with_pin(self):
        email, finish, run = self.merge(self.state(delivered_sha=SHA), head=RuntimeError("gh down"))
        self.assertEqual(run.call_args.args[0][-2:], ["--match-head-commit", SHA])
        finish.assert_called_once()

    def test_old_gh_fails_closed_without_unpinned_retry(self):
        email, finish, run = self.merge(self.state(delivered_sha=SHA),
                                        runs=(done(1, "unknown flag: --match-head-commit"), done()))
        self.assertEqual(run.call_count, 1)
        self.assertIn("--match-head-commit", run.call_args.args[0])
        finish.assert_not_called()

    def test_rejected_because_head_moved_between_check_and_merge(self):
        state = self.state(delivered_sha=SHA)
        email, finish, run = self.merge(state, head=[{"headRefOid": SHA}, {"headRefOid": OTHER}],
                                        runs=(done(1, "Head branch was modified"),))
        finish.assert_not_called()
        self.assertIn("changed since review", email.call_args.args[1])
        self.assertEqual(state["delivered_sha"], OTHER)

    def test_other_rejection_keeps_the_generic_failure_email(self):
        email, finish, run = self.merge(self.state(delivered_sha=SHA),
                                        runs=(done(1, "Pull request is not mergeable"),))
        finish.assert_not_called()
        self.assertIn("merge failed", email.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
