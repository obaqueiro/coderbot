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

    def test_a_pr_closed_on_github_is_dropped_and_retry_opens_a_fresh_one(self):
        for phase, extra in (("PUSHING", {}), ("OPEN_PR", {"draft": True})):
            with self.subTest(phase=phase), FakeGitHub() as github, \
                 patch.object(main, "_enter_stuck") as stuck:
                url = github.add(111, "task", state="CLOSED", **extra)
                state = {"state": phase, "branch": "task", "delivered_sha": "c" * 40,
                         **({"draft_pr_url": url} if extra else {"pr_url": url, "draft_pr_url": url,
                                                                 "draft_pr_ready": True})}
                self.assertFalse(main._deliver(state, "body", publication=phase == "OPEN_PR"))
                self.assertEqual(github.pushes, [])
                self.assertEqual(stuck.call_args.args[1], phase)  # 'retry' resumes the same step
                self.assertIn("closed without merging", stuck.call_args.args[2])
                for key in ("pr_url", "draft_pr_url", "draft_pr_ready", "delivered_sha"):
                    self.assertNotIn(key, state)
                self.assertEqual(state["repository"]["closed_prs"], [111])
                self.assertTrue(main._deliver(state, "body", publication=phase == "OPEN_PR"))  # the retry
                self.assertEqual(state["pr_url"], "https://github.com/acme/project/pull/112")
                self.assertEqual(github.pushes, ["task"])

    def test_a_pr_merged_on_github_finishes_the_task(self):
        with FakeGitHub() as github, patch.object(main, "_finish_task") as finish, patch.object(main, "trail"):
            state = self.state(github)
            github.prs[111]["state"] = "MERGED"
            self.assertFalse(main._deliver(state, "body"))
        self.assertEqual(github.pushes, [])
        self.assertTrue(finish.call_args.kwargs["merged"])

    def test_pushing_never_changes_draft_status(self):
        with FakeGitHub() as github:
            state = self.state(github, draft_pr_url="https://github.com/acme/project/pull/111", draft_pr_ready=True)
            github.prs[111]["draft"] = True  # a reviewer converted it back to draft
            main._deliver(state, "body")
            self.assertTrue(github.prs[111]["draft"])
            self.assertFalse([c for c in github.calls if c[:2] in (("pr", "ready"), ("pr", "edit"))])
            main._deliver(state, "body", publication=True)
            self.assertFalse(github.prs[111]["draft"])

    def test_a_renamed_repository_is_followed_not_refused(self):
        moved = "https://github.com/acme-renamed/project/pull/111"
        with FakeGitHub() as github:
            state = self.state(github)
            github.canonical[111] = moved
            self.assertTrue(main._deliver(state, "body"))
        self.assertEqual((state["pr_url"], state["repository"]["pr_url"]), (moved, moved))
        self.assertEqual(github.pushes, ["task"])

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

    def test_uncommitted_tracked_changes(self):
        with FakeGitHub() as github:
            state = self.state(github)
            github.dirty = " M src/app.py"
            with self.assertRaisesRegex(errors.AgentFault, "committed tracked changes"):
                main._deliver(state, "body", publication=True)  # publishing an existing PR, as before
            self.assertEqual(github.pushes, [])
            self.assertTrue(main._deliver(state, "body"))  # PUSHING pushes what is committed
            self.assertEqual(github.pushes, ["task"])


class PullRequestURLs(unittest.TestCase):
    def test_one_parser_for_every_host_and_shape(self):
        for url, parsed in (
                ("https://github.com/acme/project/pull/5", ("github.com", "acme/project", 5)),
                ("https://GitHub.com/Acme/Project/pull/5/files", ("github.com", "Acme/Project", 5)),
                ("https://ghe.corp.example/team/app.git/pull/12#discussion_r1", ("ghe.corp.example", "team/app", 12)),
                ("https://github.com/acme/project/issues/5", None), ("not a url", None), (None, None)):
            self.assertEqual(main.lifecycle.parse_pr_url(url), parsed, url)
        ops = main.lifecycle.RepoOps.checkout("/tmp", "main", "https://github.com/acme/project/pull/5/files")
        self.assertEqual((ops.remote, ops.slug), ("github.com/acme/project", "acme/project"))

    def test_an_unparseable_pr_url_never_raises_out_of_a_wait(self):
        with patch.object(main.operations, "tool") as tool:
            self.assertIsNone(main.code_review_check("https://example.com/nope"))
            self.assertIsNone(main.lifecycle.review_threads(main._pr_ops("https://example.com/nope"), None))
            self.assertFalse(main.check_pr_status({"state": "WAIT_MERGE", "pr_url": "garbage", "branch": "b"}))
            self.assertEqual(main.review_summary({"pr_url": "garbage"}), "")
            self.assertEqual(main._resolve_review_threads({"pr_url": "garbage", "review_threads": [{"id": "T"}]},
                                                          "RESOLVE: T fixed"), 0)
        tool.assert_not_called()
