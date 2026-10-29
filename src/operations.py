"""Monotonic budgets and observable operations shared by retries and tools."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import os
import tempfile
import threading
import time
import uuid
import signal
import subprocess

import turn_control
import config
import errors


_budget: ContextVar = ContextVar("operation_budget", default=None)
_lock = threading.Lock()
_active = {}
_resource_locks = {}


class Budget:
    def __init__(self, seconds, clock=time.monotonic):
        self.clock = clock
        self.deadline = clock() + seconds

    def remaining(self, limit=None):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError("Aggregate execution budget exhausted")
        return remaining if limit is None else min(limit, remaining)


@contextmanager
def budget(seconds):
    inherited = _budget.get()
    token = _budget.set(Budget(inherited.remaining(seconds)) if inherited else Budget(seconds))
    try:
        yield _budget.get()
    finally:
        _budget.reset(token)


def bounded(seconds):
    def decorate(function):
        from functools import wraps
        @wraps(function)
        def call(*args, **kwargs):
            limit = seconds() if callable(seconds) else seconds
            with budget(limit):
                return function(*args, **kwargs)
        return call
    return decorate


def remaining(limit):
    current = _budget.get()
    return current.remaining(limit) if current else limit


def begin(kind, label, seconds, *, id=None):
    id = id or uuid.uuid4().hex
    with _lock:
        _active[id] = {"id": id, "kind": kind, "label": label[:140],
                       "role": turn_control.role.get(),
                       "started_at": time.time(), "deadline": time.monotonic() + remaining(seconds),
                       "timeout": seconds,
                       "last_progress": time.time()}
    return id


def finish(id):
    with _lock:
        _active.pop(id, None)


def snapshot(role="work"):
    with _lock:
        return [{**entry, "remaining": max(0, entry["deadline"] - time.monotonic())}
                for entry in _active.values() if role is None or entry.get("role", "work") == role]


def expired(role="work"):
    return [entry for entry in snapshot(role) if not entry["remaining"]]


def observe(event):
    """Only observable state, never commands, output or reasoning."""
    kind = event.get("type")
    part = event.get("part") or {}
    session = event.get("sourceSessionID") or event.get("sessionID")
    # Pending argument generation/queueing is not tool execution. The aggregate
    # agent deadline still bounds it; do not start or refresh a tool watchdog.
    if kind == "tool_start" and (part.get("state") or {}).get("status") != "running":
        return
    progress = kind in ("tool_start", "tool_use", "step_finish", "text", "reasoning")
    if progress and session:
        with _lock:
            for entry in _active.values():
                if entry["kind"] == "subagent" and session == entry.get("session_id"):
                    entry["deadline"] = time.monotonic() + entry["timeout"]
                    entry["last_progress"] = time.time()
    id = part.get("id")
    if kind == "tool_start" and id:
        tool = part.get("tool", "tool")
        if any(entry["id"] == id for entry in snapshot()):
            metadata = part.get("state", {}).get("metadata") or {}
            if metadata.get("sessionId"):
                with _lock:
                    _active[id]["session_id"] = metadata["sessionId"]
            return
        limit = (config.SUBAGENT_TIMEOUT_SECONDS if tool == "task" else
                 config.E2E_TIMEOUT_SECONDS if tool == "bash" else config.LOCAL_TOOL_TIMEOUT_SECONDS)
        begin("subagent" if tool == "task" else "tool", tool, limit, id=id)
        if tool == "task":
            metadata = part.get("state", {}).get("metadata") or {}
            with _lock:
                _active[id]["session_id"] = metadata.get("sessionId")
                _active[id]["parent_session_id"] = session
    elif kind == "tool_use" and id:
        finish(id)
    elif kind == "session_status":
        sid = "provider:" + str(event.get("sourceSessionID") or event.get("sessionID"))
        status = event.get("status", {}).get("type")
        if status == "retry":
            if not any(entry["id"] == sid for entry in snapshot()):
                begin("provider", "provider retry", config.PROVIDER_TIMEOUT_SECONDS, id=sid)
        else:
            finish(sid)


def clear():
    with _lock:
        for id in list(_active):
            if _active[id].get("role", "work") == turn_control.role.get():
                del _active[id]


def terminate(process, grace=3):
    """Signal the whole group even if its leader already exited."""
    if not turn_control.signal_group(process.pid, signal.SIGTERM):
        process.wait()
        return
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    turn_control.signal_group(process.pid, signal.SIGKILL)
    process.wait(timeout=5)


@contextmanager
def resources(names):
    with _lock:
        locks = [_resource_locks.setdefault(name, threading.Lock()) for name in sorted(set(names))]
    acquired = []
    waiting = begin("resource_wait", "exclusive check resource", 3600) if locks else None
    try:
        for lock in locks:
            if not lock.acquire(timeout=remaining(3600)):
                raise TimeoutError("Exclusive execution resource unavailable")
            acquired.append(lock)
        if waiting:
            finish(waiting)
            waiting = None
        yield
    finally:
        if waiting:
            finish(waiting)
        for lock in reversed(acquired):
            lock.release()


def run(argv, *, cwd=None, env=None, timeout=900, input=None, kind="check", cleanup=None,
        exclusive=()):
    with resources(exclusive), budget(timeout):
        return _run(argv, cwd=cwd, env=env, timeout=timeout, input=input, kind=kind, cleanup=cleanup)


def _run(argv, *, cwd=None, env=None, timeout=900, input=None, kind="check", cleanup=None):
    operation = begin(kind, str(argv[0]), timeout)
    process = None
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if input else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True)
        turn_control.register(process)
        try:
            stdout, stderr = process.communicate(input=input, timeout=remaining(timeout))
        except BaseException:
            terminate(process)
            raise
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    finally:
        kicked = turn_control.release(process) if process is not None else False
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()
        try:
            if cleanup:
                cleanup()
        finally:
            finish(operation)
        if kicked:
            raise turn_control.TurnKicked("user requested KICK")


STDERR_TAIL = 64 * 1024
_NO_PROMPT = {"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}


def tool(argv, *, cwd=None, env=None, timeout=None, input=None, text=True, check=False):
    """A short git/gh/CLI call: own session with a group kill on timeout or shutdown,
    output spooled to temp files (stderr keeps a bounded tail), never an interactive
    prompt. Not interruptible by KICK: a user restart must not tear a commit apart.
    A missing executable raises errors.MissingExecutable (a FileNotFoundError)."""
    timeout = remaining(timeout or config.SUBPROCESS_TIMEOUT_SECONDS)
    env = {**(os.environ if env is None else env), **_NO_PROMPT}
    data = input.encode() if isinstance(input, str) else input
    started = time.time()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=out, stderr=err, start_new_session=True,
                                       stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL)
        except FileNotFoundError as error:
            if cwd is not None and error.filename == str(cwd):
                raise
            raise errors.MissingExecutable(str(argv[0])) from error
        try:
            process.communicate(input=data, timeout=timeout)
        except BaseException as error:
            terminate(process)
            _drop_stale_index_lock(argv, cwd, started)
            if isinstance(error, subprocess.TimeoutExpired):
                raise subprocess.TimeoutExpired(argv, timeout) from None
            raise
        out.seek(0)
        stdout = out.read()
        err.seek(max(0, err.seek(0, 2) - STDERR_TAIL))
        stderr = err.read()
    if text:
        stdout, stderr = stdout.decode(errors="replace"), stderr.decode(errors="replace")
    result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result


def _drop_stale_index_lock(argv, cwd, started):
    """A killed git leaves .git/index.lock behind, failing every later git call."""
    if not argv or Path(str(argv[0])).name != "git":
        return
    lock = Path(cwd or ".") / ".git" / "index.lock"
    try:
        if lock.stat().st_mtime >= started - 1:
            lock.unlink()
    except OSError:
        pass
