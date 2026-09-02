"""Focused behavior tests for managed Claude plugin wiring."""
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import claude_runner


class ClaudeRunnerTests(unittest.TestCase):
    def test_new_and_resumed_sessions_load_both_managed_plugins(self):
        superpowers = Path("/managed/superpowers")
        bridge = Path("/managed/bridge")
        responses = [
            subprocess.CompletedProcess(
                [], 0, json.dumps({"session_id": "new", "result": "done"}), ""),
            subprocess.CompletedProcess(
                [], 0, json.dumps({"session_id": "resumed", "result": "done"}), ""),
        ]
        with patch.object(claude_runner.config, "SUPERPOWERS_PLUGIN_DIR", superpowers,
                          create=True), \
             patch.object(claude_runner.config, "BRIDGE_PLUGIN_DIR", bridge, create=True), \
             patch("claude_runner._run", side_effect=responses) as run:
            claude_runner.run("start")
            claude_runner.resume("old", "continue")

        for call in run.call_args_list:
            command = call.args[0]
            plugin_dirs = [
                command[index + 1]
                for index, argument in enumerate(command)
                if argument == "--plugin-dir"
            ]
            self.assertEqual(plugin_dirs, [
                str(superpowers),
                str(bridge),
            ])


if __name__ == "__main__":
    unittest.main()
