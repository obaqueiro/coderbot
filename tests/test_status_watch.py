"""A standalone watcher never edits task state or competes with the coding agent."""
import sys
import unittest
from unittest.mock import Mock, patch

with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(),
                              "gmail_client": Mock()}):
    from scripts import status_watch


class StandaloneStatusWatch(unittest.TestCase):
    def test_answers_only_status_then_exits_when_task_advances(self):
        active = {"state": "IMPLEMENTING", "item": "Example app", "slug": "example-app",
                  "thread_id": "C123:1.0", "task_language": "Spanish",
                  "last_transition": 100.0}
        later = dict(active, state="VERIFYING")
        with patch.object(status_watch.config, "COMM_CHANNEL", "slack"), \
             patch.object(status_watch.codebot, "load_state",
                          side_effect=[active, active, later]), \
             patch.object(status_watch.slack_client, "poll_status",
                          return_value=("C123:2.0", "C123:1.0")) as poll, \
             patch.object(status_watch.slack_client, "send") as send, \
             patch.object(status_watch.slack_client, "mark_processed") as consumed, \
             patch.object(status_watch.codebot.agent_runner, "run") as agent, \
             patch.object(status_watch.time, "sleep"):
            status_watch.watch(0)
        poll.assert_called_once_with("C123:1.0")
        self.assertIn("Fase: IMPLEMENTING", send.call_args.args[1])
        self.assertIn("Tarea: Example app", send.call_args.args[1])
        self.assertNotIn("no puedo confirmar", send.call_args.args[1])
        consumed.assert_called_once_with("C123:2.0")
        agent.assert_not_called()

    def test_not_started_without_active_task(self):
        with patch.object(status_watch.config, "COMM_CHANNEL", "slack"), \
             patch.object(status_watch.codebot, "load_state", return_value={"state": "IDLE"}), \
             patch.object(status_watch.slack_client, "poll_status") as poll:
            with self.assertRaisesRegex(RuntimeError, "active working task"):
                status_watch.watch(0)
        poll.assert_not_called()
