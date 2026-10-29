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


if __name__ == "__main__":
    unittest.main()
