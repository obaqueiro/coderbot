import json
import subprocess
import unittest
from unittest.mock import patch

import agent_runner
from agent_errors import AgentContentFilterError, AgentTransportError


class AgentTransportTests(unittest.TestCase):
    def test_content_filter_is_typed_even_when_generic_error_follows(self):
        events = [
            {"type": "error", "error": {"name": "ContentFilterError", "data": {
                "message": "The response was blocked by the provider's content filter"}}},
            {"type": "error", "error": {"message": "OpenCode session failed"}},
        ]
        output = "\n".join(json.dumps(event) for event in events)
        process = subprocess.CompletedProcess(["opencode", "run"], 1, output, "stderr")
        with patch.object(agent_runner, "_run_streaming", return_value=process), \
             patch.object(agent_runner, "_opencode_environment", return_value={}):
            with self.assertRaises(AgentContentFilterError) as error:
                agent_runner._opencode("work")
        self.assertEqual(error.exception.stdout, output)
        self.assertEqual(error.exception.stderr, "stderr")
        self.assertEqual(error.exception.request, "work")
        self.assertIn("ContentFilterError", error.exception.provider_detail)

    def test_content_filter_finish_is_rejected_even_with_zero_exit(self):
        output = json.dumps({"type": "step_finish", "sessionID": "ses_one",
                             "part": {"reason": "content-filter"}})
        process = subprocess.CompletedProcess(["opencode", "run"], 0, output, "")
        with patch.object(agent_runner, "_run_streaming", return_value=process), \
             patch.object(agent_runner, "_opencode_environment", return_value={}):
            with self.assertRaises(AgentContentFilterError):
                agent_runner._opencode("work")

    def test_fetch_failure_is_typed_and_retains_full_process_output(self):
        output = json.dumps({"type": "error", "error": {"message": "fetch failed", "detail": "x" * 5000}})
        process = subprocess.CompletedProcess(["node", "bridge"], 1, output, "transport stderr")
        with patch.object(agent_runner, "_run_streaming", return_value=process), \
             patch.object(agent_runner, "_opencode_environment", return_value={}):
            with self.assertRaises(AgentTransportError) as error:
                agent_runner._opencode("work")
        self.assertEqual(error.exception.stdout, output)
        self.assertEqual(error.exception.stderr, "transport stderr")

    def test_schema_error_is_not_transport_error(self):
        process = subprocess.CompletedProcess(["node", "bridge"], 1, "invalid", "stderr")
        with patch.object(agent_runner, "_run_streaming", return_value=process), \
             patch.object(agent_runner, "_opencode_environment", return_value={}):
            with self.assertRaises(RuntimeError) as error:
                agent_runner._opencode("work")
        self.assertNotIsInstance(error.exception, AgentTransportError)
