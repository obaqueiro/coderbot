import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import sys
from unittest.mock import Mock, patch

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
    import main
import feedback
import replanning
import verification_ledger
import review_intent
from execution_store import ExecutionStore
from agent_errors import AgentContentFilterError


class FeedbackRecoveryTests(unittest.TestCase):
    def test_exact_logging_reply_finishes_exploration_then_creates_initial_proposal(self):
        message = ("3. Dejemos el registro como está actualmente usando LiteLLM, con registro asíncrono. "
                   "No exigir configuración de BDD válida (como está ahorita), "
                   "solo enviar info cuando el LiteLLM esté configurado.")
        state = {"item": "task", "state": "WAIT_REPLY", "return_state": "EXPLORING",
                 "slug": "task", "session_id": "session", "execution_task_id": "durable",
                 "pending_question": "¿Qué política? 1. Durable 2. Cola 3. Registro nativo"}
        main.handoffs.remember_decision(state, "question during EXPLORING", state["pending_question"])
        result = SimpleNamespace(output="", session_id="session", attachments=[], question=None)
        with patch.object(main.agent_runner, "run", return_value=SimpleNamespace(output='{"action":"answer"}')) as run, \
             patch.object(main.agent_runner, "resume", return_value=result) as resume, \
             patch.object(main, "handle_result", return_value=False), \
             patch.object(main, "_undo_premature_work"), patch.object(main, "announce_milestone"), \
             patch.object(main, "trail"), patch.object(main.feedback, "receive") as receive:
            main._dispatch_wait_reply(state, "message", message)
        self.assertEqual(state["state"], "PROPOSING")
        self.assertNotIn("replan", state)
        receive.assert_not_called()
        self.assertIn(message, resume.call_args.args[1])
        self.assertIn("do NOT modify files", resume.call_args.args[1])
        self.assertIn("Displayed decision", run.call_args.args[0])

    def test_exploration_answer_with_adjusted_option_never_investigates_approved_artifacts(self):
        message = ("3. Dejemos el registro como está actualmente usando LiteLLM, con registro asíncrono. "
                   "No exigir configuración de BDD válida (como está ahorita), "
                   "solo enviar info cuando el LiteLLM esté configurado.")
        for phase in ("EXPLORING", "PROPOSING"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root:
                database = ExecutionStore(Path(root) / "db")
                state = {"item": "task", "state": "WAIT_REPLY", "return_state": phase,
                         "pending_question": "Which logging policy?", "slug": "task"}
                feedback.receive(database, state, root, "message", message)
                with patch.object(main.phase_checkpoint, "store", return_value=database), \
                     patch.object(main.config, "REPO_PATH", Path(root)), \
                     patch.object(main, "_handle_reply") as handle, \
                     patch.object(main.agent_runner, "run") as run:
                    self.assertTrue(main._dispatch_feedback(state))
                handle.assert_called_once_with(state, message)
                run.assert_not_called()
                self.assertEqual(state["return_state"], phase)
                self.assertNotIn("replan", state)
                self.assertEqual(database.list("feedback", database.task_identity(state, root))[0]["status"], "complete")

    def test_active_planning_feedback_resumes_its_phase_after_restart(self):
        for phase in ("EXPLORING", "PROPOSING"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root:
                database = ExecutionStore(Path(root) / "db")
                state = {"item": "task", "state": phase, "slug": "task", "session_id": "session"}
                message = "Mantén la BDD opcional; no implementes todavía."
                feedback.receive(database, state, root, "message", message)
                state = json.loads(json.dumps(state))
                with patch.object(main.phase_checkpoint, "store", return_value=database), \
                     patch.object(main.config, "REPO_PATH", Path(root)), \
                     patch.object(main, "save_state"), \
                     patch.object(main, "do_question_reply") as resume, \
                     patch.object(main.agent_runner, "run") as run:
                    self.assertTrue(main._dispatch_feedback(state))
                resume.assert_called_once_with(state, message)
                run.assert_not_called()
                self.assertEqual(state["return_state"], phase)
                self.assertNotIn("replan", state)

    def test_filtered_stuck_feedback_is_not_retried_and_new_text_can_resume(self):
        with tempfile.TemporaryDirectory() as root:
            database = ExecutionStore(Path(root) / "db")
            state = {"item": "task", "state": "WAIT_STUCK", "stuck_return": "VERIFYING",
                     "stuck_error": "original blocker", "task_language": "Spanish"}
            feedback.receive(database, state, root, "blocked", "old message")
            error = AgentContentFilterError("blocked", SimpleNamespace(
                args=[], returncode=1, stdout="", stderr=""))
            with patch.object(main.phase_checkpoint, "store", return_value=database), \
                 patch.object(main.config, "REPO_PATH", Path(root)), \
                 patch.object(main.agent_runner, "run", side_effect=[
                     SimpleNamespace(output='{"kind":"answer"}'), error]) as run, \
                 patch.object(main, "save_state"), patch.object(main, "email") as notify:
                self.assertTrue(main._dispatch_feedback(state))
                self.assertFalse(main._dispatch_feedback(state))
                self.assertEqual(run.call_count, 2)
                self.assertIn("Cambia el texto", notify.call_args.args[2])
                self.assertTrue(notify.call_args.kwargs["localized"])
            self.assertEqual(state["state"], "WAIT_STUCK")
            self.assertEqual(state["stuck_return"], "VERIFYING")
            self.assertEqual(state["stuck_error"], "original blocker")
            rows = database.list("feedback", database.task_identity(state, root))
            self.assertEqual(rows[0]["status"], "blocked")
            feedback.receive(database, state, root, "revised", "retry")
            with patch.object(main.phase_checkpoint, "store", return_value=database), \
                 patch.object(main.config, "REPO_PATH", Path(root)), \
                 patch.object(main.agent_runner, "run", side_effect=[
                     SimpleNamespace(output='{"kind":"answer"}'),
                     SimpleNamespace(output='{"action":"retry"}')]):
                self.assertTrue(main._dispatch_feedback(state))
            self.assertEqual(state["state"], "VERIFYING")

    def test_filtered_direct_reply_is_consumed_without_losing_question(self):
        state = {"item": "task", "state": "WAIT_REPLY", "return_state": "IMPLEMENTING",
                 "pending_question": "Which behavior?", "thread_id": "thread", "task_language": "Spanish"}
        error = AgentContentFilterError("blocked", SimpleNamespace(
            args=[], returncode=1, stdout="", stderr=""))
        with patch.object(main.gmail_client, "poll_reply", return_value=("m1", "old message")), \
             patch.object(main.gmail_client, "foreign_command", return_value=None), \
             patch.object(main.gmail_client, "mark_processed") as processed, \
             patch.object(main.agent_runner, "run", side_effect=error), \
             patch.object(main, "_note_contact"), patch.object(main, "save_state"), \
             patch.object(main, "email"):
            main.handle_wait(state)
        processed.assert_called_once_with("m1")
        self.assertEqual(state["state"], "WAIT_REPLY")
        self.assertEqual(state["pending_question"], "Which behavior?")
        self.assertEqual(state["return_state"], "IMPLEMENTING")

    def test_filtered_working_turn_pauses_without_using_llm_for_notice(self):
        state = {"item": "task", "state": "IMPLEMENTING", "session_id": "session",
                 "task_language": "Spanish", "technical_retry": {"attempts": 3}}
        with patch.object(main.agent_runner, "run") as run, \
              patch.object(main.gmail_client, "send", return_value="thread"), \
              patch.object(main.gmail_client, "deliver", return_value={"thread_id": "thread"}), \
              patch.object(main, "trail"), patch.object(main, "_note_contact"), \
             patch.object(main, "save_state"):
            main._content_filter_stop(state, "IMPLEMENTING")
        run.assert_not_called()
        self.assertEqual(state["state"], "WAIT_STUCK")
        self.assertEqual(state["stuck_return"], "IMPLEMENTING")
        self.assertEqual(state["session_id"], "session")
        self.assertNotIn("technical_retry", state)

    def test_loop_content_filter_does_not_consume_failure_budget_or_retry_phase(self):
        state = {"item": "task", "state": "IMPLEMENTING", "task_language": "Spanish"}
        error = AgentContentFilterError("blocked", SimpleNamespace(
            args=[], returncode=1, stdout="", stderr=""))
        with patch.object(main, "load_state", return_value=state), \
             patch.object(main, "_announce_state"), patch.object(main, "check_commands", return_value=False), \
             patch.object(main.task_records, "apply_contacts"), \
             patch.object(main.gmail_client, "retry_deliveries"), \
             patch.object(main, "_dispatch_feedback", return_value=False), \
             patch.dict(main.PHASES, {"IMPLEMENTING": Mock(side_effect=error)}), \
             patch.object(main, "email"), patch.object(main, "save_state"), \
             patch.object(main, "_escalate") as escalate, \
             patch.object(main.gmail_client, "wait", side_effect=SystemExit("paused")):
            with self.assertRaisesRegex(SystemExit, "paused"):
                main._run_loop()
            main.PHASES["IMPLEMENTING"].assert_called_once_with(state)
        escalate.assert_not_called()
        self.assertEqual(state["state"], "WAIT_STUCK")
        self.assertEqual(state["failures"], {})

    def test_dropdown_feedback_replans_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as root:
            database = ExecutionStore(Path(root) / "db")
            state = {"item": "models", "state": "ADDRESS_PR_THREADS", "slug": "models",
                     "session_id": "session", "branch": "feature", "pr_url": "https://example/pr/1"}
            message = "No veo un drop down donde se pueda elegir el modelo. Eso está implementado? Eso es parte de feature"
            feedback.receive(database, state, root, "message1", message)
            value = {"action": "replan", "answer": "The selector is missing.",
                     "reason": "Session/provider design must change.", "references": "design.md; toolbar.tsx"}
            with patch.object(main.phase_checkpoint, "store", return_value=database), \
                 patch.object(main.config, "REPO_PATH", Path(root)), \
                 patch.object(main.agent_runner, "run", return_value=SimpleNamespace(output=json.dumps(value))), \
                 patch.object(main, "email"):
                self.assertTrue(main._dispatch_feedback(state))
            self.assertEqual(state["state"], "REPLANNING")
            self.assertEqual(state["replan"]["feedback"], message)
            self.assertEqual(state["pr_url"], "https://example/pr/1")
            restored = json.loads(json.dumps(state))
            self.assertEqual(restored["replan"], state["replan"])
            self.assertEqual(database.list("feedback", database.task_identity(state, root))[0]["status"], "planning")

    def test_complementary_replan_preserves_archive_and_pr(self):
        state = {"state": "WAIT_MERGE", "slug": "models", "archive_path": "openspec/changes/archive/old",
                 "pr_url": "https://example/pr/1", "approved_proposal": "old"}
        row = {"id": "message1", "data": {"text": "Need a selector", "assessment": {}}}
        replanning.begin(state, row)
        prompt = replanning.prompt(state, Path("/repo"))
        self.assertIn("openspec new change", prompt)
        self.assertIn("do not rewrite", prompt)
        self.assertEqual(state["replan"]["original_archive"], "openspec/changes/archive/old")
        self.assertNotIn("approved_proposal", state)

    def test_coverage_and_review_provenance(self):
        inventory = [{"id": "model", "title": "Model selector"}, {"id": "effort", "title": "Effort"}]
        report = {"snapshot": "head", "requirements": [{"id": "effort", "status": "implemented",
                   "implementation": "toolbar", "verification": "tests"}]}
        self.assertEqual(verification_ledger.validate_coverage(inventory, report, "head"), ["Model selector"])
        self.assertEqual(review_intent.provenance({"author": "custom-reviewer", "body": "ai-review-inline"}), "automated")
        self.assertEqual(review_intent.provenance({"author": "unknown"}), "unknown")

    def test_transient_review_failure_does_not_consume_round(self):
        state = {"item": "models", "state": "ADDRESS_PR_THREADS", "session_id": "session", "branch": "feature",
                 "pr_threads": [], "pr_thread_round": 2}
        with patch.object(main.agent_runner, "resume", side_effect=ConnectionError("fetch failed")), \
             patch.object(main, "_render_review_threads", return_value="threads"):
            with self.assertRaises(ConnectionError):
                main.do_address_pr_threads(state)
        self.assertEqual(state["pr_thread_round"], 2)

    def test_recovery_question_preserves_original_blocker(self):
        state = {"item": "task", "state": "WAIT_STUCK", "stuck_return": "RECOVERING",
                 "stuck_error": "dirty index", "session_id": "session", "recovery_id": "incident"}
        result = SimpleNamespace(session_id="session2", output="Question", question="Which behavior?", attachments=[])
        with patch.object(main.agent_runner, "run", return_value=SimpleNamespace(output='{"action":"instructions"}')), \
             patch.object(main.agent_runner, "resume", return_value=result), \
             patch.object(main, "email"), patch.object(main, "trail"):
            main.do_stuck_reply(state, "Inspect the change")
        self.assertEqual(state["state"], "WAIT_REPLY")
        self.assertEqual(state["return_state"], "RECOVERING")
        self.assertEqual(state["recovery_id"], "incident")
        self.assertEqual(state["stuck_error"], "dirty index")

    def test_revised_proposal_edits_never_undo_implementation(self):
        state = {"item": "task", "state": "WAIT_APPROVAL", "slug": "revision",
                 "session_id": "session", "replan": {"feedback": "selector"}}
        with patch.object(main.agent_runner, "run", return_value=SimpleNamespace(output='{"action":"changes","feedback":"revise"}')), \
             patch.object(main.agent_runner, "resume", return_value=SimpleNamespace(session_id="session", output="revised", question=None)), \
             patch.object(main, "_send_proposal_review"), patch.object(main, "_undo_premature_work") as undo, \
             patch.object(main, "handle_result", return_value=False):
            main.do_approval_reply(state, "Revise the API behavior")
        undo.assert_not_called()

    def test_legacy_coverage_does_not_block_delivery_before_evidence_work(self):
        with tempfile.TemporaryDirectory() as root:
            store = ExecutionStore(Path(root) / "db")
            state = {"item": "task", "state": "OPEN_PR", "slug": "models", "session_id": "session",
                     "approved_proposal": "approved", "pr_url": "https://example/pr/1",
                     "coverage_report": {"requirements": [{"status": "missing"}]}}
            with patch.object(main.phase_checkpoint, "store", return_value=store), \
                 patch.object(main.config, "REPO_PATH", Path(root)), \
                 patch.object(main, "content_snapshot", return_value="head"), \
                 patch.object(main.agent_runner, "run") as audit, \
                 patch.object(main, "save_state"), \
                 patch.object(main.evidence, "record_evidence", side_effect=RuntimeError("evidence reached")) as record:
                with self.assertRaisesRegex(RuntimeError, "evidence reached"):
                    main.finalize_pr(state)
            self.assertEqual(state["state"], "OPEN_PR")
            record.assert_called_once()
            audit.assert_not_called()

    def test_stale_merge_cannot_bypass_revision(self):
        state = {"item": "task", "state": "WAIT_MERGE", "replan": {"feedback": "selector"},
                 "pr_url": "https://example/pr/1"}
        with patch.object(main.agent_runner, "run", return_value=SimpleNamespace(output='{"action":"merge","force":true}')), \
             patch.object(main, "email") as notify, patch.object(main, "_pr_merge_state") as merge:
            main.do_merge_reply(state, "merge anyway")
        merge.assert_not_called()
        self.assertIn("requires approval", notify.call_args.args[2])

    def test_failed_feedback_delivery_keeps_pending_record(self):
        with tempfile.TemporaryDirectory() as root:
            store = ExecutionStore(Path(root) / "db")
            state = {"item": "task", "state": "ADDRESS_PR_THREADS", "slug": "task"}
            feedback.receive(store, state, root, "m1", "Where is the selector?")
            value = {"action": "answer", "answer": "It is in the toolbar.", "reason": "Implemented.", "references": "toolbar.tsx"}
            with patch.object(main.phase_checkpoint, "store", return_value=store), \
                 patch.object(main.config, "REPO_PATH", Path(root)), \
                 patch.object(main.agent_runner, "run", return_value=SimpleNamespace(output=json.dumps(value))), \
                 patch.object(main, "email", side_effect=ConnectionError("offline")):
                with self.assertRaises(ConnectionError):
                    main._dispatch_feedback(state)
            self.assertEqual(len(feedback.pending(store, state, root)), 1)
            self.assertFalse(feedback.pending(store, state, root)[0]["data"].get("assessment_delivered"))

    def test_active_turn_receipt_is_durable_and_keeps_control_commands(self):
        with tempfile.TemporaryDirectory() as root:
            database = ExecutionStore(Path(root) / "db")
            state = {"item": "task", "state": "IMPLEMENTING", "thread_id": "C:1"}
            with patch.object(main.phase_checkpoint, "store", return_value=database), \
                 patch.object(main.config, "REPO_PATH", Path(root)), \
                 patch.object(main.gmail_client, "poll_reply", return_value=("m1", "Missing selector")), \
                 patch.object(main.gmail_client, "foreign_command", return_value=None), \
                 patch.object(main.gmail_client, "send") as ack, \
                 patch.object(main.gmail_client, "mark_processed") as processed:
                main._receive_feedback(state)
                main._receive_feedback(state)
            self.assertEqual(len(database.list("conversation", database.task_identity(state, root))), 1)
            self.assertEqual(len(feedback.pending(database, state, root)), 0)
            ack.assert_not_called()  # The lateral worker answers after classification.
            with patch.object(main.gmail_client, "poll_reply", return_value=("m2", "ABORT")), \
                 patch.object(main.gmail_client, "mark_processed") as processed:
                self.assertFalse(main._receive_feedback(state))
                processed.assert_not_called()
