"""Interrupt only the current coding CLI process group, never codebot's FSM."""
import os
import signal
import subprocess
import threading
from contextvars import ContextVar


class TurnKicked(BaseException):
    """An explicit user-requested restart, not a phase failure or an abort."""


_lock = threading.Lock()
_process: subprocess.Popen | None = None
_requested = False
_processes = {}
_kicked = set()
role = ContextVar("turn_role", default="work")
_roles = {}


def register(process: subprocess.Popen) -> None:
    import selfheal_events
    try:
        from pathlib import Path
        ticks = Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        selfheal_events.emit("child_started", pid=process.pid, start_ticks=ticks)
    except (OSError, IndexError):
        pass
    global _process, _requested
    with _lock:
        if role.get() == "work":
            _process = process
            _requested = False
        _processes[process.pid] = process
        _roles[process.pid] = role.get()


def release(process: subprocess.Popen) -> bool:
    global _process, _requested
    with _lock:
        _processes.pop(process.pid, None)
        _roles.pop(process.pid, None)
        was_kicked = process.pid in _kicked
        _kicked.discard(process.pid)
        if _process is not process:
            return was_kicked
        kicked = was_kicked
        _process = None
        _requested = False
        return kicked


def request_kick(target_role="work") -> bool:
    """SIGTERM the agent's own process group; escalate after a short grace period."""
    global _requested
    with _lock:
        processes = [p for p in _processes.values() if p.poll() is None
                     and (target_role is None or _roles.get(p.pid, "work") == target_role)]
        if not processes:
            return False
        if target_role == "work":
            _requested = True
        for proc in processes:
            if proc.pid in _kicked:
                continue
            _kicked.add(proc.pid)
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def escalate() -> None:
        with _lock:
            for proc in processes:
                if proc.pid in _processes and proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    timer = threading.Timer(3, escalate)
    timer.daemon = True
    timer.start()
    return True
