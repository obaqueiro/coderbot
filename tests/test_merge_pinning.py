"""The merge is pinned to the PR head the user reviewed; every failure mode falls back safely."""
import sys
import unittest
from unittest.mock import Mock, patch

from tests.fake_github import FakeGitHub

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main

PR = "https://github.com/acme/project/pull/42"
SHA = "a" * 40
OTHER = "b" * 40


class Pinning(unittest.TestCase):
    def state(self, **extra):
        return {"state": "WAIT_MERGE", "item": "task", "slug": "s", "branch": "b",
                "session_id": "sid", "pr_url": PR, **extra}

    def merge(self, state, *, head=SHA, exists=True, fail=None, moves_to=None):
        """(email, finish, merges) for a 'merge' reply; GitHub's PR head is `head`. With
        `moves_to`, someone pushes that head between the pre-merge read and the merge."""
        github = FakeGitHub(base=main.config.BASE_BRANCH)
        if exists:
            github.add(42, "b", sha=head)
        github.fail_merge = fail
        original = github.gh

        def gh(ops, *args):
            if moves_to and args[:2] == ("pr", "merge"):
                github.prs[42]["sha"] = moves_to
                raise main.errors.AgentFault("Head branch was modified")
            return original(ops, *args)
        github.gh = gh
        with github, patch.object(main, "trail"), \
             patch.object(main, "unresolved_review_threads", return_value=[]), \
             patch.object(main, "_conversation_delivery_blocked", return_value=False), \
             patch.object(main, "content_snapshot", return_value="x"), \
             patch.object(main, "email") as email, \
             patch.object(main, "_finish_task") as finish:
            main.do_merge_reply(state, "merge")
        return email, finish, [c for c in github.calls if c[:2] == ("pr", "merge")]

    def test_legacy_state_merges_without_pin(self):
        email, finish, merges = self.merge(self.state())
        self.assertNotIn("--match-head-commit", merges[0])
        finish.assert_called_once()

    def test_unchanged_head_merges_pinned(self):
        email, finish, merges = self.merge(self.state(delivered_sha=SHA))
        self.assertEqual(list(merges[0][-2:]), ["--match-head-commit", SHA])
        finish.assert_called_once()
        email.assert_not_called()

    def test_moved_head_blocks_and_adopts_the_new_head(self):
        state = self.state(delivered_sha=SHA, reviewed_pr_snapshot="x")
        email, finish, merges = self.merge(state, head=OTHER)
        self.assertEqual(merges, [])
        finish.assert_not_called()
        self.assertIn("changed since review", email.call_args.args[1])
        self.assertEqual(state["delivered_sha"], OTHER)
        self.assertNotIn("reviewed_pr_snapshot", state)
        self.assertEqual(state["state"], "WAIT_MERGE")

    def test_an_unreadable_pr_blocks_the_merge(self):
        email, finish, merges = self.merge(self.state(delivered_sha=SHA), exists=False)
        self.assertEqual(merges, [])
        finish.assert_not_called()
        self.assertIn("could not check", email.call_args.args[1])

    def test_old_gh_fails_closed_without_unpinned_retry(self):
        email, finish, merges = self.merge(self.state(delivered_sha=SHA), fail="unknown flag: --match-head-commit")
        self.assertEqual(len(merges), 1)
        self.assertIn("--match-head-commit", merges[0])
        finish.assert_not_called()

    def test_rejected_because_head_moved_between_check_and_merge(self):
        state = self.state(delivered_sha=SHA)
        email, finish, merges = self.merge(state, moves_to=OTHER)
        finish.assert_not_called()
        self.assertIn("changed since review", email.call_args.args[1])
        self.assertEqual(state["delivered_sha"], OTHER)

    def test_other_rejection_keeps_the_generic_failure_email(self):
        email, finish, merges = self.merge(self.state(delivered_sha=SHA), fail="Pull request is not mergeable")
        finish.assert_not_called()
        self.assertIn("merge failed", email.call_args.args[1])

    def test_ci_and_draft_never_gate_an_explicit_merge_in_repo_mode(self):
        github = FakeGitHub(base=main.config.BASE_BRANCH)
        github.add(42, "b", draft=True, mergeable="UNKNOWN")
        with github, patch.object(main.lifecycle, "green", return_value=False):
            target, records = main._repo_target(self.state())
            self.assertEqual(main.lifecycle.precheck(target, records, ["."], gates=False), [])
            self.assertEqual([p[1:3] for p in main.lifecycle.precheck(target, records, ["."])],
                             [("OPEN", "not mergeable (UNKNOWN)")])  # the workspace gate


if __name__ == "__main__":
    unittest.main()
