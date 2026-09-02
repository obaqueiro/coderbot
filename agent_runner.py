"""Run the configured headless coding agent and normalize its result."""
import json
import logging
import os
import subprocess

import claude_runner
import config

log = logging.getLogger(__name__)

SENTINEL = claude_runner.SENTINEL
OUTBOX_DIR = claude_runner.OUTBOX_DIR
EVIDENCE_CONTRACT = claude_runner.EVIDENCE_CONTRACT

_SESSION_RECOVERY_CONTEXT = """
The previous OpenCode session is unavailable. Continue this task autonomously from
the repository's current branch and working tree. Reconstruct any needed context
from the OpenSpec change artifacts and git history before acting; do not ask the
user to repeat information that is available there.
"""


def _opencode_environment() -> dict[str, str]:
    env = os.environ.copy()
    try:
        inline = json.loads(env.get("OPENCODE_CONFIG_CONTENT") or "{}")
    except json.JSONDecodeError as err:
        raise RuntimeError("OPENCODE_CONFIG_CONTENT must contain valid JSON") from err
    if not isinstance(inline, dict):
        raise RuntimeError("OPENCODE_CONFIG_CONTENT must contain a JSON object")

    plugins = inline.get("plugin", [])
    skills = inline.get("skills", {})
    if not isinstance(plugins, list) or not isinstance(skills, dict):
        raise RuntimeError("OPENCODE_CONFIG_CONTENT plugin/skills values have invalid types")
    paths = skills.get("paths", [])
    if not isinstance(paths, list):
        raise RuntimeError("OPENCODE_CONFIG_CONTENT skills.paths must be an array")
    if not all(isinstance(plugin, str) for plugin in plugins):
        raise RuntimeError("OPENCODE_CONFIG_CONTENT plugin entries must be strings")
    if not all(isinstance(path, str) for path in paths):
        raise RuntimeError("OPENCODE_CONFIG_CONTENT skills.paths entries must be strings")

    managed_plugins = [str(config.SUPERPOWERS_PLUGIN_DIR), str(config.BRIDGE_PLUGIN_DIR)]
    inline["plugin"] = list(dict.fromkeys([*plugins, *managed_plugins]))
    inline.update(model=config.OPENCODE_MODEL, share="disabled", autoupdate=False)
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(inline)
    return env


class OpenCodeResult:
    def __init__(self, session_id: str, output: str):
        self.session_id = session_id
        self.output = output

    @property
    def question(self) -> str | None:
        idx = self.output.rfind(SENTINEL)
        return self.output[idx + len(SENTINEL):].strip() if idx >= 0 else None

    @property
    def attachments(self) -> list[str]:
        # Delegate path validation to the established implementation.
        return claude_runner.ClaudeResult(self.session_id, self.output).attachments


def _opencode(prompt: str, session_id: str | None = None) -> OpenCodeResult:
    cmd = ["opencode", "run", "--dir", str(config.REPO_PATH), "--model", config.OPENCODE_MODEL,
           "--auto", "--format", "json"]
    if session_id:
        cmd.extend(["--session", session_id])
    cmd.append(prompt)
    log.info("opencode %s model=%s (prompt %d chars)",
             "resume" if session_id else "run", config.OPENCODE_MODEL, len(prompt))
    proc = subprocess.run(cmd, cwd=config.REPO_PATH, env=_opencode_environment(),
                          capture_output=True, text=True,
                          timeout=config.AGENT_TIMEOUT_SECONDS)

    output, errors, observed_session = [], [], None
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError as err:
            raise RuntimeError(f"opencode returned invalid JSON event: {line[:500]!r}") from err
        observed_session = event.get("sessionID") or observed_session
        if event.get("type") == "text":
            part = event.get("part", {})
            text = part.get("text")
            if isinstance(text, str):
                output.append(text)
        elif event.get("type") == "error":
            errors.append(str(event.get("error", "unknown OpenCode error")))

    if proc.returncode != 0 or errors:
        detail = "\n".join(errors) or proc.stderr[-2000:] or proc.stdout[-2000:]
        raise RuntimeError(f"opencode exited {proc.returncode}: {detail}")
    if not observed_session:
        raise RuntimeError(f"opencode output missing sessionID: {proc.stdout[:2000]!r}")
    result = OpenCodeResult(observed_session, "\n".join(output))
    log.info("opencode returned session=%s output=%d chars sentinel=%s",
             result.session_id, len(result.output), result.question is not None)
    log.debug("opencode output:\n%s", result.output)
    return result


def run(prompt: str):
    if config.AGENT == "claude":
        return claude_runner.run(prompt)
    if config.AGENT == "opencode":
        return _opencode(claude_runner.SENTINEL_CONTRACT + "\n\n" + prompt)
    raise RuntimeError(f"unsupported CODEBOT_AGENT: {config.AGENT!r}")


def resume(session_id: str, prompt: str):
    if config.AGENT == "claude":
        return claude_runner.resume(session_id, prompt)
    if config.AGENT == "opencode":
        try:
            return _opencode(claude_runner.EVIDENCE_CONTRACT + "\n\n" + prompt, session_id)
        except RuntimeError as err:
            if "Session not found" not in str(err):
                raise
            # Sessions can be lost when an in-flight task changes agents or OpenCode's
            # local store is reset. The task artifacts and branch remain authoritative.
            log.warning("OpenCode session %s is unavailable; starting a recovery session", session_id)
            return _opencode(claude_runner.SENTINEL_CONTRACT + "\n\n" +
                             _SESSION_RECOVERY_CONTEXT + "\n\n" + prompt)
    raise RuntimeError(f"unsupported CODEBOT_AGENT: {config.AGENT!r}")
