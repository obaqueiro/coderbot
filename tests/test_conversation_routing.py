"""Routing latency and receipt status must not hide or reinterpret user instructions."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import agent_runner
import operations
import parallel_conversation as conversation
from execution_store import ExecutionStore

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main


class RoutingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = ExecutionStore(self.root / "execution.sqlite")
        self.state = {"execution_task_id": "task", "item": "ZIP Otros", "slug": "zip",
                      "state": "WAIT_STUCK", "stuck_return": "VERIFYING", "thread_id": "C:1",
                      "task_language": "Spanish", "pending_question": "¿Cómo seguimos?"}

    def test_immediate_receipt_precedes_classification_and_inbox_consumption(self):
        events = []
        worker = SimpleNamespace(notify=lambda row, stage: events.append(stage), wake=Mock())
        before = copy.deepcopy(self.state)
        with patch.object(main.phase_checkpoint, "store", return_value=self.store), \
             patch.object(main.config, "REPO_PATH", self.root), patch.object(main, "_conversation_worker", worker), \
             patch.object(main.gmail_client, "mark_processed", side_effect=lambda _: events.append("processed")), \
             patch.object(main.agent_runner, "run") as generate:
            self.assertTrue(main._queue_conversation(self.state, "message", "/btw Diagnostica sin repetir E2E."))
        self.assertEqual(events, ["received", "processed"])
        self.assertEqual(self.state, before)
        generate.assert_not_called()
        self.assertEqual(self.store.list("conversation", "task")[0]["data"]["classification"], None)

    def test_failed_receipt_notification_keeps_original_for_routing(self):
        worker = SimpleNamespace(notify=Mock(side_effect=ConnectionError("Slack unavailable")), wake=Mock())
        with patch.object(main.phase_checkpoint, "store", return_value=self.store), \
             patch.object(main.config, "REPO_PATH", self.root), patch.object(main, "_conversation_worker", worker), \
             patch.object(main.gmail_client, "mark_processed") as consumed:
            self.assertTrue(main._queue_conversation(self.state, "message", "/btw Diagnostica."))
        consumed.assert_called_once_with("message")
        self.assertEqual(self.store.list("conversation", "task")[0]["data"]["original_text"], "/btw Diagnostica.")

    def test_classification_context_excludes_reports_and_history_but_keeps_decisions_and_references(self):
        self.state["pending_decision"] = {"question": "¿Autorizas continuar?", "wait_state": "WAIT_STUCK",
            "task": "zip", "options": [{"label": "No — no hagas merge", "wait": True, "details": False}]}
        self.state["final_check_report"] = {"checks": [{"report": "LARGE-REPORT-" * 1000}] * 20}
        self.state["failure_context"] = {"traceback": "LARGE-TRACE-" * 1000}
        row = conversation.receive(self.store, self.state, self.root, "message", "/btw No amplíes el alcance.")
        view = row["data"]["snapshot"]
        view["history"] = [{"user": "OLD-HISTORY-" * 1000}] * 20
        view["proposal"] = {"design.md": "LARGE-DESIGN-" * 1000}
        view["inputs"] = [{"id": "input-42", "kind": "change", "status": "clarifying", "question": "¿Qué formato?"}]
        before = copy.deepcopy(row)
        prompt = conversation.classification_prompt(row)
        self.assertLess(len(prompt), 10000)
        for value in ("LARGE-REPORT", "LARGE-TRACE", "OLD-HISTORY", "LARGE-DESIGN"):
            self.assertNotIn(value, prompt)
        for value in ("No — no hagas merge", "input-42", "¿Qué formato?", "No amplíes el alcance"):
            self.assertIn(value, prompt)
        self.assertEqual(row, before)
        # Detailed evidence remains available to the separate answering harness.
        self.assertIn("LARGE-REPORT", conversation.response_prompt(row, []))

    def test_oversized_decision_cannot_authorize_flow_from_a_partial_routing_context(self):
        self.state["pending_question"] = "condition " * 1000
        row = conversation.receive(self.store, self.state, self.root, "message", "1. Sí, pero conserva los límites")
        saved = conversation.save_classification(self.store, row, {
            "intent": "answer", "resolves_pending_question": True, "inputs": []})
        self.assertEqual(saved["data"]["route"], "lateral")
        self.assertFalse(saved["data"]["classification"]["resolves_pending_question"])

    def test_retry_and_blocked_notices_are_specific_and_do_not_ask_for_rephrasing(self):
        text = "/btw Diagnostica los informes; no ejecutes E2E ni hagas merge."
        row = conversation.receive(self.store, self.state, self.root, "message", text)
        before = copy.deepcopy(self.state)
        notices = []
        def notify(current, stage):
            notices.append((stage, conversation.status_text(current, stage)))
        def generate(prompt, session):
            self.assertTrue(agent_runner._routing.get())
            current = self.store.get("conversation", row["id"])
            self.assertEqual(current["data"]["processing_stage"], "classification")
            self.assertTrue(notices[-1][0].startswith("classifying"))
            raise subprocess.TimeoutExpired(["node", "bridge"], 30)
        deliver = Mock(return_value={"complete": True, "notification_id": "notice"})
        for attempt in range(3):
            current = self.store.get("conversation", row["id"])
            self.store.update("conversation", current, retry_at=0)
            conversation.process_one(self.store, self.root, generate, deliver, notify=notify)
        saved = self.store.get("conversation", row["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["data"]["original_text"], text)
        self.assertEqual(self.state, before)
        retry = next(body for stage, body in notices if stage == "retry:1")
        self.assertIn("30", retry)
        self.assertIn("2/3", retry)
        self.assertIn("No lo reenvíes", retry)
        blocked = saved["data"]["output"]
        self.assertIn("3", blocked)
        self.assertIn("30", blocked)
        self.assertNotIn("reformulado", blocked)
        self.assertNotIn("1800", blocked)
        self.assertFalse(agent_runner._routing.get())

    def test_status_delivery_is_deduplicated_and_does_not_share_answer_receipts(self):
        row = conversation.receive(self.store, self.state, self.root, "message", "/btw Diagnostica.")
        backend = Mock(__name__="slack")
        backend.attachment_limits.return_value = {"file": 10000, "mime": False}
        sent = []
        def send(envelope, receipts, confirm):
            if receipts.get("body", {}).get("status") != "confirmed":
                sent.append(envelope["body"])
                confirm("body", {"status": "confirmed"})
            return "C:1"
        backend.send_envelope.side_effect = send
        for _ in range(2):
            conversation.delivery(self.store, row, self.root, self.root, backend, notice="received")
        self.assertEqual(len(sent), 1)
        self.assertIn("Recibí", sent[0])
        row = self.store.update("conversation", row, status="generated", output="Actual answer")
        conversation.delivery(self.store, row, self.root, self.root, backend)
        self.assertEqual(sent[-1], "Actual answer")
        self.assertEqual(len(self.store.list("delivery_receipt", "task")), 2)

    def test_routing_uses_low_effort_short_deadline_and_preserves_working_settings(self):
        existing = {"provider": {"azure": {"options": {"baseURL": "https://provider.example"},
                    "models": {"model": {"options": {"textVerbosity": "low"}}}}}}
        process = subprocess.CompletedProcess([], 0,
            '{"type":"text","sessionID":"chat","part":{"text":"{}"}}\n', "")
        for transport in ("cli", "http"):
            with self.subTest(transport=transport), patch.object(agent_runner.config, "AGENT", "opencode"), \
                 patch.object(agent_runner.config, "OPENCODE_MODEL", "azure/model"), \
                 patch.object(agent_runner.config, "OPENCODE_EFFORT", "high"), \
                 patch.object(agent_runner.config, "OPENCODE_ROLE_EFFORTS", {}), \
                 patch.object(agent_runner.config, "OPENCODE_TRANSPORT", transport), \
                 patch.object(agent_runner.config, "DATA_DIR", self.root), \
                 patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": json.dumps(existing)}), \
                 patch.object(agent_runner, "_run_streaming", return_value=process) as run:
                with agent_runner.routing():
                    agent_runner.converse("Classify this message.")
                routed = run.call_args
                inline = json.loads(routed.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
                self.assertEqual(inline["provider"]["azure"]["models"]["model"]["options"]["reasoningEffort"], "low")
                self.assertEqual(inline["provider"]["azure"]["options"], existing["provider"]["azure"]["options"])
                self.assertEqual(inline["permission"], {"*": "deny"})
                self.assertEqual(routed.kwargs["timeout"], 60)
                agent_runner._opencode("Working turn")
                working = json.loads(run.call_args.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
                self.assertEqual(working["provider"]["azure"]["models"]["model"]["options"]["reasoningEffort"], "high")
                self.assertEqual(agent_runner.config.OPENCODE_EFFORT, "high")

    def test_claude_routing_effort_does_not_change_coding_effort(self):
        process = subprocess.CompletedProcess([], 0, '{"session_id":"chat","result":"{}"}', "")
        with patch.object(agent_runner.config, "AGENT", "claude"), \
             patch.object(agent_runner.config, "CLAUDE_EFFORT", "high"), \
             patch.object(agent_runner.claude_runner, "_run", return_value=process) as run:
            with agent_runner.routing():
                agent_runner.converse("Classify")
            argv = run.call_args.args[0]
            self.assertEqual(argv[argv.index("--effort") + 1], "low")
            self.assertEqual(agent_runner.config.CLAUDE_EFFORT, "high")

    def test_streaming_timeout_reports_effective_parent_budget_and_elapsed_time(self):
        with operations.budget(.15):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                agent_runner._run_streaming([sys.executable, "-c", "import time; print('partial',flush=True); time.sleep(5)"],
                    cwd=None, env=dict(os.environ), timeout=1800, on_line=lambda _: None)
        self.assertLessEqual(caught.exception.timeout, .15)
        self.assertIn("partial", caught.exception.output)
        self.assertIn("elapsed", caught.exception.stderr)
        self.assertGreater(caught.exception.elapsed, 0)

    def test_classifying_record_survives_restart_and_still_blocks_publication(self):
        row = conversation.receive(self.store, self.state, self.root, "message", "/btw Corrige el harness.")
        self.store.update("conversation", row, status="classifying", processing_stage="classification")
        reopened = ExecutionStore(self.root / "execution.sqlite")
        self.assertTrue(conversation.delivery_blockers(reopened, self.state, self.root))
        generate = Mock(return_value=SimpleNamespace(output=json.dumps({
            "intent": "change_request", "resolves_pending_question": False,
            "inputs": [{"kind": "change", "text": "Corrige el harness."}]})))
        self.assertTrue(conversation.process_one(reopened, self.root, generate,
            Mock(return_value={"complete": True, "notification_id": "notice"})))
        conversation.transfer_changes(reopened, self.state, self.root)
        self.assertEqual(len(reopened.list("feedback", "task")), 1)

    def test_status_exposes_message_retry_instead_of_asking_user_to_repeat_decision(self):
        row = conversation.receive(self.store, self.state, self.root, "message", "/btw Diagnostica sin repetir E2E.")
        self.store.update("conversation", row, status="retry", processing_stage="classification",
                          attempts=1, last_error="execution deadline exceeded (30s)")
        with patch.object(main.phase_checkpoint, "store", return_value=self.store), \
             patch.object(main.config, "REPO_PATH", self.root), \
             patch.object(main.activity, "snapshot", return_value={"active": False}), \
             patch.object(main, "_ping_body") as ping, patch.object(main.agent_runner, "run") as provider:
            text = main._short_status(self.state, {}, 100)
        self.assertIn("reintento automático 2/3", text)
        self.assertIn("30s", text)
        self.assertIn("No lo reenvíes", text)
        ping.assert_not_called()
        provider.assert_not_called()

    def test_routing_context_is_thread_local_and_cannot_lower_concurrent_coding_effort(self):
        efforts = []
        with patch.object(agent_runner.config, "OPENCODE_EFFORT", "high"), \
             patch.object(agent_runner.config, "OPENCODE_ROLE_EFFORTS", {}):
            with agent_runner.routing():
                thread = threading.Thread(target=lambda: efforts.append(agent_runner._opencode_effort()))
                thread.start()
                thread.join(2)
                self.assertEqual(agent_runner._opencode_effort(), "low")
            self.assertEqual(agent_runner._opencode_effort(), "high")
        self.assertEqual(efforts, ["high"])

    def test_routing_preserves_provider_defaults_when_reasoning_was_not_configured(self):
        with patch.object(agent_runner.config, "OPENCODE_EFFORT", ""), \
             patch.object(agent_runner.config, "OPENCODE_ROLE_EFFORTS", {}), agent_runner.routing():
            self.assertEqual(agent_runner._opencode_effort(), "")

    def test_classifier_redacts_before_clipping_long_credentials(self):
        secret = "private-example-value-that-crosses-the-question-boundary"
        self.state["pending_question"] = "x" * 1180 + secret
        row = conversation.receive(self.store, self.state, self.root, "message", "Explain")
        with patch("redaction.redact", side_effect=lambda text: text.replace(secret, "[REDACTED]")):
            prompt = conversation.classification_prompt(row)
        self.assertNotIn("private-example", prompt)
