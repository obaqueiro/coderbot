"""Evidence video offload: the stitched mp4 goes to Drive and the emails link to it
(the link rides the body so the activity trail mirrors it); anything that is not an
mp4, or whose upload failed, is attached as before."""
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.fake_github import FakeGitHub

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main

main.config.STATE_PATH = pathlib.Path(tempfile.mkdtemp()) / "state.json"

LINK = "https://drive.google.com/file/d/abc/view"
MP4 = Path("/tmp/evidence.mp4")


class OffloadHelperTests(unittest.TestCase):
    def setUp(self):
        self.state = {"slug": "api-version", "branch": "codebot-api-version"}
        p = patch.object(main.config, "EVIDENCE_UPLOAD", True)
        p.start()
        self.addCleanup(p.stop)

    def test_uploads_only_mp4_and_drops_it_from_attachments(self):
        with patch.object(main.evidence_delivery, "publish", return_value={"status": "complete", "url": LINK}) as upload:
            remaining, text = main._offload_evidence_video(
                self.state, [Path("/tmp/report.html"), MP4, Path("/tmp/shot.png")])

        self.assertEqual(remaining, [Path("/tmp/report.html"), Path("/tmp/shot.png")])
        self.assertEqual(text, LINK)
        self.assertEqual(self.state["evidence_url"], LINK)
        upload.assert_called_once()
        self.assertEqual(upload.call_args.args[-1], MP4)

    def test_failed_upload_keeps_mp4_attached(self):
        with patch.object(main.evidence_delivery, "publish", return_value={"status": "retryable"}):
            remaining, text = main._offload_evidence_video(self.state, [MP4])
        self.assertEqual(remaining, [MP4])
        self.assertIsNone(text)
        self.assertNotIn("evidence_url", self.state)

    def test_disabled_upload_is_a_no_op(self):
        with patch.object(main.config, "EVIDENCE_UPLOAD", False):
            self.assertEqual(main._offload_evidence_video(self.state, [MP4]), ([MP4], None))

    def test_two_videos_get_distinct_names(self):
        with patch.object(main.evidence_delivery, "publish",
                          side_effect=[{"status": "complete", "url": LINK}, {"status": "complete", "url": LINK + "2"}]) as upload:
            remaining, text = main._offload_evidence_video(
                self.state, [MP4, Path("/tmp/outbox/demo.mp4")])
        self.assertEqual(remaining, [])
        self.assertEqual(text, f"{LINK}\n{LINK}2")
        paths = [c.args[-1] for c in upload.call_args_list]
        self.assertNotEqual(paths[0], paths[1])


class FinalizePrTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.mp4 = Path(self.scratch.name) / "evidence.mp4"
        self.mp4.write_bytes(b"recording")
        self.report = Path(self.scratch.name) / "report.html"
        self.report.write_text("report")
        self.state = {"state": "ADDRESS_REVIEW", "slug": "api-version",
                      "branch": "codebot-api-version", "item": "Support another API version",
                      "pr_url": "https://github.com/acme/repo/pull/7", "pr_summary": "summary",
                      "e2e_kind": "playwright", "has_e2e_harness": True}
        for p in (patch.object(main.config, "EVIDENCE_UPLOAD", True),
                  patch.object(main.config, "DATA_DIR", Path(self.scratch.name) / "data"),
                  patch.object(main, "content_snapshot", return_value="fixture"),
                  patch.object(main, "_run_checked", return_value='{"body":"summary"}'),
                  patch.object(main, "save_state"),
                  patch.object(main, "_publish_progress"),
                  patch.object(main, "_sync_pr_video"),
                  patch.object(main.evidence, "valid_media", return_value=True),
                  patch.object(main.lifecycle, "review_threads", return_value=[])):
            p.start()
            self.addCleanup(p.stop)

    def _finalize(self, evidence_files, link):
        with patch.object(main.evidence, "record_evidence", return_value=evidence_files), \
             patch.object(main.evidence_delivery, "publish", return_value={"status": "complete" if link else "retryable", "url": link}) as upload, \
             patch.object(main, "email", return_value=None) as email:
            main.finalize_pr(self.state)
        return email.call_args.args, upload

    def test_links_video_and_sends_no_attachment(self):
        args, _ = self._finalize([self.mp4], LINK)
        self.assertEqual(args[1], "PR ready for review")
        self.assertIn(f"Video: {LINK}", args[2])
        self.assertNotIn("Attached:", args[2])
        self.assertEqual([p.suffix for p in args[3]], [".md"])
        self.assertEqual(self.state["evidence_url"], LINK)
        self.assertEqual(self.state["state"], "WAIT_MERGE")

    def test_slack_pr_ready_links_drive_video_in_the_task_thread(self):
        self.state["thread_id"] = "C123:100.000001"
        with patch.object(main.config, "COMM_CHANNEL", "slack"), \
             patch.object(main.evidence, "record_evidence", return_value=[self.mp4]), \
             patch.object(main.evidence_delivery, "publish", return_value={"status": "complete", "url": LINK}), \
             patch.object(main.gmail_client, "send", return_value="C123:100.000001") as banner, \
             patch.object(main.gmail_client, "deliver", return_value={"thread_id": "C123:100.000001", "complete": True}) as send, \
             patch.object(main, "trail"):
            main.finalize_pr(self.state)
        self.assertEqual(send.call_args.args[3], "C123:100.000001")
        self.assertIn(LINK, send.call_args.args[2])
        self.assertEqual(send.call_args.args[4][0].suffix, ".md")
        self.assertEqual(banner.call_args.kwargs["progress"].stem, "pr_review")

    def test_failed_upload_attaches_as_before(self):
        args, _ = self._finalize([self.mp4], None)
        self.assertIn("Evidence: a Playwright video (mp4) queued for delivery", args[2])
        self.assertNotIn("Video:", args[2])
        self.assertEqual(args[3][0], self.mp4)
        self.assertEqual(args[3][-1].suffix, ".md")

    def test_newman_report_is_not_uploaded(self):
        self.state["e2e_kind"] = "newman"
        report = self.report
        args, upload = self._finalize([report], LINK)
        upload.assert_not_called()
        self.assertIn("Evidence: a Newman run report (html) queued for delivery", args[2])
        self.assertEqual(args[3][0], report)
        self.assertEqual(args[3][-1].suffix, ".md")

    def test_reset_keys_forget_the_link_between_tasks(self):
        self.assertIn("evidence_url", main.RESET_KEYS)

    def test_replayed_upload_restores_video_link_and_updates_pr_metadata(self):
        def replay(database, task_id, identity, step, action, **kwargs):
            if step == "RECORD":
                return [str(self.mp4)]
            if step == "UPLOAD":
                return {"files": [], "url": LINK}
            return action()
        with patch.object(main.delivery_checkpoint, "step", side_effect=replay), \
             patch.object(main, "_sync_pr_video") as sync, patch.object(main, "email"):
            main.finalize_pr(self.state)
        self.assertEqual(self.state["evidence_url"], LINK)
        sync.assert_called_once_with(self.state, LINK)

    def test_missing_video_blocks_ready_cover_and_starts_implementation_repair(self):
        with patch.object(main.evidence_delivery, "review_files", return_value=[]), \
             patch.object(main.evidence_delivery, "recovery_details", return_value="recording.log: missing tests"), \
             patch.object(main, "email") as notify, \
             patch.object(main.evidence_delivery, "publish") as publish:
            main.finalize_pr(self.state)
        self.assertEqual(self.state["state"], "REPAIR_CHECKS")
        self.assertTrue(self.state["check_repair"]["evidence"])
        self.assertEqual(self.state["push_context"]["continuation"], "evidence")
        publish.assert_not_called()
        self.assertNotIn("PR ready for review", [c.args[1] for c in notify.call_args_list])
        self.assertIn("implementing a fix", notify.call_args.args[2])


class FeedbackPushTests(unittest.TestCase):
    def test_feedback_push_links_video_and_attaches_the_rest(self):
        state = {"state": "PUSHING", "branch": "codebot-api-version", "slug": "api-version",
                 "pr_url": "https://github.com/acme/repo/pull/7",
                 "push_context": {"continuation": "feedback", "output": "fixed details",
                                  "attachments": [str(MP4), "/tmp/evidence.txt"]}}
        github = FakeGitHub(base=main.config.BASE_BRANCH)
        github.add(7, "codebot-api-version")
        with patch.object(main.config, "EVIDENCE_UPLOAD", True), \
             patch.object(main.final_checks, "run", return_value={"status": "pass"}), \
             patch.object(main, "git"), github, \
             patch.object(main.evidence_delivery, "publish", return_value={"status": "complete", "url": LINK}), \
             tempfile.TemporaryDirectory() as root, \
             patch.object(main.config, "DATA_DIR", Path(root)), \
             patch.object(main, "_sync_pr_video") as sync, \
             patch.object(main, "_publish_progress"), \
             patch.object(main, "email") as email:
            main.do_push(state)

        args = email.call_args.args
        self.assertEqual(args[1], "PR updated")
        self.assertIn("fixed details", args[2])
        self.assertTrue(args[2].endswith(f"Video: {LINK}"))
        self.assertEqual(args[3][0], Path("/tmp/evidence.txt"))
        self.assertEqual(args[3][-1].suffix, ".md")
        self.assertEqual(state["state"], "WAIT_MERGE")
        self.assertNotIn("push_context", state)
        sync.assert_called_once_with(state, LINK)


if __name__ == "__main__":
    unittest.main()
