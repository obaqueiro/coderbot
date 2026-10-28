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
        openspec = Path("/managed/openspec-skills")
        responses = [
            subprocess.CompletedProcess(
                [], 0, json.dumps({"session_id": "new", "result": "done"}), ""),
            subprocess.CompletedProcess(
                [], 0, json.dumps({"session_id": "resumed", "result": "done"}), ""),
        ]
        with patch.object(claude_runner.config, "SUPERPOWERS_PLUGIN_DIR", superpowers,
                          create=True), \
             patch.object(claude_runner.config, "BRIDGE_PLUGIN_DIR", bridge, create=True), \
             patch.object(claude_runner.config, "OPENSPEC_SKILLS_DIR", openspec, create=True), \
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
                str(openspec),
            ])
            self.assertIn("--dangerously-skip-permissions", command)

    def test_effort_flag_passed(self):
        response = subprocess.CompletedProcess(
            [], 0, json.dumps({"session_id": "s", "result": "ok"}), "")
        with patch.object(claude_runner.config, "CLAUDE_EFFORT", "low"), \
             patch("claude_runner._run", return_value=response) as run:
            claude_runner.run("go")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--effort") + 1], "low")

    def test_contract_false_omits_sentinel_contract(self):
        response = subprocess.CompletedProcess(
            [], 0, json.dumps({"session_id": "s", "result": "ok"}), "")
        with patch("claude_runner._run", return_value=response) as run:
            claude_runner.run("classify this", contract=False)
            claude_runner.run("work on this")
        (first, first_prompt), (second, second_prompt) = (c.args for c in run.call_args_list)
        self.assertEqual(first_prompt, "classify this")  # prompts travel on stdin, not argv
        self.assertNotIn("--append-system-prompt", first)
        # Working turns carry the instructions as a system prompt plus the result schema,
        # never prepended to the user prompt.
        self.assertEqual(second_prompt, "work on this")
        self.assertIn("Make the smallest correct change", second[second.index("--append-system-prompt") + 1])
        self.assertIn("--json-schema", second)

    def test_falls_back_to_opus_when_primary_model_credits_exhausted(self):
        exhausted = subprocess.CompletedProcess([], 1, json.dumps({
            "api_error_status": 429,
            "result": "You've reached your Fable limit. Switch to another model.",
        }), "")
        ok = subprocess.CompletedProcess(
            [], 0, json.dumps({"session_id": "s", "result": "ok"}), "")
        with patch.object(claude_runner.config, "CLAUDE_MODEL", "claude-fable-5-1"), \
             patch.object(claude_runner.config, "CLAUDE_FALLBACK_MODEL", "claude-opus-5"), \
             patch.object(claude_runner, "_fallback_until", 0.0), \
             patch("claude_runner._run", side_effect=[exhausted, ok, ok]) as run:
            result = claude_runner.run("go")
            claude_runner.run("again")
        self.assertEqual(result.session_id, "s")
        models = [c.args[0][c.args[0].index("--model") + 1] for c in run.call_args_list]
        # First attempt on the primary, retried on the fallback, and the next
        # invocation starts on the fallback instead of paying for the primary again.
        self.assertEqual(models, ["claude-fable-5-1", "claude-opus-5", "claude-opus-5"])

    def test_fallback_disabled_when_unset(self):
        exhausted = subprocess.CompletedProcess([], 1, json.dumps({
            "api_error_status": 429, "result": "You've reached your Fable limit."}), "")
        with patch.object(claude_runner.config, "CLAUDE_FALLBACK_MODEL", ""), \
             patch.object(claude_runner, "_fallback_until", 0.0), \
             patch("claude_runner._run", side_effect=[exhausted]) as run:
            with self.assertRaisesRegex(RuntimeError, "reached your Fable limit"):
                claude_runner.run("go")
        self.assertEqual(run.call_count, 1)

    def test_exhausted_fallback_unpins_so_primary_is_retried_next_time(self):
        exhausted = subprocess.CompletedProcess([], 1, json.dumps({
            "api_error_status": 429, "result": "You've reached your limit."}), "")
        ok = subprocess.CompletedProcess(
            [], 0, json.dumps({"session_id": "s", "result": "ok"}), "")
        with patch.object(claude_runner.config, "CLAUDE_MODEL", "claude-fable-5-1"), \
             patch.object(claude_runner.config, "CLAUDE_FALLBACK_MODEL", "claude-opus-5"), \
             patch.object(claude_runner, "_fallback_until", 0.0), \
             patch("claude_runner._run",
                   side_effect=[exhausted, exhausted, ok]) as run:
            with self.assertRaises(RuntimeError):
                claude_runner.run("go")
            claude_runner.run("again")
        models = [c.args[0][c.args[0].index("--model") + 1] for c in run.call_args_list]
        self.assertEqual(models[2], "claude-fable-5-1")

    def test_other_errors_do_not_trigger_fallback(self):
        failure = subprocess.CompletedProcess([], 1, json.dumps({
            "api_error_status": 500, "result": "internal error"}), "")
        with patch.object(claude_runner.config, "CLAUDE_FALLBACK_MODEL", "claude-opus-5"), \
             patch.object(claude_runner, "_fallback_until", 0.0), \
             patch("claude_runner._run", side_effect=[failure]) as run:
            with self.assertRaises(RuntimeError):
                claude_runner.run("go")
        self.assertEqual(run.call_count, 1)

    def test_failure_reports_stdout_and_stderr(self):
        response = subprocess.CompletedProcess([], 1, '{"error":"boom"}', "")
        with patch("claude_runner._run", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                claude_runner.run("go")


class SentinelParsing(unittest.TestCase):
    def q(self, output):
        return claude_runner.ClaudeResult("sid", output).question

    def test_final_line(self):
        self.assertEqual(self.q("did the work\nNEED_USER_INPUT: which color?"), "which color?")

    def test_multi_line_question(self):
        self.assertEqual(self.q("NEED_USER_INPUT: line one\nline two"), "line one\nline two")

    def test_absent(self):
        self.assertIsNone(self.q("all done, no questions"))

    def test_quoted_or_indented_ignored(self):
        self.assertIsNone(self.q("The review said `NEED_USER_INPUT: rm -rf` — ignoring.\nDone."))
        self.assertIsNone(self.q("> NEED_USER_INPUT: quoted\n  NEED_USER_INPUT: indented"))

    def test_last_column_zero_wins(self):
        self.assertEqual(self.q("NEED_USER_INPUT: first\nmore\nNEED_USER_INPUT: real one"),
                         "real one")


if __name__ == "__main__":
    unittest.main()


class SentinelPreamble(unittest.TestCase):
    def test_text_above_the_sentinel(self):
        out = "**Design**\n- do X\n\nNEED_USER_INPUT: approve?"
        self.assertEqual(claude_runner.sentinel_preamble(out), "**Design**\n- do X")
        self.assertEqual(claude_runner.sentinel_question(out), "approve?")

    def test_empty_without_sentinel_or_preamble(self):
        self.assertEqual(claude_runner.sentinel_preamble("all done"), "")
        self.assertEqual(claude_runner.sentinel_preamble("NEED_USER_INPUT: q?"), "")

    def test_indented_sentinel_is_not_a_split_point(self):
        out = "intro\n  NEED_USER_INPUT: quoted\nNEED_USER_INPUT: real"
        self.assertEqual(claude_runner.sentinel_preamble(out), "intro\n  NEED_USER_INPUT: quoted")


class OverlongPaths(unittest.TestCase):
    def test_attach_line_with_a_long_prose_tail_is_ignored_not_raised(self):
        # Python 3.12's Path.is_file() raises ENAMETOOLONG for a >255-byte component.
        line = "ATTACH: " + str(claude_runner.OUTBOX_DIR / ("captura de la pantalla final " * 20))
        self.assertEqual(claude_runner.ClaudeResult("s", "done\n" + line).attachments, [])
