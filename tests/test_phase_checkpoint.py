from pathlib import Path
import json
import tempfile
import subprocess
import unittest
from unittest.mock import patch

import agent_runner
import phase_checkpoint
from agent_errors import AgentTransportError


class CheckpointReplayTests(unittest.TestCase):
    def test_failed_exploration_resumes_root_after_restart_and_partial_edits(self):
        for transport in ("cli", "http"):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as root, \
                 patch.object(phase_checkpoint.config, "DATA_DIR", Path(root) / "data"), \
                 patch.object(phase_checkpoint.config, "REPO_PATH", Path(root)), \
                 patch.object(agent_runner.config, "AGENT", "opencode"), \
                 patch.object(agent_runner.config, "OPENCODE_TRANSPORT", transport), \
                 patch.object(agent_runner, "_opencode_environment", return_value={}):
                subprocess.run(["git", "init", "-q", root], check=True)
                (Path(root) / ".gitignore").write_text("data/\n")
                state = {"state": "EXPLORING", "branch": "feature", "item": "task"}
                events = [
                    {"type": "session_start", "sessionID": "ses_root"},
                    {"type": "tool_use", "sessionID": "ses_root", "sourceSessionID": "ses_child",
                     "part": {"type": "tool", "tool": "read"}},
                    {"type": "error", "sessionID": "ses_root", "error": {"message": "fetch failed"}},
                ]
                def failed(cmd, **kwargs):
                    for event in events:
                        agent_runner._stream_line(json.dumps(event))
                    self.assertEqual(phase_checkpoint.interrupted(state, "explore"), "ses_root")
                    (Path(root) / "findings.txt").write_text("existing findings")
                    return subprocess.CompletedProcess(cmd, 1, "\n".join(map(json.dumps, events)), "")
                try:
                    agent_runner.set_task_context(state)
                    with patch.object(agent_runner, "_run_streaming", side_effect=failed):
                        with self.assertRaises(AgentTransportError):
                            agent_runner.run("explore")
                    # A utility must not overwrite the failed coding invocation.
                    agent_runner.set_task_context(dict(state))
                    utility = subprocess.CompletedProcess([], 0, json.dumps({"type": "text",
                        "sessionID": "ses_utility", "part": {"text": "Spanish"}}), "")
                    with patch.object(agent_runner, "_run_streaming", return_value=utility):
                        agent_runner.run("classify", contract=False)
                    self.assertEqual(phase_checkpoint.interrupted(state, "explore"), "ses_root")
                    self.assertIsNone(phase_checkpoint.interrupted(dict(state, state="PROPOSING"), "explore"))
                    successful = subprocess.CompletedProcess([], 0, json.dumps({"type": "text",
                        "sessionID": "ses_root", "part": {"text": "complete"}}), "")
                    with patch.object(agent_runner, "_run_streaming", return_value=successful) as run:
                        result = agent_runner.run("explore")
                        if transport == "http":
                            request = json.loads(run.call_args.kwargs["input_text"])
                            self.assertEqual(request["sessionID"], "ses_root")
                            prompt = request["prompt"]
                        else:
                            cmd = run.call_args.args[0]
                            self.assertEqual(cmd[cmd.index("--session") + 1], "ses_root")
                            prompt = cmd[-1]
                        self.assertIn("connection failure", prompt)
                        self.assertIn("subagent results", prompt)
                        self.assertEqual(result.output, "complete")
                        agent_runner.run("explore")
                        self.assertEqual(run.call_count, 1)
                    self.assertIsNone(phase_checkpoint.interrupted(state, "explore"))
                    self.assertEqual((Path(root) / "findings.txt").read_text(), "existing findings")
                finally:
                    agent_runner.set_task_context(None)

    def test_timeout_keeps_fresh_review_session_instead_of_creating_another(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.object(phase_checkpoint.config, "DATA_DIR", Path(root) / "data"), \
             patch.object(phase_checkpoint.config, "REPO_PATH", Path(root)), \
             patch.object(agent_runner.config, "AGENT", "opencode"):
            subprocess.run(["git", "init", "-q", root], check=True)
            (Path(root) / ".gitignore").write_text("data/\n")
            state = {"state": "INTERNAL_REVIEW", "branch": "b", "item": "task"}
            def timeout(*args):
                agent_runner._stream_line(json.dumps({"type": "session_start", "sessionID": "review"}))
                raise subprocess.TimeoutExpired("opencode", 10)
            agent_runner.set_task_context(state)
            try:
                with patch.object(agent_runner, "_opencode", side_effect=timeout):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        agent_runner.resume("implementation", "review code")
                agent_runner.set_task_context({"state": "INTERNAL_REVIEW", "branch": "b", "item": "task"})
                with patch.object(agent_runner, "_opencode", return_value=agent_runner.OpenCodeResult("review", '{"status": "done", "summary": "done"}')) as run:
                    agent_runner.resume("implementation", "review code")
                self.assertEqual(run.call_args.args[1], "review")
                self.assertIn("connection failure", run.call_args.args[0])
            finally:
                agent_runner.set_task_context(None)

    def test_interrupted_session_retired_or_changed_agent_is_not_reused(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.object(phase_checkpoint.config, "DATA_DIR", Path(root) / "data"), \
             patch.object(phase_checkpoint.config, "REPO_PATH", Path(root)), \
             patch.object(agent_runner.config, "AGENT", "opencode"):
            state = {"state": "EXPLORING", "branch": "b", "item": "task"}
            phase_checkpoint.remember_session(state, "explore", "ses_old")
            with patch.object(agent_runner.config, "AGENT", "claude"):
                self.assertIsNone(phase_checkpoint.interrupted(state, "explore"))
            phase_checkpoint.retire(state, "EXPLORING")
            self.assertIsNone(phase_checkpoint.interrupted(state, "explore"))

    def test_missing_interrupted_session_reconstructs_context_and_tracks_replacement(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.object(phase_checkpoint.config, "DATA_DIR", Path(root) / "data"), \
             patch.object(phase_checkpoint.config, "REPO_PATH", Path(root)), \
             patch.object(agent_runner.config, "AGENT", "opencode"):
            state = {"state": "EXPLORING", "branch": "b", "item": "task"}
            phase_checkpoint.remember_session(state, "explore", "ses_missing")
            def replacement(*args):
                agent_runner._stream_line(json.dumps({"type": "session_start", "sessionID": "ses_new"}))
                raise ConnectionError("fetch failed again")
            agent_runner.set_task_context(state)
            try:
                calls = []
                def invoke(prompt, session_id=None):
                    calls.append((prompt, session_id))
                    if session_id:
                        raise RuntimeError("Session not found")
                    return replacement(prompt)
                with patch.object(agent_runner, "_opencode", side_effect=invoke):
                    with self.assertRaises(ConnectionError):
                        agent_runner.run("explore")
                self.assertEqual(calls[0][1], "ses_missing")
                self.assertIn("repository's current branch", calls[1][0])
                self.assertEqual(phase_checkpoint.interrupted(state, "explore"), "ses_new")
            finally:
                agent_runner.set_task_context(None)

    def test_review_uses_fresh_session_and_bounded_handoff(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.object(phase_checkpoint.config, "DATA_DIR", Path(root) / "data"), \
             patch.object(phase_checkpoint.config, "REPO_PATH", Path(root)), \
             patch.object(agent_runner.config, "AGENT", "opencode"), \
             patch("agent_runner._opencode", return_value=agent_runner.OpenCodeResult("review", '{"status": "done", "summary": "done"}')) as call:
            subprocess.run(["git", "init", "-q", root], check=True)
            (Path(root) / ".gitignore").write_text("data/\n")
            state = {"state": "INTERNAL_REVIEW", "branch": "b", "item": "t"}
            agent_runner.set_task_context(state)
            try:
                agent_runner.resume("huge_history", "review")
                self.assertIsNone(call.call_args.args[1])
                self.assertIn("Durable task handoff", call.call_args.args[0])
                self.assertEqual(state["phase_sessions"]["INTERNAL_REVIEW"], "review")
            finally:
                agent_runner.set_task_context(None)
    def test_crash_before_fsm_save_replays_completed_turn_not_agent(self):
        with tempfile.TemporaryDirectory() as root, \
             patch.object(phase_checkpoint.config, "DATA_DIR", Path(root) / "data"), \
             patch.object(phase_checkpoint.config, "REPO_PATH", Path(root)), \
             patch.object(agent_runner.config, "AGENT", "opencode"), \
             patch("agent_runner._opencode", return_value=agent_runner.OpenCodeResult("sid", "done")) as run:
            state = {"state": "IMPLEMENTING", "branch": "feature", "item": "task"}
            subprocess.run(["git", "init", "-q", root], check=True)
            (Path(root) / ".gitignore").write_text("data/\n")
            agent_runner.set_task_context(state)
            try:
                first = agent_runner.resume("old", "implement")
                second = agent_runner.resume("old", "implement")
                self.assertEqual(first.output, second.output)
                self.assertEqual(run.call_count, 1)
                phase_checkpoint.retire(state, "IMPLEMENTING")
                agent_runner.resume("sid", "implement")
                self.assertEqual(run.call_count, 2)
            finally:
                agent_runner.set_task_context(None)


class ReviewSessionHandover(unittest.TestCase):
    def test_reviewer_session_never_replaces_the_implementer(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import Mock
        with patch.dict(sys.modules, {"gdoc_client": Mock(), "task_source": Mock(), "gmail_client": Mock()}):
            import main
        state = {"state": "INTERNAL_REVIEW", "item": "t", "session_id": "implementer",
                 "phase_sessions": {"INTERNAL_REVIEW": "reviewer"}}
        with patch.object(main, "save_state"), patch.object(main.check_plan, "remember"):
            main.handle_result(state, SimpleNamespace(session_id="reviewer", output="ok", question=None,
                                                      attachments=[], preamble=""), "INTERNAL_REVIEW")
        self.assertEqual(state["session_id"], "implementer")
        self.assertEqual(state["review_session_id"], "reviewer")
