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


class MissingExecutable(FileNotFoundError):
    """A required CLI is not installed. A FileNotFoundError, so `except OSError` call
    sites keep working; kind() reports it as fatal."""
    kind = FATAL

    def __init__(self, name):
        super().__init__(f"{name}: executable not found")
        self.code, self.resource, self.retry_after = "MissingExecutable", name, None


TRANSIENT_ERRNOS = {errno.ENOSPC, errno.EMFILE, errno.ENFILE, errno.ENOMEM, errno.EAGAIN,
                    errno.EBUSY, errno.EIO, errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNREFUSED,
                    errno.EHOSTUNREACH, errno.ENETUNREACH}
TRANSIENT_TOOL_ERROR = re.compile(
    r"could not resolve host|unable to access|connection (?:timed out|reset|refused)|"
    r"operation timed out|network is unreachable|temporary failure in name resolution|"
    r"tls handshake|gnutls|ssl_|early eof|the remote end hung up|"
    r"(?:http|status|error)[ :/]*(?:429|500|502|503|504)\b|"
    r"bad gateway|service unavailable|gateway time-?out|rate ?limit|secondary rate|"
    r"api rate|overloaded", re.IGNORECASE)
# Credentials or access the user must fix; retrying cannot.
FATAL_TOOL_ERROR = re.compile(
    r"authentication failed|bad credentials|requires authentication|http 401|"
    r"could not read username|permission denied \(publickey\)|gh auth login|"
    r"insufficient_scopes|token (?:has )?(?:expired|been revoked)", re.IGNORECASE)
_TRANSIENT_TYPES = (ssl.SSLError, ConnectionError, TimeoutError, socket.gaierror, socket.herror,
                    subprocess.TimeoutExpired)
_FATAL_NAMES = {"MissingCredentials", "WorkspaceError", "TargetMismatch"}
_SLACK_FATAL = {"invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired",
                "missing_scope", "not_in_channel", "channel_not_found", "is_archived"}


def http_kind(status, text="") -> str:
    if status in (408, 429) or (status or 0) >= 500 or TRANSIENT_TOOL_ERROR.search(text):
        return TRANSIENT
    return FATAL if status in (401, 403) else AGENT


def _typed(kind_name, message, **details):
    return {TRANSIENT: Transient, FATAL: Fatal}.get(kind_name, AgentFault)(message, **details)


def from_tool(message, *, code="", resource="") -> CoderbotError:
    """The typed error for a failed git/gh/CLI call, from its message and stderr."""
    found = TRANSIENT if TRANSIENT_TOOL_ERROR.search(message) else \
        FATAL if FATAL_TOOL_ERROR.search(message) else AGENT
    return _typed(found, message, code=code, resource=resource)


def from_http(status, message, *, code="", resource="", retry_after=None) -> CoderbotError:
    return _typed(http_kind(status, message), message, code=code or f"http_{status}",
                  resource=resource, retry_after=retry_after)


def kind(error: BaseException) -> str:
    if isinstance(error, (CoderbotError, MissingExecutable)):
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
    if isinstance(status, int):
        return http_kind(status, str(error))
    if type(error).__name__ == "RefreshError":  # google-auth: revoked/expired grant
        return TRANSIENT if getattr(error, "retryable", False) else FATAL
    if type(error).__name__ == "SlackApiError":
        response = getattr(error, "response", None) or {}
        slack_code = response.get("error", "") if hasattr(response, "get") else ""
        if slack_code in _SLACK_FATAL:
            return FATAL
        return TRANSIENT if slack_code == "ratelimited" else \
            http_kind(getattr(response, "status_code", 0), slack_code)
    if any(cls.__name__ in _FATAL_NAMES for cls in type(error).__mro__):
        return FATAL
    return AGENT


def code(error: BaseException) -> str:
    """A stable incident code: the wrapper's code, else the exception type."""
    return error.code if isinstance(error, (CoderbotError, MissingExecutable)) else type(error).__name__
