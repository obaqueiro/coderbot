"""Live progress is channel-neutral, bounded and never replaces user messages."""
import json
import logging
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import agent_runner
import config
import conversation
import progress
import slack_client as slack


class ProgressCapture(unittest.TestCase):
    def setUp(self):
        progress._tails.clear()
        progress.install()
        self.state = {"branch": "task-a", "state": "EXPLORING", "task_language": "Spanish"}
        agent_runner.set_task_context(self.state)
        self.addCleanup(agent_runner.set_task_context, None)

    def test_last_three_safe_tool_lines_exclude_text_reasoning_and_prompts(self):
        with patch.object(agent_runner.stream_log, "level", logging.INFO):
            for index in range(4):
                agent_runner._stream_line(json.dumps({"type": "tool_use", "part": {
                    "tool": "bash", "state": {"input": {"command": f"pytest test_{index}"}}}}))
            for kind in ("text", "reasoning"):
                agent_runner._stream_line(json.dumps({"type": kind, "part": {"text": "PRIVATE TEXT"}}))
            snapshot = progress.snapshot(self.state, active=True)
            self.assertEqual(len(snapshot["log_lines"]), 3)
            self.assertNotIn("test_0", str(snapshot))
            self.assertIn("pytest test_3", str(snapshot))
            self.assertNotIn("PRIVATE TEXT", str(snapshot))
            agent_runner._stream_line(json.dumps({"type": "tool_use", "part": {
                "tool": "task", "state": {"input": {"prompt": "PRIVATE PROMPT"}, "output": "PRIVATE OUTPUT"}}}))
            self.assertNotIn("PRIVATE", str(progress.snapshot(self.state)))

    def test_controller_checks_publish_safe_progress_from_parallel_context(self):
        import checks
        from check_plan import Check
        from execution_store import ExecutionStore
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ExecutionStore(root / "data/execution.sqlite")
            check = Check("unit", ["sh", "-c", "echo PRIVATE_COMMAND"])
            with patch.object(checks, "tool_versions", return_value={}), \
                 patch.object(checks, "check_environment", return_value={}), \
                 patch.object(checks.operations, "run", return_value=subprocess.CompletedProcess(
                     check.argv, 0, "PRIVATE_OUTPUT", "")):
                checks.execute_plan([check], root, store, "task")
                first = progress.snapshot(self.state)
                self.assertIn("check started: unit", str(first))
                self.assertIn("check finished: unit status=pass", str(first))
                checks.execute_plan([check], root, store, "task")
                snapshot = progress.snapshot(self.state)
            self.assertEqual(len(snapshot["log_lines"]), 3)
            self.assertIn("check reused: unit status=pass", str(snapshot))
            self.assertNotIn("PRIVATE", str(snapshot))
            self.assertNotIn(str(root), str(snapshot))
            rendered = slack._render_progress("Task", snapshot)
            self.assertIn("check finished: unit", rendered)
            self.assertIn("```", rendered)

    def test_redacts_credentials_before_truncation_and_escapes_fences(self):
        with patch.dict("os.environ", {"EXAMPLE_API_KEY": "super-private-value"}):
            safe = progress.sanitize("curl --token=super-private-value Authorization: Bearer abc123 "
                                     "password=hunter2 xoxb-123-abc ``` api_key='multi word secret'")
        for secret in ("super-private-value", "abc123", "hunter2", "xoxb-123-abc", "```", "word secret"):
            self.assertNotIn(secret, safe)

    def test_separate_tasks_and_waiting_phase(self):
        log = logging.getLogger("agent")
        with patch.object(log, "level", logging.INFO):
            log.info("private", extra={"public_progress": "task a"})
        state_b = {"branch": "task-b", "state": "WAIT_REPLY", "return_state": "IMPLEMENTING"}
        snapshot = progress.snapshot(state_b)
        self.assertEqual(snapshot["log_lines"], [])
        self.assertEqual(snapshot["phase"], "IMPLEMENTING")
        self.assertEqual(snapshot["situation"], "waiting_input")

    def test_live_turn_overrides_wait_without_changing_persisted_state(self):
        for wait in ("WAIT_REPLY", "WAIT_APPROVAL", "WAIT_MERGE", "WAIT_STUCK", "WAIT_CLEAN"):
            with self.subTest(wait=wait):
                state = dict(self.state, state=wait, return_state="EXPLORING")
                snapshot = progress.snapshot(state, active=True)
                self.assertEqual(snapshot["situation"], "working")
                self.assertEqual(state["state"], wait)
                rendered = slack._render_progress("Task", snapshot)
                self.assertIn(":loading:", rendered)
                self.assertNotIn("esperando", rendered)
                self.assertEqual(progress.snapshot(state)["situation"], "waiting_input")

    def test_explicit_situation_and_idle_still_override_activity(self):
        self.assertEqual(progress.snapshot(self.state, active=True, situation="paused")["situation"],
                         "paused")
        self.assertEqual(progress.snapshot(dict(self.state, state="IDLE"), active=True)["situation"],
                         "completed")

    def test_non_editable_backend_does_not_send_anything(self):
        backend = SimpleNamespace(send=MagicMock())
        with patch.object(conversation, "_backend", return_value=backend):
            conversation.update_progress("email-thread", progress.snapshot(self.state))
        backend.send.assert_not_called()

    def test_facade_passes_snapshot_to_editable_backend(self):
        backend = SimpleNamespace(update_progress=MagicMock())
        snapshot = progress.snapshot(self.state)
        with patch.object(conversation, "_backend", return_value=backend):
            conversation.update_progress("thread", snapshot)
        backend.update_progress.assert_called_once_with("thread", snapshot)


class SlackProgress(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = MagicMock()
        self.api.conversations_history.return_value = {"messages": []}
        self.api.chat_postMessage.return_value = {"ts": "100.0"}
        for name, value in (("DATA_DIR", Path(self.temp.name)), ("SLACK_CHANNEL_ID", "C123")):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(slack, "web", return_value=self.api)
        p.start()
        self.addCleanup(p.stop)
        self.state = {"branch": "task-a", "item": "Feature", "thread_intro": "*Original feature*",
                      "state": "EXPLORING", "task_language": "Spanish"}
        self.thread = slack.open_thread(self.state)
        self.original = self.api.chat_postMessage.call_args.kwargs["text"]

    def test_updates_owned_root_preserving_intro_marker_and_deduplicates(self):
        snapshot = {**progress.snapshot(self.state, active=True),
                    "log_lines": ["old", "one", "two", "three <@U123> ```"]}
        with patch.object(slack.time, "time", return_value=100):
            slack.update_progress(self.thread, snapshot)
            slack.update_progress(self.thread, snapshot)
        self.api.chat_update.assert_called_once()
        sent = self.api.chat_update.call_args.kwargs
        self.assertEqual(sent["ts"], "100.0")
        self.assertEqual(sent["channel"], "C123")
        self.assertTrue(sent["text"].startswith(self.original))
        self.assertIn("Estado: Explore :loading:", sent["text"])
        self.assertNotIn("\nold", sent["text"])
        self.assertIn("&lt;@U123&gt;", sent["text"])
        self.assertEqual(sent["text"].count("```"), 2)
        self.api.chat_postMessage.assert_called_once()

    def test_resource_links_appear_above_status_and_survive_later_updates(self):
        self.state.update(pr_url="https://github.com/org/repo/pull/1",
                          evidence_url="https://drive.google.com/file/d/video/view")
        with patch.object(slack.time, "time", return_value=100):
            slack.update_progress(self.thread, progress.snapshot(self.state))
        text = self.api.chat_update.call_args.kwargs["text"]
        self.assertTrue(text.startswith(self.original))
        self.assertLess(text.index("PR: <"), text.index("Estado:"))
        self.assertLess(text.index("Video: <"), text.index("Estado:"))
        with patch.object(slack.time, "time", return_value=131):
            slack.update_progress(self.thread, progress.snapshot(self.state, situation="completed"))
        text = self.api.chat_update.call_args.kwargs["text"]
        self.assertEqual(text.count("PR: <"), 1)
        self.assertEqual(text.count("Video: <"), 1)

    def test_resources_are_independent_and_unsafe_links_are_not_rendered(self):
        text = slack._render_progress("Task", {"pr_url": "https://github.com/org/repo/pull/1"})
        self.assertIn("PR: <", text)
        self.assertNotIn("Video:", text)
        text = slack._render_progress("Task", {"video_url": "javascript:alert(1)"})
        self.assertNotIn("javascript", text)

    def test_throttles_then_replaces_status_and_finishes_without_spinner(self):
        with patch.object(slack.time, "time", return_value=100):
            slack.update_progress(self.thread, progress.snapshot(self.state, active=True))
            self.state.update(state="WAIT_APPROVAL")
            slack.update_progress(self.thread, progress.snapshot(self.state))
        self.assertEqual(self.api.chat_update.call_count, 1)
        with patch.object(slack.time, "time", return_value=129):
            slack.update_progress(self.thread, progress.snapshot(self.state))
            slack.flush_progress()
        self.assertEqual(self.api.chat_update.call_count, 1)
        with patch.object(slack.time, "time", return_value=130):
            slack.update_progress(self.thread, progress.snapshot(self.state))
            sent = self.api.chat_update.call_args.kwargs["text"]
            self.assertIn(":question:", sent)
            self.assertNotIn(":loading:", sent)
            self.assertEqual(sent.count("Estado:"), 1)
            slack.update_progress(self.thread, progress.snapshot(self.state, situation="completed"))
            self.assertIn("Finalizado", self.api.chat_update.call_args.kwargs["text"])
            slack.update_progress(self.thread, progress.snapshot(self.state, active=True))
        self.assertEqual(self.api.chat_update.call_count, 3)

    def test_rate_limit_retry_does_not_interrupt_or_duplicate(self):
        error = RuntimeError("rate limited")
        error.response = SimpleNamespace(headers={"Retry-After": "30"})
        self.api.chat_update.side_effect = [error, {}]
        snapshot = progress.snapshot(self.state, active=True)
        with patch.object(slack.time, "time", return_value=100):
            with self.assertRaises(RuntimeError):
                slack.update_progress(self.thread, snapshot)
        with patch.object(slack.time, "time", return_value=110):
            slack.update_progress(self.thread, snapshot)
            slack.update_progress(self.thread, progress.snapshot(self.state, situation="completed"))
        self.assertEqual(self.api.chat_update.call_count, 1)
        with patch.object(slack.time, "time", return_value=131):
            slack.flush_progress()
        self.assertEqual(self.api.chat_update.call_count, 2)
        self.assertIn("Finalizado", self.api.chat_update.call_args.kwargs["text"])

    def test_final_update_retries_after_thread_retirement_and_restart(self):
        self.api.chat_update.side_effect = RuntimeError("offline")
        with patch.object(slack.time, "time", return_value=100):
            with self.assertRaises(RuntimeError):
                slack.update_progress(self.thread, progress.snapshot(self.state, situation="completed"))
        slack.close_thread(self.thread)
        self.api.chat_update.side_effect = None
        with patch.object(slack.time, "time", return_value=106):
            slack.flush_progress()
        self.assertIn("Finalizado", self.api.chat_update.call_args.kwargs["text"])
        with slack._database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM progress").fetchone()[0], 0)

    def test_resuming_cancels_obsolete_throttled_wait_update(self):
        active = progress.snapshot(self.state, active=True)
        with patch.object(slack.time, "time", return_value=100):
            slack.update_progress(self.thread, active)
            waiting = progress.snapshot(dict(self.state, state="WAIT_REPLY", return_state="EXPLORING"))
            slack.update_progress(self.thread, waiting)
            slack.update_progress(self.thread, active)
        with patch.object(slack.time, "time", return_value=130):
            slack.flush_progress()
        self.api.chat_update.assert_called_once()

    def test_old_installation_backfills_only_bot_owned_root(self):
        with slack._database() as db:
            db.execute("DELETE FROM progress")
        self.api.conversations_replies.return_value = {"messages": [
            {"ts": "100.0", "user": "Ubot", "text": self.original}]}
        with patch.object(slack, "_bot_user", "Ubot"):
            slack.update_progress(self.thread, progress.snapshot(self.state))
        self.assertTrue(self.api.chat_update.call_args.kwargs["text"].startswith(self.original))
        slack.close_thread(self.thread)
        self.api.chat_update.reset_mock()
        slack.update_progress(self.thread, progress.snapshot(self.state))
        self.api.chat_update.assert_not_called()
