"""Typed error classification drives retry vs failure budget vs WAIT_STUCK."""
import errno
import subprocess
import unittest

import errors
from google_auth import MissingCredentials


class Kind(unittest.TestCase):
    def test_typed_errors_carry_their_kind_and_code(self):
        self.assertEqual(errors.kind(errors.Transient("x", code="gh_outage")), errors.TRANSIENT)
        self.assertEqual(errors.kind(errors.Fatal("x")), errors.FATAL)
        self.assertEqual(errors.code(errors.Transient("x", code="gh_outage")), "gh_outage")
        self.assertEqual(errors.code(ValueError("x")), "ValueError")

    def test_outages_are_transient(self):
        for error in (ConnectionResetError(), TimeoutError(), subprocess.TimeoutExpired("git", 5),
                      OSError(errno.ENOSPC, "full"), RuntimeError("fatal: unable to access 'https://x'"),
                      RuntimeError("HTTP 502 bad gateway")):
            with self.subTest(error=error):
                self.assertEqual(errors.kind(error), errors.TRANSIENT)

    def test_credentials_are_fatal_and_bugs_are_agent_faults(self):
        self.assertEqual(errors.kind(MissingCredentials("no token")), errors.FATAL)
        self.assertEqual(errors.kind(ValueError("bad gate")), errors.AGENT)
        self.assertEqual(errors.kind(OSError(errno.ENAMETOOLONG, "long")), errors.AGENT)


class Wrappers(unittest.TestCase):
    """Recorded failure payloads from each client map to the right kind."""

    def test_git_and_gh_failures_are_typed_by_their_stderr(self):
        cases = {
            "git push exited 128: fatal: Authentication failed for 'https://github.com/a/b'": errors.FATAL,
            "git fetch exited 128: git@github.com: Permission denied (publickey).": errors.FATAL,
            "gh pr view failed (exit 1): HTTP 401: Bad credentials (https://api.github.com/graphql)": errors.FATAL,
            "gh api graphql failed (exit 1): API rate limit exceeded for user ID 1": errors.TRANSIENT,
            "git push exited 1: ! [rejected] main -> main (non-fast-forward)": errors.AGENT,
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                error = errors.from_tool(message, code="git_failed")
                self.assertIsInstance(error, RuntimeError)
                self.assertEqual(errors.kind(error), expected)

    def test_main_git_raises_a_typed_error(self):
        import main
        failed = subprocess.CompletedProcess([], 128, "", "fatal: Authentication failed for 'x'")
        from unittest.mock import patch
        with patch.object(main.operations, "tool", return_value=failed):
            with self.assertRaises(errors.Fatal) as caught:
                main.git("push")
        self.assertEqual(caught.exception.code, "git_failed")

    def test_github_projects_scope_error_is_fatal(self):
        import github_projects_client as ghp
        from unittest.mock import patch
        failing = subprocess.CompletedProcess([], 1, "", "GraphQL: INSUFFICIENT_SCOPES read:project")
        with patch.object(ghp.operations, "tool", return_value=failing):
            with self.assertRaises(errors.Fatal):
                ghp._gh("api", "graphql")
        outage = subprocess.CompletedProcess([], 1, "", "HTTP 502: Bad Gateway")
        with patch.object(ghp.operations, "tool", return_value=outage):
            with self.assertRaises(errors.Transient):
                ghp._gh("api", "graphql")

    def test_http_statuses(self):
        self.assertEqual(errors.kind(errors.from_http(429, "Jira: HTTP 429", retry_after=7)), errors.TRANSIENT)
        self.assertEqual(errors.from_http(429, "x", retry_after=7).retry_after, 7)
        self.assertEqual(errors.kind(errors.from_http(401, "Jira: HTTP 401")), errors.FATAL)
        self.assertEqual(errors.kind(errors.from_http(404, "Jira: HTTP 404")), errors.AGENT)

    def test_google_http_errors(self):
        from googleapiclient.errors import HttpError
        from unittest.mock import Mock

        def http(status, reason):
            return HttpError(Mock(status=status, reason=reason), b'{"error": {"message": "%s"}}' % reason.encode())
        self.assertEqual(errors.kind(http(401, "Invalid Credentials")), errors.FATAL)
        self.assertEqual(errors.kind(http(403, "User Rate Limit Exceeded")), errors.TRANSIENT)
        self.assertEqual(errors.kind(http(403, "rateLimitExceeded")), errors.TRANSIENT)
        self.assertEqual(errors.kind(http(403, "The caller does not have permission")), errors.FATAL)
        self.assertEqual(errors.kind(http(503, "Backend Error")), errors.TRANSIENT)
        self.assertEqual(errors.kind(http(404, "Not Found")), errors.AGENT)

    def test_slack_api_errors(self):
        from slack_sdk.errors import SlackApiError
        from slack_sdk.web import SlackResponse

        def slack(code, status=200):
            return SlackApiError(code, SlackResponse(client=None, http_verb="POST", api_url="u", req_args={},
                                                     data={"ok": False, "error": code}, headers={},
                                                     status_code=status))
        self.assertEqual(errors.kind(slack("ratelimited", 429)), errors.TRANSIENT)
        self.assertEqual(errors.kind(slack("invalid_auth")), errors.FATAL)
        self.assertEqual(errors.kind(slack("not_in_channel")), errors.FATAL)
        self.assertEqual(errors.kind(slack("internal_error", 500)), errors.TRANSIENT)
        self.assertEqual(errors.kind(slack("msg_too_long")), errors.AGENT)

    def test_google_refresh_errors(self):
        from google.auth.exceptions import RefreshError
        self.assertEqual(errors.kind(RefreshError("invalid_grant: Token has been expired or revoked.")),
                         errors.FATAL)
        self.assertEqual(errors.kind(RefreshError("server error", retryable=True)), errors.TRANSIENT)


if __name__ == "__main__":
    unittest.main()
