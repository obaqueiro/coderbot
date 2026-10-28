"""Decision cards, recorded check outcomes and evidence/feedback handoffs."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import sys
import json
from types import SimpleNamespace

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(),
                              "gmail_client": Mock()}):
    import main
import handoffs


class HumanHandoffs(unittest.TestCase):
    def test_binary_selection_is_bound_to_active_task_and_phase(self):
        state = {"state": "WAIT_REPLY", "return_state": "VERIFYING", "slug": "task",
                 "verification_blocker": {"kind": "unfinished or unclassified tasks"}}
        card = handoffs.remember_decision(state, "Verification blocked")
        self.assertIn("Do you authorize", card)
        self.assertIn("1. Yes", card)
        self.assertIn("2. No", card)
        self.assertIn("Recommended", card)
        self.assertIn("approved spec", handoffs.selected_reply(state, "Sí")["reply"])
        self.assertTrue(handoffs.selected_reply(state, "NO")["wait"])
        self.assertEqual(handoffs.selected_reply(state, "yes, but change the scope")["reply"],
                         "yes, but change the scope")
        reloaded = json.loads(json.dumps(state))
        self.assertIn("explicitly authorize", handoffs.selected_reply(reloaded, "1")["reply"])
        reloaded["return_state"] = "PROPOSING"
        self.assertIsNone(handoffs.selected_reply(reloaded, "1"))
        reloaded = dict(state, slug="other")
        self.assertIsNone(handoffs.selected_reply(reloaded, "1"))

    def test_all_controller_decisions_have_feasible_choices(self):
        phases = ["proposal for review", "revised proposal", "PR ready for review", "PR updated",
                  "merge blocked", "merge failed — needs your help", "stuck in VERIFYING",
                  "e2e suite stuck — needs your help", "OpenSpec archival stuck - needs your help",
                  "blocked: dirty working tree", "service unavailable", "content blocked"]
        for phase in phases:
            with self.subTest(phase=phase):
                value = handoffs.decision({"state": "WAIT_STUCK"}, phase)
                self.assertIsNotNone(value)
                self.assertIn(len(value["options"]), (2, 3, 4))
                self.assertTrue(any(option["wait"] for option in value["options"]))
                self.assertTrue(all(option["reply"] not in ("abort", "complete") for option in value["options"]))

    def test_numeric_alternative_requires_details_and_never_treats_yes_as_merge(self):
        state = {"state": "WAIT_MERGE", "slug": "task"}
        handoffs.remember_decision(state, "PR ready for review")
        self.assertIn("error", handoffs.selected_reply(state, "yes"))
        self.assertIn("error", handoffs.selected_reply(state, "1"))
        self.assertEqual(handoffs.selected_reply(state, "1: Add cancellation")["reply"],
                         "1: Add cancellation")
        self.assertEqual(handoffs.selected_reply(state, "2")["reply"], "merge")
        self.assertTrue(handoffs.selected_reply(state, "3")["wait"])
        self.assertIn("error", handoffs.selected_reply(state, "4"))

    def test_agent_domain_options_and_fallback_remain_readable(self):
        state = {"state": "IMPLEMENTING", "slug": "task"}
        question = "Which storage boundary?\n1. Separate DB (Recommended) — adds cost\n2. Shared DB — reduces isolation\n3. Keep waiting"
        card = handoffs.remember_decision(state, "question during IMPLEMENTING", question)
        self.assertEqual(card.count("1. Separate DB"), 1)
        state.update(state="WAIT_REPLY", return_state="IMPLEMENTING")
        self.assertIn("Separate DB", handoffs.selected_reply(state, "1")["reply"])
        self.assertTrue(handoffs.selected_reply(state, "3")["wait"])
        fallback = handoffs.remember_decision(state, "question during IMPLEMENTING", "Which customer owns the data?")
        self.assertIn("Which customer owns the data?", fallback)
        self.assertNotIn("Answer the question", fallback)
        self.assertIn("error", handoffs.selected_reply(state, "1"))

    def test_inline_options_are_real_choices_on_separate_lines(self):
        for question in (
                "¿Autorizas crear el cambio? Responde Sí/No. 1. Sí: crear solo planificación. 2. No: conservar el trabajo.",
                "Which policy? 1. Native async logging 2. Durable queue 3. Keep waiting",
                "Which policy?\n1. Native async logging\n   with optional PostgreSQL\n2. Durable queue\n3. Keep waiting"):
            with self.subTest(question=question):
                state = {"state": "REPLANNING", "slug": "task"}
                card = handoffs.remember_decision(state, "question during REPLANNING", question)
                self.assertRegex(card, r"\n1\. ")
                self.assertRegex(card, r"\n2\. ")
                self.assertNotIn("Answer the question", card)
                self.assertNotIn("Explain the alternatives", card)
                self.assertEqual(card.count("1. "), 1)
                self.assertEqual(card.count("2. "), 1)
                state.update(state="WAIT_REPLY", return_state="REPLANNING")
                self.assertNotIn("error", handoffs.selected_reply(state, "1"))
        self.assertTrue(state["pending_decision"]["provided_choices"])

    def test_one_question_and_one_option_set_in_both_channels(self):
        question = ("¿Autorizas crear el cambio OpenSpec ausente mediante el CLI? Responde Sí/No. "
                    "1. Sí: crear únicamente la planificación, sin implementar. "
                    "2. No: mantener el trabajo pausado.")
        for channel in ("email", "slack"):
            with self.subTest(channel=channel):
                state = {"state": "REPLANNING", "slug": "task", "item": "task"}
                result = SimpleNamespace(session_id="session", output=question, question=question,
                    preamble="Contexto útil.\n\n" + question, attachments=[])
                with patch.object(main.config, "COMM_CHANNEL", channel), \
                     patch.object(main.diagnostics, "report", return_value=Path("details.txt")), \
                     patch.object(main.gmail_client, "deliver", return_value={"thread_id": "thread"}) as deliver, \
                     patch.object(main.gmail_client, "send", return_value="thread") as send, \
                     patch.object(main, "trail"), patch.object(main, "_note_contact"), \
                     patch.object(main, "_transcript_append"), patch.object(main, "_transcript_note"):
                    self.assertTrue(main.handle_result(state, result, "REPLANNING"))
                body = deliver.call_args.args[2] if deliver.called else send.call_args.args[1]
                self.assertEqual(body.count("¿Autorizas"), 1)
                self.assertEqual(body.count("1. Sí:"), 1)
                self.assertEqual(body.count("2. No:"), 1)
                self.assertRegex(body, r"\n1\. Sí:")
                self.assertRegex(body, r"\n2\. No:")
                self.assertIn("Contexto útil", body)
                self.assertNotIn("Answer the question", body)
                self.assertEqual(state["state"], "WAIT_REPLY")
                self.assertEqual(state["return_state"], "REPLANNING")

    def test_agent_contract_requires_one_decision_then_wait(self):
        from claude_runner import SENTINEL_CONTRACT
        self.assertIn("exactly one self-contained question", SENTINEL_CONTRACT)
        self.assertIn("own following line", SENTINEL_CONTRACT)
        self.assertIn("wait for that answer", SENTINEL_CONTRACT)
        self.assertIn("Do not repeat", SENTINEL_CONTRACT)

    def test_selection_routes_to_existing_handler_without_feedback_investigation(self):
        state = {"state": "WAIT_REPLY", "return_state": "VERIFYING", "slug": "task",
                 "execution_task_id": "durable", "verification_blocker": {"kind": "unfinished or unclassified tasks"}}
        handoffs.remember_decision(state, "Verification blocked")
        with patch.object(main, "_handle_reply") as handle, patch.object(main.feedback, "receive") as feedback:
            main._dispatch_wait_reply(state, "msg", "sí")
        handle.assert_called_once()
        self.assertIn("explicitly authorize", handle.call_args.args[1])
        feedback.assert_not_called()
        with patch.object(main, "_handle_reply") as handle, patch.object(main, "email") as send:
            main._dispatch_wait_reply(state, "msg2", "no")
        handle.assert_not_called()
        send.assert_called_once()
        self.assertEqual(state["state"], "WAIT_REPLY")

    def test_full_text_is_preserved_for_every_decision(self):
        cases = [("WAIT_APPROVAL", None, "proposal for review"),
                 ("WAIT_MERGE", None, "PR ready for review"),
                 ("WAIT_STUCK", "VERIFYING", "stuck in VERIFYING"),
                 ("WAIT_REPLY", "EXPLORING", "question during EXPLORING"),
                 ("WAIT_REPLY", "VERIFYING", "Verification blocked"),
                 ("WAIT_CLEAN", None, "blocked: dirty working tree")]
        replies = ["3. Dejemos el registro como está actualmente usando LiteLLM, con registro asíncrono.",
                   "1) Dominio completo", "opción 1: Dominio completo", "1 Dominio completo",
                   "Sí, pero cambia el diseño", "2. No hagas merge todavía",
                   "3. No esperes; revisa primero la configuración", "Prefiero otra alternativa.\nEstos son los detalles."]
        for stage, origin, phase in cases:
            state = {"state": stage, "slug": "task"}
            if stage == "WAIT_STUCK":
                state["stuck_return"] = origin
            else:
                state["return_state"] = origin
            handoffs.remember_decision(state, phase, "Which policy?" if stage == "WAIT_REPLY" and origin == "EXPLORING" else None)
            for reply in replies:
                with self.subTest(stage=stage, reply=reply):
                    selected = handoffs.selected_reply(state, reply)
                    self.assertEqual(selected["reply"], reply)
                    self.assertFalse(selected["wait"])
                    self.assertTrue(selected["prose"])

    def test_prose_is_routed_verbatim_not_as_an_automatic_merge_or_wait(self):
        for reply in ("2. No hagas merge todavía", "3: Revisa los fallos antes de esperar"):
            state = {"state": "WAIT_MERGE", "slug": "task"}
            handoffs.remember_decision(state, "PR ready for review")
            with patch.object(main, "_handle_reply") as handle, patch.object(main, "email") as send:
                main._dispatch_wait_reply(state, "msg", reply)
            handle.assert_called_once_with(state, reply)
            send.assert_not_called()

    def test_classification_context_contains_active_options_and_prose_rules(self):
        state = {"state": "WAIT_MERGE", "slug": "task"}
        handoffs.remember_decision(state, "PR ready for review")
        context = handoffs.classification_context(state)
        self.assertIn("2. Authorize 'merge'", context)
        self.assertIn("full reply", context)
        state["slug"] = "other"
        self.assertEqual(handoffs.classification_context(state), "")

    def test_post_approval_prose_still_gets_scope_assessment(self):
        state = {"state": "WAIT_REPLY", "return_state": "IMPLEMENTING", "slug": "task",
                 "execution_task_id": "durable"}
        handoffs.remember_decision(state, "question during IMPLEMENTING", "Which storage?")
        reply = "1. Cambia toda la arquitectura y añade otra BDD"
        with patch.object(main, "_handle_reply") as handle, \
             patch.object(main.feedback, "receive") as receive, \
             patch.object(main, "_dispatch_feedback") as dispatch:
            main._dispatch_wait_reply(state, "msg", reply)
        handle.assert_not_called()
        receive.assert_called_once()
        self.assertEqual(receive.call_args.args[-1], reply)
        dispatch.assert_called_once_with(state)

    def test_qualified_approval_merge_and_recovery_reach_semantic_classifier(self):
        cases = [("WAIT_APPROVAL", "proposal for review", main.do_approval_reply,
                  "1. Sí, pero cambia el diseño", "Do you authorize implementing"),
                 ("WAIT_MERGE", "PR ready for review", main.do_merge_reply,
                  "2. No hagas merge todavía", "Authorize 'merge'"),
                 ("WAIT_STUCK", "stuck in VERIFYING", main.do_stuck_reply,
                  "2. Investiga primero, no reintentes aún", "Retry the same step")]
        for stage, phase, handler, reply, option in cases:
            with self.subTest(stage=stage):
                state = {"state": stage, "slug": "task", "stuck_return": "VERIFYING"}
                handoffs.remember_decision(state, phase)
                with patch.object(main.agent_runner, "run", return_value=SimpleNamespace(output='{"action":"unclear"}')) as run, \
                     patch.object(main, "email"), patch.object(main.agent_runner, "resume") as resume:
                    main._dispatch_wait_reply(state, "msg", reply)
                prompt = run.call_args.args[0]
                self.assertIn(reply, prompt)
                self.assertIn(option, prompt)
                self.assertIn("leading number alone", prompt)
                self.assertEqual(state["state"], stage)
                resume.assert_not_called()

    def test_decision_stays_visible_with_reports_in_both_channels(self):
        for channel in ("email", "slack"):
            with self.subTest(channel=channel):
                state = {"state": "WAIT_REPLY", "return_state": "VERIFYING", "slug": "task", "item": "task",
                         "verification_blocker": {"kind": "unfinished or unclassified tasks"}}
                with patch.object(main.config, "COMM_CHANNEL", channel), \
                     patch.object(main.diagnostics, "report", return_value=Path("details.txt")), \
                     patch.object(main.gmail_client, "deliver", return_value={"thread_id": "thread"}) as deliver, \
                     patch.object(main, "trail"), patch.object(main, "_note_contact"):
                    main.email(state, "Verification blocked", "Traceback (most recent call last):\n" + "frame\n" * 50)
                body = deliver.call_args.args[2]
                self.assertIn("Do you authorize", body)
                self.assertIn("1. Yes", body)
                self.assertIn("2. No", body)
                self.assertIn("Reply Yes/No", body)

    def test_yes_to_pending_approved_work_reaches_implementation_transition(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "openspec/changes/task/tasks.md"
            path.parent.mkdir(parents=True)
            path.write_text("- [ ] Complete approved implementation\n")
            state = {"state": "WAIT_REPLY", "return_state": "VERIFYING", "slug": "task", "item": "task",
                     "session_id": "sid", "verification_blocker": {"kind": "unfinished or unclassified tasks"},
                     "approved_task_inventory": ["Complete approved implementation"]}
            handoffs.remember_decision(state, "Verification blocked")
            with patch.object(main.config, "REPO_PATH", Path(root)), \
                 patch.object(main.agent_runner, "run", return_value=SimpleNamespace(output='{"action":"resume_implementation"}')), \
                 patch.object(main.agent_runner, "resume") as resume, \
                 patch.object(main, "save_state"), patch.object(main, "trail"):
                main._dispatch_wait_reply(state, "message", "Sí")
            self.assertEqual(state["state"], "IMPLEMENTING")
            resume.assert_not_called()
            self.assertEqual(path.read_text(), "- [ ] Complete approved implementation\n")
            self.assertIsNone(handoffs.selected_reply(state, "Sí"))

    def test_cards_name_decision_without_changing_reply_rules(self):
        self.assertIn("approve", handoffs.decision_card({}, "proposal for review"))
        self.assertIn("answer", handoffs.decision_card({}, "question during EXPLORING"))
        self.assertIn("retry", handoffs.decision_card({}, "verification stuck"))
        self.assertIn("'merge'", handoffs.decision_card({}, "PR ready for review"))
        self.assertIn("approve", handoffs.decision_card(
            {"state": "WAIT_APPROVAL"}, "clarification needed"))

    def test_checks_are_derived_from_persisted_contracts_not_presence_of_harness(self):
        state = {"quality_report": {"openspec": "pass", "commands": ["pytest: pass"],
                                    "preexisting": ["lint: same on main"]},
                 "internal_review_report": {"status": "pass"}, "has_e2e_harness": False}
        text = handoffs.verification(state)
        self.assertIn("OpenSpec: passed", text)
        self.assertIn("lint: same on main", text)
        self.assertIn("E2E: not applicable", text)
        state["has_e2e_harness"] = True
        self.assertIn("E2E: outcome unavailable", handoffs.verification(state))
        state["e2e_passed"] = True
        self.assertIn("E2E: passed", handoffs.verification(state))

    def test_evidence_index_does_not_invent_missing_files(self):
        self.assertIn("No artifact available", handoffs.evidence_index([], None))
        self.assertIn("Demo video", handoffs.evidence_index([], "https://drive.example/video"))

    def test_focused_handoff_does_not_hide_unknown_historical_audits(self):
        state = {"final_check_report": {"status": "pass", "verification_scope": "focused_e2e",
            "checks": [{"check": "zip", "status": "pass", "gate": {"status": "pass"}}],
            "audit_checks": [{"status": "unknown", "required": False, "report": "old-audit.log"}]}}
        detail = handoffs.verification(state)
        self.assertIn("Final zip: pass", detail)
        self.assertIn("Historical full E2E audit: unknown", detail)
        self.assertIn("not current verification", detail)
        self.assertIn("old-audit.log", detail)
        self.assertIn("not reported as green", handoffs.concise_verification(state))

    def test_feedback_recap_uses_queued_request_only_after_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp) / "report.html"
            evidence.write_text("report")
            state = {"state": "WAIT_MERGE", "slug": "example", "branch": "bot-example",
                     "item": "Create API", "pr_url": "https://github.com/a/b/pull/3"}
            with patch.object(main, "save_state"), \
                 patch.object(main.final_checks, "run", return_value={"status": "pass"}), \
                 patch.object(main, "git"), \
                 patch.object(main, "email") as send:
                main._queue_push(state, "feedback", "Added pagination and ran unit tests",
                                 [evidence], feedback="Add pagination")
                self.assertEqual(state["state"], "PUSHING")
                self.assertEqual(state["push_context"]["feedback"], "Add pagination")
                main.do_push(state)
        self.assertIn("Requested: Add pagination", send.call_args.args[2])
        self.assertIn("Changed (implementation report): Added pagination", send.call_args.args[2])
        self.assertIn("report.html", send.call_args.args[2])
        self.assertEqual(state["state"], "WAIT_MERGE")
