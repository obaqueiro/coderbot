"""Run the configured headless coding agent and normalize its result."""
import json
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from contextvars import ContextVar
from contextlib import contextmanager

import claude_runner
import activity
import config
import secret_store
import turn_control
import phase_checkpoint
import repo_provenance
from agent_errors import AgentContentFilterError, AgentTransportError
import operations
import handoff_context
import package_registry
import performance
import progress

log = logging.getLogger(__name__)
# Live account of what the agent is doing (tool calls as they complete, text as it is
# emitted), so `docker compose logs -f` shows progress during a run instead of one
# "opencode resume" line followed by silence for an hour.
stream_log = logging.getLogger("agent")

STREAM_TEXT_MAX_CHARS = 1500
STREAM_DETAIL_MAX_CHARS = 300

SENTINEL = claude_runner.SENTINEL
OUTBOX_DIR = claude_runner.OUTBOX_DIR
EVIDENCE_CONTRACT = claude_runner.EVIDENCE_CONTRACT
_task_language: ContextVar[str | None] = ContextVar("task_language", default=None)
_task_context: ContextVar[dict | None] = ContextVar("task_context", default=None)
_utility: ContextVar[bool] = ContextVar("utility", default=False)
_lateral: ContextVar[bool] = ContextVar("lateral", default=False)
_routing: ContextVar[bool] = ContextVar("routing", default=False)
_lateral_session: ContextVar = ContextVar("lateral_session", default=None)
_invocation: ContextVar[dict | None] = ContextVar("invocation", default=None)
_turn_lock = threading.Lock()
_turn: dict = {"active": False}


def turn_snapshot() -> dict:
    """Observable activity, never the agent's prompt or its untrusted tool output."""
    with _turn_lock:
        return dict(_turn)


def set_task_context(state: dict | None) -> None:
    """Tag progress with the task being worked; never store its prompt or output."""
    _task_context.set(state)
    progress.set_context(state)


def _persist(action, *args, **kwargs) -> None:
    try:
        action(*args, **kwargs)
    except (OSError, ValueError) as error:
        log.warning("could not persist agent progress: %s", error)
    except Exception:  # noqa: BLE001 — telemetry must never fail the coding turn
        log.exception("could not persist agent progress")


def _begin_turn(session_id: str = "") -> None:
    with _turn_lock:
        _turn.clear()
        _turn.update(active=True, started_at=time.time(), last_activity=None,
                     activity="coding agent running", session_id=session_id,
                     persisted_at=0.0)
    context = _task_context.get()
    if context:
        _persist(activity.begin, context, session_id)
    stream_log.info("agent turn started", extra={"public_progress": "agent turn started"})


def _end_turn() -> None:
    operations.clear()
    with _turn_lock:
        _turn["active"] = False
        last = _turn.get("last_activity")
        label = _turn.get("activity")
        session_id = _turn.get("session_id")
    context = _task_context.get()
    if context:
        if last:
            _persist(activity.observe, context, now=last, activity=label,
                     session_id=session_id or None)
        _persist(activity.finish, context)
    stream_log.info("agent turn ended", extra={"public_progress": "agent turn ended"})


def _observe(event: dict) -> None:
    if _lateral.get():
        root = event.get("sessionID")
        if root and event.get("sourceSessionID", root) == root and _lateral_session.get():
            _lateral_session.get()(root)
        return
    invocation = _invocation.get()
    root = event.get("sessionID")
    # HTTP bridge events carry the root in sessionID and the child in
    # sourceSessionID. Never replace the recovery session with a child session.
    if (invocation is not None and isinstance(root, str) and root
            and event.get("sourceSessionID", root) == root
            and root != invocation.get("session_id")):
        phase_checkpoint.remember_session(invocation["state"], invocation["prompt"], root)
        invocation["session_id"] = root
    kind = event.get("type")
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    is_tool = kind == "tool_use" or part.get("type") == "tool"
    if kind not in ("session_start", "tool_use", "step_finish", "step_start", "text", "reasoning") and not is_tool:
        return
    observed = time.time()
    with _turn_lock:
        if _turn.get("active"):
            _turn["last_activity"] = observed
            if isinstance(event.get("sessionID"), str):
                _turn["session_id"] = event["sessionID"]
            if is_tool:
                tool = str(part.get("tool") or "tool")[:35]
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                inputs = state.get("input") if isinstance(state.get("input"), dict) else {}
                # Display short task descriptions, never commands, prompts or tool output.
                description = inputs.get("description") if tool == "task" else None
                _turn["activity"] = ("task: " + " ".join(description.split())[:100]
                                     if isinstance(description, str) and description.strip()
                                     else f"tool: {tool}")
            label = _turn["activity"] if is_tool else None
            session_id = _turn.get("session_id") or None
            persist = is_tool or observed - _turn["persisted_at"] >= 2
            if persist:
                _turn["persisted_at"] = observed
        else:
            return
    context = _task_context.get()
    if context and persist:
        _persist(activity.observe, context, activity=label, session_id=session_id, now=observed)
    if context and kind == "step_finish" and event.get("sourceSessionID", event.get("sessionID")) == _turn.get("session_id"):
        tokens = part.get("tokens") or {}
        context["session_context_tokens"] = tokens.get("total", 0)
        performance.event(str(context.get("branch", "task")), phase=context.get("state"),
                          kind="llm_step", tokens=tokens.get("total", 0))


def set_task_language(language: str | None) -> None:
    """The FSM sets this once per tick; classifiers remain outside the working session."""
    _task_language.set(language)


def _language_prompt(prompt: str) -> str:
    # Reassert the transport on every turn: a resumed agent retains older turns
    # and generic skill wording that may refer to email even in Slack mode.
    if config.COMM_CHANNEL == "slack":
        channel = ("You communicate with the user in this task's Slack thread. Coderbot "
                   "posts your questions, explanations and attachments there. Do not say "
                   "you will email the user or ask them to reply by email. If access to "
                   "secrets is needed, ask for secure runtime configuration, never for "
                   "secrets in Slack or by email. Refer to email only if the task itself "
                   "is about email.")
    else:
        channel = ("You communicate with the user through the task's email thread. "
                   "Coderbot sends your questions, explanations and attachments there; "
                   "do not describe this conversation as Slack.")
    language = _task_language.get()
    if not language:
        return channel + "\n\n" + prompt
    return (channel + "\n\n" +
            f"The backlog task's language is {language}. Use {language} for ALL human-readable "
            "text you write for this task: exploration and progress reports, questions and "
            "answers to the user, PR title/body, commit messages, and all OpenSpec artifacts "
            "(proposal.md, design.md, specs including requirement/scenario text, tasks.md). "
            "Give subagents the same language instruction. Follow the language of the "
            "backlog task even when a later chat reply or review comment is in another "
            "language; reply to a human reviewer in that reviewer's language when required. "
            "Keep machine-readable contracts and markers (NEED_USER_INPUT:, QUALITY_GATE:, "
            "INTERNAL_REVIEW:, RESOLVE:, ATTACH:, E2E_SPEC:), JSON keys/enum values, "
            "commands, code identifiers, file paths and proper names unchanged. In OpenSpec "
            "preserve parser-required headings/keywords (such as `## ADDED Requirements`, "
            "`### Requirement:`, `#### Scenario:`, SHALL, WHEN, THEN) and task checkboxes; "
            "translate the human-readable text following them.\n\n" + prompt)

_SESSION_RECOVERY_CONTEXT = """
The previous OpenCode session is unavailable. Continue this task autonomously from
the repository's current branch and working tree. Reconstruct any needed context
from the OpenSpec change artifacts and git history before acting; do not ask the
user to repeat information that is available there.
"""

_KICK_RECOVERY_CONTEXT = """The user interrupted the previous coding turn with KICK.
The task, branch and approvals are unchanged. Inspect the actual git working tree,
OpenSpec artifacts and completed tasks first; preserve partial valid work, repair
unfinished edits and continue from the last verified checkpoint. Do not blindly
repeat completed tasks or skip tests/reviews because of this interruption.
"""

_TRANSPORT_RECOVERY_CONTEXT = """The previous turn was interrupted by a connection failure.
Continue the SAME task and phase from this session's existing history. Preserve
valid partial work. Reuse findings, completed tool results and subagent results;
inspect unfinished subagent sessions and resume those by task_id when available
instead of dispatching the same investigations again. Check whether pending
commands completed before rerunning them. Finish the original request below;
do not restart exploration or repeat completed work.
"""


def _working_opencode(prompt, session_id=None, *, interrupted=False):
    if interrupted:
        prompt = _TRANSPORT_RECOVERY_CONTEXT + "\n\n" + prompt
        log.info("recovering interrupted OpenCode turn in session=%s", session_id)
    try:
        return _opencode(prompt, session_id)
    except RuntimeError as error:
        if not session_id or "Session not found" not in str(error):
            raise
        log.warning("OpenCode session %s is unavailable; starting a recovery session", session_id)
        return _opencode(claude_runner.contract_text() + "\n\n" +
                         _SESSION_RECOVERY_CONTEXT + "\n\n" + prompt)


def _after_kick(prompt: str) -> str:
    state = _task_context.get() or {}
    return _KICK_RECOVERY_CONTEXT + "\n" + prompt if state.get("kick_pending") else prompt


@contextmanager
def routing():
    """Use a lightweight routing policy without changing concurrent coding turns."""
    token = _routing.set(True)
    try:
        yield
    finally:
        _routing.reset(token)


def _opencode_effort():
    # Only lower an already configured reasoning policy; preserve other providers' defaults.
    if not _routing.get():
        return config.OPENCODE_EFFORT
    return config.OPENCODE_ROLE_EFFORTS.get("routing", "low" if config.OPENCODE_EFFORT else "")


def _invocation_timeout():
    # Routing includes service startup; give it one minute, not a coding-turn budget.
    return max(60, config.UTILITY_TIMEOUT_SECONDS) if _routing.get() else (
        config.UTILITY_TIMEOUT_SECONDS if _utility.get() else config.AGENT_TIMEOUT_SECONDS)


def _opencode_environment() -> dict[str, str]:
    env = secret_store.environment()  # the process env plus this task's credentials
    env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] = "1"
    npmrc = Path(env.get("NPM_CONFIG_USERCONFIG") or Path.home() / ".npmrc")
    if npmrc.is_file():
        runtime_npmrc = package_registry.public_runtime_npmrc(npmrc, config.DATA_DIR / "runtime.npmrc")
        env["NPM_CONFIG_USERCONFIG"] = str(runtime_npmrc)
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
    caveman_enabled = config.USE_CAVEMAN and not (_utility.get() or _lateral.get())
    env["CAVEMAN_DEFAULT_MODE"] = "caveman" if caveman_enabled else "off"
    if caveman_enabled:
        managed_plugins.append(str(config.CAVEMAN_PLUGIN_DIR / "src/plugins/opencode"))
    inline["plugin"] = list(dict.fromkeys([*plugins, *managed_plugins]))
    # The bridge plugin adds its own skills dir; the openspec-* skills live outside any
    # plugin, so they are added here (the OpenCode variant of the generated set).
    openspec_skills = str(config.OPENSPEC_SKILLS_DIR / "opencode")
    inline["skills"] = {**skills, "paths": list(dict.fromkeys([*paths, openspec_skills]))}
    if caveman_enabled:
        inline["skills"]["paths"] = list(dict.fromkeys([
            *inline["skills"]["paths"], str(config.CAVEMAN_PLUGIN_DIR / "skills")]))
        # HTTP and CLI both receive the native slash-command template.
        commands = inline.setdefault("command", {})
        if not isinstance(commands, dict):
            raise RuntimeError("OPENCODE_CONFIG_CONTENT command must contain a JSON object")
        commands["caveman"] = {"description": "Activate caveman mode (off | status)",
                               "template": "Activate caveman mode: $ARGUMENTS"}
    inline.update(model=config.OPENCODE_MODEL, share="disabled", autoupdate=False)
    # This runtime is explicitly trusted. The managed hook also overrides agents
    # loaded from project/global files after this inline configuration is merged.
    inline["permission"] = {"*": "allow"}
    agents = inline.setdefault("agent", {})
    if not isinstance(agents, dict):
        raise RuntimeError("OPENCODE_CONFIG_CONTENT agent must contain a JSON object")
    for name in set(agents) | {"build", "plan", "general", "explore"}:
        if not isinstance(agents.setdefault(name, {}), dict):
            raise RuntimeError("OPENCODE_CONFIG_CONTENT agent entries must be objects")
        agents[name]["permission"] = {"*": "allow"}
    for role, model in config.OPENCODE_ROLE_MODELS.items():
        agents.setdefault(role, {})["model"] = model
    for role, effort in config.OPENCODE_ROLE_EFFORTS.items():
        agents.setdefault(role, {}).setdefault("options", {})["reasoningEffort"] = effort
    effort = _opencode_effort()
    if effort:
        if effort not in ("none", "minimal", "low", "medium", "high", "xhigh"):
            name = "CODEBOT_OPENCODE_ROLE_EFFORTS.routing" if _routing.get() else "OPENCODE_EFFORT"
            raise RuntimeError(name + " must be none, minimal, low, medium, high or xhigh")
        provider_id, separator, model_id = config.OPENCODE_MODEL.partition("/")
        if not separator or not provider_id or not model_id:
            raise RuntimeError("OPENCODE_MODEL must be provider/model when setting OPENCODE_EFFORT")
        # Merge only the selected model, retaining custom endpoints, auth and options.
        current = inline
        for key in ("provider", provider_id, "models", model_id):
            child = current.setdefault(key, {})
            if not isinstance(child, dict):
                raise RuntimeError(f"OPENCODE_CONFIG_CONTENT {key} must contain a JSON object")
            current = child
        for key in ("options", "variants"):
            if not isinstance(current.setdefault(key, {}), dict):
                raise RuntimeError(f"OPENCODE_CONFIG_CONTENT model {key} must contain a JSON object")
        current["options"]["reasoningEffort"] = effort
        # Explicit selection also overrides a variant retained by a resumed session.
        current["variants"]["coderbot-effort"] = {"reasoningEffort": effort}
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(inline)
    if _utility.get():
        inline["plugin"] = []
        inline["skills"] = {"paths": []}
        inline["agent"] = {"utility": {"mode": "primary", "description": "Text-only utility", "permission": {"*": "allow"}}}
        inline["default_agent"] = "utility"
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(inline)
        # Do not discover project plugins/agents, including hooks which grant all
        # permissions. The conversation has no reason to run inside the checkout.
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
    if _lateral.get():
        inline["permission"] = {"*": "deny"}
        inline["mcp"] = {}
        inline["plugin"] = []
        inline["skills"] = {"paths": []}
        inline["agent"] = {"conversation": {"mode": "primary", "description": "Text-only conversation",
                                            "permission": {"*": "deny"}}}
        inline["default_agent"] = "conversation"
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(inline)
    return env


class OpenCodeResult:
    def __init__(self, session_id: str, output: str):
        self.session_id = session_id
        self.output = output

    @property
    def question(self) -> str | None:
        return claude_runner.sentinel_question(self.output)

    @property
    def preamble(self) -> str:
        return claude_runner.sentinel_preamble(self.output)

    @property
    def attachments(self) -> list[str]:
        # Delegate path validation to the established implementation.
        return claude_runner.ClaudeResult(self.session_id, self.output).attachments


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split()) if "\n" not in text.strip() else text.strip()
    return text if len(text) <= limit else text[:limit] + " [...]"


def summarize_event(event: dict) -> str | None:
    """One log line for an OpenCode JSON event, or None for events not worth a line.
    Shapes (from `opencode run --format json`): {"type":"text","part":{"text":..}},
    {"type":"tool_use","part":{"tool":..,"state":{"status","input","output","title"}}},
    {"type":"step_start"|"step_finish","part":{..}}, {"type":"error","error":..}."""
    kind = event.get("type")
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    if kind == "text":
        text = part.get("text")
        text = text.strip() if isinstance(text, str) else ""
        return f"[agent] {_clip(text, STREAM_TEXT_MAX_CHARS)}" if text else None
    if kind == "tool_use" or part.get("type") == "tool":
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        inputs = state.get("input") if isinstance(state.get("input"), dict) else {}
        detail = None
        for key in ("command", "filePath", "path", "pattern", "description", "prompt", "url"):
            if isinstance(inputs.get(key), str) and inputs[key].strip():
                detail = inputs[key]
                break
        if detail is None:
            detail = state.get("title") if isinstance(state.get("title"), str) else None
        if detail is None:
            detail = json.dumps(inputs, ensure_ascii=False) if inputs else ""
        line = f"[tool {part.get('tool', '?')}] {_clip(detail, STREAM_DETAIL_MAX_CHARS)}".rstrip()
        if state.get("status") == "error":
            line += f" -> ERROR: {_clip(str(state.get('error', '')), STREAM_DETAIL_MAX_CHARS)}"
        return line
    if kind == "error":
        return f"[error] {_clip(str(event.get('error', 'unknown OpenCode error')), STREAM_DETAIL_MAX_CHARS)}"
    if kind == "step_finish":
        tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
        total = tokens.get("total")
        return f"[step] {part.get('reason', 'finished')}" + (f" tokens={total}" if total else "")
    return None


def _stream_line(line: str) -> None:
    try:
        event = json.loads(line)
    except ValueError:
        return  # the final parse in _opencode reports malformed events
    if not isinstance(event, dict):
        return
    _observe(event)
    if not _lateral.get():
        operations.observe(event)
    summary = summarize_event(event)
    if summary is None:
        return
    # Publish only tool metadata, not the summary's fallback prompt, errors or text.
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    public = None
    if event.get("type") == "tool_use" or part.get("type") == "tool":
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        inputs = state.get("input") if isinstance(state.get("input"), dict) else {}
        detail = next((inputs[key] for key in ("command", "filePath", "path", "pattern", "description")
                       if isinstance(inputs.get(key), str)), "")
        public = f"[tool {part.get('tool', '?')}] {detail}".rstrip()
    stream_log.log(logging.DEBUG if summary.startswith("[step]") else logging.INFO, "%s", summary,
                   extra={"public_progress": public})


def _run_streaming(cmd: list[str], *, cwd, env: dict[str, str], timeout: float,
                   on_line=_stream_line, input_text=None) -> subprocess.CompletedProcess:
    """subprocess.run(capture_output=True, text=True, timeout=...) equivalent that hands
    every stdout line to on_line as it arrives. Raises subprocess.TimeoutExpired (with
    the partial output) after killing the process, as subprocess.run would."""
    effective_timeout = operations.remaining(timeout)
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, stdin=subprocess.PIPE if input_text is not None else None,
                            text=True, start_new_session=True)
    turn_control.register(proc)
    stderr_chunks: list[str] = []
    stderr_reader = threading.Thread(
        target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
    stderr_reader.start()
    timed_out = threading.Event()
    expired_operation = []
    started = time.monotonic()
    invocation_role = turn_control.role.get()

    def _kill_on_timeout() -> None:
        timed_out.set()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    timer = threading.Timer(effective_timeout, _kill_on_timeout)
    timer.daemon = True
    timer.start()
    stop_watch = threading.Event()
    def watch():
        while not stop_watch.wait(.5):
            if expired := operations.expired(invocation_role):
                expired_operation.extend(expired[:1])
                _kill_on_timeout()
                return
    watchdog = threading.Thread(target=watch, daemon=True)
    watchdog.start()
    if input_text is not None:
        proc.stdin.write(input_text)
        proc.stdin.close()
    stdout_lines: list[str] = []
    try:
        for line in proc.stdout:
            stdout_lines.append(line)
            try:
                on_line(line.rstrip("\n"))
            except Exception:  # noqa: BLE001 — logging must never abort the run
                log.exception("agent stream logging failed")
        proc.wait()
    finally:
        stop_watch.set()
        timer.cancel()
        if proc.poll() is None:
            operations.terminate(proc)
        stderr_reader.join(timeout=5)
        proc.stdout.close()
        proc.stderr.close()
        kicked = turn_control.release(proc)
    stdout, stderr = "".join(stdout_lines), "".join(stderr_chunks)
    if kicked:
        raise turn_control.TurnKicked("user requested KICK")
    if timed_out.is_set():
        if expired_operation:
            entry = expired_operation[0]
            detail = (f"{entry['kind']} {entry['label']} ({entry['id']}) exceeded "
                      f"its {entry['timeout']}s operation limit; turn elapsed {time.monotonic() - started:.1f}s")
            log.warning("agent interrupted: %s", detail)
            raise subprocess.TimeoutExpired(cmd, entry["timeout"], output=stdout,
                                            stderr=stderr + "\n" + detail)
        elapsed = time.monotonic() - started
        error = subprocess.TimeoutExpired(cmd, effective_timeout, output=stdout,
            stderr=stderr + f"\nEffective execution limit {effective_timeout:.2f}s; elapsed {elapsed:.2f}s.")
        error.elapsed = elapsed
        raise error
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _opencode(prompt: str, session_id: str | None = None) -> OpenCodeResult:
    effort = _opencode_effort()
    timeout = _invocation_timeout()
    directory = config.DATA_DIR / "conversation_runtime" if _lateral.get() else config.REPO_PATH
    if _lateral.get():
        directory.mkdir(parents=True, exist_ok=True)
    cmd = ["opencode", "run", "--dir", str(directory), "--model", config.OPENCODE_MODEL,
           "--auto", "--format", "json"]
    if _lateral.get():
        cmd.remove("--auto")  # Never let auto-approval override lateral denials.
    if effort:
        cmd.extend(["--variant", "coderbot-effort"])
    if session_id:
        cmd.extend(["--session", session_id])
    if _lateral.get():
        cmd.extend(["--agent", "conversation"])
    cmd.append(prompt)
    log.info("opencode %s model=%s effort=%s (prompt %d chars)",
             "resume" if session_id else "run", config.OPENCODE_MODEL,
             effort or "default", len(prompt))
    if config.OPENCODE_TRANSPORT == "http":
        bridge = Path(config.BRIDGE_PLUGIN_DIR) / "runtime" / "run.js"
        proc = _run_streaming(["node", str(bridge)], cwd=directory,
            env=_opencode_environment(), timeout=timeout,
            input_text=json.dumps({"directory": str(directory), "model": config.OPENCODE_MODEL,
                "sessionID": session_id, "variant": "coderbot-effort" if effort else None,
                "agent": "conversation" if _lateral.get() else "utility" if _utility.get() else "build",
                "utility": _utility.get(), "conversation": _lateral.get(),
                "prompt": prompt}))
    else:
        proc = _run_streaming(cmd, cwd=directory, env=_opencode_environment(),
                              timeout=timeout)

    output, errors, observed_session = [], [], None
    content_filtered = False
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError as err:
            raise RuntimeError(f"opencode returned invalid JSON event: {line[:500]!r}") from err
        if not observed_session and event.get("sourceSessionID", event.get("sessionID")) == event.get("sessionID"):
            observed_session = event.get("sessionID")
            _observe(event)
        if event.get("type") == "text":
            part = event.get("part", {})
            text = part.get("text")
            if isinstance(text, str):
                output.append(text)
        elif event.get("type") == "error":
            error = event.get("error")
            if isinstance(error, dict) and error.get("name") == "ContentFilterError":
                content_filtered = True
            errors.append(str(event.get("error", "unknown OpenCode error")))
        elif event.get("type") == "step_finish" and event.get("part", {}).get("reason") == "content-filter":
            content_filtered = True

    if content_filtered:
        raise AgentContentFilterError("The response was blocked by the provider's content filter", proc,
            request=prompt, partial_response="\n".join(output), provider_detail="\n".join(errors))
    if proc.returncode != 0 or errors:
        detail = "\n".join(errors) or proc.stderr or proc.stdout
        if errors and any("fetch failed" in error.casefold() for error in errors):
            raise AgentTransportError("OpenCode connection failed", proc)
        raise RuntimeError(f"opencode exited {proc.returncode}: {detail}")
    if not observed_session:
        raise RuntimeError(f"opencode output missing sessionID: {proc.stdout[:2000]!r}")
    result = OpenCodeResult(observed_session, "\n".join(output))
    log.info("opencode returned session=%s output=%d chars sentinel=%s",
             result.session_id, len(result.output), result.question is not None)
    log.debug("opencode output:\n%s", result.output)
    return result


def converse(prompt: str, session_id: str | None = None, *, on_session=None):
    """Text-only independent harness session; never starts/ends the work turn."""
    tokens = [(turn_control.role, turn_control.role.set("conversation")),
              (_lateral, _lateral.set(True)), (_utility, _utility.set(True)),
              (_task_context, _task_context.set(None)), (_invocation, _invocation.set(None)),
              (_lateral_session, _lateral_session.set(on_session))]
    try:
        with operations.budget(_invocation_timeout()):
            if config.AGENT == "claude":
                args = ["--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                        "--setting-sources", "", "--disable-slash-commands"]
                if session_id:
                    args.extend(["--resume", session_id])
                result = claude_runner._invoke(args, prompt, **({"effort": "low"} if _routing.get() else {}))
                if on_session:
                    on_session(result.session_id)
                return result
            if config.AGENT == "opencode":
                try:
                    return _opencode(prompt, session_id)
                except RuntimeError as error:
                    if session_id and "Session not found" in str(error):
                        return _opencode(prompt)
                    raise
            raise RuntimeError(f"unsupported conversation harness: {config.AGENT!r}")
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


@operations.bounded(lambda: config.AGENT_TIMEOUT_SECONDS)
def run(prompt: str, contract: bool = True):
    """Start a fresh session. contract=False for one-shot utility calls (PICK, reply
    classifiers) whose only output instruction must be their own JSON contract."""
    _begin_turn()
    utility_token = _utility.set(not contract)
    context = None
    before = None
    invocation_token = _invocation.set(None)
    input_rows = []
    try:
        context = _task_context.get() if contract else None
        if context:
            import parallel_conversation
            extra, input_rows = parallel_conversation.context_prompt(phase_checkpoint.store(), context, config.REPO_PATH,
                                                                    invocation=phase_checkpoint.key(context, prompt))
            prompt += extra
        before = None
        if context:
            try:
                before = repo_provenance.inspect(config.REPO_PATH)
            except (OSError, subprocess.SubprocessError):
                pass
            _persist(repo_provenance.record, phase_checkpoint.store(), context, config.REPO_PATH)
        original_prompt = prompt
        if context and (saved := phase_checkpoint.replay(context, prompt)):
            if input_rows:
                parallel_conversation.incorporated(phase_checkpoint.store(), input_rows, saved["session_id"])
            return OpenCodeResult(saved["session_id"], saved["output"])
        recovering = phase_checkpoint.interrupted(context, original_prompt) if context and config.AGENT == "opencode" else None
        if context and config.AGENT == "opencode":
            _invocation.set({"state": context, "prompt": original_prompt})
        if contract:
            prompt = _language_prompt(_after_kick(prompt))
        if config.AGENT == "claude":
            result = claude_runner.run(prompt, contract=contract)
        elif config.AGENT == "opencode":
            full = claude_runner.contract_text() + "\n\n" + prompt if contract else prompt
            with operations.budget(config.UTILITY_TIMEOUT_SECONDS if not contract else config.AGENT_TIMEOUT_SECONDS):
                result = (_working_opencode(full, recovering, interrupted=True) if recovering
                          else _opencode(full))
        else:
            raise RuntimeError(f"unsupported CODEBOT_AGENT: {config.AGENT!r}")
        if context:
            phase_checkpoint.record(context, original_prompt, result)
            if input_rows:
                parallel_conversation.incorporated(phase_checkpoint.store(), input_rows, result.session_id)
            _persist(repo_provenance.record, phase_checkpoint.store(), context, config.REPO_PATH)
        return result
    except AgentContentFilterError as error:
        if not error.request:
            error.request = prompt
        raise
    finally:
        if context and before is not None:
            try:
                after = repo_provenance.inspect(config.REPO_PATH)
                _persist(repo_provenance.record_turn, phase_checkpoint.store(), context,
                         config.REPO_PATH, before, after)
            except (OSError, subprocess.SubprocessError):
                pass
        _utility.reset(utility_token)
        _invocation.reset(invocation_token)
        _end_turn()


@operations.bounded(lambda: config.AGENT_TIMEOUT_SECONDS)
def resume(session_id: str, prompt: str):
    _begin_turn(session_id)
    context = None
    before = None
    invocation_token = _invocation.set(None)
    input_rows = []
    try:
        context = _task_context.get()
        if context:
            import parallel_conversation
            extra, input_rows = parallel_conversation.context_prompt(phase_checkpoint.store(), context, config.REPO_PATH,
                                                                    invocation=phase_checkpoint.key(context, prompt))
            prompt += extra
        before = None
        if context:
            try:
                before = repo_provenance.inspect(config.REPO_PATH)
            except (OSError, subprocess.SubprocessError):
                pass
            _persist(repo_provenance.record, phase_checkpoint.store(), context, config.REPO_PATH)
        original_prompt = prompt
        if context and (saved := phase_checkpoint.replay(context, prompt)):
            if input_rows:
                parallel_conversation.incorporated(phase_checkpoint.store(), input_rows, saved["session_id"])
            return OpenCodeResult(saved["session_id"], saved["output"])
        recovering = phase_checkpoint.interrupted(context, original_prompt) if context and config.AGENT == "opencode" else None
        if context and config.AGENT == "opencode":
            _invocation.set({"state": context, "prompt": original_prompt})
        if context:
            phase = context.get("state")
            sessions = context.setdefault("phase_sessions", {})
            fresh = (phase == "INTERNAL_REVIEW" and phase not in sessions) or (
                context.get("session_context_tokens", 0) > config.CONTEXT_TOKEN_BUDGET)
            if recovering:
                session_id = recovering
            elif fresh:
                prompt = handoff_context.render(context, phase) + "\n\n" + prompt
                session_id = None
                context["session_context_tokens"] = 0
        prompt = _language_prompt(_after_kick(prompt))
        if context and config.AGENT == "opencode" and session_id:
            phase_checkpoint.remember_session(context, original_prompt, session_id)
        if config.AGENT == "claude":
            result = claude_runner.resume(session_id, prompt) if session_id else claude_runner.run(prompt)
        elif config.AGENT == "opencode":
            result = _working_opencode(claude_runner.EVIDENCE_CONTRACT + "\n\n" + prompt,
                                       session_id, interrupted=bool(recovering))
        else:
            raise RuntimeError(f"unsupported CODEBOT_AGENT: {config.AGENT!r}")
        if context:
            phase_checkpoint.record(context, original_prompt, result)
            if input_rows:
                parallel_conversation.incorporated(phase_checkpoint.store(), input_rows, result.session_id)
            _persist(repo_provenance.record, phase_checkpoint.store(), context, config.REPO_PATH)
            context.setdefault("phase_sessions", {})[context["state"]] = result.session_id
        return result
    except AgentContentFilterError as error:
        if not error.request:
            error.request = prompt
        raise
    finally:
        if context and before is not None:
            try:
                after = repo_provenance.inspect(config.REPO_PATH)
                _persist(repo_provenance.record_turn, phase_checkpoint.store(), context,
                         config.REPO_PATH, before, after)
            except (OSError, subprocess.SubprocessError):
                pass
        _invocation.reset(invocation_token)
        _end_turn()
