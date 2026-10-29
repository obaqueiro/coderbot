"""All proposal handoffs share the same complete, frozen review document."""
import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(),
                              "gmail_client": Mock()}):
    import main


class ProposalHandoff(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.change = self.repo / "openspec/changes/example"
        (self.change / "specs/api").mkdir(parents=True)
        for name in ("proposal.md", "design.md", "tasks.md"):
            (self.change / name).write_text(f"## {name}\nFirst\n")
        (self.change / "specs/api/spec.md").write_text("## API\nFirst\n")
        self.state = {"state": "PROPOSING", "slug": "example", "branch": "bot-example",
                      "item": "Create API"}

    def test_initial_and_revisions_send_current_complete_package(self):
        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main.config, "DATA_DIR", self.root / "data"), \
             patch.object(main, "save_state"), patch.object(main, "email") as send:
            main._send_proposal_review(self.state, "Proposal summary")
            self.assertEqual(self.state["state"], "WAIT_APPROVAL")
            first = send.call_args.args[3][0]
            self.assertIn("specs/api/spec.md", first.read_text())
            self.assertTrue(Path(self.state["proposal_snapshot"]).is_file())
            (self.change / "design.md").write_text("## design.md\nSecond\n")
            main._send_proposal_review(self.state, "Revised summary", revised=True,
                                       feedback="Change design")
            second = send.call_args.args[3][0]
            self.assertNotEqual(first, second)
            self.assertIn("Changed in design.md", send.call_args.args[2])
            self.assertIn("First", first.read_text())
            self.assertIn("Second", second.read_text())
            send.reset_mock()
            main._send_proposal_review(self.state, "Revised summary", revised=True,
                                       feedback="Change design")
            send.assert_not_called()

    def test_missing_bundle_never_requests_approval(self):
        (self.change / "tasks.md").unlink()
        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main.config, "DATA_DIR", self.root / "data"), \
             patch.object(main, "email") as send, \
             patch.object(main, "_enter_stuck") as stuck:
            main._send_proposal_review(self.state, "summary")
        send.assert_not_called()
        self.assertIn("complete OpenSpec review package", stuck.call_args.args[2])

    def test_same_package_is_not_resent_for_different_feedback_or_revision_flag(self):
        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main.config, "DATA_DIR", self.root / "data"), \
             patch.object(main, "save_state"), patch.object(main, "email") as send:
            main._send_proposal_review(self.state, "First summary")
            send.reset_mock()
            main._send_proposal_review(self.state, "Different narration", revised=True,
                                       feedback="No implementes hasta mi aprobación")
            send.assert_not_called()
            self.assertEqual(self.state["state"], "WAIT_APPROVAL")

    def test_proposal_notice_does_not_dump_agent_narration(self):
        with patch.object(main.config, "REPO_PATH", self.repo), \
             patch.object(main.config, "DATA_DIR", self.root / "data"), \
             patch.object(main, "save_state"), patch.object(main, "email") as send:
            main._send_proposal_review(self.state, "Verbose agent narration\n" * 300)
            body = send.call_args.args[2]
            self.assertNotIn("Verbose agent narration", body)
            self.assertNotIn("Decision needed:", body)
            self.assertLess(len(body), 1000)
            self.assertLessEqual(len(body.splitlines()), 10)

    def test_wait_instruction_does_not_revise_or_resend_proposal(self):
        self.state.update(state="WAIT_APPROVAL", session_id="sid")
        with patch.object(main.agent_runner, "run", return_value=SimpleNamespace(output='{"action":"wait"}')), \
             patch.object(main.agent_runner, "resume") as resume, \
             patch.object(main, "_send_proposal_review") as send, \
             patch.object(main, "email") as notify:
            main.do_approval_reply(self.state, "No implementes hasta mi aprobación; sigue esperando")
        resume.assert_not_called()
        send.assert_not_called()
        self.assertEqual(self.state["state"], "WAIT_APPROVAL")
        self.assertLess(len(notify.call_args.args[2]), 250)

    def test_approval_reminder_is_short_even_with_full_pending_decision(self):
        state = dict(self.state, state="WAIT_APPROVAL", task_language="Spanish",
                     pending_decision={"wait_state": "WAIT_APPROVAL", "question": "Long proposal " * 500})
        body = main._ping_body(state, 0, 1)
        self.assertLess(len(body), 250)
        self.assertNotIn("Long proposal", body)

    def test_wait_classifier_contract_does_not_turn_deferral_into_revision(self):
        self.assertIn('"wait"', main.replies.prompt("approval", "r"))
        self.assertIn("no implementes hasta mi aprobación", main.replies.prompt("approval", "r"))

    def test_direct_and_question_continuation_use_same_sender(self):
        result = SimpleNamespace(output="Summary", session_id="sid", question=None)
        with patch.object(main.agent_runner, "resume", return_value=result), \
             patch.object(main, "handle_result", return_value=False), \
             patch.object(main, "_undo_premature_work", return_value=""), \
             patch.object(main, "_send_proposal_review") as sender:
            self.state["session_id"] = "sid"
            main.do_propose(self.state)
            main._continue_proposing(self.state, result)
        self.assertEqual(sender.call_count, 2)

    def test_complete_review_package_sent_in_both_channels(self):
        for channel, thread in (("email", "gmail-1"), ("slack", "C1:1.0")):
            with self.subTest(channel=channel), \
                 patch.object(main.config, "REPO_PATH", self.repo), \
                 patch.object(main.config, "DATA_DIR", self.root / channel), \
                 patch.object(main.config, "COMM_CHANNEL", channel), \
                 patch.object(main, "save_state"), \
                  patch.object(main, "trail"), \
                  patch.object(main.gmail_client, "send", return_value=thread) as banner, \
                  patch.object(main.gmail_client, "deliver", return_value={"thread_id": thread, "complete": True}) as send:
                state = dict(self.state, thread_id=thread)
                main._send_proposal_review(state, "Summary")
                self.assertEqual(send.call_args.args[4][0].suffix, ".html")
                self.assertIn("specs/api/spec.md", send.call_args.args[4][0].read_text())
                self.assertEqual(state["state"], "WAIT_APPROVAL")
                self.assertEqual(banner.call_args.kwargs["progress"].stem, "approval")
