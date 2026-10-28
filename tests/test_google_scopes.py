"""Google consent is conditional on the selected source, channel and evidence."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ConditionalScopes(unittest.TestCase):
    def scopes(self, source, channel, evidence):
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ | {
                "PYTHONPATH": str(ROOT / "src"), "CODEBOT_DATA_DIR": tmp,
                "CODEBOT_TASK_SOURCE": source, "CODEBOT_COMM_CHANNEL": channel,
                "CODEBOT_EVIDENCE_UPLOAD": evidence,
            }
            output = subprocess.run([sys.executable, "-c", "import json,config; print(json.dumps(config.SCOPES))"],
                                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=10,
                                    check=True).stdout
            return json.loads(output)

    def test_jira_slack_without_upload_has_no_google_consent(self):
        self.assertEqual(self.scopes("jira", "slack", "off"), [])

    def test_gdoc_slack_and_jira_email_request_only_necessary_scopes(self):
        gdoc = self.scopes("gdoc", "slack", "off")
        self.assertEqual(len(gdoc), 2)
        self.assertTrue(any("documents" in scope for scope in gdoc))
        self.assertTrue(any("drive" in scope for scope in gdoc))
        self.assertFalse(any("gmail" in scope for scope in gdoc))
        jira_email = self.scopes("jira", "email", "off")
        self.assertEqual(len(jira_email), 1)
        self.assertIn("gmail", jira_email[0])


if __name__ == "__main__":
    unittest.main()


class CredentialLoading(unittest.TestCase):
    def test_missing_token_is_an_ordinary_error_not_process_exit(self):
        # SystemExit passed through every `except Exception`, so a missing token in an
        # optional feature (Drive upload) became a crash-restart loop.
        import google_auth
        from pathlib import Path
        from unittest.mock import patch
        with patch.object(google_auth.config, "TOKEN_PATH", Path("/nonexistent/token.json")):
            with self.assertRaises(RuntimeError):
                google_auth.load_credentials()

    def test_token_rewrite_is_atomic(self):
        import os
        import tempfile
        from pathlib import Path
        import google_auth
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "token.json"
            target.write_text("old")
            google_auth._write_atomically(target, '{"new": 1}')
            self.assertEqual(target.read_text(), '{"new": 1}')
            self.assertEqual(os.listdir(tmp), ["token.json"])
