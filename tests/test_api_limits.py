"""H11 contract tests: recorded-shape payloads beyond the clients' old page/size limits."""
import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
import errors
import gmail_client
import main
import slack_client as slack

PR = "https://github.com/acme/project/pull/42"


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


class ReviewThreads(unittest.TestCase):
    def comment(self, n, author="alice"):
        return {"author": {"login": author, "__typename": "User"}, "body": f"c{n}",
                "databaseId": n, "createdAt": f"2026-10-0{n % 9 + 1}T00:00:00Z"}

    def test_long_thread_keeps_opener_and_latest_comments(self):
        payload = {"data": {"viewer": {"login": "codebot"}, "repository": {"pullRequest": {
            "reviewThreads": {"pageInfo": {"hasNextPage": False}, "nodes": [{
                "id": "T1", "isResolved": False, "isOutdated": False, "path": "a.py", "line": 1,
                "opener": {"nodes": [self.comment(1, "github-actions")]},
                "comments": {"totalCount": 150,
                             "nodes": [self.comment(n) for n in range(51, 151)]}}]}}}}}
        with patch.object(main.operations, "tool",
                          return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), "")) as tool:
            [thread] = main.lifecycle.review_threads(main._pr_ops(PR), 42)
        query = next(a for a in tool.call_args.args[0] if a.startswith("query="))
        self.assertIn("comments(last: 100)", query)
        self.assertEqual(thread["comment_id"], 1)  # replies still target the opener
        self.assertEqual(thread["comments"][-1]["body"], "c150")
        self.assertEqual(len(thread["comments"]), 101)
        self.assertTrue(main.lifecycle.has_new_comments(thread, {"T1": 149}))
        self.assertFalse(main.lifecycle.has_new_comments(thread, {"T1": 150}))

    def test_bot_comments_read_every_page(self):
        page1 = [{"user": {"login": "github-actions[bot]"}, "body": f"p1-{n}"} for n in range(100)]
        page2 = [{"user": {"login": "github-actions[bot]"}, "body": "summary"},
                 {"user": {"login": "alice"}, "body": "human"}]
        out = json.dumps(page1) + "\n" + json.dumps(page2)
        with patch.object(main.operations, "tool",
                          return_value=subprocess.CompletedProcess([], 0, out, "")) as tool:
            comments = main._bot_comments("repos/acme/project/issues/42/comments")
        self.assertIn("--paginate", tool.call_args.args[0])
        self.assertEqual(len(comments), 101)
        self.assertEqual(comments[-1]["body"], "summary")


class SlackLimits(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for target, name, value in ((config, "DATA_DIR", Path(self.dir.name)),
                                    (config, "SLACK_CHANNEL_ID", "C123"), (slack, "_bot_user", "Ubot")):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.web = MagicMock()
        self.web.chat_postMessage.return_value = {"ts": "900.000001"}
        p = patch.object(slack, "web", return_value=self.web)
        p.start()
        self.addCleanup(p.stop)
        with slack._database() as db:
            db.execute("INSERT INTO roots(channel,root_ts,nonce) VALUES(?,?,?)", ("C123", "100.0", "n"))

    def test_reply_with_files_is_kept(self):
        # Recorded shape of a Slack message event for an uploaded screenshot.
        event = {"type": "message", "subtype": "file_share", "ts": "101.0", "thread_ts": "100.0",
                 "channel": "C123", "user": "Uhuman", "text": "the error looks like this",
                 "files": [{"id": "F1", "name": "error.png", "mimetype": "image/png",
                            "permalink": "https://acme.slack.com/files/U1/F1/error.png"}]}
        self.assertTrue(slack._accept_event({"event": event}))
        _id, text = slack.poll_reply("C123:100.0")
        self.assertIn("the error looks like this", text)
        self.assertIn("[attached file: error.png (https://acme.slack.com/files/U1/F1/error.png)]", text)
        joined = {**event, "subtype": "channel_join", "ts": "102.0"}
        self.assertFalse(slack._accept_event({"event": joined}))

    def test_task_root_search_is_bounded_by_time_not_five_pages(self):
        pages = [{"messages": [{"user": "Uother", "ts": f"{n}.0", "text": "chatter"}],
                  "response_metadata": {"next_cursor": f"c{n}"}} for n in range(12)]
        pages.append({"messages": [{"user": "Ubot", "ts": "50.0", "text": "x\n[codebot-task:deep]"}]})
        self.web.conversations_history.side_effect = pages
        self.assertEqual(slack.open_thread({"item": "t", "thread_nonce": "deep"}), "C123:50.0")
        self.web.chat_postMessage.assert_not_called()
        self.assertIn("oldest", self.web.conversations_history.call_args.kwargs)

    def test_runaway_history_is_a_transient_failure(self):
        self.web.conversations_history.return_value = {
            "messages": [], "response_metadata": {"next_cursor": "again"}}
        with patch.object(slack, "_ROOT_SEARCH_PAGES", 3):
            with self.assertRaises(errors.Transient):
                slack.open_thread({"item": "t", "thread_nonce": "never"})
        self.web.chat_postMessage.assert_not_called()


class EmailBodies(unittest.TestCase):
    def test_html_only_reply_is_read_without_quoted_history(self):
        # Recorded shape of an Outlook/Apple Mail reply with no text/plain part.
        html = ("<html><head><style>p{color:red}</style></head><body><div>Approve, but rename"
                "<br>the flag&nbsp;please.</div><img src=cid:1><div class=\"gmail_quote\">On Mon wrote:"
                "<blockquote>old <b>proposal</b></blockquote></div></body></html>")
        payload = {"mimeType": "multipart/alternative",
                   "parts": [{"mimeType": "text/html", "body": {"data": b64(html)}}]}
        self.assertEqual(gmail_client._extract_body(payload), "Approve, but rename\nthe flag\xa0please.")

    def test_plain_text_still_wins(self):
        payload = {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/html", "body": {"data": b64("<p>html</p>")}},
            {"mimeType": "text/plain", "body": {"data": b64("plain")}}]}
        self.assertEqual(gmail_client._extract_body(payload), "plain")


if __name__ == "__main__":
    unittest.main()
