"""Credentials the user hands over for one task: private storage, expiry, injection.

Values live in DATA_DIR/secrets/<task>/<NAME>.json (directory 0700, files 0600), never in
state.json, logs, transcripts, prompts or Git. They are injected into the agent's and the
checks' environments while their task is active, and registered with the log redactor.

PrivateBin links in incoming replies are captured where the bot *reads* a reply
(conversation.poll_reply): each link is registered for redaction and replaced by
``[secure-link]`` before the text reaches logs, transcripts or the model. The raw link
stays available to the credential handler for that message id only.

Every function used from an existing code path is total: on any error it logs and
behaves as if this feature did not exist. With CODEBOT_CREDENTIAL_REQUESTS off, nothing
here changes any text or environment.
"""
import hashlib
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

import config
import persistence
import privatebin
import redaction

log = logging.getLogger(__name__)

PLACEHOLDER = "[secure-link]"
_NAME = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
# Names the agent may never request: the bot's own credentials and process plumbing.
_RESERVED_PREFIXES = ("CODEBOT_", "CLAUDE_", "ANTHROPIC_", "OPENCODE_", "GH_", "GIT_", "SLACK_",
                      "LD_", "DYLD_", "PYTHON", "NODE_", "NPM_", "SSH_", "XDG_", "DOCKER_")
_RESERVED = {"PATH", "HOME", "USER", "SHELL", "LANG", "TERM", "PWD", "TMPDIR", "GITHUB_TOKEN"}

_active_task: str | None = None
_captured: dict[str, list[str]] = {}


def enabled() -> bool:
    return bool(getattr(config, "CREDENTIAL_REQUESTS", False))


def _root() -> Path:
    return config.DATA_DIR / "secrets"


def _task_dir(task_key: str) -> Path:
    return _root() / hashlib.sha256(task_key.encode()).hexdigest()[:24]


def allowed_name(name) -> bool:
    return (isinstance(name, str) and bool(_NAME.fullmatch(name)) and name not in _RESERVED
            and not name.startswith(_RESERVED_PREFIXES))


# ---------------------------------------------------------------- storage

def put(task_key: str, name: str, value: str, *, ttl: int, source: str) -> None:
    """Store a value for the task (raises on failure: the caller reports it)."""
    if not allowed_name(name):
        raise ValueError(f"{name!r} cannot be provided as a credential")
    directory = _task_dir(task_key)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(_root(), 0o700)
    os.chmod(directory, 0o700)
    payload = json.dumps({"value": value, "expires": time.time() + ttl, "source": source})
    persistence.atomic_write(directory / f"{name}.json", payload, mode=0o600)
    redaction.register(value)


def values(task_key: str | None) -> dict[str, str]:
    """Unexpired values for the task; expired files are deleted. Never raises."""
    if not task_key:
        return {}
    found = {}
    try:
        directory = _task_dir(task_key)
        if not directory.is_dir():
            return {}
        for path in directory.glob("*.json"):
            try:
                data = json.loads(path.read_text())
                if data.get("expires", 0) <= time.time() or not isinstance(data.get("value"), str):
                    path.unlink(missing_ok=True)
                    continue
                found[path.stem] = data["value"]
            except (OSError, ValueError):
                continue
    except Exception:  # noqa: BLE001
        log.warning("could not read stored credentials", exc_info=True)
    redaction.register(*found.values())
    return found


def drop_task(task_key: str | None) -> None:
    if not task_key:
        return
    try:
        shutil.rmtree(_task_dir(task_key), ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def sweep() -> None:
    """Delete expired values and empty task directories. Never raises."""
    try:
        root = _root()
        if not root.is_dir():
            return
        for directory in root.iterdir():
            if not directory.is_dir():
                continue
            for path in directory.glob("*.json"):
                try:
                    if json.loads(path.read_text()).get("expires", 0) <= time.time():
                        path.unlink(missing_ok=True)
                except (OSError, ValueError):
                    path.unlink(missing_ok=True)
            if not any(directory.iterdir()):
                directory.rmdir()
    except Exception:  # noqa: BLE001
        log.warning("could not sweep stored credentials", exc_info=True)


# ---------------------------------------------------------------- injection

def activate(task_key: str | None) -> None:
    """Select whose credentials are injected (the current task's, or nobody's)."""
    global _active_task
    _active_task = task_key or None
    if _active_task and enabled():
        values(_active_task)  # registers them with the redactor after a restart


def available(name: str) -> bool:
    try:
        return bool((os.environ.get(name) or "").strip()) or name in values(_active_task)
    except Exception:  # noqa: BLE001
        return False


def environment(base=None) -> dict:
    """``base`` (default: the process environment) plus the active task's credentials."""
    env = dict(os.environ if base is None else base)
    if not enabled():
        return env
    try:
        env.update(values(_active_task))
    except Exception:  # noqa: BLE001
        log.warning("could not add stored credentials to the environment", exc_info=True)
    return env


# ---------------------------------------------------------------- incoming links

def capture(message_id, text):
    """Replace PrivateBin links in an incoming reply with a placeholder (see module doc)."""
    if not enabled() or not isinstance(text, str):
        return text
    try:
        links = privatebin.LINK_RE.findall(text)
        if not links:
            return text
        for link in links:
            redaction.register(link, link.split("#", 1)[1])
            text = text.replace(link, PLACEHOLDER)
        _captured[str(message_id)] = links
        log.info("captured %d secure link(s) from message %s", len(links), message_id)
        return text
    except Exception:  # noqa: BLE001
        log.warning("could not inspect a reply for secure links", exc_info=True)
        return text


def take_links(message_id) -> list[str]:
    return list(_captured.get(str(message_id), []))


def forget(message_id) -> bool:
    """Drop a captured message's links; True when there were any."""
    return _captured.pop(str(message_id), None) is not None


def scrub(text: str) -> str:
    """The same replacement, for copies of the message stored elsewhere."""
    return privatebin.LINK_RE.sub(PLACEHOLDER, text) if isinstance(text, str) else text


# ---------------------------------------------------------------- agent requests

def parse_request(output) -> dict | None:
    """The last ``CREDENTIAL_REQUEST: {...}`` line of agent output, validated; else None.

    Never raises: a malformed request is ignored and the phase continues as today."""
    if not enabled() or not isinstance(output, str):
        return None
    try:
        lines = [line[len("CREDENTIAL_REQUEST:"):].strip() for line in output.splitlines()
                 if line.startswith("CREDENTIAL_REQUEST:")]
        if not lines:
            return None
        value = json.loads(lines[-1])
        name = value.get("env") if isinstance(value, dict) else None
        if not allowed_name(name):
            log.warning("ignoring credential request for a disallowed name: %r", name)
            return None
        text = lambda key, limit: str(value.get(key) or "").strip()[:limit]
        return {"env": name, "purpose": text("purpose", 300) or "testing against the real service",
                "service": text("service", 100) or "the external service",
                "optional": value.get("optional") is not False}
    except Exception:  # noqa: BLE001
        log.warning("ignoring malformed credential request", exc_info=True)
        return None
