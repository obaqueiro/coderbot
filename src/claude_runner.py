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
import handoffs
import turn_control
import operations
import check_plan
import prompts

log = logging.getLogger(__name__)

# claude keeps its mutable config here; HOME is /home/bot in the container.
CONFIG_PATH = Path(os.environ.get("CLAUDE_CONFIG_PATH") or (Path.home() / ".claude.json"))

SENTINEL = "NEED_USER_INPUT:"

# Claude runs with cwd = the target repo, which no longer contains codebot, so
# the outbox must be referenced by absolute path.
OUTBOX_DIR = (config.DATA_DIR / "outbox").resolve()

EVIDENCE_CONTRACT = f"""
Evidence files (screenshots, screen recordings, videos, test/run reports) must NEVER
be committed to the git repo, added to a branch, or pushed as part of a PR — the repo
is for application and test code only, never for recorded proof. Whenever you produce
evidence, or the user asks you to "attach" evidence, save the file(s) under
{OUTBOX_DIR}/ and list each one's ABSOLUTE path on its own line starting with
`ATTACH: ` in your response; codebot shares them in the task's conversation thread. This
applies in every phase (implementing, addressing review feedback, answering a
question) — not only when you use the NEED_USER_INPUT mechanism below.
Questions, decisions, recommendations and context needed for a reply MUST also
appear as readable prose in your response. Never make the user open ATTACH files
or read raw JSON to discover what you are asking. Attach logs/specs/full technical
reports as supporting material, not as a replacement for the conversation.
"""

SENTINEL_CONTRACT = prompts.MINIMAL_CHANGE_POLICY + prompts.TESTING_POLICY + f"""
You are running headlessly with no interactive user. If at any point you need
the user to answer a question or make a decision, do NOT ask interactively.
Instead, END your response with a line starting exactly with `{SENTINEL}`
followed by exactly one self-contained question. Put each numbered option on its
own following line, then stop working and wait for that answer. Do not repeat the
question in your preceding summary or ask another decision in the same turn.
Otherwise finish the work and summarize what you did.
Controller configuration, duplicate check registrations and bounded timeout increases
are operational repairs, not human decisions. Emit CHECK_PLAN with explicit timeout
values when needed; never ask the user to edit the controller for them. Keep ATTACH
and CHECK_PLAN contracts separate from the final question.
Make the question self-contained: explain the decision, available options and
important consequences in plain language. Do not emit only 'answer the attached
question' or put the actionable request solely in an attachment.
{EVIDENCE_CONTRACT}
{handoffs.DECISION_INSTRUCTIONS}
Each of your turns is a brand-new, one-shot headless process: nothing monitors
this session between invocations. If you start a background process (including
via the Bash tool's run_in_background), it is orphaned the moment this turn
ends — no future turn will check on it, read its output, or wait for it. NEVER
end a turn by saying you are "waiting" for a background job to finish; that
job will never be checked again and the task will stall. Run builds, servers,
and test suites to completion in the foreground within the current turn. If
something is occupying a resource you need (e.g. a port already bound), find
and stop the actual owning process yourself rather than assuming a background
task will free it up later.
"""


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
    def __init__(self, session_id: str, output: str):
        self.session_id = session_id
        self.output = output

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
            if candidate.is_relative_to(OUTBOX_DIR) and candidate.is_file():
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
def _invoke(args: list[str], prompt: str, *, effort=None) -> ClaudeResult:
    global _fallback_until
    options = {"effort": effort} if effort is not None else {}
    proc, model = _invoke_with_model(args, prompt, _active_model(), **options)
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
    return _parse(proc)


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
    elif config.USE_CAVEMAN:
        cmd.extend(["--plugin-dir", str(config.CAVEMAN_PLUGIN_DIR)])
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


def _parse(proc: subprocess.CompletedProcess) -> ClaudeResult:
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as err:
        raise RuntimeError(f"claude returned non-JSON output: {proc.stdout[:2000]!r}") from err
    if "session_id" not in data:
        raise RuntimeError(f"claude output missing session_id: {proc.stdout[:2000]!r}")
    result = ClaudeResult(session_id=data["session_id"], output=data.get("result", ""))
    log.info("claude returned session=%s output=%d chars sentinel=%s",
             result.session_id, len(result.output), result.question is not None)
    log.debug("claude output:\n%s", result.output)
    return result


CREDENTIAL_CONTRACT = """
If a check or live test needs a credential you do not have (for example a third-party
API key), never ask for its value in a question. Instead END your response with one line
  CREDENTIAL_REQUEST: {"env": "VARIABLE_NAME", "service": "<service>", "purpose": "<why>"}
and stop working. The user either provides it (it then appears as environment variable
VARIABLE_NAME for you and for controller-run checks) or tells you to continue without it.
Never print, log, write to a file or commit a credential's value. Checks that need it list
it in "env_keys" with "optional": true, so they are skipped rather than failed without it.
"""


def contract_text() -> str:
    """The working-session contract; the credential part only when the feature is on."""
    return SENTINEL_CONTRACT + (CREDENTIAL_CONTRACT if getattr(config, "CREDENTIAL_REQUESTS", False) else "")


def run(prompt: str, contract: bool = True) -> ClaudeResult:
    # Agentic working sessions get the NEED_USER_INPUT contract. One-shot utility calls
    # (PICK, reply classifiers) pass contract=False so their sole output instruction is
    # the JSON contract in their own prompt — the sentinel contract would otherwise let
    # a classifier emit NEED_USER_INPUT instead of JSON and loop the reply forever.
    full = contract_text() + "\n\n" + prompt if contract else prompt
    if not contract:
        with operations.budget(config.UTILITY_TIMEOUT_SECONDS):
            return _invoke(["--tools", ""], full)
    return _invoke([], full)


def resume(session_id: str, prompt: str) -> ClaudeResult:
    # Re-state the evidence rule on every resumed turn, not just the task's first
    # run(): each phase is a fresh headless process resuming old conversation history,
    # and a prompt several turns back (e.g. "attach the evidence file") is easy for the
    # model to reinterpret as "commit it" without this reminder at the decision point.
    return _invoke(["--resume", session_id], EVIDENCE_CONTRACT + "\n\n" + prompt)
