"""Best-effort versioned worker events; no provider or application imports."""
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import time
import traceback
import uuid

INCIDENT_VERSION = 2
SOURCE_ROOT = Path(__file__).resolve().parent.parent


def emit(kind, state=None, **fields):
    root = os.environ.get("CODEBOT_SUPERVISOR_DIR")
    if not root:
        return
    state = state or {}
    value = {"version": 1, "id": uuid.uuid4().hex, "kind": kind,
             "boot": os.environ.get("CODEBOT_WORKER_BOOT"), "epoch": os.environ.get("CODEBOT_RUNTIME_EPOCH"),
             "created": time.time(), "task_id": state.get("execution_task_id"), "phase": state.get("state"),
             "provider": os.environ.get("CODEBOT_TASK_SOURCE", "gdoc"),
             "channel": os.environ.get("CODEBOT_COMM_CHANNEL", "email"),
             "conversation": state.get("thread_id"), **fields}
    try:
        path = Path(root) / ("worker.json" if kind == "heartbeat" else "events/" + value["id"] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + value["id"] + ".tmp")
        with temporary.open("x") as stream:
            temporary.chmod(0o600)
            json.dump(value, stream)
            stream.flush()
            if kind != "heartbeat":
                os.fsync(stream.fileno())
        temporary.replace(path)
        if kind != "heartbeat":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except (OSError, TypeError, ValueError):
        pass  # Supervisor can still observe exits; telemetry never breaks tasks.


def frames(error, limit=16):
    """The innermost `file:line:function` frames, paths relative to the checkout (no values)."""
    result = [f"{_relative(frame.filename)}:{frame.lineno}:{frame.name}"
              for frame in traceback.extract_tb(error.__traceback__)[-limit:]]
    if isinstance(error, SyntaxError) and error.filename:  # the offending file never runs
        result.append(f"{_relative(error.filename)}:{error.lineno or 0}:<module>")
    return result


def _relative(filename):
    try:
        return Path(filename).resolve().relative_to(SOURCE_ROOT).as_posix()
    except (OSError, ValueError):
        return Path(filename).name


def incident_fields(error):
    """A typed incident: the supervisor dispatches on `class`/`code`, never on log text."""
    try:
        import errors
        kind, code = errors.kind(error), errors.code(error)
    except Exception:  # an import-time failure may predate the errors module
        kind, code = "agent", type(error).__name__
    if isinstance(error, SystemExit) and error.code not in (0, None):
        kind = "fatal"
    number = getattr(error, "errno", None)
    message = re.sub(r"(?i)(bearer\s+|token[=:]\s*)\S+", r"\1[REDACTED]", str(error))
    return {"version": INCIDENT_VERSION, "class": kind, "code": code,
            "errno": errno.errorcode.get(number, "") if isinstance(number, int) else "",
            "resource": str(getattr(error, "resource", "") or getattr(error, "filename", "") or "")[:300],
            "retry_after": getattr(error, "retry_after", None), "message": message[:500],
            "frames": frames(error)}


def incident(state, error, **fields):
    try:
        emit("incident", state, **incident_fields(error), **fields)
    except Exception:
        pass  # A malformed error object must never break the worker's failure path.


def runtime_epoch():
    return os.environ.get("CODEBOT_RUNTIME_EPOCH", "legacy")


def state_binding(state):
    # Versioned protocol duplicated in the independent supervisor; parity is tested.
    keys = ("execution_task_id", "item_id", "item", "state", "return_state", "stuck_return",
            "pending_question", "pending_decision", "thread_id", "workspace", "branch", "base_sha",
            "conversation_version", "pending_feedback", "replan")
    values = {key: state.get(key) for key in keys}
    if values.get("workspace"):
        values["workspace"] = {key: values["workspace"].get(key) for key in ("identity", "root", "mode")}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def recover(state):
    root = os.environ.get("CODEBOT_SUPERVISOR_DIR")
    if not root:
        return state
    path = Path(root) / "recovery.json"
    try:
        ticket = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return state
    if (not isinstance(ticket, dict) or ticket.get("version") != 1 or not ticket.get("incident") or
            ticket.get("incident") == state.get("runtime_recovery_incident") or
            ticket.get("epoch") != runtime_epoch() or ticket.get("task_id") != state.get("execution_task_id") or
            ticket.get("target") != os.environ.get("CODEBOT_REPO_PATH")):
        return state
    binding = state_binding(state)
    if binding != ticket.get("binding"):
        return state  # New decision/pause/task supersedes the repair's intent.
    if (state.get("state") == "WAIT_STUCK" and state.get("stuck_return") == ticket.get("phase") and
            ticket.get("phase") not in {"IDLE", "WAIT_REPLY", "WAIT_APPROVAL", "WAIT_MERGE", "WAIT_STUCK"}):
        state = {**state, "state": ticket["phase"]}
        state.pop("stuck_return", None)
        state.pop("stuck_error", None)
        state["runtime_recovery_incident"] = ticket["incident"]
        state["failures"] = {**state.get("failures", {}), ticket["phase"]: 0}
    return state  # Caller persists recovery before renaming/acknowledging ticket.


def acknowledge(state):
    root = os.environ.get("CODEBOT_SUPERVISOR_DIR")
    if not root:
        return
    path = Path(root) / "recovery.json"
    try:
        ticket = json.loads(path.read_text())
        if ticket.get("incident") == state.get("runtime_recovery_incident"):
            path.replace(path.with_suffix(".consumed"))
    except (OSError, json.JSONDecodeError):
        pass
