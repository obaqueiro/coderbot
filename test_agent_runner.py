"""Focused behavior tests for the OpenCode CLI adapter."""
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_runner


class OpenCodeRunnerTests(unittest.TestCase):
    superpowers = Path("/managed/superpowers")
    bridge = Path("/managed/bridge")

    def _successful_process(self, session_id="ses_123"):
        return subprocess.CompletedProcess(
            [], 0, json.dumps({
                "type": "text",
                "sessionID": session_id,
                "part": {"text": "done"},
            }), "")

    def assert_managed_config(self, call):
        self.assertIn("env", call.kwargs, "OpenCode subprocess is missing managed environment")
        environment = call.kwargs["env"]
        inline = json.loads(environment["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(inline["model"], agent_runner.config.OPENCODE_MODEL)
        self.assertEqual(inline["share"], "disabled")
        self.assertIs(inline["autoupdate"], False)
        self.assertEqual(inline["plugin"].count(str(self.superpowers)), 1)
        self.assertEqual(inline["plugin"].count(str(self.bridge)), 1)

    def test_collects_final_text_and_session_id(self):
        stdout = '\n'.join([
            '{"type":"step_start","sessionID":"ses_123","part":{}}',
            '{"type":"text","sessionID":"ses_123","part":{"text":"first"}}',
            '{"type":"text","sessionID":"ses_123","part":{"text":"second"}}',
        ])
        proc = subprocess.CompletedProcess([], 0, stdout, "")
        with patch("agent_runner.subprocess.run", return_value=proc):
            result = agent_runner._opencode("hello")
        self.assertEqual(result.session_id, "ses_123")
        self.assertEqual(result.output, "first\nsecond")

    def test_raises_for_opencode_error_event(self):
        proc = subprocess.CompletedProcess(
            [], 0, '{"type":"error","sessionID":"ses_123","error":"bad credentials"}', "")
        with patch("agent_runner.subprocess.run", return_value=proc):
            with self.assertRaisesRegex(RuntimeError, "bad credentials"):
                agent_runner._opencode("hello")

    def test_raises_when_session_id_is_missing(self):
        proc = subprocess.CompletedProcess([], 0, '{"type":"text","part":{"text":"done"}}', "")
        with patch("agent_runner.subprocess.run", return_value=proc):
            with self.assertRaisesRegex(RuntimeError, "missing sessionID"):
                agent_runner._opencode("hello")

    def test_resume_passes_session_flag(self):
        proc = subprocess.CompletedProcess(
            [], 0, '{"type":"text","sessionID":"ses_123","part":{"text":"done"}}', "")
        with patch("agent_runner.subprocess.run", return_value=proc) as run:
            agent_runner._opencode("continue", "ses_123")
        command = run.call_args.args[0]
        self.assertEqual(command[0:2], ["opencode", "run"])
        self.assertIn("--session", command)
        self.assertEqual(command[command.index("--session") + 1], "ses_123")

    def test_resume_recovers_from_missing_session(self):
        missing = subprocess.CompletedProcess([], 1, "", "Error: Session not found")
        recovered = subprocess.CompletedProcess(
            [], 0, '{"type":"text","sessionID":"ses_new","part":{"text":"done"}}', "")
        with patch.object(agent_runner.config, "AGENT", "opencode"), \
             patch("agent_runner.subprocess.run", side_effect=[missing, recovered]) as run:
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
             patch("agent_runner.subprocess.run", return_value=failed) as run:
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
             patch("agent_runner.subprocess.run", side_effect=responses) as run:
            agent_runner.run("start")
            agent_runner.resume("ses_old", "continue")
            agent_runner.resume("ses_missing", "recover")

        self.assertEqual(run.call_count, 4)
        for call in run.call_args_list:
            self.assert_managed_config(call)
            inline = json.loads(call.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
            self.assertEqual(inline["theme"], "custom")
            self.assertEqual(inline["permission"], {"bash": "ask"})
            self.assertEqual(inline["skills"], existing["skills"])
            self.assertIn("existing-plugin", inline["plugin"])

    def test_does_not_create_inline_skills_config(self):
        with patch.object(agent_runner.config, "SUPERPOWERS_PLUGIN_DIR", self.superpowers), \
             patch.object(agent_runner.config, "BRIDGE_PLUGIN_DIR", self.bridge), \
             patch.dict(os.environ, {"OPENCODE_CONFIG_CONTENT": "{}"}):
            inline = json.loads(agent_runner._opencode_environment()[
                "OPENCODE_CONFIG_CONTENT"])
        self.assertNotIn("skills", inline)
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
