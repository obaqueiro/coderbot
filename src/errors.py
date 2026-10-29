"""Typed failures: what the loop does with an error depends on its kind, not its text.

- Transient: the environment or a service is unavailable. Retry with backoff; never
  burns the per-state failure budget.
- AgentFault: the step itself went wrong (bad agent output, failed gate). Burns the
  failure budget; at the cap the task goes to WAIT_STUCK.
- Fatal: retrying cannot help (missing credential, broken configuration, wrong
  target). Goes straight to WAIT_STUCK with the diagnosis.

Wrappers raise these where they know the cause. `kind()` classifies everything else
(stdlib, googleapiclient, git/gh stderr) so call sites never match error strings.
"""
import errno
import re
import socket
import ssl
import subprocess

TRANSIENT, AGENT, FATAL = "transient", "agent", "fatal"


class CoderbotError(RuntimeError):
    kind = AGENT

    def __init__(self, message, *, code="", resource="", retry_after=None):
        super().__init__(message)
        self.code = code or type(self).__name__
        self.resource = resource
        self.retry_after = retry_after


class Transient(CoderbotError):
    kind = TRANSIENT


class AgentFault(CoderbotError):
    kind = AGENT


class Fatal(CoderbotError):
    kind = FATAL


TRANSIENT_ERRNOS = {errno.ENOSPC, errno.EMFILE, errno.ENFILE, errno.ENOMEM, errno.EAGAIN,
                    errno.EBUSY, errno.EIO, errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNREFUSED,
                    errno.EHOSTUNREACH, errno.ENETUNREACH}
TRANSIENT_TOOL_ERROR = re.compile(
    r"could not resolve host|unable to access|connection (?:timed out|reset|refused)|"
    r"operation timed out|network is unreachable|temporary failure in name resolution|"
    r"tls handshake|gnutls|ssl_|early eof|the remote end hung up|"
    r"(?:http|status|error)[ :/]*(?:429|500|502|503|504)\b|"
    r"bad gateway|service unavailable|gateway time-?out|rate limit|secondary rate|"
    r"api rate|overloaded", re.IGNORECASE)
_TRANSIENT_TYPES = (ssl.SSLError, ConnectionError, TimeoutError, socket.gaierror, socket.herror,
                    subprocess.TimeoutExpired)
_FATAL_NAMES = {"MissingCredentials", "WorkspaceError", "TargetMismatch"}


def kind(error: BaseException) -> str:
    if isinstance(error, CoderbotError):
        return error.kind
    if isinstance(error, _TRANSIENT_TYPES) or type(error).__name__ == "MalformedPayload":
        return TRANSIENT
    if type(error).__module__.startswith("httplib2"):  # ServerNotFoundError & co.
        return TRANSIENT
    if isinstance(error, OSError) and error.errno in TRANSIENT_ERRNOS:
        return TRANSIENT
    # git/gh/CLI wrappers raise RuntimeError with the tool's stderr in the message.
    if isinstance(error, (RuntimeError, subprocess.CalledProcessError)) and \
            TRANSIENT_TOOL_ERROR.search(f"{error} {getattr(error, 'stderr', '') or ''}"):
        return TRANSIENT
    status = getattr(getattr(error, "resp", None), "status", None)  # googleapiclient HttpError
    if status in (429, 500, 502, 503, 504):
        return TRANSIENT
    if any(cls.__name__ in _FATAL_NAMES for cls in type(error).__mro__):
        return FATAL
    return AGENT


def code(error: BaseException) -> str:
    """A stable incident code: the wrapper's code, else the exception type."""
    return getattr(error, "code", "") if isinstance(error, CoderbotError) else type(error).__name__
