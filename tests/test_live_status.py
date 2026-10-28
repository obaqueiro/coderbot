"""STATUS is answered while the main coding turn is blocked, without a second agent."""
import json
import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import config

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(),
                              "gmail_client": Mock()}):
    import main

agent_runner = main.agent_runner
import slack_client


class AgentActivity(unittest.TestCase):
    def test_sdk_http_body_debug_is_suppressed_without_hiding_our_logs(self):
        self.assertGreaterEqual(logging.getLogger("slack_sdk.web.base_client").getEffectiveLevel(),
                                logging.WARNING)
        self.assertEqual(logging.getLogger("codebot").level, main.log.level)

    def test_streamed_task_is_observed_without_prompt_or_output(self):
        agent_runner._begin_turn()
        try:
            event = {"type": "tool_use", "part": {"tool": "task", "state": {
                "input": {"description": "Implementar sesiones backend API",
                          "prompt": "PRIVATE PROMPT"}}}}
            agent_runner._stream_line(json.dumps(event))
            snapshot = agent_runner.turn_snapshot()
            self.assertTrue(snapshot["active"])
            self.assertIn("Implementar sesiones backend API", snapshot["activity"])
            self.assertNotIn("PRIVATE PROMPT", str(snapshot))
            previous = snapshot["last_activity"]
            agent_runner._stream_line(json.dumps({"type": "step_finish", "part": {}}))
            self.assertGreaterEqual(agent_runner.turn_snapshot()["last_activity"], previous)
        finally:
            agent_runner._end_turn()
        self.assertFalse(agent_runner.turn_snapshot()["active"])

    def test_failed_agent_turn_clears_active_marker(self):
        with patch.object(config, "AGENT", "claude"), \
             patch.object(agent_runner.claude_runner, "run", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError):
                agent_runner.run("task", contract=False)
        self.assertFalse(agent_runner.turn_snapshot()["active"])


class Supervisor(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old_state = main._current_state
        self.addCleanup(setattr, main, "_current_state", self.old_state)
        self.addCleanup(main._work_active.clear)
        self.state = {"state": "IMPLEMENTING", "item": "Configurar API", "slug": "api",
                      "thread_id": "C123:100.0", "task_language": "Spanish"}
        main._current_state = self.state

    def test_progress_refresh_without_status_and_while_waiting(self):
        self.state.update(state="WAIT_REPLY", return_state="EXPLORING")
        with patch.object(main.gmail_client, "update_progress") as update, \
             patch.object(main, "save_state") as save:
            main._refresh_progress()
        args = update.call_args.args
        self.assertEqual(args[0], self.state["thread_id"])
        self.assertEqual(args[1]["situation"], "waiting_input")
        self.assertEqual(args[1]["phase"], "EXPLORING")
        save.assert_not_called()

    def test_progress_transport_failure_does_not_mutate_task(self):
        with patch.object(main.gmail_client, "update_progress", side_effect=RuntimeError("offline")), \
             self.assertLogs("codebot", level="ERROR"):
            main._refresh_progress()
        self.assertEqual(self.state["state"], "IMPLEMENTING")

    def test_resumed_question_progress_uses_live_turn_not_wait_polling(self):
        self.state.update(state="WAIT_REPLY", return_state="EXPLORING")
        main._work_active.set()
        with patch.object(main.gmail_client, "update_progress") as update, \
             patch.object(main.agent_runner, "turn_snapshot", return_value={"active": True}) as turn, \
             patch.object(main, "save_state") as save:
            main._refresh_progress()
            snapshot = update.call_args.args[1]
            self.assertEqual(snapshot["situation"], "working")
            self.assertEqual(snapshot["phase"], "EXPLORING")
            turn.return_value = {"active": False}
            main._refresh_progress()
            self.assertEqual(update.call_args.args[1]["situation"], "waiting_input")
        self.assertEqual(self.state["state"], "WAIT_REPLY")
        save.assert_not_called()

    def test_status_during_resumed_question_does_not_ask_for_another_reply(self):
        self.state.update(state="WAIT_REPLY", return_state="EXPLORING")
        observed = {"active": True, "started_at": 100.0, "last_activity": 200.0,
                    "activity": "tool: webfetch"}
        with patch.object(main.activity, "snapshot", return_value=observed), \
             patch.object(main, "_ping_body") as ping:
            body = main._short_status(self.state, observed, 210.0)
        self.assertIn("Fase: EXPLORING", body)
        self.assertIn("Estoy trabajando; no necesito nada de ti.", body)
        ping.assert_not_called()

    def test_thread_status_answers_during_blocked_agent_and_preserves_abort(self):
        started, release = threading.Event(), threading.Event()
        def blocked_agent(_prompt, contract=False):
            started.set()
            self.assertTrue(release.wait(5))
            return Mock(output="done")
        api = MagicMock()
        api.conversations_replies.return_value = {"messages": []}
        with patch.object(config, "COMM_CHANNEL", "slack"), \
             patch.object(config, "SLACK_CHANNEL_ID", "C123"), \
             patch.object(config, "DATA_DIR", Path(self.temp.name)), \
             patch.object(config, "AGENT", "claude"), \
             patch.object(slack_client, "_bot_user", "Ubot"), \
             patch.object(slack_client, "web", return_value=api), \
             patch.object(agent_runner.claude_runner, "run", side_effect=blocked_agent) as run:
            with slack_client._database() as db:
                db.execute("INSERT INTO roots(channel,root_ts,nonce) VALUES(?,?,?)",
                           ("C123", "100.0", "task-nonce"))
            worker = threading.Thread(target=agent_runner.run, args=("task",),
                                      kwargs={"contract": False})
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                main._work_active.set()
                for ts, text in (("101.0", "ABORT"), ("102.0", "status?")):
                    slack_client._accept_event({"event": {"type": "message", "ts": ts,
                                                   "thread_ts": "100.0", "channel": "C123",
                                                   "user": "Uhuman", "text": text}})
                self.assertTrue(main._status_supervisor_once())
                self.assertTrue(worker.is_alive())
                send = api.chat_postMessage.call_args.kwargs
                self.assertEqual(send["thread_ts"], "100.0")
                self.assertIn("Fase: IMPLEMENTING", send["text"])
                self.assertIn("Tarea: Configurar API", send["text"])
                self.assertNotIn("no puedo confirmar", send["text"])
                self.assertEqual(slack_client.poll_command()[2], "ABORT")
                self.assertFalse(main._status_supervisor_once())
                run.assert_called_once()
            finally:
                release.set()
                worker.join(timeout=2)

    def test_top_level_status_gets_a_threaded_answer_even_when_idle(self):
        self.state.update(state="IDLE", item=None)
        with patch.object(config, "COMM_CHANNEL", "slack"), \
             patch.object(config, "SLACK_CHANNEL_ID", "C123"), \
             patch.object(config, "DATA_DIR", Path(self.temp.name)), \
             patch.object(slack_client, "_bot_user", "Ubot"), \
             patch.object(slack_client, "web", return_value=MagicMock()) as web:
            self.assertTrue(slack_client._accept_event({"event": {"type": "message",
                "ts": "202.0", "channel": "C123", "user": "Uhuman", "text": "status?"}}))
            self.assertTrue(main._status_supervisor_once())
            self.assertEqual(web.return_value.chat_postMessage.call_args.kwargs["thread_ts"],
                             "202.0")
            self.assertFalse(main._status_supervisor_once())

    def test_gmail_status_is_brief_without_updating_fsm_or_starting_agent(self):
        state = dict(self.state, thread_id="gmail-thread", task_language="English")
        main._current_state = state
        main._work_active.set()
        with patch.object(config, "COMM_CHANNEL", "email"), \
             patch.object(main.gmail_client, "poll_kick", return_value=None), \
             patch.object(main.gmail_client, "poll_status", return_value=("m1", "gmail-thread")), \
             patch.object(main.gmail_client, "send") as send, \
             patch.object(main.gmail_client, "mark_processed") as mark, \
             patch.object(main, "save_state") as save, \
             patch.object(main.agent_runner, "run") as agent:
            self.assertTrue(main._status_supervisor_once())
        self.assertIn("Phase: IMPLEMENTING", send.call_args.args[1])
        self.assertLess(len(send.call_args.args[1]), 450)
        mark.assert_called_once_with("m1")
        save.assert_not_called()
        agent.assert_not_called()
        self.assertEqual(state["state"], "IMPLEMENTING")

    def test_kick_is_processed_during_a_busy_turn_without_touching_the_fsm(self):
        main._work_active.set()
        with patch.object(config, "COMM_CHANNEL", "slack"), \
             patch.object(main.agent_runner, "turn_snapshot", return_value={"active": True}), \
             patch.object(main.gmail_client, "poll_kick", return_value=("m2", "C123:100.0")), \
             patch.object(main.gmail_client, "poll_status") as status, \
             patch.object(main.turn_control, "request_kick", return_value=True) as kick, \
             patch.object(main.gmail_client, "mark_processed") as handled, \
             patch.object(main.gmail_client, "send") as send, \
             patch.object(main, "save_state") as save:
            self.assertTrue(main._status_supervisor_once())
        kick.assert_called_once()
        handled.assert_called_once_with("m2")
        self.assertIn("Reiniciando", send.call_args.args[1])
        status.assert_not_called()
        save.assert_not_called()
        self.assertEqual(self.state["state"], "IMPLEMENTING")

    def test_old_observation_reports_its_age_without_guessing_progress(self):
        body = main._short_status(self.state, {"active": True, "started_at": 100.0,
                                               "last_activity": 200.0,
                                               "activity": "task: Implementar backend"}, 700.0)
        self.assertIn("hace 8 min", body)
        self.assertIn("task: Implementar backend", body)
        self.assertNotIn("no puedo confirmar", body)
        self.assertNotIn("bloqueo", body)

    def test_readable_status_includes_plan_and_two_subagents_without_reasoning(self):
        observed = {"active": True, "started_at": 200.0, "last_activity": 540.0,
                    "activity": "subagent: Cerrar proveedor API", "current_task": "Completar API",
                    "todos": {"in_progress": 1, "pending": 4},
                    "children": [{"title": "Cerrar proveedor API", "at": 540.0},
                                 {"title": "Finalizar Playwright", "at": 420.0}]}
        with patch.object(main.activity, "snapshot", return_value=observed):
            body = main._short_status(self.state, observed, 600.0)
        self.assertIn("Ahora: Completar API", body)
        self.assertIn("1 en curso, 4 pendientes", body)
        self.assertIn("Subagentes: Cerrar proveedor API", body)
        self.assertIn("Finalizar Playwright", body)
        self.assertNotIn("reasoning", body)
