"""Focused behavior tests for the OpenCode CLI adapter."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_runner
import claude_runner


class StreamingTests(unittest.TestCase):
    def test_operation_timeout_reports_operation_limit_and_keeps_partial_output(self):
        import operations
        expired = {"id": "child", "kind": "subagent", "label": "task", "timeout": 600}
        with patch.object(operations, "expired", return_value=[expired]):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                agent_runner._run_streaming([sys.executable, "-c",
                    "import time; print('partial', flush=True); time.sleep(30)"],
                    cwd=None, env=dict(os.environ), timeout=1800, on_line=lambda _: None)
        self.assertEqual(caught.exception.timeout, 600)
        self.assertIn("subagent task", caught.exception.stderr)
        self.assertIn("partial", caught.exception.output)
    def test_summarize_events(self):
        cases = [
            ({"type": "text", "part": {"text": "  Running lint now.\n"}}, "[agent] Running lint now."),
            ({"type": "tool_use", "part": {"tool": "bash", "state": {
                "status": "completed", "title": "ignored when command present",
                "input": {"command": "cd frontend && npm run lint"}}}},
             "[tool bash] cd frontend && npm run lint"),
            ({"type": "tool_use", "part": {"tool": "read", "state": {
                "status": "completed", "input": {"filePath": "/repo/app.py"}}}},
             "[tool read] /repo/app.py"),
            ({"type": "tool_use", "part": {"tool": "bash", "state": {
                "status": "error", "error": "exit 1", "input": {"command": "false"}}}},
             "[tool bash] false -> ERROR: exit 1"),
            ({"type": "error", "error": "bad credentials"}, "[error] bad credentials"),
            ({"type": "step_finish", "part": {"reason": "tool-calls", "tokens": {"total": 80114}}},
             "[step] tool-calls tokens=80114"),
            ({"type": "step_start", "part": {}}, None),
            ({"type": "text", "part": {"text": "   "}}, None),
        ]
        for event, expected in cases:
            with self.subTest(event=event):
                self.assertEqual(agent_runner.summarize_event(event), expected)

    def test_long_details_are_clipped(self):
        line = agent_runner.summarize_event(
            {"type": "tool_use", "part": {"tool": "bash", "state": {"input": {"command": "x" * 1000}}}})
        self.assertLess(len(line), 400)
        self.assertTrue(line.endswith("[...]"))

    def test_run_streaming_hands_lines_over_as_they_arrive_and_returns_completed_process(self):
        seen = []
        script = "import sys; print('{\"type\":\"text\"}'); print('two'); sys.stderr.write('warn'); sys.exit(3)"
        proc = agent_runner._run_streaming(
            [sys.executable, "-c", script], cwd=None, env=dict(os.environ), timeout=30,
            on_line=seen.append)
        self.assertEqual(seen, ['{"type":"text"}', "two"])
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(proc.stdout, '{"type":"text"}\ntwo\n')
        self.assertEqual(proc.stderr, "warn")

    def test_run_streaming_kills_on_timeout(self):
        script = "import time, sys; print('started', flush=True); time.sleep(30)"
        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            agent_runner._run_streaming([sys.executable, "-c", script], cwd=None,
                                        env=dict(os.environ), timeout=0.5, on_line=lambda _: None)
        self.assertIn("started", raised.exception.output)

    def test_stream_line_logs_tool_calls_and_ignores_garbage(self):
        with self.assertLogs("agent", level="INFO") as logs:
            agent_runner._stream_line("not json")
            agent_runner._stream_line('{"type":"step_start","part":{}}')
            agent_runner._stream_line('{"type":"tool_use","part":{"tool":"bash","state":{"input":{"command":"ls"}}}}')
        self.assertEqual(logs.output, ["INFO:agent:[tool bash] ls"])


class OpenCodeRunnerTests(unittest.TestCase):
    def test_utility_configuration_omits_coding_plugins(self):
        token = agent_runner._utility.set(True)
        try:
            config = json.loads(agent_runner._opencode_environment()["OPENCODE_CONFIG_CONTENT"])
            self.assertEqual(config["plugin"], [])
            self.assertEqual(config["default_agent"], "utility")
        finally:
            agent_runner._utility.reset(token)
    def test_role_routing_does_not_change_primary_model(self):
        with patch.object(agent_runner.config, "OPENCODE_ROLE_MODELS", {"explore": "provider/fast"}), \
             patch.object(agent_runner.config, "OPENCODE_ROLE_EFFORTS", {"explore": "low"}):
            config = json.loads(agent_runner._opencode_environment()["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["model"], agent_runner.config.OPENCODE_MODEL)
        self.assertEqual(config["agent"]["explore"]["model"], "provider/fast")
        self.assertEqual(config["agent"]["explore"]["options"]["reasoningEffort"], "low")
    def setUp(self):
        self.transport = patch.object(agent_runner.config, "OPENCODE_TRANSPORT", "cli")
        self.transport.start()
        self.addCleanup(self.transport.stop)
    superpowers = Path("/managed/superpowers")
    bridge = Path("/managed/bridge")

    def _successful_process(self, session_id="ses_123"):
        return subprocess.CompletedProcess(
            [], 0, json.dumps({
                "type": "text",
                "sessionID": session_id,
                "part": {"text": "done"},
            }), "")

    def test_working_prompts_name_the_active_conversation_channel(self):
        with patch.object(agent_runner.config, "COMM_CHANNEL", "slack"):
            prompt = agent_runner._language_prompt("Continue exploring.")
            self.assertIn("this task's Slack thread", prompt)
            self.assertIn("Do not say you will email the user", prompt)
            self.assertIn("secure runtime configuration", prompt)
            self.assertNotIn("by email", claude_runner.SENTINEL_CONTRACT)
            self.assertNotIn("relevant email", claude_runner.EVIDENCE_CONTRACT)
        with patch.object(agent_runner.config, "COMM_CHANNEL", "email"):
            prompt = agent_runner._language_prompt("Continue exploring.")
            self.assertIn("task's email thread", prompt)
            self.assertIn("do not describe this conversation as Slack", prompt)

    def assert_managed_config(self, call):
        self.assertIn("env", call.kwargs, "OpenCode subprocess is missing managed environment")
        environment = call.kwargs["env"]
        inline = json.loads(environment["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(inline["model"], agent_runner.config.OPENCODE_MODEL)
        self.assertEqual(inline["share"], "disabled")
        self.assertIs(inline["autoupdate"], False)
        self.assertEqual(inline["plugin"].count(str(self.superpowers)), 1)
        self.assertEqual(inline["plugin"].count(str(self.bridge)), 1)
        self.assertEqual(inline["skills"]["paths"].count(
            str(agent_runner.config.OPENSPEC_SKILLS_DIR / "opencode")), 1)

    def test_collects_final_text_and_session_id(self):
        stdout = '\n'.join([
            '{"type":"step_start","sessionID":"ses_123","part":{}}',
            '{"type":"text","sessionID":"ses_123","part":{"text":"first"}}',
            '{"type":"text","sessionID":"ses_123","part":{"text":"second"}}',
        ])
        proc = subprocess.CompletedProcess([], 0, stdout, "")
        with patch("agent_runner._run_streaming", return_value=proc):
            result = agent_runner._opencode("hello")
        self.assertEqual(result.session_id, "ses_123")
        self.assertEqual(result.output, "first\nsecond")

    def test_http_transport_uses_stdin_without_prompt_in_process_args(self):
        with patch.object(agent_runner.config, "OPENCODE_TRANSPORT", "http"), \
             patch("agent_runner._run_streaming", return_value=self._successful_process()) as run:
            result = agent_runner._opencode("private prompt", "ses_old")
        self.assertEqual(result.output, "done")
        self.assertEqual(run.call_args.args[0][0], "node")
        self.assertNotIn("private prompt", " ".join(run.call_args.args[0]))
        self.assertEqual(json.loads(run.call_args.kwargs["input_text"])["sessionID"], "ses_old")

    def test_raises_for_opencode_error_event(self):
        proc = subprocess.CompletedProcess(
            [], 0, '{"type":"error","sessionID":"ses_123","error":"bad credentials"}', "")
        with patch("agent_runner._run_streaming", return_value=proc):
            with self.assertRaisesRegex(RuntimeError, "bad credentials"):
                agent_runner._opencode("hello")

    def test_raises_when_session_id_is_missing(self):
        proc = subprocess.CompletedProcess([], 0, '{"type":"text","part":{"text":"done"}}', "")
        with patch("agent_runner._run_streaming", return_value=proc):
            with self.assertRaisesRegex(RuntimeError, "missing sessionID"):
                agent_runner._opencode("hello")

    def test_resume_passes_session_flag(self):
        proc = subprocess.CompletedProcess(
            [], 0, '{"type":"text","sessionID":"ses_123","part":{"text":"done"}}', "")
        with patch("agent_runner._run_streaming", return_value=proc) as run:
            agent_runner._opencode("continue", "ses_123")
        command = run.call_args.args[0]
        self.assertEqual(command[0:2], ["opencode", "run"])
        self.assertIn("--session", command)
        self.assertEqual(command[command.index("--session") + 1], "ses_123")

    def test_effort_applies_to_new_and_resumed_runs_preserving_provider_config(self):
        model_id = "gpt-6.1-sol/gpt-6.1-sol"
        existing = {"provider": {"azure": {
            "options": {"baseURL": "https://example.test"},
            "models": {model_id: {"options": {"textVerbosity": "low", "reasoningEffort": "high"},
                                  "variants": {"custom": {"reasoningEffort": "high"}}},
                       "other": {"options": {"reasoningEffort": "high"}}}}}}
        with patch.object(agent_runner.config, "OPENCODE_MODEL", "azure/" + model_id), \
             patch.object(agent_runner.config, "OPENCODE_EFFORT", "low"), \
             patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": json.dumps(existing)}), \
             patch("agent_runner._run_streaming", return_value=self._successful_process()) as run:
            agent_runner._opencode("start")
            agent_runner._opencode("continue", "ses_old")
        for call in run.call_args_list:
            command = call.args[0]
            self.assertEqual(command[command.index("--variant") + 1], "coderbot-effort")
            provider = json.loads(call.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])["provider"]["azure"]
            self.assertEqual(provider["options"], existing["provider"]["azure"]["options"])
            self.assertEqual(provider["models"][model_id]["options"],
                             {"textVerbosity": "low", "reasoningEffort": "low"})
            self.assertEqual(provider["models"][model_id]["variants"]["coderbot-effort"],
                             {"reasoningEffort": "low"})
            self.assertEqual(provider["models"][model_id]["variants"]["custom"],
                             {"reasoningEffort": "high"})
            self.assertEqual(provider["models"]["other"], existing["provider"]["azure"]["models"]["other"])

    def test_blank_effort_preserves_defaults_and_omits_variant(self):
        existing = {"provider": {"azure": {"models": {
            "gpt-6.1-sol": {"options": {"reasoningEffort": "high"}}}}}}
        with patch.object(agent_runner.config, "OPENCODE_EFFORT", ""), \
             patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": json.dumps(existing)}), \
             patch("agent_runner._run_streaming", return_value=self._successful_process()) as run:
            agent_runner._opencode("start")
        self.assertNotIn("--variant", run.call_args.args[0])
        inline = json.loads(run.call_args.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(inline["provider"], existing["provider"])

    def test_invalid_effort_fails_before_launching_opencode(self):
        with patch.object(agent_runner.config, "OPENCODE_EFFORT", "typo"), \
             patch("agent_runner._run_streaming") as run:
            with self.assertRaisesRegex(RuntimeError, "OPENCODE_EFFORT must be"):
                agent_runner._opencode("start")
        run.assert_not_called()

    def test_resume_recovers_from_missing_session(self):
        missing = subprocess.CompletedProcess([], 1, "", "Error: Session not found")
        recovered = subprocess.CompletedProcess(
            [], 0, '{"type":"text","sessionID":"ses_new","part":{"text":"done"}}', "")
        with patch.object(agent_runner.config, "AGENT", "opencode"), \
             patch("agent_runner._run_streaming", side_effect=[missing, recovered]) as run:
            result = agent_runner.resume("ses_missing", "continue")
        self.assertEqual(result.session_id, "ses_new")
        first_command = run.call_args_list[0].args[0]
        second_command = run.call_args_list[1].args[0]
        self.assertIn("--session", first_command)
        self.assertNotIn("--session", second_command)
        self.assertIn("previous OpenCode session is unavailable", second_command[-1])

    def test_resume_does_not_recover_from_other_errors(self):
        failed = subprocess.CompletedProcess([], 1, "", "Error: authentication failed")
        with patch.object(agent_runner.config, "AGENT", "opencode"), \
             patch("agent_runner._run_streaming", return_value=failed) as run:
            with self.assertRaisesRegex(RuntimeError, "authentication failed"):
                agent_runner.resume("ses_123", "continue")
        self.assertEqual(run.call_count, 1)

    def test_new_resumed_and_recovery_calls_receive_managed_inline_config(self):
        missing = subprocess.CompletedProcess([], 1, "", "Error: Session not found")
        responses = [
            self._successful_process("ses_new"),
            self._successful_process("ses_resumed"),
            missing,
            self._successful_process("ses_recovered"),
        ]
        existing = {
            "theme": "custom",
            "plugin": [
                "existing-plugin",
                str(self.superpowers),
            ],
            "skills": {
                "paths": ["/existing/skills"],
                "urls": ["https://example.test/skills"],
            },
            "permission": {"bash": "ask"},
        }
        with patch.object(agent_runner.config, "AGENT", "opencode"), \
             patch.object(agent_runner.config, "SUPERPOWERS_PLUGIN_DIR", self.superpowers,
                          create=True), \
             patch.object(agent_runner.config, "BRIDGE_PLUGIN_DIR", self.bridge, create=True), \
             patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": json.dumps(existing)}), \
             patch("agent_runner._run_streaming", side_effect=responses) as run:
            agent_runner.run("start")
            agent_runner.resume("ses_old", "continue")
            agent_runner.resume("ses_missing", "recover")

        self.assertEqual(run.call_count, 4)
        for call in run.call_args_list:
            self.assert_managed_config(call)
            inline = json.loads(call.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
            self.assertEqual(inline["theme"], "custom")
            # Trusted runtime, except coderbot-only commands (tool_policy).
            self.assertEqual(inline["permission"]["*"], "allow")
            self.assertEqual(inline["permission"]["bash"]["git push*"], "deny")
            self.assertEqual(inline["skills"]["urls"], existing["skills"]["urls"])
            self.assertEqual(inline["skills"]["paths"][0], "/existing/skills")
            self.assertIn("existing-plugin", inline["plugin"])

    def test_adds_openspec_skills_path_to_empty_config(self):
        with patch.object(agent_runner.config, "SUPERPOWERS_PLUGIN_DIR", self.superpowers), \
             patch.object(agent_runner.config, "BRIDGE_PLUGIN_DIR", self.bridge), \
             patch.object(agent_runner.config, "OPENSPEC_SKILLS_DIR", Path("/managed/os"),
                          create=True), \
             patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": "{}"}):
            inline = json.loads(agent_runner._opencode_environment()[
                "OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(inline["skills"], {"paths": ["/managed/os/opencode"]})
        self.assertEqual(inline["plugin"], [str(self.superpowers), str(self.bridge)])

    def test_rejects_non_string_plugin_entries(self):
        with patch.dict(os.environ, {
            "OPENCODE_CONFIG_CONTENT": json.dumps({"plugin": ["valid", 42]}),
        }):
            with self.assertRaisesRegex(RuntimeError, "plugin entries must be strings"):
                agent_runner._opencode_environment()

    def test_rejects_non_string_skill_path_entries(self):
        with patch.dict(os.environ, {
            "OPENCODE_CONFIG_CONTENT": json.dumps({"skills": {"paths": ["valid", 42]}}),
        }):
            with self.assertRaisesRegex(RuntimeError, "skills.paths entries must be strings"):
                agent_runner._opencode_environment()


if __name__ == "__main__":
    unittest.main()
