"""Spawn and resume headless Claude Code sessions."""
import json
import logging
import os
import subprocess
import signal
import time
from pathlib import Path

import config
import secret_store
import turn_control
import operations
import check_plan
import prompts
import tool_policy
import turns

log = logging.getLogger(__name__)

# claude keeps its mutable config here; HOME is /home/bot in the container.
CONFIG_PATH = Path(os.environ.get("CLAUDE_CONFIG_PATH") or (Path.home() / ".claude.json"))

SENTINEL = "NEED_USER_INPUT:"

# Claude runs with cwd = the target repo, which no longer contains codebot, so
# the outbox must be referenced by absolute path.
OUTBOX_DIR = (config.DATA_DIR / "outbox").resolve()

# The working-turn instructions are one fixed system prompt (prompts.SYSTEM): never
# repeated inside user turns, where every resume used to pile up the same policy text.
# The names below remain for callers; OpenCode, which has no --append-system-prompt
# here, receives contract_text() at the start of a fresh session instead.
EVIDENCE_CONTRACT = ""
RESUME_REMINDER = "(End this turn with the result object described in your instructions.)"


def contract_text() -> str:
    """The working-session instructions; the credential rule only when the feature is on."""
    return prompts.system_prompt(OUTBOX_DIR, credentials=bool(getattr(config, "CREDENTIAL_REQUESTS", False)))


SENTINEL_CONTRACT = contract_text()


def sentinel_preamble(output: str) -> str:
    """Everything the agent wrote BEFORE the sentinel line that sentinel_question()
    returns the question from (e.g. the design it is asking the user to approve), or
    "" when there is no sentinel or nothing precedes it."""
    lines = output.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(SENTINEL):
            return check_plan.visible_text("\n".join(lines[:i])).strip()
    return ""


def sentinel_question(output: str) -> str | None:
    """The question after the LAST sentinel that BEGINS A LINE at column 0 (exactly as
    the contract instructs the model to emit it), else None. Untrusted text interpolated
    into prompts (e2e output, review comments) is fenced as data, but this is the
    belt-and-suspenders guard: a quoted or indented "NEED_USER_INPUT:" (email reply
    quote "> ...", a code block) never triggers a spurious question."""
    lines = output.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith(SENTINEL):
            tail = "\n".join([lines[i][len(SENTINEL):], *lines[i + 1:]])
            return check_plan.visible_text(tail).strip()
    return None


class ClaudeResult:
    def __init__(self, session_id: str, output: str, data: dict | None = None):
        self.session_id = session_id
        self.output = output
        self.data = data  # the validated result object (turns.RESULT_SCHEMA), or None

    @property
    def question(self) -> str | None:
        return sentinel_question(self.output)

    @property
    def preamble(self) -> str:
        return sentinel_preamble(self.output)

    @property
    def attachments(self) -> list[str]:
        # Model output is influenced by untrusted email content, so confine
        # attachment paths to the outbox dir to prevent host-file exfiltration.
        valid = []
        for line in self.output.splitlines():
            if not line.startswith("ATTACH:"):
                continue
            # Joining with an absolute path yields that path; the join only
            # kicks in for (out-of-contract) repo-relative ones.
            candidate = (config.REPO_PATH / line[len("ATTACH:"):].strip()).resolve()
            # os.path never raises: on Python 3.12 Path.is_file() raises for a
            # >255-byte component, and model text can be that long.
            if candidate.is_relative_to(OUTBOX_DIR) and os.path.isfile(candidate):
                valid.append(str(candidate))
        return valid


def _config_report() -> str:
    """One-line health summary of ~/.claude.json, for diagnosing corruption."""
    try:
        raw = CONFIG_PATH.read_bytes()
    except OSError as err:
        return f"{CONFIG_PATH}: unreadable ({err})"
    try:
        json.loads(raw)
        parses = "OK"
    except ValueError as err:
        parses = f"FAIL ({err})"
    mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0
    return f"{CONFIG_PATH}: {len(raw)} bytes, mtime={mtime:.0f}, parses={parses}"


def _is_config_corrupt(stderr: str) -> bool:
    return "configuration file" in stderr and "corrupted" in stderr


def _restore_config() -> bool:
    """Repair a corrupt ~/.claude.json: restore the newest parseable backup claude wrote,
    else reset to an empty object. Returns True if a valid config is now in place."""
    backups = sorted((Path.home() / ".claude" / "backups").glob(".claude.json.backup.*"))
    for backup in reversed(backups):
        try:
            data = backup.read_bytes()
            json.loads(data)
        except (OSError, ValueError):
            continue
        CONFIG_PATH.write_bytes(data)
        log.warning("restored %s from backup %s", CONFIG_PATH, backup.name)
        return True
    try:
        CONFIG_PATH.write_text("{}")
        log.warning("no parseable backup; reset %s to empty object", CONFIG_PATH)
        return True
    except OSError as err:
        log.error("could not reset %s: %s", CONFIG_PATH, err)
        return False


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    env = secret_store.environment()  # the process env plus this task's credentials
    # Overrides inherited/project defaults, including headless Claude's manual default.
    env["CAVEMAN_DEFAULT_MODE"] = (
        "caveman" if config.USE_CAVEMAN and "--tools" not in cmd else "off")
    if "--tools" not in cmd:
        env.update(tool_policy.environment(tool_policy.current_phase()))
    return operations.run(cmd, cwd=config.REPO_PATH, timeout=config.AGENT_TIMEOUT_SECONDS,
                          kind="agent", env=env)


# Monotonic deadline until which _active_model() keeps returning the fallback model.
# 0 means "no exhaustion seen recently"; set by _invoke when the primary model 429s.
_fallback_until = 0.0


def _fallback_model() -> str:
    """The configured fallback, unless it is empty or the same as the primary model."""
    fallback = (config.CLAUDE_FALLBACK_MODEL or "").strip()
    return fallback if fallback and fallback != config.CLAUDE_MODEL else ""


def _active_model() -> str:
    """The model this invocation should use: the fallback while the primary model's
    credits are known to be exhausted, otherwise the primary one."""
    if _fallback_until and time.monotonic() < _fallback_until and _fallback_model():
        return _fallback_model()
    return config.CLAUDE_MODEL


def _is_usage_exhausted(proc: subprocess.CompletedProcess) -> bool:
    """True when the CLI failed because the model's usage credits ran out. With
    --output-format json the error payload lands on stdout as
    {"api_error_status": 429, "result": "You've reached your <model> limit. ..."}."""
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return False
    if not isinstance(data, dict) or data.get("api_error_status") != 429:
        return False
    return "limit" in str(data.get("result", "")).lower()


@operations.bounded(lambda: config.AGENT_TIMEOUT_SECONDS)
def _invoke(args: list[str], prompt: str, *, effort=None, structured=False, model=None) -> ClaudeResult:
    global _fallback_until
    options = {"effort": effort} if effort is not None else {}
    proc, model = _invoke_with_model(args, prompt, model or _active_model(), **options)
    if (proc.returncode != 0 and _is_usage_exhausted(proc) and _fallback_model()
            and model != _fallback_model()):
        log.warning("model %s is out of usage credits; falling back to %s for %ds",
                    model, _fallback_model(), config.CLAUDE_FALLBACK_COOLDOWN_SECONDS)
        _fallback_until = time.monotonic() + config.CLAUDE_FALLBACK_COOLDOWN_SECONDS
        proc, model = _invoke_with_model(args, prompt, _fallback_model(), **options)
    if (proc.returncode != 0 and _is_usage_exhausted(proc)
            and model == _fallback_model()):
        # The fallback itself is exhausted; stop pinning it so the next cycle starts
        # on the primary model again (its limit window may have reset by then).
        log.warning("fallback model %s is also out of usage credits", model)
        _fallback_until = 0.0
    if proc.returncode != 0:
        # --output-format json puts the error payload on stdout, so stderr alone is
        # routinely empty on failure ("claude exited 1:"). Report both.
        raise RuntimeError(f"claude exited {proc.returncode}: "
                           f"stderr={proc.stderr[-2000:]!r} stdout={proc.stdout[-2000:]!r}")
    return _parse(proc, structured=structured)


def _invoke_with_model(args: list[str], prompt: str,
                       model: str, *, effort=None) -> tuple[subprocess.CompletedProcess, str]:
    effort = effort if effort is not None else config.CLAUDE_EFFORT
    cmd = ["claude", *args, "-p", prompt, "--model", model,
           "--effort", effort,
           "--plugin-dir", str(config.SUPERPOWERS_PLUGIN_DIR),
           "--plugin-dir", str(config.BRIDGE_PLUGIN_DIR),
           "--plugin-dir", str(config.OPENSPEC_SKILLS_DIR),
           "--dangerously-skip-permissions", "--output-format", "json"]
    if "--tools" in args:
        for _ in range(3):
            index = cmd.index("--plugin-dir")
            del cmd[index:index + 2]
    else:
        if config.USE_CAVEMAN:
            cmd.extend(["--plugin-dir", str(config.CAVEMAN_PLUGIN_DIR)])
        cmd.extend(tool_policy.claude_args(tool_policy.current_phase()))
    log.info("claude %s model=%s effort=%s (prompt %d chars); config %s",
             " ".join(args) or "run", model, effort,
             len(prompt), _config_report())
    proc = _run(cmd)
    if proc.returncode != 0 and _is_config_corrupt(proc.stderr):
        # ~/.claude.json got corrupted (historically by a concurrent writer sharing the
        # file). Snapshot its state, repair it, and retry once before giving up.
        log.error("claude reported a corrupt config; %s", _config_report())
        if _restore_config():
            log.info("retrying claude after config recovery")
            proc = _run(cmd)
    return proc, model


def _parse(proc: subprocess.CompletedProcess, *, structured: bool = False) -> ClaudeResult:
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as err:
        raise RuntimeError(f"claude returned non-JSON output: {proc.stdout[:2000]!r}") from err
    if "session_id" not in data:
        raise RuntimeError(f"claude output missing session_id: {proc.stdout[:2000]!r}")
    output, result_data = data.get("result", "") or "", None
    if structured:
        output, result_data = turns.finalize(output, data.get("structured_output"))
    result = ClaudeResult(session_id=data["session_id"], output=output, data=result_data)
    log.info("claude returned session=%s output=%d chars sentinel=%s",
             result.session_id, len(result.output), result.question is not None)
    log.debug("claude output:\n%s", result.output)
    return result


def _work_args() -> list[str]:
    return ["--append-system-prompt", contract_text(), "--json-schema", turns.SCHEMA_JSON]


def _finish(result: "ClaudeResult") -> "ClaudeResult":
    """A working turn always ends with a validated result object, rendered for the
    controller. A missing one (the CLI reports success with structured_output null when
    the model cannot satisfy the schema) gets ONE repair resume without tools; if that
    fails too, the raw text stands, which is today's behaviour."""
    if result.data is not None or not turns.needs_result(tool_policy.current_phase()):
        return result
    try:
        repaired = _invoke(["--resume", result.session_id, "--tools", "", *_work_args()], turns.REPAIR_PROMPT,
                           structured=True)
    except Exception as error:  # noqa: BLE001 - the original turn's text still stands
        log.warning("result repair failed: %s", error)
        return result
    if repaired.data is None:
        log.warning("turn ended without a result object; using its text")
        return result
    log.info("turn result recovered by a repair resume")
    return ClaudeResult(result.session_id, repaired.output, repaired.data)


UTILITY_ISOLATION = ["--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                     "--setting-sources", "", "--disable-slash-commands", "--no-session-persistence"]


def _utility(prompt: str) -> ClaudeResult:
    """A one-shot classifier/title call: no tools, settings, MCP or saved session, low
    effort, optionally a cheaper model (CODEBOT_UTILITY_MODEL); one retry on the main
    model if the cheaper one fails for another reason than exhausted credits."""
    if not config.UTILITY_MODEL:
        return _invoke(UTILITY_ISOLATION, prompt, effort="low")
    try:
        return _invoke(UTILITY_ISOLATION, prompt, effort="low", model=config.UTILITY_MODEL)
    except RuntimeError as error:
        log.warning("utility model %s failed (%s); retrying on %s", config.UTILITY_MODEL, error, _active_model())
        return _invoke(UTILITY_ISOLATION, prompt, effort="low")


def run(prompt: str, contract: bool = True) -> ClaudeResult:
    # One-shot utility calls (PICK, reply classifiers) pass contract=False so their sole
    # output instruction is the JSON contract in their own prompt.
    if not contract:
        with operations.budget(config.UTILITY_TIMEOUT_SECONDS):
            return _utility(prompt)
    return _finish(_invoke(_work_args(), prompt, structured=True))


def resume(session_id: str, prompt: str) -> ClaudeResult:
    return _finish(_invoke(["--resume", session_id, *_work_args()], prompt, structured=True))
