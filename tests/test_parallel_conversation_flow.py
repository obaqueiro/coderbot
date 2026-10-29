"""Controller/worker boundaries use real durable receipts and controlled harnesses."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import markdown  # Keep extension classes outside the temporary module registry.
with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main
import parallel_conversation as lateral
from execution_store import ExecutionStore


class ParallelFlowTests(unittest.TestCase):
    def test_merge_during_push_without_saved_decision_is_queued_for_same_pr(self):
        self.state.update(state="PUSHING", pr_url="https://github.com/org/repo/pull/1")
        row = self.classify("merge anyway", "answer")
        self.assertEqual(row["data"]["route"], "flow")
        generate = Mock()
        self.assertFalse(lateral.process_one(self.store, self.repo, generate, Mock()))
        generate.assert_not_called()
        with patch.object(main, "_dispatch_wait_reply") as handler, patch.object(main, "save_state"):
            self.assertFalse(main._dispatch_conversation(self.state))
            self.state["state"] = "WAIT_MERGE"
            self.assertTrue(main._dispatch_conversation(self.state))
            handler.assert_called_once_with(self.state, row["identity"], "merge anyway")

    def test_early_merge_cannot_target_another_pr(self):
        self.state.update(state="PUSHING", pr_url="https://github.com/org/repo/pull/1")
        self.classify("merge anyway", "answer")
        self.state.update(state="WAIT_MERGE", pr_url="https://github.com/org/repo/pull/2")
        with patch.object(main, "_dispatch_wait_reply") as handler, patch.object(main, "email"):
            self.assertTrue(main._dispatch_conversation(self.state))
            handler.assert_not_called()

    def test_plain_merge_during_push_is_queued_without_lateral_refusal(self):
        self.state.update(state="PUSHING", pr_url="https://github.com/org/repo/pull/1")
        row = self.classify("merge", "answer")
        self.assertEqual(row["data"]["route"], "flow")
        self.assertEqual(row["data"]["queued_pr_url"], self.state["pr_url"])
        self.assertFalse(lateral.process_one(self.store, self.repo, Mock(), Mock()))

    def test_captured_instruction_is_acknowledged_and_transferred_once(self):
        self.state["state"] = "PUSHING"
        row = lateral.receive(self.store, self.state, self.repo, "instruction", "Publica el MP4")
        row = lateral.save_classification(self.store, row, {
            "intent": "conversation", "resolves_pending_question": False,
            "requested_action": "deliver evidence", "inputs": []})
        generate = Mock()
        deliver = Mock(return_value={"complete": True, "notification_id": "r"})
        self.assertTrue(lateral.process_one(self.store, self.repo, generate, deliver))
        generate.assert_not_called()
        self.assertIn("quedó en cola", deliver.call_args.args[0]["data"]["output"])
        lateral.transfer_changes(self.store, self.state, self.repo)
        lateral.transfer_changes(self.store, self.state, self.repo)
        events = self.store.list("feedback", "task")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["text"], "Publica el MP4")

    def test_merge_anyway_during_repairs_waits_for_safe_merge_decision(self):
        self.state.update(state="ADDRESS_PR_THREADS", pr_url="https://github.com/org/repo/pull/1")
        self.state.pop("pending_question", None)
        main.handoffs.remember_decision(self.state, "PR ready for review")
        row = self.classify("merge anyway", "answer")
        self.assertEqual(row["data"]["route"], "flow")
        with patch.object(main, "_dispatch_wait_reply") as handler, patch.object(main, "save_state"):
            self.assertFalse(main._dispatch_conversation(self.state))
            handler.assert_not_called()
            generate = Mock()
            self.assertFalse(lateral.process_one(self.store, self.repo, generate, Mock()))
            self.state["state"] = "WAIT_MERGE"
            self.assertTrue(main._dispatch_conversation(self.state))
            handler.assert_called_once_with(self.state, row["identity"], "merge anyway")

    def test_deferred_merge_never_applies_to_a_new_pr_decision(self):
        self.state.update(state="PUSHING", pr_url="https://github.com/org/repo/pull/1")
        main.handoffs.remember_decision(self.state, "PR ready for review")
        self.classify("merge anyway", "answer")
        self.state.update(state="WAIT_MERGE", pr_url="https://github.com/org/repo/pull/2")
        with patch.object(main, "_dispatch_wait_reply") as handler, patch.object(main, "email"):
            self.assertTrue(main._dispatch_conversation(self.state))
            handler.assert_not_called()

    def test_numbered_merge_choice_is_retained_during_push(self):
        self.state.update(state="PUSHING", pr_url="https://github.com/org/repo/pull/1")
        main.handoffs.remember_decision(self.state, "PR ready for review")
        row = self.classify("2", "answer")
        self.assertEqual(row["data"]["route"], "flow")
        self.assertTrue(row["data"]["deferred_pr_decision"])

    def test_explicit_btw_merge_during_repairs_remains_lateral(self):
        self.state.update(state="ADDRESS_PR_THREADS", pr_url="https://github.com/org/repo/pull/1")
        main.handoffs.remember_decision(self.state, "PR ready for review")
        row = self.classify("/btw merge anyway", "answer")
        self.assertEqual(row["data"]["route"], "lateral")
        self.assertNotIn("deferred_pr_decision", row["data"])

    def test_deferred_decision_does_not_starve_other_actionable_replies(self):
        self.state.update(state="PUSHING", pr_url="https://github.com/org/repo/pull/1")
        main.handoffs.remember_decision(self.state, "PR ready for review")
        self.classify("merge anyway", "answer")
        self.state.update(state="WAIT_STUCK", pending_question="Retry?", stuck_return="PUSHING")
        recovery = self.classify("retry", "answer")
        with patch.object(main, "save_state"), patch.object(main, "_dispatch_wait_reply") as handler:
            self.assertTrue(main._dispatch_conversation(self.state))
            handler.assert_called_once_with(self.state, recovery["identity"], "retry")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.store = ExecutionStore(self.repo / "execution.sqlite")
        self.state = {"execution_task_id": "task", "item": "Filter models", "slug": "filter",
                      "state": "WAIT_STUCK", "thread_id": "C:1", "session_id": "work",
                      "pending_question": "How should I recover?", "stuck_return": "PROPOSING",
                      "question_rounds": 2, "task_language": "Spanish"}
        for target, name, value in ((main.config, "REPO_PATH", self.repo),
                                    (main.config, "DATA_DIR", self.repo),
                                    (main.phase_checkpoint, "store", lambda: self.store)):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def classify(self, text, intent="conversation", inputs=()):
        row = lateral.receive(self.store, self.state, self.repo, str(len(self.store.list("conversation", "task"))), text)
        return lateral.save_classification(self.store, row,
            {"intent": intent, "resolves_pending_question": intent == "answer", "inputs": list(inputs)})

    def test_explanation_then_valid_reply_preserves_wait_until_answer(self):
        before = copy.deepcopy(self.state)
        row = self.classify("No entiendo, ¿qué tenemos que hacer?")
        generate = Mock(return_value=SimpleNamespace(output="El proveedor bloqueó el paso.", session_id="chat"))
        deliver = Mock(return_value={"complete": True, "notification_id": "r"})
        lateral.process_one(self.store, self.repo, generate, deliver)
        self.assertEqual(self.state, before)
        self.classify("retry", "answer")
        with patch.object(main, "save_state"), patch.object(main, "_dispatch_wait_reply") as handler:
            self.assertTrue(main._dispatch_conversation(self.state))
        handler.assert_called_once()
        self.assertEqual(handler.call_args.args[2], "retry")
        self.assertEqual(self.store.get("conversation", row["id"])["status"], "complete")

    def test_stale_choice_never_authorizes_new_decision(self):
        row = self.classify("1", "answer")
        self.state["pending_question"] = "A different decision"
        with patch.object(main, "_dispatch_wait_reply") as handler:
            self.assertTrue(main._dispatch_conversation(self.state))
        handler.assert_not_called()
        self.assertNotEqual(self.store.get("conversation", row["id"])["data"]["route"], "flow")

    def test_initial_planning_change_does_not_consume_question(self):
        self.state.update(state="WAIT_REPLY", return_state="EXPLORING")
        before = copy.deepcopy(self.state)
        row = self.classify("Add provider filter", "change_request",
                            [{"kind": "change", "text": "Add provider filter"}])
        lateral.register_inputs(self.store, row, self.repo)
        lateral.transfer_changes(self.store, self.state, self.repo)
        event = self.store.list("feedback", "task")[0]
        result = SimpleNamespace(output="Planning updated", session_id="work")
        with patch.object(main.agent_runner, "resume", return_value=result) as resume, \
             patch.object(main, "save_state"), patch.object(main, "email"), \
             patch.object(main, "do_question_reply") as question:
            main._apply_received_feedback(self.state, self.store, event)
        question.assert_not_called()
        self.assertEqual(self.state, before)
        self.assertIn("does NOT answer", resume.call_args.args[1])

    def test_change_clarification_does_not_replace_workflow_question(self):
        self.state.update(state="WAIT_REPLY", return_state="IMPLEMENTING", stuck_return="IMPLEMENTING")
        before = copy.deepcopy(self.state)
        row = self.classify("Add ambiguous filter", "change_request",
                            [{"kind": "change", "text": "Add ambiguous filter"}])
        lateral.register_inputs(self.store, row, self.repo)
        lateral.transfer_changes(self.store, self.state, self.repo)
        event = self.store.list("feedback", "task")[0]
        assessment = {"action": "question", "answer": "Which provider?", "reason": "Unspecified", "references": "spec.md"}
        with patch.object(main.agent_runner, "run", return_value=SimpleNamespace(output=json.dumps(assessment))), \
             patch.object(main, "save_state"), patch.object(main, "email"), \
             patch("message_delivery.deliver", return_value={"complete": True}):
            main._apply_received_feedback(self.state, self.store, event)
        self.assertEqual(self.state, before)
        item = self.store.list("conversation_input", "task")[0]
        self.assertEqual(item["status"], "clarifying")
        self.assertEqual(item["data"]["question"], "Which provider?")

    def test_btw_never_calls_operational_handler_in_either_channel(self):
        for channel in ("email", "slack"):
            with self.subTest(channel=channel), patch.object(main.config, "COMM_CHANNEL", channel), \
                 patch.object(main.gmail_client, "mark_processed"), \
                 patch.object(main, "_dispatch_wait_reply") as handler:
                self.assertTrue(main._queue_conversation(self.state, channel, "/btw abort"))
                self.assertFalse(main._dispatch_conversation(self.state))
                handler.assert_not_called()

    def test_lateral_result_for_old_task_does_not_mutate_new_task(self):
        self.classify("retry", "answer")
        new = {**self.state, "execution_task_id": "new-task"}
        with patch.object(main, "_dispatch_wait_reply") as handler:
            self.assertFalse(main._dispatch_conversation(new))
        handler.assert_not_called()

    def test_receipt_delivery_works_in_both_adapters_without_regeneration(self):
        for channel in ("email", "slack"):
            with self.subTest(channel=channel):
                row = self.classify("Explain " + channel)
                row = self.store.update("conversation", row, output="Explanation", status="generated")
                backend = Mock(__name__=channel)
                backend.__name__ = channel
                backend.attachment_limits.return_value = {"file": 10000, "mime": 10000}
                def send(envelope, receipts, confirm):
                    confirm("body", {"status": "confirmed"})
                    return "C:1"
                backend.send_envelope.side_effect = send
                receipt = lateral.delivery(self.store, row, self.repo, self.repo, backend)
                self.assertTrue(receipt["complete"])
                lateral.delivery(self.store, row, self.repo, self.repo, backend)
                self.assertEqual(len(self.store.list("delivery_receipt", "task")), 1 if channel == "email" else 2)

    def test_main_delivery_barrier_blocks_before_publishing(self):
        row = self.classify("Add X", "change_request", [{"kind": "change", "text": "Add X"}])
        lateral.register_inputs(self.store, row, self.repo)
        with patch.object(main, "_receive_feedback", return_value=False), patch.object(main, "email"), \
             patch.object(main, "git") as git:
            main.do_open_pr(self.state)
        git.assert_not_called()

    def test_status_remains_available_while_lateral_worker_is_busy(self):
        main._work_active.set()
        self.addCleanup(main._work_active.clear)
        with patch.object(main, "_current_state", {**self.state, "state": "IMPLEMENTING"}), \
             patch.object(main.config, "COMM_CHANNEL", "slack"), \
             patch.object(main.agent_runner, "turn_snapshot", return_value={"active": True}), \
             patch.object(main.operations, "snapshot", return_value=[]), \
             patch.object(main.gmail_client, "poll_verify", return_value=None), \
             patch.object(main.gmail_client, "poll_kick", return_value=None), \
             patch.object(main.gmail_client, "poll_status", return_value=("status", "C:1")), \
             patch.object(main.gmail_client, "send") as send, \
             patch.object(main.gmail_client, "mark_processed"), \
             patch.object(main, "_short_status", return_value="Working"):
            self.assertTrue(main._status_supervisor_once())
        send.assert_called_once()

    def test_classified_replan_requires_approval(self):
        self.state.update(state="IMPLEMENTING")
        row = self.classify("Add provider filter", "change_request",
                            [{"kind": "change", "text": "Add provider filter"}])
        lateral.register_inputs(self.store, row, self.repo)
        lateral.transfer_changes(self.store, self.state, self.repo)
        event = self.store.list("feedback", "task")[0]
        assessment = {"action": "replan", "answer": "New scope", "reason": "Missing requirement", "references": "spec.md"}
        with patch.object(main.agent_runner, "run", return_value=SimpleNamespace(output=json.dumps(assessment))), \
             patch.object(main, "save_state"), patch.object(main, "email"):
            main._apply_received_feedback(self.state, self.store, event)
        self.assertEqual(self.state["state"], "REPLANNING")
        self.assertNotIn("approved_proposal", self.state)
        lateral.sync_inputs(self.store, self.state, self.repo)
        self.assertEqual(self.store.list("conversation_input", "task")[0]["status"], "waiting_approval")

    def test_controller_does_not_fallback_to_chat_after_technical_failure(self):
        row = self.classify("retry", "answer")
        with patch.object(main, "_dispatch_wait_reply", side_effect=ConnectionError("offline")):
            with self.assertRaises(ConnectionError):
                main._dispatch_conversation(self.state)
        self.assertEqual(self.store.get("conversation", row["id"])["data"]["route"], "flow")

    def test_pending_change_blocks_merge_before_github_action(self):
        self.state.update(state="WAIT_MERGE", pr_url="https://github.com/org/repo/pull/1")
        row = self.classify("Add X", "change_request", [{"kind": "change", "text": "Add X"}])
        lateral.register_inputs(self.store, row, self.repo)
        with patch.object(main.agent_runner, "run", return_value=SimpleNamespace(output='{"action":"merge"}')), \
             patch.object(main, "_receive_feedback", return_value=False), patch.object(main, "email"), \
             patch.object(main, "_pr_merge_state") as github:
            main.do_merge_reply(self.state, "merge")
        github.assert_not_called()

    def test_shutdown_stops_lateral_worker_without_mutating_task(self):
        before = copy.deepcopy(self.state)
        worker = lateral.Worker(lambda: self.store, self.repo, Mock(), Mock())
        with patch("turn_control.request_kick") as kick:
            worker.close()
        self.assertTrue(worker.stop.is_set())
        kick.assert_called_once_with("conversation")
        self.assertEqual(self.state, before)

    def test_conversation_requires_no_configuration_flag(self):
        self.assertFalse(hasattr(main.config, "PARALLEL_CONVERSATION"))
        with patch.object(main.gmail_client, "mark_processed"):
            self.assertTrue(main._queue_conversation(self.state, "always-on", "/btw Explain"))
        rows = self.store.list("conversation", "task", identity="always-on")
        self.assertEqual(len(rows), 1)


class EarlyPrDecisions(unittest.TestCase):
    def test_a_merge_sent_during_re_verification_waits_for_the_pr(self):
        import parallel_conversation
        decision = {"wait_state": "WAIT_MERGE", "task": "t", "question": "q", "options": []}
        workspace = {"slug": "t", "pending_decision": decision, "state": "VERIFYING",
                     "workspace": {"repositories": {"api": {"pr_url": "https://github.com/o/api/pull/1"},
                                                    "ui": {"pr_url": "https://github.com/o/ui/pull/2"}}}}
        single = {"slug": "t", "pending_decision": decision, "state": "INTERNAL_REVIEW",
                  "pr_url": "https://github.com/o/r/pull/3"}
        for state in (workspace, single):
            with self.subTest(state=state["state"]):
                self.assertTrue(parallel_conversation.deferred_pr_reply(state, "merge"))
        self.assertFalse(parallel_conversation.deferred_pr_reply({**single, "state": "IMPLEMENTING"}, "merge"))
