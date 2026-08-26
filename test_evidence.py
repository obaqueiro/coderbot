"""Regression tests for evidence collection input and recording wiring."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import evidence


class EvidenceTests(unittest.TestCase):
    def test_reported_specs_strip_markdown_code_delimiters(self):
        output = "summary\nE2E_SPEC: `dynamic-page-title.spec.ts`\n"

        self.assertEqual(evidence.reported_specs(output), ["dynamic-page-title.spec.ts"])

    def test_playwright_recorder_uses_target_video_environment_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            e2e_dir = Path(tmp)
            with patch.object(evidence, "E2E_DIR", e2e_dir), \
                 patch("evidence.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                evidence._record_playwright_video(["dynamic-page-title.spec.ts"])

        self.assertEqual(run.call_args.args[0], ["./run.sh", "dynamic-page-title.spec.ts"])
        self.assertEqual(run.call_args.kwargs["env"]["PICA_E2E_VIDEO"], "on")


if __name__ == "__main__":
    unittest.main()
