import gzip
import os
from pathlib import Path
import tempfile
import unittest

import attachments
import diagnostics
from execution_store import ExecutionStore


class DiagnosticsTests(unittest.TestCase):
    def test_full_chain_and_redaction_survive_reopening(self):
        with tempfile.TemporaryDirectory() as root:
            store = ExecutionStore(Path(root) / "db")
            try:
                try:
                    raise ValueError("x" * 5000 + " ghp_exampleSecretToken")
                except ValueError as cause:
                    raise RuntimeError("wrapper") from cause
            except RuntimeError as error:
                path = diagnostics.report(store, {"item": "task"}, root, root,
                                          "VERIFYING", error=error)
            text = path.read_text()
            self.assertEqual(path.suffix, ".md")
            self.assertTrue(text.startswith("# Diagnostic report\n"))
            self.assertIn("## Full exception chain\n\n```text\n", text)
            self.assertIn("x" * 5000, text)
            self.assertIn("direct cause", text)
            self.assertIn("wrapper", text)
            self.assertNotIn("ghp_exampleSecretToken", text)
            reopened = ExecutionStore(Path(root) / "db")
            self.assertEqual(len(reopened.list("incident", store.task_identity({"item": "task"}, root))), 1)

    def test_markdown_sections_preserve_prose_and_fence_raw_output(self):
        with tempfile.TemporaryDirectory() as root:
            store = ExecutionStore(Path(root) / "db")
            error = RuntimeError("failed")
            error.cmd = ["python", "check.py"]
            error.returncode = 1
            error.stdout = b"# literal heading\n```\noutput\n```\n"
            error.stderr = "Authorization: Bearer bearersecret"
            state = {"item": "task", "execution_attempt_id": "attempt-1"}
            path = diagnostics.report(store, state, root, root, "VERIFYING", error=error,
                                      detail="**Context**\n\n- Check failed.")
            text = path.read_text()
            self.assertIn("- Attempt: attempt-1\n", text)
            self.assertIn("## Details\n\n**Context**\n\n- Check failed.\n", text)
            self.assertIn("## cmd\n\n```text\n['python', 'check.py']\n```", text)
            self.assertIn("## returncode\n\n```text\n1\n```", text)
            self.assertIn("## stdout\n\n````text\n# literal heading\n```\noutput\n```\n````", text)
            self.assertIn("Authorization: Bearer [REDACTED]", text)
            self.assertNotIn("bearersecret", text)
            repeated = diagnostics.report(store, state, root, root, "VERIFYING", error=error,
                                          detail="**Context**\n\n- Check failed.")
            self.assertEqual(repeated, path)
            self.assertEqual(repeated.read_text(), text)

    def test_markdown_migrates_existing_incident_without_changing_identity(self):
        with tempfile.TemporaryDirectory() as root:
            store = ExecutionStore(Path(root) / "db")
            state = {"item": "task"}
            row = store.record("incident", state, root, "stable", {"phase": "VERIFYING"}, status="recorded")
            legacy = Path(root) / "diagnostics" / f"diagnostic-verifying-{row['id'][:12]}.txt"
            legacy.parent.mkdir()
            legacy.write_text("Original diagnostic")
            store.update("incident", row, path=str(legacy))
            path = diagnostics.report(store, state, root, root, "VERIFYING", detail="Details", identity="stable")
            self.assertEqual(path, legacy.with_suffix(".md"))
            self.assertEqual(legacy.read_text(), "Original diagnostic")
            self.assertEqual(len(store.list("incident", store.task_identity(state, root))), 1)
            self.assertEqual(store.get("incident", row["id"])["data"]["path"], str(path))

    def test_size_limits_are_lossless_for_compression_and_parts(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "report.txt"
            for data in (b"repeat" * 10000, os.urandom(30000)):
                path.write_bytes(data)
                files = attachments.prepare(attachments.describe(path, role="diagnostic"),
                                            8192, root, mime=True)
                self.assertTrue(all(attachments.encoded_size(f["size"]) <= 8192 for f in files))
                if len(files) == 1:
                    result = gzip.decompress(Path(files[0]["path"]).read_bytes())
                else:
                    result = gzip.decompress(b"".join(Path(f["path"]).read_bytes() for f in files[1:]))
                self.assertEqual(result, data)

    def test_configured_secrets_and_headers(self):
        text = "key=supersecret Authorization: Bearer bearersecret https://example?token=secret&x=1"
        result = diagnostics.redact(text, {"API_KEY": "supersecret"})
        for secret in ("supersecret", "bearersecret", "token=secret"):
            self.assertNotIn(secret, result)

    def test_long_summary_preserves_cause_instead_of_replacing_it_with_generic_error(self):
        detail = "zip-browser: unsupported timeout_millis; expected timeout. " + "context " * 100
        summary = diagnostics.summary(detail=detail)
        self.assertIn("unsupported timeout_millis", summary)
        self.assertNotIn("Execution failed; full details", summary)
        self.assertLessEqual(len(summary), 240)

    def test_structured_summary_extracts_reason_without_dumping_transport_json(self):
        self.assertEqual(diagnostics.summary(detail='{"reason":"Missing focused browser cases","checks":[]}'),
                         "Missing focused browser cases")
