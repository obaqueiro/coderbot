"""Review-thread model, merge gate and conflict routing — I/O edges stubbed."""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import time
from unittest.mock import Mock, patch

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main

main.config.STATE_PATH = pathlib.Path(tempfile.mkdtemp()) / "state.json"

PR = "https://github.com/acme/project/pull/42"


def verdict(**fields):
    return Mock(output=json.dumps(fields))


def ocr(tid="T1"):
    return {"id": tid, "comment_id": 7, "path": "a.py", "line": 3, "outdated": False,
            "author": "github-actions", "body": "nit"}


def human(tid="H1"):
    return {"id": tid, "comment_id": 8, "path": "b.py", "line": 9, "outdated": False,
            "author": "alice", "body": "please rename"}


def human_thread(tid="H1"):
    """A reviewer's thread the bot already answered once and the reviewer pushed back on."""
    t = human(tid)
    t["bot_login"] = "codebot"
    t["comments"] = [{"author": "alice", "body": "why the lock?", "comment_id": 8},
                     {"author": "codebot", "body": "no change: it is needed", "comment_id": 9},
                     {"author": "alice", "body": "drop it, simplify", "comment_id": 10}]
    return t


class ThreadHelpers(unittest.TestCase):
    def test_is_ocr_thread_accepts_both_spellings(self):
        self.assertTrue(main._is_ocr_thread({"author": "github-actions"}))
        self.assertTrue(main._is_ocr_thread({"author": "github-actions[bot]"}))
        self.assertFalse(main._is_ocr_thread({"author": "alice"}))
        self.assertFalse(main._is_ocr_thread({}))

    def test_format_with_ids_for_the_worker(self):
        text = main.format_unresolved_threads([ocr(), human()], ids=True)
        self.assertIn("thread_id: T1", text)
        self.assertIn("a.py:3 (github-actions)", text)
        self.assertIn("(alice)", text)

    def test_format_with_ids_renders_full_thread(self):
        text = main.format_unresolved_threads([human_thread()], ids=True, bot_comment_ids=[9])
        self.assertIn("alice: why the lock?", text)
        self.assertIn("codebot (you): no change: it is needed", text)
        self.assertIn("alice (latest — respond to this): drop it, simplify", text)

    def test_format_tells_operator_apart_from_bot_on_shared_account(self):
        t = human_thread()
        t["comments"].append({"author": "codebot", "body": "Codebot: just do it", "comment_id": 11})
        t["comments"].append({"author": "codebot", "body": "answered: because", "comment_id": 12})
        text = main.format_unresolved_threads([t], ids=True, bot_comment_ids=[9, 12])
        self.assertIn("codebot (you): no change: it is needed", text)
        self.assertIn("codebot (your account) (latest — respond to this): Codebot: just do it", text)
        self.assertIn("codebot (you): answered: because", text)
        self.assertNotIn("alice (latest", text)

    def test_legacy_bot_replies_recognised_by_shape(self):
        t = human_thread()  # bot reply id 9 is NOT in bot_comment_ids
        text = main.format_unresolved_threads([t], ids=True)
        self.assertIn("codebot (you): no change: it is needed", text)
        self.assertIn("alice (latest — respond to this): drop it, simplify", text)

    def test_reply_ids_are_remembered(self):
        state = {"pr_url": PR, "pr_threads": [human_thread()]}
        ok = subprocess.CompletedProcess([], 0, '{"id": 77}', "")
        with patch.object(main.subprocess, "run", return_value=ok):
            main._resolve_review_threads(state, "RESOLVE: H1 answered: because X", key="pr_threads")
        self.assertEqual(state["bot_comment_ids"], [77])
        # Email view stays opener-only.
        short = main.format_unresolved_threads([human_thread()])
        self.assertIn("please rename", short)
        self.assertNotIn("drop it", short)

    def test_thread_has_new_comments(self):
        t = human_thread()
        self.assertTrue(main.thread_has_new_comments(t, {}))
        self.assertFalse(main.thread_has_new_comments(t, {"H1": 3}))
        self.assertTrue(main.thread_has_new_comments(t, {"H1": 2}))

    def test_query_failure_returns_none(self):
        proc = subprocess.CompletedProcess([], 1, "", "boom")
        with patch.object(main.subprocess, "run", return_value=proc):
            self.assertIsNone(main.unresolved_review_threads(PR))

    def test_query_parses_and_paginates(self):
        page1 = {"data": {"viewer": {"login": "codebot"}, "repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
            "nodes": [{"id": "T1", "isResolved": False, "isOutdated": False, "path": "a.py",
                       "line": 3, "comments": {"nodes": [{"author": {"login": "github-actions"},
                                                          "body": "nit", "databaseId": 7},
                                                         {"author": {"login": "codebot"},
                                                          "body": "fixed", "databaseId": 71},
                                                         {"author": {"login": "alice"},
                                                          "body": "not quite", "databaseId": 72}]}},
                      {"id": "T2", "isResolved": True, "comments": {"nodes": []}}]}}}}}
        page2 = {"data": {"viewer": {"login": "codebot"}, "repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [{"id": "H1", "isResolved": False, "isOutdated": True, "path": "b.py",
                       "line": None, "comments": {"nodes": [{"author": {"login": "alice"},
                                                             "body": "x", "databaseId": 8}]}}]}}}}}
        procs = [subprocess.CompletedProcess([], 0, json.dumps(page1), ""),
                 subprocess.CompletedProcess([], 0, json.dumps(page2), "")]
        with patch.object(main.subprocess, "run", side_effect=procs) as run:
            threads = main.unresolved_review_threads(PR)
        self.assertEqual([t["id"] for t in threads], ["T1", "H1"])
        self.assertTrue(threads[1]["outdated"])
        self.assertEqual(threads[0]["comment_id"], 7)  # replies go on the opener
        self.assertEqual(threads[0]["author"], "github-actions")
        self.assertEqual([c["author"] for c in threads[0]["comments"]],
                         ["github-actions", "codebot", "alice"])
        self.assertEqual(threads[0]["bot_login"], "codebot")
        self.assertIn("cursor=c1", run.call_args_list[1].args[0])

    def test_resolve_honors_only_known_threads(self):
        state = {"pr_url": PR, "review_threads": [ocr("T1")]}
        output = "done\nRESOLVE: T1 fixed: renamed\nRESOLVE: EVIL not yours\n"
        ok = subprocess.CompletedProcess([], 0, "{}", "")
        with patch.object(main.subprocess, "run", return_value=ok) as run:
            resolved = main._resolve_review_threads(state, output)
        self.assertEqual(resolved, 1)
        commands = [c.args[0] for c in run.call_args_list]
        self.assertEqual(len(commands), 2)  # reply + resolve for T1 only
        self.assertIn("comments/7/replies", commands[0][2])
        self.assertIn("body=fixed: renamed", commands[0])
        self.assertTrue(any("resolveReviewThread" in arg for arg in commands[1]))

    def test_resolve_closes_human_thread_when_fixed(self):
        state = {"pr_url": PR, "pr_threads": [human_thread()]}
        ok = subprocess.CompletedProcess([], 0, "{}", "")
        with patch.object(main.subprocess, "run", return_value=ok) as run:
            resolved = main._resolve_review_threads(state, "RESOLVE: H1 fixed: lock removed",
                                                    key="pr_threads")
        self.assertEqual(resolved, 1)
        self.assertEqual(len(run.call_args_list), 2)  # reply + resolve
        self.assertNotIn("H1", state.get("pr_threads_seen", {}))

    def test_resolve_leaves_human_thread_open_when_only_answered(self):
        state = {"pr_url": PR, "pr_threads": [human_thread()]}
        ok = subprocess.CompletedProcess([], 0, "{}", "")
        with patch.object(main.subprocess, "run", return_value=ok) as run:
            resolved = main._resolve_review_threads(state, "RESOLVE: H1 answered: because X",
                                                    key="pr_threads")
        self.assertEqual(resolved, 0)
        commands = [c.args[0] for c in run.call_args_list]
        self.assertEqual(len(commands), 1)  # reply only, no resolveReviewThread
        self.assertIn("body=answered: because X", commands[0])
        self.assertEqual(state["pr_threads_seen"], {"H1": 4})  # 3 comments + the reply

    def test_ocr_thread_is_resolved_even_when_declined(self):
        state = {"pr_url": PR, "review_threads": [ocr("T1")]}
        ok = subprocess.CompletedProcess([], 0, "{}", "")
        with patch.object(main.subprocess, "run", return_value=ok) as run:
            resolved = main._resolve_review_threads(state, "RESOLVE: T1 false positive")
        self.assertEqual(resolved, 1)
        self.assertEqual(len(run.call_args_list), 2)

    def test_resolve_without_known_threads_is_noop(self):
        with patch.object(main.subprocess, "run") as run:
            self.assertEqual(main._resolve_review_threads({"pr_url": PR}, "RESOLVE: T1 x"), 0)
        run.assert_not_called()


class MergeGate(unittest.TestCase):
    def test_repair_handoff_refreshes_snapshot_and_next_merge_succeeds(self):
        state = self.state()
        state.update(state="PUSHING", reviewed_pr_snapshot="old", pr_threads=[],
                     push_context={"continuation": "threads", "output": "Fixed"})
        with patch.object(main, "_resolve_review_threads", return_value=0), \
             patch.object(main, "content_snapshot", return_value="repaired"):
            main._finish_address_pr_threads(state)
            self.assertEqual(state["reviewed_pr_snapshot"], "repaired")
            self.assertEqual(state["state"], "WAIT_MERGE")
            email, finish, merge = self.run_merge(state, threads=[], reply="merge")
        merge.assert_called_once()
        finish.assert_called_once()

    def test_changes_after_repair_handoff_still_block_merge(self):
        state = self.state()
        state["reviewed_pr_snapshot"] = "repaired"
        with patch.object(main, "content_snapshot", return_value="changed-again"):
            email, finish, merge = self.run_merge(state, threads=[], reply="merge")
        merge.assert_not_called()
        finish.assert_not_called()
        self.assertIn("content changed", email.call_args.args[2])

    def test_empty_merge_wait_does_not_log_inbox_or_last_notification(self):
        state = self.state()
        state.update(thread_id="C123:1", last_email={"subject": "merge blocked"})
        with patch.object(main.gmail_client, "poll_reply", return_value=None) as poll, \
             patch.object(main.log, "debug") as log:
            main.handle_wait(state)
        poll.assert_called_once_with("C123:1")
        log.assert_not_called()

    def state(self):
        return {"state": "WAIT_MERGE", "item": "task", "slug": "s", "branch": "b",
                "session_id": "sid", "pr_url": PR}

    def run_merge(self, state, threads, force=None, mergeable="MERGEABLE", reply="merge if review is clear"):
        v = {"action": "merge"}
        if force is not None:
            v["force"] = force
        with patch.object(main.agent_runner, "run", return_value=verdict(**v)), \
             patch.object(main, "trail"), \
             patch.object(main, "_pr_merge_state", return_value=("OPEN", mergeable)), \
             patch.object(main, "unresolved_review_threads", return_value=threads), \
             patch.object(main, "email") as email, \
             patch.object(main, "_finish_task") as finish, \
             patch.object(main.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, "", "")) as merge:
            main.do_merge_reply(state, reply)
        return email, finish, merge

    def test_failed_thread_query_blocks(self):
        email, finish, merge = self.run_merge(self.state(), threads=None)
        merge.assert_not_called()
        finish.assert_not_called()
        self.assertIn("could not verify", email.call_args.args[1])

    def test_unresolved_threads_block(self):
        state = self.state()
        email, finish, merge = self.run_merge(state, threads=[human()])
        merge.assert_not_called()
        self.assertIn("unresolved review comments", email.call_args.args[1])
        self.assertTrue(state["pr_thread_notified"])

    def test_non_boolean_force_does_not_bypass(self):
        email, finish, merge = self.run_merge(self.state(), threads=[human()], force="false")
        merge.assert_not_called()

    def test_force_true_merges_despite_threads(self):
        email, finish, merge = self.run_merge(self.state(), threads=[human()], force=True)
        merge.assert_called_once()
        finish.assert_called_once()

    def test_plain_merge_preserves_review_gate_without_classifier(self):
        for reply in ("merge", "MERGE!", "fusiona"):
            with self.subTest(reply=reply):
                email, finish, merge = self.run_merge(self.state(), threads=[human()], reply=reply)
                merge.assert_not_called()
                finish.assert_not_called()
                email.assert_called_once()

    def test_explicit_merge_anyway_bypasses_review_gate(self):
        for reply in ("merge anyway", "fusiona de todos modos"):
            with self.subTest(reply=reply):
                email, finish, merge = self.run_merge(self.state(), threads=[human()], reply=reply)
                merge.assert_called_once()
                finish.assert_called_once()
                email.assert_not_called()

    def test_clean_pr_merges(self):
        email, finish, merge = self.run_merge(self.state(), threads=[])
        merge.assert_called_once()
        self.assertIn("--squash", merge.call_args.args[0])
        finish.assert_called_once()

    def test_conflicting_pr_enters_resolution(self):
        state = self.state()
        email, finish, merge = self.run_merge(state, threads=[], mergeable="CONFLICTING")
        merge.assert_not_called()
        self.assertEqual(state["state"], "RESOLVE_CONFLICTS")
        self.assertTrue(state["stale_replies"])
        self.assertEqual(state["conflict_return"], "WAIT_MERGE")
        self.assertIn("resolving automatically", email.call_args.args[1])

    def test_unknown_pr_state_blocks(self):
        state = self.state()
        with patch.object(main.agent_runner, "run", return_value=verdict(action="merge")), \
             patch.object(main, "_pr_merge_state", return_value=(None, None)), \
             patch.object(main, "email") as email, \
             patch.object(main.subprocess, "run") as merge:
            main.do_merge_reply(state, "merge")
        merge.assert_not_called()
        self.assertEqual(state["state"], "WAIT_MERGE")


class Conflicts(unittest.TestCase):
    def test_check_routes_conflicting_pr(self):
        state = {"state": "WAIT_REVIEW", "pr_url": PR}
        with patch.object(main, "_pr_merge_state", return_value=("OPEN", "CONFLICTING")):
            self.assertTrue(main.check_pr_conflicts(state))
        self.assertEqual(state["state"], "RESOLVE_CONFLICTS")
        self.assertEqual(state["conflict_return"], "WAIT_REVIEW")
        self.assertNotIn("stale_replies", state)

    def test_check_ignores_unknown_and_resets_on_mergeable(self):
        state = {"state": "WAIT_MERGE", "pr_url": PR, "conflict_rounds": 2}
        with patch.object(main, "_pr_merge_state", return_value=(None, None)):
            self.assertFalse(main.check_pr_conflicts(state))
        self.assertEqual(state["conflict_rounds"], 2)
        with patch.object(main, "_pr_merge_state", return_value=("OPEN", "MERGEABLE")):
            self.assertFalse(main.check_pr_conflicts(state))
        self.assertNotIn("conflict_rounds", state)

    def test_status_finishes_task_when_merged_externally(self):
        state = {"state": "WAIT_MERGE", "pr_url": PR, "item": "task"}
        with patch.object(main, "_pr_merge_state", return_value=("MERGED", "UNKNOWN")), \
             patch.object(main, "_finish_task") as finish, \
             patch.object(main, "trail"):
            self.assertTrue(main.check_pr_status(state))
        finish.assert_called_once()
        self.assertFalse(finish.call_args.kwargs["reset_repo"])
        self.assertIn("merged on GitHub", finish.call_args.args[1])

    def test_status_escalates_when_closed_without_merge(self):
        state = {"state": "WAIT_REVIEW", "pr_url": PR, "item": "task"}
        with patch.object(main, "_pr_merge_state", return_value=("CLOSED", "UNKNOWN")), \
             patch.object(main, "email") as email:
            self.assertTrue(main.check_pr_status(state))
        self.assertEqual(state["state"], "WAIT_STUCK")
        self.assertEqual(state["stuck_return"], "WAIT_REVIEW")
        self.assertIn("closed", email.call_args.args[2])

    def test_status_falls_through_to_conflicts(self):
        state = {"state": "WAIT_MERGE", "pr_url": PR}
        with patch.object(main, "_pr_merge_state", return_value=("OPEN", "CONFLICTING")):
            self.assertTrue(main.check_pr_status(state))
        self.assertEqual(state["state"], "RESOLVE_CONFLICTS")
        state = {"state": "WAIT_MERGE", "pr_url": PR}
        with patch.object(main, "_pr_merge_state", return_value=(None, None)):
            self.assertFalse(main.check_pr_status(state))
        self.assertEqual(state["state"], "WAIT_MERGE")

    def test_cap_escalates_to_stuck(self):
        state = {"state": "WAIT_MERGE", "pr_url": PR, "conflict_rounds": 3, "item": "t"}
        with patch.object(main.config, "CONFLICT_MAX_ROUNDS", 3), \
             patch.object(main, "email"):
            self.assertFalse(main._enter_conflict_resolution(state))
        self.assertEqual(state["state"], "WAIT_STUCK")
        self.assertEqual(state["stuck_return"], "WAIT_MERGE")

    def test_resolution_with_commit_queues_push_for_re_review(self):
        state = {"state": "RESOLVE_CONFLICTS", "branch": "b", "session_id": "sid", "pr_url": PR,
                 "conflict_return": "WAIT_MERGE", "stale_replies": True, "has_code_review": True}
        with patch.object(main, "git", side_effect=["head-1", "head-2"]), \
             patch.object(main.agent_runner, "resume", return_value=Mock(
                 session_id="sid", output="merged", question=None, attachments=[])), \
             patch.object(main, "save_state"):
            main.do_resolve_conflicts(state)
        self.assertEqual(state["state"], "PUSHING")
        self.assertEqual(state["push_context"]["continuation"], "conflicts")
        with patch.object(main, "git"), patch.object(main.final_checks, "run", return_value={"status": "pass"}):
            main.do_push(state)
        self.assertEqual(state["state"], "WAIT_REVIEW")
        self.assertTrue(state["await_new_run"])
        self.assertTrue(state["stale_replies"])

    def test_resolution_without_commit_returns_to_origin(self):
        state = {"state": "RESOLVE_CONFLICTS", "branch": "b", "session_id": "sid", "pr_url": PR,
                 "conflict_return": "WAIT_MERGE", "stale_replies": True}
        with patch.object(main, "git", return_value="head-1"), \
             patch.object(main.agent_runner, "resume", return_value=Mock(
                 session_id="sid", output="nothing to do", question=None, attachments=[])):
            main.do_resolve_conflicts(state)
        self.assertEqual(state["state"], "WAIT_MERGE")
        self.assertNotIn("stale_replies", state)
        self.assertNotIn("conflict_head", state)


class ReviewWait(unittest.TestCase):
    def state(self):
        return {"state": "WAIT_REVIEW", "item": "t", "pr_url": PR, "review_since": time.time(),
                "e2e_specs": [], "await_new_run": True}

    def test_new_run_with_ocr_threads_enters_address_review(self):
        state = self.state()
        with patch.object(main, "code_review_check", return_value={"bucket": "fail", "link": "L1"}), \
             patch.object(main, "unresolved_review_threads", return_value=[ocr(), human()]):
            main.handle_review_wait(state)
        self.assertEqual(state["state"], "ADDRESS_REVIEW")
        self.assertEqual([t["id"] for t in state["review_threads"]], ["T1"])
        self.assertEqual(state["review_run_link"], "L1")

    def test_query_failure_does_not_consume_the_run(self):
        state = self.state()
        with patch.object(main, "code_review_check", return_value={"bucket": "pass", "link": "L1"}), \
             patch.object(main, "unresolved_review_threads", return_value=None), \
             patch.object(main, "finalize_pr") as finalize:
            main.handle_review_wait(state)
        finalize.assert_not_called()
        self.assertNotIn("review_run_link", state)

    def test_resolution_only_round_finalizes_from_processed_run(self):
        state = self.state() | {"review_run_link": "L1", "await_new_run": False}
        with patch.object(main, "code_review_check", return_value={"bucket": "pass", "link": "L1"}), \
             patch.object(main, "unresolved_review_threads", return_value=[human()]), \
             patch.object(main, "finalize_pr") as finalize:
            main.handle_review_wait(state)
        finalize.assert_called_once()

    def test_awaiting_new_run_never_concludes_from_old_run(self):
        state = self.state() | {"review_run_link": "L1", "review_since": main.time.time()}
        with patch.object(main, "code_review_check", return_value={"bucket": "pass", "link": "L1"}), \
             patch.object(main, "unresolved_review_threads", return_value=[]), \
             patch.object(main, "finalize_pr") as finalize:
            main.handle_review_wait(state)
        finalize.assert_not_called()


class MergeWait(unittest.TestCase):
    def test_failed_query_only_checks_inbox(self):
        state = {"state": "WAIT_MERGE", "pr_url": PR, "pr_thread_round": 1}
        with patch.object(main, "unresolved_review_threads", return_value=None), \
             patch.object(main, "handle_wait") as wait:
            main.handle_merge_wait(state)
        wait.assert_called_once()
        self.assertEqual(state["state"], "WAIT_MERGE")
        self.assertEqual(state["pr_thread_round"], 1)

    def test_threads_are_addressed_proactively(self):
        state = {"state": "WAIT_MERGE", "pr_url": PR}
        with patch.object(main, "unresolved_review_threads", return_value=[human()]), \
             patch.object(main, "handle_wait") as wait:
            main.handle_merge_wait(state)
            wait.assert_called_once()
        self.assertEqual(state["state"], "ADDRESS_PR_THREADS")
        self.assertEqual(state["pr_threads"][0]["id"], "H1")

    def test_answered_thread_waits_for_the_reviewer(self):
        state = {"state": "WAIT_MERGE", "pr_url": PR, "pr_threads_seen": {"H1": 3}}
        with patch.object(main, "unresolved_review_threads", return_value=[human_thread()]), \
             patch.object(main, "handle_wait") as wait:
            main.handle_merge_wait(state)
        wait.assert_called_once()
        self.assertEqual(state["state"], "WAIT_MERGE")

    def test_answered_thread_reenters_on_new_reviewer_comment(self):
        state = {"state": "WAIT_MERGE", "pr_url": PR, "pr_threads_seen": {"H1": 2}}
        with patch.object(main, "unresolved_review_threads", return_value=[human_thread()]), \
             patch.object(main, "handle_wait") as wait:
            main.handle_merge_wait(state)
            wait.assert_called_once()
        self.assertEqual(state["state"], "ADDRESS_PR_THREADS")


if __name__ == "__main__":
    unittest.main()
