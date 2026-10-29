"""Channel-neutral progress and bounded, safe operational log tails per task."""
import logging
import re
import threading
from collections import OrderedDict, deque
from contextvars import ContextVar

import redaction

_context: ContextVar[dict | None] = ContextVar("progress_task", default=None)
_lock = threading.Lock()
_tails: OrderedDict[str, deque] = OrderedDict()
_formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")


def task_key(state: dict) -> str:
    return str(state.get("thread_nonce") or state.get("branch") or state.get("slug") or "")


def set_context(state: dict | None) -> None:
    _context.set(state)


def sanitize(text: str) -> str:
    """Redact known credentials and inline assignments before truncating.

    Progress tails are short public lines, so the assignment heuristic below is acceptable
    here; prompts, state and logs go through ``redaction.redact`` only.
    """
    text = redaction.redact(text)
    text = re.sub(r"(?i)((?:bearer\s+)|(?:[\w-]*(?:token|secret|password|api[_-]?key)[\w-]*"
                  r"[\"']?\s*(?:=|:|\s)\s*))(?:\"[^\"]*\"|'[^']*'|[^\s\"']+)",
                  r"\1[REDACTED]", text)
    return " ".join(text.replace("```", "''' ").split())[:500]


class ProgressHandler(logging.Handler):
    """Only explicitly marked metadata is publishable, never arbitrary log bodies."""

    def emit(self, record):
        state = _context.get()
        message = getattr(record, "public_progress", None)
        key = task_key(state) if state else ""
        if not key or not isinstance(message, str):
            return
        safe_record = logging.makeLogRecord({**record.__dict__, "msg": sanitize(message), "args": ()})
        line = _formatter.format(safe_record)
        with _lock:
            _tails.setdefault(key, deque(maxlen=3)).append(line)
            _tails.move_to_end(key)
            while len(_tails) > 32:
                _tails.popitem(last=False)


def install() -> None:
    root = logging.getLogger()
    if not any(isinstance(handler, ProgressHandler) for handler in root.handlers):
        root.addHandler(ProgressHandler())


def snapshot(state: dict, *, active: bool = False, situation: str | None = None) -> dict:
    phase = state.get("state", "IDLE")
    if situation is None:
        situation = ("working" if active and phase != "IDLE" else
                     "waiting_input" if phase in {"WAIT_REPLY", "WAIT_APPROVAL", "WAIT_MERGE",
                                                  "WAIT_STUCK", "WAIT_CLEAN"} else
                     "completed" if phase == "IDLE" else "pending")
    if phase == "WAIT_REPLY":
        phase = state.get("return_state") or phase
    with _lock:
        lines = list(_tails.get(task_key(state), ()))
    return {"task_id": task_key(state), "phase": phase, "situation": situation,
             "log_lines": lines, "language": state.get("task_language", "English"),
             "waiting_for": state.get("state"), "pr_url": state.get("pr_url"),
             "video_url": state.get("evidence_url")}
