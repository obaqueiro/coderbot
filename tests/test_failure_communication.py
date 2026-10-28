"""Failure notices must support a real recovery decision, not just name a phase."""
import json
import tempfile
import unittest
import sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

import check_plan
import diagnostics
import handoffs
with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main
import parallel_conversation
from execution_store import ExecutionStore
from agent_errors import AgentContentFilterError


class FailureCommunication(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.bad = 'CHECK_PLAN: {"version":1,"checks":[{"id":"e2e-compartido","argv":["bash","e2e/run.sh","zip.spec.ts"],"timeout_millis":900}]}'
        self.state = {"state": "VERIFYING", "slug": "zip", "item": "ZIP feature", "execution_task_id": "task",
            "thread_id": "thread", "reported_check_plan_error": check_plan.reported_error(self.bad),
            "rejected_check_plan": self.bad, "check_plan_repair_round": 3, "check_plan_repair_revision": 1,
            "final_check_report": {"status": "indeterminate", "stage": "focused_e2e", "snapshot": "tested-source",
                "checks": [{"check": "zip-unit", "status": "pass", "gate": {"status": "pass"}, "report": "unit.log"}]}}
        self.detail = "The agent could not produce a valid check plan; no malformed plan was executed."

    def test_stopped_notice_explains_cause_impact_recovery_and_one_decision_in_both_modes_and_channels(self):
        for mode in ("repo", "workspace"):
            for channel in ("email", "slack"):
                with self.subTest(mode=mode, channel=channel):
                    state = json.loads(json.dumps(self.state))
                    with patch.object(main.config, "REPO_PATH", self.root), patch.object(main.config, "DATA_DIR", self.data), \
                         patch.object(main.config, "TARGET_MODE", mode), patch.object(main.config, "COMM_CHANNEL", channel), \
                         patch.object(main.gmail_client, "deliver", return_value={"thread_id": "thread", "complete": True}) as deliver, \
                         patch.object(main, "trail"), patch.object(main, "_note_contact"):
                        main._enter_stuck(state, "VERIFYING", self.detail)
                    body = deliver.call_args.args[2]
                    self.assertIn("e2e-compartido", body)
                    self.assertIn("timeout_millis", body)
                    self.assertIn("not a failed product test", body)
                    self.assertIn("3 recorded", body)
                    self.assertIn("recorded", body.lower())
                    self.assertIn("focused_e2e", body)
                    self.assertIn("preserv", body.lower())
                    self.assertIn("gates", body)
                    self.assertEqual(body.count("1. "), 1)
                    self.assertEqual(body.count("2. "), 1)
                    self.assertEqual(body.count("3. "), 1)
                    self.assertNotIn("Reply 'retry'", body)
                    self.assertNotIn("'abort'", body)
                    self.assertNotIn("'complete'", body)
                    self.assertEqual(handoffs.selected_reply(state, "1")["reply"], "retry")
                    self.assertTrue(handoffs.selected_reply(state, "3")["wait"])

    def test_diagnostic_contains_rejected_payload_attempt_and_recorded_verification(self):
        store = ExecutionStore(self.data / "execution.sqlite")
        path = diagnostics.report(store, self.state, self.root, self.data, "VERIFYING", detail=self.detail)
        content = path.read_text()
        self.assertIn("- Attempt: 3", content)
        self.assertIn("timeout_millis", content)
        self.assertIn("e2e-compartido", content)
        self.assertIn("## Rejected CHECK_PLAN", content)
        self.assertIn("## Recorded verification", content)
        self.assertIn("tested-source", content)
        self.assertIn("unit.log", content)
        self.state["rejected_check_plan"] = self.bad.replace("900", "901")
        newer = diagnostics.report(store, self.state, self.root, self.data, "VERIFYING", detail=self.detail)
        self.assertNotEqual(path, newer)
        self.assertEqual(path.read_text(), content)

    def test_legacy_failure_recovers_last_plan_from_durable_checkpoint_for_diagnostics_and_followup(self):
        store = ExecutionStore(self.data / "execution.sqlite")
        self.state.pop("rejected_check_plan")
        store.put("checkpoint", task_id="task", status="consumed", data={"phase": "VERIFYING", "output": self.bad})
        path = diagnostics.report(store, self.state, self.root, self.data, "VERIFYING", detail=self.detail)
        self.assertIn("timeout_millis", path.read_text())
        self.state.update(state="WAIT_STUCK", stuck_return="VERIFYING", stuck_error=self.detail,
                          stuck_diagnostic=str(path))
        view = parallel_conversation.snapshot(store, self.state, self.root)
        self.assertIn("timeout_millis", json.dumps(view["failure_context"]))
        self.assertIn("checkpoint", json.dumps(view["failure_context"]).lower())

    def test_workspace_option_mapping_does_not_resume_on_wait_or_guess_missing_guidance(self):
        state = {**self.state, "state": "WAIT_STUCK", "stuck_return": "VERIFYING"}
        handoffs.remember_decision(state, "stuck in VERIFYING")
        with patch.object(main.config, "TARGET_MODE", "workspace"), patch.object(main, "_handle_reply") as handle, \
             patch.object(main, "email"):
            main._dispatch_wait_reply(state, "wait-message", "3")
            handle.assert_not_called()
            main._dispatch_wait_reply(state, "guidance-message", "2")
            handle.assert_not_called()
            main._dispatch_wait_reply(state, "retry-message", "1")
        handle.assert_called_once_with(state, "retry")

    def test_filtered_user_reply_is_visible_redacted_and_frozen_for_followup(self):
        text = 'Use this input: {"password":"ghp_exampleCredential123456789"}\nKeep ZIP scope.'
        for channel in ("email", "slack"):
            for mode in ("repo", "workspace"):
                with self.subTest(channel=channel, mode=mode):
                    state = {"state": "WAIT_REPLY", "return_state": "VERIFYING", "slug": "zip",
                             "item": "ZIP", "execution_task_id": "task", "task_language": "English"}
                    error = AgentContentFilterError("provider blocked response", SimpleNamespace(
                        args=["node", "bridge"], returncode=1, stdout="", stderr=""), request=text)
                    with patch.object(main.config, "REPO_PATH", self.root), patch.object(main.config, "DATA_DIR", self.data), \
                         patch.object(main.config, "TARGET_MODE", mode), patch.object(main.config, "COMM_CHANNEL", channel), \
                         patch.object(main.gmail_client, "deliver", return_value={"thread_id": "thread"}) as deliver, \
                         patch.object(main, "trail"), patch.object(main, "_note_contact"), patch.object(main, "save_state"), \
                         patch.object(main.agent_runner, "run") as run:
                        main._content_filter_stop(state, "WAIT_REPLY", error=error, user_text=text)
                    run.assert_not_called()
                    body = deliver.call_args.args[2]
                    self.assertIn("Source: your reply", body)
                    self.assertIn("> Keep ZIP scope.", body)
                    self.assertIn("[REDACTED]", body)
                    self.assertNotIn("ghp_exampleCredential123456789", body)
                    self.assertIn("did not identify", body)
                    self.assertIn("No blocked response text", body)
                    self.assertEqual(state["state"], "WAIT_REPLY")
                    self.assertEqual(state["return_state"], "VERIFYING")
                    path = Path(state["stuck_diagnostic"])
                    self.assertNotIn("ghp_exampleCredential123456789", path.read_text())
                    store = ExecutionStore(self.data / "execution.sqlite")
                    view = parallel_conversation.snapshot(store, state, self.root)
                    self.assertIn("Keep ZIP scope", json.dumps(view["failure_context"]))
                    frozen = path.read_text()
                    state["provider_block"]["request"] = "another request"
                    self.assertEqual(path.read_text(), frozen)

    def test_filtered_internal_turn_quotes_request_without_asking_user_to_rephrase_their_message(self):
        state = {"state": "VERIFYING", "slug": "zip", "item": "ZIP", "execution_task_id": "task",
                 "task_language": "Spanish"}
        request = "Diagnose recorded audit results. " + "context " * 400
        error = AgentContentFilterError("provider blocked response", SimpleNamespace(
            args=["node", "bridge"], returncode=1, stdout="", stderr=""), request=request,
            partial_response="Read-only diagnosis started.", provider_detail="ContentFilterError")
        with patch.object(main.config, "REPO_PATH", self.root), patch.object(main.config, "DATA_DIR", self.data), \
             patch.object(main.gmail_client, "deliver", return_value={"thread_id": "thread"}) as deliver, \
             patch.object(main, "trail"), patch.object(main, "_note_contact"), patch.object(main, "save_state"), \
             patch.object(main.agent_runner, "run") as run:
            main._content_filter_stop(state, "VERIFYING", error=error)
        run.assert_not_called()
        body = deliver.call_args.args[2]
        self.assertIn("solicitud interna del bot durante VERIFYING", body)
        self.assertIn("Diagnose recorded audit results.", body)
        self.assertIn("extracto", body)
        self.assertIn("Read-only diagnosis started.", body)
        self.assertNotIn("Cambia el texto", body)
        self.assertNotIn("(Recommended)", body)
        self.assertIn("Decisión necesaria", body)
        self.assertEqual(state["state"], "WAIT_STUCK")
        self.assertEqual(state["stuck_return"], "VERIFYING")
        self.assertIn(request, Path(state["stuck_diagnostic"]).read_text())

    def test_rejected_payload_is_redacted_before_state_and_diagnostic_serialization(self):
        secret = "multiline-secret-value\nwith-second-line"
        with patch.dict("os.environ", {"INCIDENT_API_KEY": secret}):
            output = self.bad.replace('"timeout_millis":900', '"timeout_millis":900,"note":' + json.dumps(secret))
            state = {"item": "task"}
            check_plan.remember(state, output)
            self.assertNotIn(secret, state["rejected_check_plan"])
            store = ExecutionStore(self.data / "execution.sqlite")
            path = diagnostics.report(store, state, self.root, self.data, "VERIFYING", detail="Rejected plan")
        self.assertNotIn("multiline-secret-value", path.read_text())

    def test_lateral_provider_filter_quotes_message_without_changing_task_or_retrying(self):
        store = ExecutionStore(self.data / "execution.sqlite")
        state = {"state": "WAIT_STUCK", "item": "ZIP", "slug": "zip", "execution_task_id": "task",
                 "stuck_return": "VERIFYING", "stuck_error": "audit unresolved", "task_language": "English"}
        message = "/btw Explain the ZIP audit and preserve its evidence."
        row = parallel_conversation.receive(store, state, self.root, "message", message)
        error = AgentContentFilterError("blocked", SimpleNamespace(args=[], returncode=1, stdout="", stderr=""),
                                       request="Explain the recorded audit.")
        deliver = Mock(return_value={"complete": True, "notification_id": "notice"})
        generate = Mock(side_effect=error)
        self.assertTrue(parallel_conversation.process_one(store, self.root, generate, deliver))
        saved = store.get("conversation", row["id"])
        self.assertIn(message, saved["data"]["output"])
        self.assertIn("Explain the recorded audit.", saved["data"]["output"])
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(state["stuck_error"], "audit unresolved")
        self.assertFalse(parallel_conversation.process_one(store, self.root, generate, deliver))
        generate.assert_called_once()
