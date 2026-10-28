"""Task-scoped routing receipts survive restarts without touching workflow state."""
import copy
from pathlib import Path
import tempfile
import unittest
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

from execution_store import ExecutionStore
import parallel_conversation as lateral
from command_text import parse_btw, parse_command


class ConversationReceiptsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        self.store = ExecutionStore(self.repo / "execution.sqlite")
        self.state = {"execution_task_id": "task", "item": "Filter models",
                      "state": "WAIT_STUCK", "thread_id": "C:1",
                      "pending_question": "How should I recover?", "stuck_return": "PROPOSING",
                      "question_rounds": 2, "api_key": "not-for-model"}

    def classification(self, **fields):
        return {"intent": "conversation", "resolves_pending_question": False,
                "requested_action": None, "action_depends_on_change": False,
                "inputs": [], **fields}

    def test_receipt_deduplicates_and_preserves_state_and_original(self):
        before = copy.deepcopy(self.state)
        row = lateral.receive(self.store, self.state, self.repo, "m", "What happened?")
        reopened = ExecutionStore(self.repo / "execution.sqlite")
        repeated = lateral.receive(reopened, self.state, self.repo, "m", "different")
        self.assertEqual(row, repeated)
        self.assertEqual(self.state, before)
        self.assertNotIn("api_key", row["data"]["snapshot"])
        self.assertEqual(row["data"]["original_text"], "What happened?")

    def test_decision_version_changes_and_snapshot_is_detached(self):
        view = lateral.snapshot(self.store, self.state, self.repo)
        self.state["pending_question"] = "A new decision"
        self.assertNotEqual(view["decision_version"], lateral.decision_version(self.state))
        self.assertEqual(view["pending_question"], "How should I recover?")

    def test_btw_cannot_authorize_a_flow_action(self):
        self.assertEqual(parse_btw("/BTW retry"), "retry")
        self.assertEqual(parse_btw("/btw"), "")
        self.assertIsNone(parse_btw("/btween test"))
        self.assertIsNone(parse_command("/btw ABORT"))
        row = lateral.receive(self.store, self.state, self.repo, "m", "/btw retry")
        row = lateral.save_classification(self.store, row, self.classification(
            intent="answer", resolves_pending_question=True, requested_action="retry"))
        self.assertEqual(row["data"]["route"], "lateral")
        self.assertFalse(row["data"]["classification"]["resolves_pending_question"])

    def test_replacement_and_clarification_are_durable_and_task_scoped(self):
        row = lateral.receive(self.store, self.state, self.repo, "m1", "Add filter")
        row = lateral.save_classification(self.store, row, self.classification(
            intent="change_request", inputs=[{"kind": "change", "text": "Add filter",
                                               "needs_clarification": True}]))
        old = lateral.register_inputs(self.store, row, self.repo)[0]
        second = lateral.receive(self.store, self.state, self.repo, "m2", "By provider")
        second = lateral.save_classification(self.store, second, self.classification(
            inputs=[{"kind": "context", "text": "By provider", "clarifies": old["id"]}]))
        lateral.register_inputs(self.store, second, self.repo)
        lateral.register_inputs(self.store, second, self.repo)
        rows = self.store.list("conversation_input", "task")
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.store.get("conversation_input", old["id"])["status"], "replaced")
        ready = next(r for r in rows if r["status"] == "ready")
        self.assertEqual(ready["data"]["kind"], "change")
        self.assertIn("Add filter", ready["data"]["text"])
        self.assertIn("By provider", ready["data"]["text"])

    def test_mixed_dependency_never_routes_as_flow(self):
        row = lateral.receive(self.store, self.state, self.repo, "m", "Before retry add X")
        row = lateral.save_classification(self.store, row, self.classification(
            intent="answer", resolves_pending_question=True, action_depends_on_change=True,
            inputs=[{"kind": "change", "text": "Add X"}]))
        self.assertEqual(row["data"]["route"], "lateral")

    def test_invalid_classification_fails_closed(self):
        for value in ({}, self.classification(resolves_pending_question="yes"),
                      self.classification(inputs=[{"kind": "approval", "text": "yes"}])):
            with self.subTest(value=value), self.assertRaises(ValueError):
                lateral.validate(value)

    def test_worker_explains_without_mutating_pending_decision(self):
        before = copy.deepcopy(self.state)
        row = lateral.receive(self.store, self.state, self.repo, "m", "No entiendo, ¿qué tenemos que hacer?")
        generate = Mock(side_effect=[SimpleNamespace(output=json.dumps(self.classification()), session_id="classifier"),
                                    SimpleNamespace(output="The provider blocked the step.", session_id="chat")])
        deliver = Mock(return_value={"complete": True, "notification_id": "receipt"})
        self.assertTrue(lateral.process_one(self.store, self.repo, generate, deliver))
        self.assertEqual(self.state, before)
        self.assertEqual(self.store.get("conversation", row["id"])["status"], "complete")
        self.assertEqual(generate.call_count, 2)

    def test_delivery_retry_does_not_regenerate_after_restart(self):
        row = lateral.receive(self.store, self.state, self.repo, "m", "/btw explain")
        row = lateral.save_classification(self.store, row, self.classification())
        generate = Mock(return_value=SimpleNamespace(output="Explanation", session_id="chat"))
        deliver = Mock(side_effect=[{"complete": False, "notification_id": "r"},
                                    {"complete": True, "notification_id": "r"}])
        with patch.object(lateral.time, "time", return_value=100):
            lateral.process_one(self.store, self.repo, generate, deliver)
        with patch.object(lateral.time, "time", return_value=131):
            lateral.process_one(ExecutionStore(self.repo / "execution.sqlite"), self.repo, generate, deliver)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(self.store.get("conversation", row["id"])["status"], "complete")

    def test_failed_old_task_delivery_does_not_starve_new_question(self):
        old = lateral.receive(self.store, self.state, self.repo, "old", "/btw explain")
        old = lateral.save_classification(self.store, old, self.classification())
        self.store.update("conversation", old, status="generated", output="Saved explanation")
        state = {**self.state, "execution_task_id": "new-task", "state": "WAIT_REPLY"}
        new = lateral.receive(self.store, state, self.repo, "new", "Only frontend, not backend")
        generate = Mock(return_value=SimpleNamespace(output=json.dumps(self.classification(
            intent="answer", resolves_pending_question=True)), session_id="classifier"))
        deliver = Mock(return_value={"complete": False, "notification_id": "r", "error": "invalid old thread"})
        with patch.object(lateral.time, "time", return_value=100):
            self.assertTrue(lateral.process_one(self.store, self.repo, generate, deliver))
            self.assertTrue(lateral.process_one(self.store, self.repo, generate, deliver))
        self.assertEqual(deliver.call_count, 1)
        self.assertEqual(self.store.get("conversation", new["id"])["data"]["route"], "flow")

    def test_context_is_incorporated_only_after_successful_checkpoint(self):
        row = lateral.receive(self.store, self.state, self.repo, "m", "BDD is optional")
        row = lateral.save_classification(self.store, row, self.classification(
            inputs=[{"kind": "context", "text": "BDD is optional"}]))
        lateral.register_inputs(self.store, row, self.repo)
        prompt, rows = lateral.context_prompt(self.store, self.state, self.repo)
        self.assertIn("BDD is optional", prompt)
        self.assertEqual(rows[0]["status"], "ready")
        lateral.incorporated(self.store, rows, "checkpoint")
        self.assertEqual(lateral.context_prompt(self.store, self.state, self.repo), ("", []))

    def test_change_transfer_is_idempotent(self):
        row = lateral.receive(self.store, self.state, self.repo, "m", "Add X")
        row = lateral.save_classification(self.store, row, self.classification(
            intent="change_request", inputs=[{"kind": "change", "text": "Add X"}]))
        lateral.register_inputs(self.store, row, self.repo)
        lateral.transfer_changes(self.store, self.state, self.repo)
        lateral.transfer_changes(self.store, self.state, self.repo)
        self.assertEqual(len(self.store.list("feedback", "task")), 1)

    def test_response_is_concurrent_with_a_work_turn(self):
        import agent_runner
        import turn_control
        import operations
        row = lateral.receive(self.store, self.state, self.repo, "m", "/btw explain")
        lateral.save_classification(self.store, row, self.classification())
        work_running = threading.Event()
        release_work = threading.Event()
        answered = threading.Event()
        def work():
            agent_runner._begin_turn("work-session")
            work_running.set()
            release_work.wait(5)
            agent_runner._end_turn()
        thread = threading.Thread(target=work)
        thread.start()
        self.addCleanup(release_work.set)
        self.assertTrue(work_running.wait(2))
        def harness(prompt, session):
            self.assertEqual(turn_control.role.get(), "conversation")
            agent_runner._observe({"type": "session_start", "sessionID": "chat-session"})
            return agent_runner.OpenCodeResult("chat-session", "Explanation")
        with patch.object(agent_runner.config, "AGENT", "opencode"), \
             patch.object(agent_runner, "_opencode", side_effect=harness):
            lateral.process_one(self.store, self.repo, agent_runner.converse,
                lambda r: (answered.set() or {"complete": True, "notification_id": "r"}))
        self.assertTrue(answered.is_set())
        self.assertTrue(agent_runner.turn_snapshot()["active"])
        self.assertEqual(agent_runner.turn_snapshot()["session_id"], "work-session")
        release_work.set()
        thread.join(2)

    def test_kick_targets_only_work_processes(self):
        import turn_control
        work, chat = Mock(pid=991), Mock(pid=992)
        work.poll.return_value = chat.poll.return_value = None
        turn_control.register(work)
        token = turn_control.role.set("conversation")
        turn_control.register(chat)
        turn_control.role.reset(token)
        try:
            with patch.object(turn_control.os, "killpg") as kill, patch.object(turn_control.threading, "Timer"):
                self.assertTrue(turn_control.request_kick())
                self.assertEqual([c.args[0] for c in kill.call_args_list], [991])
        finally:
            turn_control.release(work)
            turn_control.release(chat)

    def test_text_only_harness_permissions_are_denied(self):
        import agent_runner
        token = agent_runner._lateral.set(True)
        try:
            env = agent_runner._opencode_environment()
        finally:
            agent_runner._lateral.reset(token)
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["permission"], {"*": "deny"})
        self.assertEqual(config["plugin"], [])
        self.assertEqual(config["agent"]["conversation"]["permission"], {"*": "deny"})

    def test_conversation_failure_does_not_end_or_clear_work(self):
        import agent_runner
        import operations
        agent_runner._begin_turn("work-session")
        operation = operations.begin("tool", "work", 30)
        try:
            with patch.object(agent_runner.config, "AGENT", "opencode"), \
                 patch.object(agent_runner, "_opencode", side_effect=RuntimeError("provider failed")):
                with self.assertRaises(RuntimeError):
                    agent_runner.converse("Explain")
            self.assertTrue(agent_runner.turn_snapshot()["active"])
            self.assertEqual(agent_runner.turn_snapshot()["session_id"], "work-session")
            self.assertTrue(any(r["id"] == operation for r in operations.snapshot()))
        finally:
            operations.finish(operation)
            agent_runner._end_turn()

    def test_informative_chat_does_not_block_delivery_but_material_change_does(self):
        row = lateral.receive(self.store, self.state, self.repo, "m", "Explain")
        self.assertTrue(lateral.delivery_blockers(self.store, self.state, self.repo))
        row = lateral.save_classification(self.store, row, self.classification())
        self.assertFalse(lateral.delivery_blockers(self.store, self.state, self.repo))
        change = lateral.receive(self.store, self.state, self.repo, "m2", "Add X")
        change = lateral.save_classification(self.store, change, self.classification(
            intent="change_request", inputs=[{"kind": "change", "text": "Add X"}]))
        lateral.register_inputs(self.store, change, self.repo)
        self.assertTrue(lateral.delivery_blockers(self.store, self.state, self.repo))

    def test_foreign_input_reference_cannot_replace_another_task(self):
        other = {**self.state, "execution_task_id": "other"}
        old = self.store.record("conversation_input", other, self.repo, "old",
                                {"kind": "change", "text": "Other task"}, status="ready")
        row = lateral.receive(self.store, self.state, self.repo, "m", "Replace")
        row = lateral.save_classification(self.store, row, self.classification(
            inputs=[{"kind": "change", "text": "Replacement", "replaces": old["id"]}]))
        inputs = lateral.register_inputs(self.store, row, self.repo)
        self.assertEqual(inputs[0]["status"], "clarifying")
        self.assertEqual(self.store.get("conversation_input", old["id"])["status"], "ready")

    def test_interrupted_generation_recovers_persisted_session(self):
        row = lateral.receive(self.store, self.state, self.repo, "m", "/btw explain")
        lateral.save_classification(self.store, row, self.classification())
        sessions = []
        def generate(prompt, session, *, on_session):
            sessions.append(session)
            on_session("chat-running")
            if len(sessions) == 1:
                raise ConnectionError("interrupted")
            return SimpleNamespace(output="Explanation", session_id="chat-running")
        deliver = Mock(return_value={"complete": True, "notification_id": "r"})
        lateral.process_one(self.store, self.repo, generate, deliver)
        saved = self.store.get("conversation", row["id"])
        self.assertEqual(saved["status"], "retry")
        self.assertEqual(saved["data"]["session_id"], "chat-running")
        self.store.update("conversation", saved, retry_at=0)
        lateral.process_one(ExecutionStore(self.repo / "execution.sqlite"), self.repo, generate, deliver)
        self.assertEqual(sessions, [None, "chat-running"])
        self.assertEqual(self.store.get("conversation", row["id"])["status"], "complete")

    def test_context_replay_keeps_same_invocation_prompt(self):
        row = lateral.receive(self.store, self.state, self.repo, "m", "Context")
        row = lateral.save_classification(self.store, row, self.classification(
            inputs=[{"kind": "context", "text": "BDD optional"}]))
        lateral.register_inputs(self.store, row, self.repo)
        first, rows = lateral.context_prompt(self.store, self.state, self.repo, invocation="turn")
        lateral.incorporated(self.store, rows, "checkpoint")
        replay, _ = lateral.context_prompt(self.store, self.state, self.repo, invocation="turn")
        self.assertEqual(first, replay)
        self.assertEqual(lateral.context_prompt(self.store, self.state, self.repo, invocation="next"), ("", []))

    def test_snapshot_uses_frozen_proposal_not_live_working_files(self):
        package = self.repo / "proposal.json"
        package.write_text(json.dumps({"proposal.md": "Approved filter", "design.md": "Approved design"}))
        self.state["proposal_snapshot"] = str(package)
        view = lateral.snapshot(self.store, self.state, self.repo)
        self.assertEqual(view["proposal"]["proposal.md"], "Approved filter")
        self.assertEqual(view["repo"], str(self.repo))

    def test_incomplete_option_is_routed_for_controller_validation(self):
        self.state.update(slug="filter")
        self.state["pending_decision"] = {"wait_state": "WAIT_STUCK", "task": "filter",
            "question": "How to recover?", "binary": False,
            "options": [{"details": True, "reply": "instructions", "wait": False}]}
        self.assertEqual(lateral.deterministic(self.state, "1")["intent"], "answer")
        self.assertIn("details", lateral.handoffs.selected_reply(self.state, "1")["error"])

    def test_lateral_finish_after_work_kick_does_not_report_chat_kicked(self):
        import turn_control
        work, chat = Mock(pid=993), Mock(pid=994)
        work.poll.return_value = chat.poll.return_value = None
        turn_control.register(work)
        token = turn_control.role.set("conversation")
        turn_control.register(chat)
        turn_control.role.reset(token)
        try:
            with patch.object(turn_control.os, "killpg"), patch.object(turn_control.threading, "Timer"):
                turn_control.request_kick()
            self.assertFalse(turn_control.release(chat))
            self.assertTrue(turn_control.release(work))
        finally:
            turn_control.release(work)
            turn_control.release(chat)

    def test_claude_conversation_has_no_tools_plugins_or_mcp(self):
        import agent_runner
        with patch.object(agent_runner.config, "AGENT", "claude"), \
             patch.object(agent_runner.claude_runner, "_invoke", return_value=SimpleNamespace(
                 output="Explanation", session_id="chat")) as invoke:
            agent_runner.converse("Explain", "chat")
        args = invoke.call_args.args[0]
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", args)
        self.assertIn("--disable-slash-commands", args)
        self.assertNotIn("--plugin-dir", args)

    def test_opencode_conversation_uses_isolated_directory_and_no_auto_approval(self):
        import agent_runner
        for transport in ("http", "cli"):
            with self.subTest(transport=transport), \
                 patch.object(agent_runner.config, "AGENT", "opencode"), \
                 patch.object(agent_runner.config, "DATA_DIR", self.repo), \
                 patch.object(agent_runner.config, "OPENCODE_TRANSPORT", transport), \
                 patch.object(agent_runner, "_run_streaming", return_value=SimpleNamespace(
                     stdout='{"type":"text","sessionID":"chat","part":{"text":"Explanation"}}\n',
                     stderr="", returncode=0)) as run:
                agent_runner.converse("Explain")
                self.assertEqual(run.call_args.kwargs["cwd"], self.repo / "conversation_runtime")
                self.assertNotIn("--auto", run.call_args.args[0])
                inline = json.loads(run.call_args.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
                self.assertEqual(inline["permission"], {"*": "deny"})
                if transport == "http":
                    request = json.loads(run.call_args.kwargs["input_text"])
                    self.assertTrue(request["conversation"])

    def test_content_filtered_generation_is_not_automatically_retried(self):
        from agent_errors import AgentContentFilterError
        row = lateral.receive(self.store, self.state, self.repo, "m", "/btw explain")
        lateral.save_classification(self.store, row, self.classification())
        error = AgentContentFilterError("blocked", SimpleNamespace(args=[], returncode=1, stdout="", stderr=""))
        generate = Mock(side_effect=error)
        deliver = Mock(return_value={"complete": True, "notification_id": "r"})
        lateral.process_one(self.store, self.repo, generate, deliver)
        lateral.process_one(self.store, self.repo, generate, deliver)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(self.store.get("conversation", row["id"])["status"], "failed")
