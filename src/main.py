"""Codebot orchestrator: works the Google Doc backlog one task at a time,
communicating with the user exclusively by email."""
import fcntl
import os
import copy
import json
import hashlib
import tempfile
import shutil
import logging
import re
import secrets
import signal
import subprocess
import threading
import time
import traceback
from datetime import date
from pathlib import Path

import agent_runner
from agent_errors import AgentContentFilterError
import activity
import phase_checkpoint
import task_records
import repo_provenance
import diagnostics
import errors
import feedback
import parallel_conversation
import replanning
import replies
import verification_ledger
import verification_recovery
import review_intent
import operations
import check_plan
import checks
import final_checks
import check_repair
from execution_identity import snapshot as content_snapshot, digest
import delivery_checkpoint
import evidence_delivery
import message_templates
import performance
import lifecycle
import persistence
import retention
import config
import evidence
import handoffs
import milestones
import task_phases
import proposal_package
import task_source
import turn_control
import conversation as gmail_client
import privatebin
import progress
import prompts
import redaction
import secret_store
import selfheal_events
from command_text import parse_command, parse_btw

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.DEBUG),
                     format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# Suppress SDK HTTP dumps (including full Slack message bodies) while keeping
# codebot's own DEBUG and SDK warnings visible.
for noisy in ("googleapiclient", "google", "google_auth_httplib2", "urllib3", "slack_sdk"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("codebot")
try:
    redaction.register(config.JIRA_API_TOKEN, config.SLACK_BOT_TOKEN, config.SLACK_APP_TOKEN,
                       *(os.environ.get(name) for name in (
                           "GH_TOKEN", "GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                           "CLAUDE_API_KEY", "ANTHROPIC_API_KEY")))
except Exception:  # redaction is best-effort; it must never keep the bot from starting
    log.warning("log redaction secrets unavailable", exc_info=True)
progress.install()
redaction.install()  # after every root handler exists (never raises)


# ---------------------------------------------------------------- state

def load_state() -> dict:
    state = persistence.read_state(config.STATE_PATH)
    if state is not None:
        state = _validate_target_state(state)
        recovered = selfheal_events.recover(state)
        if recovered != state:
            save_state(recovered)  # Commit the resume before consuming its ticket or running a phase.
            selfheal_events.acknowledge(recovered)
        state = recovered
        return task_records.migrate(state, phase_checkpoint.store(), config.REPO_PATH)
    return _validate_target_state({"state": "IDLE"})


def _notify_operator(topic: str, body: str) -> bool:
    """A one-way operational notice outside any task thread. Never raises."""
    try:
        if config.COMM_CHANNEL == "slack":
            gmail_client.announce(f"{config.INSTANCE_ID}: {body}")
        else:
            gmail_client.send(f"{config.SUBJECT_PREFIX} {topic}", body)
        return True
    except Exception:  # noqa: BLE001 — a notice must never stall the loop
        log.warning("could not send the %s notice", topic, exc_info=True)
        return False


def _report_quarantined() -> None:
    """Tell the operator once about every unparseable file set aside."""
    while persistence.quarantined:
        path, moved = persistence.quarantined[0]
        log.error("%s did not parse; moved it to %s and continued without it", path, moved)
        if not _notify_operator("unreadable state file", f"{path} was unreadable (not valid JSON). "
                                f"It was moved to {moved} and I continued without it; check that "
                                "copy for anything to restore."):
            return
        persistence.quarantined.pop(0)


def _probe_runtime() -> None:
    """Startup check of the pinned CLIs: log and notify once per distinct mismatch."""
    try:
        problems = retention.probe(config.AGENT)
        if not problems:
            return
        log.error("runtime probe: %s", "; ".join(problems))
        stamp = config.DATA_DIR / ".runtime-probe"
        digest_text = hashlib.sha256("\n".join(problems).encode()).hexdigest()
        if stamp.exists() and stamp.read_text() == digest_text:
            return
        if _notify_operator("runtime mismatch", "the installed CLIs differ from the pinned "
                            "runtime; I keep running, but expect failures:\n- " + "\n- ".join(problems)):
            stamp.write_text(digest_text)
    except Exception:  # noqa: BLE001 — the probe only informs
        log.warning("runtime probe failed", exc_info=True)


def _retention_keep(state: dict) -> tuple[list, list]:
    """What the current and held tasks still use: recovery worktrees and checkpoints."""
    saved = [s for s in [state] + [h.get("saved") or {} for h in _load_holds()] if s.get("item")]
    store = phase_checkpoint.store()
    paths = [s.get("workspace_path") for s in saved] + \
        [config.DATA_DIR / "workspaces" / str(s.get("execution_task_id", ""))[:16]
         for s in saved if s.get("execution_task_id")]
    return paths, [store.task_identity(s, config.REPO_PATH) for s in saved]


def _validate_target_state(state: dict) -> dict:
    # Validate before task migration can replay feedback or write durable records.
    if config.TARGET_MODE == "workspace" or state.get("workspace"):
        import workspace
        import workspace_state
        target = workspace.discover(config.REPO_PATH, mode=config.TARGET_MODE,
                                    manifest_path=config.WORKSPACE_MANIFEST,
                                    base_branch=config.BASE_BRANCH if config.TARGET_MODE == "repo" else None)
        return workspace_state.bind_and_validate(state, target)
    return state


def save_state(state: dict) -> None:
    if "repository" in state:
        persistence.repository_record(state)
    persistence.write_json(config.STATE_PATH, state, indent=2)
    global _conversation_state
    # Rebinding is atomic: no lock, so a save never waits behind the status thread's I/O.
    _conversation_state = copy.deepcopy(state)
    if state.get("conversation_flow_id"):
        store = phase_checkpoint.store()
        row = store.get("conversation", state["conversation_flow_id"])
        if row and row["task_id"] == store.task_identity(state, config.REPO_PATH):
            store.update("conversation", row, flow_applied=True)


TASK_LANGUAGE_ATTEMPTS = 3


def _language_name(value) -> str | None:
    if isinstance(value, str) and 2 <= len(value.strip()) <= 48 and value.strip().isprintable():
        return value.strip()
    return None


def _ensure_task_language(state: dict) -> None:
    """Classify once from the source ticket, not from the conversation or agent output.
    Best effort: it runs before mailbox commands every tick, so a failure must never
    raise (that made even ABORT unreachable). After a few failed attempts the task
    proceeds in English rather than retrying a utility call on every tick."""
    if not state.get("item") or state.get("task_language"):
        return
    try:
        result = agent_runner.run(prompts.render(
            prompts.TASK_LANGUAGE, item=state["item"], detail=state.get("item_detail", "")),
            contract=False)
        language = _language_name(parse_json_reply(result.output).get("language"))
        if not language:
            raise ValueError("invalid task language")
    except turn_control.TurnKicked:
        raise
    except Exception as error:  # noqa: BLE001
        failures = state.get("task_language_failures", 0) + 1
        state["task_language_failures"] = failures
        log.warning("task language detection failed (%d/%d): %s", failures,
                    TASK_LANGUAGE_ATTEMPTS, error)
        if failures < TASK_LANGUAGE_ATTEMPTS:
            save_state(state)
            return
        language = "English"
    state["task_language"] = language
    state.pop("task_language_failures", None)
    save_state(state)
    log.info("task language from backlog item: %s", language)


# ---------------------------------------------------------------- helpers

def git(*args: str) -> str:
    log.debug("git %s", " ".join(args))
    proc = operations.tool(["git", *args], cwd=config.REPO_PATH)
    if proc.returncode != 0:
        raise errors.from_tool(f"git {' '.join(args)} exited {proc.returncode}: {proc.stderr.strip()}", code="git_failed")
    if proc.stdout.strip():
        log.debug("git %s -> %s", args[0], proc.stdout.strip()[:300])
    return proc.stdout.strip()


def subject(state: dict, phase: str) -> str:
    return f"{config.SUBJECT_PREFIX} {state.get('slug', 'general')} — {phase}"


def _localized(state: dict, message: str) -> str:
    language = state.get("task_language", "English")
    static = message_templates.translate(message, language)
    if static is not None:
        return static
    if language.casefold() == "english" or not message.strip():
        return message
    try:
        with operations.budget(30):
            result = agent_runner.run(prompts.render(
                prompts.LOCALIZE_MESSAGE, language=language, message=message), contract=False)
    except (RuntimeError, ConnectionError, TimeoutError, subprocess.TimeoutExpired):
        return message
    text = result.output.strip()
    if not text:
        raise ValueError("empty localized message")
    return text


def _send_localized(state: dict, subj: str, body: str, thread_id: str) -> None:
    gmail_client.send(_localized(state, subj), _localized(state, body), thread_id)


def _localized_pair(state, phase, body):
    if state.get("task_language", "English").casefold() == "english":
        return phase, body
    static_phase = message_templates.translate(phase, state["task_language"])
    static_body = message_templates.translate(body, state["task_language"])
    if static_phase is not None and static_body is not None:
        return static_phase, static_body
    cache = state.setdefault("localized_messages", {})
    key = hashlib.sha256((phase + "\0" + body).encode()).hexdigest()
    if key in cache:
        return tuple(cache[key])
    # One text-only utility handles both fields; reuse its output for trail.
    try:
        with operations.budget(30):
            result = agent_runner.run("Translate subject and body to " + state["task_language"]
                + '. Return ONLY JSON with keys subject and body. Preserve commands, URLs and code.\n'
                + json.dumps({"subject": phase, "body": body}, ensure_ascii=False), contract=False)
        value = parse_json_reply(result.output)
        pair = (value["subject"], value["body"])
        if not all(isinstance(text, str) and text.strip() for text in pair):
            raise ValueError('Invalid localized message fields')
    except (RuntimeError, ConnectionError, ValueError, KeyError, TimeoutError, subprocess.TimeoutExpired):
        log.warning('localization unavailable; delivering original report without retrying coding work')
        pair = (message_templates.translate(phase, state['task_language']) or phase, body)
    cache[key] = list(pair)
    if len(cache) > 30:
        cache.pop(next(iter(cache)))
    return pair


def _language_state_for_thread(state: dict, thread_id: str | None) -> dict:
    if thread_id == state.get("thread_id"):
        return state
    return next((h.get("saved") or {} for h in _load_holds()
                 if h.get("thread_id") == thread_id), state)


def _reply_here() -> str:
    return ("Reply in this Slack thread to continue." if config.COMM_CHANNEL == "slack"
            else "Reply to this email to continue.")


RESEND_WINDOW_SECONDS = 600


def _message_key(subj: str, body: str, thread_id, attachments) -> str:
    return hashlib.sha256(json.dumps([subj, body, thread_id, sorted(str(a) for a in attachments or [])]).encode()).hexdigest()[:16]


def _recently_sent(state: dict, subj: str, body: str, thread_id, attachments) -> bool:
    """A step that failed after sending and is retried must not send the same message
    again. Identical subject, body, thread and attachments delivered within
    RESEND_WINDOW_SECONDS count as the same message; later deliberate repeats go out."""
    sent = state.get("sent_messages") or {}
    stamp = sent.get(_message_key(subj, body, thread_id, attachments))
    return stamp is not None and time.time() - stamp < RESEND_WINDOW_SECONDS


def _record_sent(state: dict, subj: str, body: str, thread_id, attachments) -> None:
    now = time.time()
    sent = {k: t for k, t in (state.get("sent_messages") or {}).items() if now - t < RESEND_WINDOW_SECONDS}
    sent[_message_key(subj, body, thread_id, attachments)] = now
    state["sent_messages"] = sent


def email(state: dict, phase: str, body: str, attachments: list[Path] | None = None,
          new_thread: bool = False, *, milestone: str | None = None,
          visible_question: str | None = None, localized: bool = False) -> None:
    thread_id = None if new_thread and config.COMM_CHANNEL == "email" else state.get("thread_id")
    fresh_stage = milestone if milestone and not milestones.announced(state, milestone) else None
    show_progress = milestone if milestone and state.get("banner_stage") != milestone else None
    decision_text = "" if milestone and " · " in body else handoffs.remember_decision(state, phase, visible_question)
    if decision_text:
        if visible_question and state.get("pending_decision", {}).get("agent_question"):
            body = handoffs.without_question(body, visible_question)
        body = f"{decision_text}\n\n{body}"
    elif card := ("" if milestone and " · " in body else handoffs.decision_card(state, phase)):
        body = f"{card}\n\n{body}"
    import message_content
    conversation_body, technical = message_content.split(body)
    if len(body) > 1600 or len(body.splitlines()) > 12 or technical:
        report = diagnostics.report(phase_checkpoint.store(), state, config.REPO_PATH,
            config.DATA_DIR, phase, detail=body)
        attachments = [*(attachments or []), report]
        body = conversation_body or "Technical results are in the attached report."
        # Length is not a reason to hide conversational text. Slack handles its
        # own message limits; email keeps a full plain-text/HTML body.
        if visible_question and not decision_text and visible_question not in body:
            question_body, _ = message_content.split(visible_question)
            if question_body and question_body not in body:
                body += "\n\n" + question_body
        body += "\n\nSupporting details: " + report.name
    if decision_text and decision_text not in body:
        body = decision_text + "\n\n" + body
    # Translate the fixed FSM copy too, not only the agent's OpenSpec/report output.
    # The one-shot localizer never resumes or changes the task's coding session.
    if state.get("task_language") and not localized:
        phase, body = _localized_pair(state, phase, body)
    body = diagnostics.redact(body)
    subj = subject(state, phase)
    # Milestone banners repeat on purpose (E2E -> WAIT_REPLY -> E2E) and are deduplicated
    # by banner_state; this guard is for steps retried after a failure.
    if not milestone and _recently_sent(state, subj, body, thread_id, attachments):
        log.info("not re-sending %r: the same message went to this thread minutes ago (retried step)", subj)
        return
    log.info("sending %r (thread=%s, %d attachment(s), %d body chars)",
             subj, thread_id or "new", len(attachments or []), len(body))
    if attachments:
        delivery = gmail_client.deliver(state, subj, body, thread_id, attachments)
        state["thread_id"] = delivery["thread_id"] or thread_id
        state["last_delivery"] = delivery
    else:
        kwargs = {"progress": milestones.image(show_progress)} if show_progress else {}
        state["thread_id"] = gmail_client.send(subj, body, thread_id, attachments, **kwargs)
        state.pop("last_delivery", None)
    if milestone:
        # Artifact delivery has its own receipts; share the banner in the same
        # thread without changing or bypassing the artifact delivery contract.
        if attachments and show_progress:
            gmail_client.send(subject(state, milestones.label(milestone)),
                              _localized(state, milestones.label(milestone)), state["thread_id"],
                              progress=milestones.image(milestone))
        state["banner_stage"] = milestone
        state["banner_state"] = {"approval": "WAIT_APPROVAL", "pr_review": "WAIT_MERGE",
                                 "merged": "IDLE"}.get(milestone, state["state"])
    if fresh_stage:
        milestones.mark(state, fresh_stage)
        save_state(state)
    if not milestone:
        _record_sent(state, subj, body, thread_id, attachments)
    # Snapshot for STATUS replies: lets the user recover what the bot last said (and
    # is therefore waiting on) if the original email was lost or unclear.
    state["last_email"] = {"subject": subj, "body": body, "sent_at": time.time()}
    _note_contact(state)
    names = [Path(a).name for a in (attachments or [])]
    activity = body + (f"\n\nFiles (delivery receipts tracked separately): {', '.join(names)}" if names else "")
    if state.get("task_language"):
        trail(state, phase, activity, localized=True)
    else:
        trail(state, phase, activity)


def announce_milestone(state: dict, stage: str, body: str) -> None:
    """Announce each substate as text; illustrate only visible stage changes."""
    if state.get("banner_state") != state["state"] or state.get("banner_stage") != stage:
        previous_email = state.get("last_email")
        email(state, milestones.label(stage), body, milestone=stage)
        if previous_email is not None:
            state["last_email"] = previous_email
        state["banner_state"] = state["state"]
        save_state(state)


def _announce_state(state: dict) -> None:
    """Recover pending state banners without replaying the completed phase."""
    if not state.get("item") or state["state"] == "IDLE" or state.get("banner_state") == state["state"]:
        return
    try:
        stage = milestones.state_stage(state)
        body = f"{milestones.label(stage)} · {state['state']}"
        if state["state"] == "IMPLEMENTING" and state.get("implementation_feedback"):
            body += ("\n\nVerification found approved work still incomplete. "
                     "I'm returning to implementation to finish it; no new approval is needed.\n\n"
                     + state["implementation_feedback"])
        announce_milestone(state, stage, body)
    except Exception:  # A notification failure must not rerun coding work.
        log.exception("could not send state banner for %s; will retry next tick", state["state"])


def trail(state: dict, headline: str, body: str = "", *, localized: bool = False) -> None:
    """Append a note to the task's activity trail on the backlog item (issue comment,
    doc comment thread...). Every user-facing email and every silent phase transition
    is noted, so the item doubles as the task's log. Best-effort by design: a tracker
    hiccup is logged and never changes the FSM; nothing is noted without a task."""
    if not config.ACTIVITY_TRAIL or not state.get("item"):
        return
    message = f"[{config.INSTANCE_ID}] {headline}"
    if body:
        message += f"\n\n{body}"
    # The same note within the resend window is a retried step, not news.
    original = message
    if _recently_sent(state, "trail", original, None, None):
        return
    try:
        if state.get("task_language") and not localized:
            message = _localized(state, message)
        ref = task_source.note_activity(state["item"], state.get("item_id"), message,
                                        state.get("trail_ref"))
        if isinstance(ref, str) and ref:
            state["trail_ref"] = ref
        _record_sent(state, "trail", original, None, None)
    except Exception:  # noqa: BLE001
        log.exception("could not note activity on the backlog item: %r", headline)


def parse_json_reply(text: str) -> dict:
    """Extract the first valid JSON object from the coding agent's output.

    Scans each '{' and uses raw_decode so surrounding prose or later brace-bearing
    text cannot corrupt the match (greedy '{.*}' did).
    """
    decoder = json.JSONDecoder()
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError(f"no JSON object in coding-agent output: {text[:500]}")


# ---------------------------------------------------------------- agent log

# One log per task, accumulated in the (git-ignored) data dir while the task runs and
# copied into the archived OpenSpec change at the end — so the reasoning that produced a
# feature is committed next to its proposal, design, and tasks. It is deliberately NOT
# written into the active change dir: the planning phases treat everything under
# openspec/ as their own output, and `openspec validate --strict` runs against the active
# change before we ever get to archive it.
TRANSCRIPT_DIR = config.DATA_DIR / "transcripts"
TRANSCRIPT_NAME = "agent-log.md"


def _transcript_path(state: dict) -> Path:
    """The task's working log, named after the branch (instance id + slug) so concurrent
    instances and a repeated slug never append to each other's file."""
    name = state.get("branch") or state.get("slug") or "general"
    return TRANSCRIPT_DIR / f"{re.sub(r'[^A-Za-z0-9._-]', '-', name)}.md"


def _transcript_append(state: dict, text: str) -> None:
    """Best-effort by design: failing to record a turn must never fail the phase."""
    try:
        path = _transcript_path(state)
        path.parent.mkdir(parents=True, exist_ok=True)
        header = "" if path.exists() else (
            f"# Agent log — {state.get('slug', 'general')}\n\n"
            f"- Task: {state.get('item', '?')}\n"
            f"- Branch: {state.get('branch', '?')}\n"
            f"- Started: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n")
        with path.open("a") as handle:
            handle.write(header + text)
    except OSError:
        log.exception("could not append to the agent log")


def _transcript_note(state: dict, note: str) -> None:
    """A codebot-side annotation between turns (what we did with the turn's output)."""
    _transcript_append(state, f"\n_codebot: {note}_\n")


def _archive_transcript(state: dict, target: Path) -> None:
    """Copy the working log into the archived change. Best-effort: an otherwise-good
    archive must not fail over the log."""
    source = _transcript_path(state)
    try:
        destination = target / TRANSCRIPT_NAME
        if destination.exists():
            log.info("preserving existing archived agent log for %s", state.get("slug"))
            return
        if not source.is_file():
            log.info("no agent log to archive for %s", state.get("slug"))
            return
        with destination.open("x") as output:
            output.write(source.read_text())
        log.info("archived agent log (%d bytes) to %s/%s",
                 source.stat().st_size, target.name, TRANSCRIPT_NAME)
    except OSError:
        log.exception("could not archive the agent log")


# Where a phase resumes after a WAIT_STUCK escalation when re-running the phase itself
# makes no sense (its trigger — the user's PR feedback — was consumed).
RESUMABLE_STATE = {"APPLY_PR_FEEDBACK": "WAIT_MERGE", "ADDRESS_PR_THREADS": "WAIT_MERGE"}


def handle_result(state: dict, result, phase: str) -> bool:
    """If the coding agent asked a question, email it and enter WAIT_REPLY. True if waiting.

    Consecutive question round-trips are capped: each round "succeeds" as a tick, so the
    per-state failure budget can never bound this loop — without the cap a session that
    keeps asking (e.g. "what should I work on next?") ping-pongs with the user forever.
    """
    if phase == "INTERNAL_REVIEW" and result.session_id == state.get("phase_sessions", {}).get("INTERNAL_REVIEW"):
        # The fresh reviewer never replaces the implementer: later fix, review-thread and
        # conflict turns must resume the session that knows the code.
        state["review_session_id"] = result.session_id
    else:
        state["session_id"] = result.session_id
    requested = check_plan.reported(result.output)
    check_plan.remember(state, result.output)
    if config.TARGET_MODE == "workspace":
        import workspace_runner
        workspace_runner.remember_checks(state, result.output)
    elif requested:
        requested = check_plan.relax(requested, _skipped_credentials(state))
        database = phase_checkpoint.store()
        task_id = database.task_identity(state, config.REPO_PATH)
        # Browser checks belong to the staged final gate, never an implementation,
        # question or review response. Register them without launching them here.
        cheap = [c for c in requested if not check_plan.is_e2e(c, config.REPO_PATH)]
        outcomes = checks.execute_plan(cheap, config.REPO_PATH, database, task_id) if cheap else []
        state["focused_check_results"] = [{**r, "gate": final_checks.gate(c, r, state, config.REPO_PATH, database)}
                                          for c, r in zip(cheap, outcomes)]
        check_plan.remember(state, "CHECK_PLAN: " + json.dumps({"version": 1, "checks": [check.to_dict() for check in requested]}))
    state.pop("kick_pending", None)
    log.debug("[%s] session=%s output=%d chars", phase, result.session_id, len(result.output))
    _transcript_append(state, f"\n## {phase} — {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n\n"
                              f"Session: `{result.session_id}`\n\n{result.output.strip()}\n")
    if _maybe_request_credential(state, result, phase):
        return True
    question = check_plan.visible_text(result.question or "")
    if not question:  # None, or a bare/blank sentinel line — nothing actionable to ask
        state.pop("question_rounds", None)  # phase produced a real result; loop broken
        state.pop("pending_question", None)
        return False
    rounds = state.get("question_rounds", 0) + 1
    state["question_rounds"] = rounds
    if rounds > config.QUESTION_MAX_ROUNDS:
        log.warning("question loop exceeded %d rounds in %s; escalating to WAIT_STUCK",
                    config.QUESTION_MAX_ROUNDS, phase)
        detail = (f"I have asked {rounds - 1} questions in a row during {phase} without "
                  f"completing the phase. The pending question was:\n\n{question}")
        if phase == "APPLY_PR_FEEDBACK":
            detail += ("\n\nNote: if you reply 'retry', please re-send the change request "
                       "you want applied — the original one was consumed.")
        _transcript_note(state, f"{rounds} consecutive questions in {phase}; "
                                f"escalated to WAIT_STUCK")
        _enter_stuck(state, RESUMABLE_STATE.get(phase, phase), detail)
        return True
    attachments = [Path(p) for p in result.attachments if Path(p).exists()]
    log.info("[%s] coding agent asked a question (%d validated attachment(s)); sending via %s",
             phase, len(attachments), config.COMM_CHANNEL)
    # The agent often puts the substance (a design to approve, options it weighed) ABOVE
    # the sentinel line and only the ask after it; the user needs both to answer.
    preamble = check_plan.visible_text(getattr(result, "preamble", "") or "")
    preamble = handoffs.without_question(preamble, question)
    body = f"Task: {state.get('item', '?')}\nPhase: {phase}\n\n"
    if preamble:
        body += f"{preamble}\n\n---\n\n"
    body += f"{question}\n\n{_reply_here()}"
    email(state, f"question during {phase}", body, attachments, visible_question=question)
    # Kept so the WAIT_REPLY classifier can judge the reply IN CONTEXT — "yes, that part
    # is done, move on" answers a sub-step question; without the question it reads like
    # a whole-task completion order.
    _transcript_note(state, f"sent this question to the user via {config.COMM_CHANNEL} "
                            f"({len(attachments)} attachment(s)); waiting for a reply")
    state["pending_question"] = question
    state["return_state"] = phase
    state["state"] = "WAIT_REPLY"
    return True


def _parse_completion_contract(output: str, marker: str) -> tuple[dict | None, str | None]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    prefix = marker + ":"
    marked = [line for line in lines if line.startswith(prefix)]
    if output.count(prefix) != 1 or len(marked) != 1:
        return None, f"expected exactly one {marker} contract"
    if not lines or lines[-1] != marked[0]:
        return None, f"{marker} contract must be the final nonblank line"
    try:
        value = json.loads(marked[0][len(prefix):].strip())
    except ValueError:
        return None, f"{marker} contract contains malformed JSON"
    if not isinstance(value, dict):
        return None, f"{marker} contract must contain a JSON object"
    return value, None


def _nonempty_strings(value) -> bool:
    return (isinstance(value, list) and bool(value) and
            all(isinstance(item, str) and item.strip() for item in value))


def _looks_failing(entry: str) -> bool:
    """Whether a "<command>: <result>" entry reports a failure rather than a pass."""
    result = entry.rsplit(":", 1)[-1].strip().lower() if ":" in entry else entry.lower()
    if result.startswith(("pass", "ok", "success", "green")):
        return False
    return bool(re.search(r"\bfail|\berror|\bfatal|\bcrash|timed? ?out|✖|✗", result))


def _failing_summary(commands) -> str:
    """" (failing: ...)" listing the commands the agent itself reported as failing, so the
    rejection reason names the real problem (e.g. 100 lint errors) instead of just
    "status is not pass"."""
    if not isinstance(commands, list):
        return ""
    failing = [c.strip() for c in commands if isinstance(c, str) and c.strip() and _looks_failing(c)]
    return f" (failing: {'; '.join(failing)})" if failing else ""


def parse_quality_gate(output: str) -> tuple[dict | None, str | None]:
    value, reason = _parse_completion_contract(output, "QUALITY_GATE")
    if reason:
        return None, reason
    required = {"status", "commands", "openspec", "tasks"}
    if not required <= set(value) or set(value) - required - {"preexisting", "deferred"}:
        return None, "quality gate has unexpected or missing fields"
    if value.get("status") != "pass":
        return None, "quality gate status is not pass" + _failing_summary(value.get("commands"))
    if not _nonempty_strings(value.get("commands")):
        return None, "quality gate commands must contain nonempty strings"
    # Failures the agent confirmed on the base branch too (see prompts.VERIFY); the task
    # is not on the hook for them, but they are recorded so the report shows them.
    preexisting = value.get("preexisting", [])
    if not isinstance(preexisting, list) or (preexisting and not _nonempty_strings(preexisting)):
        return None, "quality gate preexisting must be a list of nonempty strings"
    if value.get("openspec") != "pass":
        return None, "OpenSpec validation did not pass"
    match = re.fullmatch(r"(\d+)/(\d+)", value.get("tasks", ""))
    deferred = value.get("deferred", 0)
    if type(deferred) is not int or deferred < 0:
        return None, "invalid deferred task count"
    if not match or int(match.group(1)) + deferred != int(match.group(2)):
        return None, "OpenSpec tasks are incomplete"
    return value, None


def parse_internal_review(output: str) -> tuple[dict | None, str | None]:
    value, reason = _parse_completion_contract(output, "INTERNAL_REVIEW")
    if reason:
        return None, reason
    if set(value) != {"status", "critical", "important", "tests"}:
        return None, "internal review has unexpected or missing fields"
    if value.get("status") != "pass":
        return None, "internal review status is not pass"
    for severity in ("critical", "important"):
        count = value.get(severity)
        if type(count) is not int or count != 0:
            return None, f"internal review has unresolved {severity} findings"
    if not _nonempty_strings(value.get("tests")):
        return None, "internal review tests must contain nonempty strings"
    return value, None


# Video extensions are the strongest signal on their own (repos essentially never
# legitimately commit one); other hints only count against a path that also names an
# evidence-ish directory/filename, to avoid flagging ordinary repo images/reports.
_EVIDENCE_EXTENSIONS = (".mp4", ".mov", ".webm", ".avi", ".mkv")
_EVIDENCE_PATH_HINTS = ("evidence", "recording")
_EVIDENCE_MEDIA = (".png", ".jpg", ".jpeg", ".gif", ".html", ".zip")


def _looks_like_evidence(path: str) -> bool:
    """Videos, and screenshots/reports with an evidence-ish name; never source files
    such as src/evidence_parser.py."""
    lower = path.lower()
    return lower.endswith(_EVIDENCE_EXTENSIONS) or (
        lower.endswith(_EVIDENCE_MEDIA) and any(h in lower for h in _EVIDENCE_PATH_HINTS))


_SECRET_FILE_RE = re.compile(
    r"(^|/)(?:\.env(?:\.(?!example$|sample$|template$|dist$)[^/]*)?|[^/]*\.(?:pem|key|p12|pfx)"
    r"|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?|credentials\.json|token\.json|service[-_]account[^/]*\.json)$",
    re.I)


def _looks_like_secret(path: str) -> bool:
    """Untracked files the bot must never sweep into one of its own commits (by name only;
    no content scanning). `.env.example`-style templates are fine."""
    return bool(_SECRET_FILE_RE.search(path.strip()))


def _branch_evidence_paths(branch: str, base_branch: str) -> list[str]:
    """Files added/changed on `branch` since it diverged from `base_branch` that look
    like recorded test evidence rather than application code — these must never be
    committed, only emailed (see agent_runner.EVIDENCE_CONTRACT)."""
    # Added by this branch only: evidence the base branch already tracks is not ours to move.
    diff = git("diff", "--name-only", "--diff-filter=A", f"{base_branch}...{branch}")
    return [p for p in diff.splitlines() if _looks_like_evidence(p)]


def _scrub_evidence_from_repo(state: dict, phase: str) -> list[Path]:
    """Evidence belongs in the conversation, not in git. coderbot moves any committed
    evidence file to the outbox and commits the removal itself; the moved files are
    returned as attachments."""
    paths = _branch_evidence_paths(state["branch"], config.BASE_BRANCH)
    if not paths:
        return []
    log.warning("[%s] evidence file(s) were committed; moving them to the outbox: %s", phase, paths)
    destination = config.DATA_DIR / "outbox" / "evidence-from-repo" / _validated_slug(state["slug"])
    moved = []
    for path in paths:
        source = config.REPO_PATH / path
        if source.is_file():
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            moved.append(target)
    # Removed from the working tree too, so a later `git add -A` cannot bring them back.
    git("rm", "-q", "-f", "--ignore-unmatch", "--", *paths)  # a retry after a failed commit
    git("commit", "-q", "-m", "chore: move test evidence out of the repository", "--", *paths)
    trail(state, "Committed evidence moved to the conversation", "\n".join(paths))
    return moved


def _collect_attachments(result, e2e_specs: list[str],
                          e2e_kind: str | None) -> list[Path]:
    """Files to attach to a post-task email: whatever the coding agent explicitly pointed to via
    ATTACH: lines in its own (non-question) output, plus whatever the evidence pipeline
    separately records. Claude may have already captured and converted its own evidence
    (e.g. mid-review, outside the dedicated E2E phase) — trust that over re-deriving
    evidence from scratch, which runs in a fresh subprocess and can fail for reasons
    Claude's own sandboxed tool calls didn't hit. Deduped by resolved path in case both
    sources happen to reference the same file."""
    agent_files = [Path(p) for p in result.attachments]
    videos = [p for p in agent_files if p.suffix.lower() in (".webm", ".mp4")]
    approved = evidence.supplied_artifacts(videos, evidence.content_identity()) if videos else []
    agent_files = [p for p in agent_files if p not in videos or p in approved]
    evidence_files = evidence.record_evidence(e2e_specs, e2e_kind)
    agent_videos = [file for file in agent_files if file.suffix.lower() == ".webm"]
    if agent_videos:
        if evidence_files:
            # The dedicated recorder is canonical when it succeeds; avoid attaching
            # duplicate raw clips from an agent's earlier manual run.
            agent_files = [file for file in agent_files if file not in agent_videos]
        else:
            stitched = evidence.stitch_playwright_clips(agent_videos)
            if stitched:
                agent_files = [file for file in agent_files if file not in agent_videos] + [stitched]
    combined, seen = [], set()
    for f in agent_files + evidence_files:
        resolved = f.resolve()
        if resolved not in seen:
            seen.add(resolved)
            combined.append(f)
    return combined


def _offload_evidence_video(state: dict, attachments: list[Path]) -> tuple[list[Path], str | None]:
    """Upload the evidence video(s) to Drive and take them out of the attachment list;
    returns (remaining attachments, link text or None). Only .mp4 files move — a
    Newman report or an agent screenshot stays attached — and an mp4 whose upload
    failed stays too, so gmail_client attaches it exactly as before (size cap and all).
    Publication is content-addressed and access-verified by the common service."""
    if not config.EVIDENCE_UPLOAD:
        return attachments, None
    remaining, links = [], []
    for path in attachments:
        if path.suffix.lower() != ".mp4":
            remaining.append(path)
            continue
        published = evidence_delivery.publish(phase_checkpoint.store(), state, config.REPO_PATH, path)
        state["evidence_delivery"] = published
        link = published.get("url") if published.get("status") == "complete" else None
        if link:
            links.append(link)
        else:
            remaining.append(path)
    if not links:
        return remaining, None
    state["evidence_url"] = links[0]
    return remaining, "\n".join(links)


def _sync_pr_video(state: dict, video_url: str) -> None:
    """Replace only the bot-owned evidence section, preserving human PR edits."""
    pr_url = _https_url(state["pr_url"])
    urls = [_https_url(url) for url in video_url.splitlines() if url.strip()]
    section = ("<!-- coderbot:video:start -->\n## Demo video\n\n"
               + "\n".join(f"- [Video]({url})" for url in urls)
               + "\n<!-- coderbot:video:end -->")
    current = _pr_body(pr_url)
    pattern = r"<!-- coderbot:video:start -->.*?<!-- coderbot:video:end -->"
    updated = (re.sub(pattern, lambda _: section, current, flags=re.DOTALL)
               if re.search(pattern, current, re.DOTALL) else current.rstrip() + "\n\n" + section)
    if updated != current:
        ops, number = _pr_ref(pr_url)
        ops.gh("pr", "edit", number, "--body", updated)
    state["pr_summary"] = updated
    verified = _pr_body(pr_url)
    if section not in verified:
        raise RuntimeError("PR evidence section could not be verified after update")


def _notify_evidence(state, phase, body, files=()):
    """Use durable provider receipts even for link-only evidence notifications."""
    import message_delivery
    database = phase_checkpoint.store()
    result = message_delivery.deliver(database, state, config.REPO_PATH, config.DATA_DIR,
        gmail_client._backend(), subject(state, phase), body, state.get("thread_id"), files)
    state["thread_id"] = result["thread_id"] or state.get("thread_id")
    state["last_delivery"] = result
    if not result["complete"]:
        raise RuntimeError("Evidence notification remains pending provider confirmation")


# ---------------------------------------------------------------- capability detection

def _has_e2e_harness() -> bool:
    return (config.REPO_PATH / "e2e" / "run.sh").is_file()


def _has_code_review_workflow() -> bool:
    workflows_dir = config.REPO_PATH / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return False
    for pattern in ("*.yml", "*.yaml"):
        for path in workflows_dir.glob(pattern):
            try:
                text = path.read_text()
            except OSError:
                continue
            # Anchored to column 0: a workflow's own top-level `name:`, not an
            # indented job/step name that happens to say "Code Review" too.
            if re.search(r'^name:\s*["\']?Code Review["\']?\s*$', text, re.MULTILINE):
                return True
    return False


def _e2e_kind_of_present_harness() -> str:
    """"playwright" | "newman" | "unknown", for a harness already known to be present."""
    e2e_dir = config.REPO_PATH / "e2e"
    if (e2e_dir / "playwright.config.ts").is_file():
        return "playwright"
    if any(e2e_dir.rglob("*.postman_collection.json")):
        return "newman"
    return "unknown"


def _has_frontend() -> bool:
    root = config.REPO_PATH
    if (root / "frontend").is_dir() or (root / "angular.json").is_file() or (root / "index.html").is_file():
        return True
    return any(root.glob("vite.config.*")) or any(root.glob("next.config.*"))


def _detect_capabilities(state: dict) -> None:
    """Detect, once per task, whether the target repo has an e2e harness and a Code
    Review workflow, and which e2e tool (Playwright/Newman) is present — or, if no
    harness is present, which one self-healing should request."""
    has_e2e = _has_e2e_harness()
    kind = _e2e_kind_of_present_harness() if has_e2e else (
        "playwright" if _has_frontend() else "newman")
    state["has_e2e_harness"] = has_e2e
    state["has_code_review"] = _has_code_review_workflow()
    state["e2e_kind"] = kind
    log.info("target repo capabilities: e2e_harness=%s kind=%s code_review=%s",
             has_e2e, kind, state["has_code_review"])


# ---------------------------------------------------------------- self-healing

E2E_HARNESS_ITEMS = {
    "playwright": "Set up a Playwright e2e harness (e2e/run.sh + Playwright specs under "
                  "e2e/tests/) so future changes can be gated on it.",
    "newman": "Set up a Newman/Postman e2e harness (e2e/run.sh + collections under "
              "e2e/collections/) so future changes can be gated on it.",
}
CODE_REVIEW_ITEM = (
    "Add a 'Code Review' GitHub Actions workflow that runs the alibaba/open-code-review "
    "(OpenCodeReview) action (https://github.com/alibaba/open-code-review) as the automated "
    "PR reviewer, using an Anthropic API key as its LLM backend. To match coderbot's "
    "automated-review integration exactly: (1) the workflow's top-level `name:` field must be "
    "exactly `Code Review` (NOT the action's own suggested name, `OpenCodeReview PR Review`) "
    "so coderbot's Code Review detection and polling can find it; (2) trigger on pull_request "
    "(or pull_request_target) `opened`, `ready_for_review`, `synchronize`, and `reopened`, with "
    "the job guarded by `if: github.event.pull_request.draft == false`: coderbot opens a draft PR "
    "early and pushes progress to it, and the review should run once it is marked ready and on "
    "every push after that, including fix-up commits; (3) invoke the action via "
    "`uses: alibaba/open-code-review@<pinned version>` per "
    "https://github.com/alibaba/open-code-review/blob/main/pages/src/content/docs/en/integrations/ci.md "
    "(or the action.yml at the repo root), with `llm_use_anthropic: true` and "
    "`llm_url`/`llm_auth_token`/`llm_model` sourced from repo secrets/variables pointing at the "
    "Anthropic API — ask the user for these credentials via NEED_USER_INPUT if they are not "
    "already configured in the repo, rather than guessing; (4) leave `github_token` at its "
    "default (`${{ github.token }}`) and `sticky_summary` at its default (`true`), so feedback "
    "posts as inline PR review comments plus a sticky summary comment under the "
    "`github-actions[bot]` identity — do NOT swap in a custom GitHub App or bot account, since "
    "coderbot only recognizes comments from `github-actions[bot]`; (5) grant "
    "`permissions: contents: read` and `pull-requests: write`.")


def _seed_self_healing_items(state: dict) -> None:
    """Add a backlog item for each piece of missing infrastructure, unless an
    equivalent item is already pending or done."""
    if not state["has_e2e_harness"]:
        if task_source.ensure_item(E2E_HARNESS_ITEMS[state["e2e_kind"]]):
            log.info("self-healing: seeded backlog item for missing e2e harness (%s)",
                     state["e2e_kind"])
    if not state["has_code_review"] and config.SELF_HEAL_CODE_REVIEW:
        if task_source.ensure_item(CODE_REVIEW_ITEM):
            log.info("self-healing: seeded backlog item for missing Code Review workflow")


_EVIDENCE_CONTRACT = (
    " coderbot later re-runs each new or changed spec by name only (`./run.sh <file>`, "
    "recording on, no extra flags or environment variables), so its real happy path must run "
    "and pass under that bare invocation: do not skip it by default or gate its assertions "
    "behind custom flags.")

_DEMO_TEST_CONTRACT = (
    "\n- One test is the demo: its title contains `@evidence`, and coderbot records only it as "
    "the video for the user. In one continuous flow it shows this feature working: keep the "
    "relevant UI in view (`scrollIntoViewIfNeeded()`), hold each meaningful state about 1 s, keep "
    "unrelated setup off camera, and assert the visible outcome. Narrate on screen with a small "
    "`narrate(page, text)` helper that injects a fixed, high-contrast overlay: a ~3 s title card "
    "(the feature, and what will prove it works), a ~2 s caption before each key step (\"Step N/M\", "
    "what happens, why it is evidence), and a closing caption naming the visible outcome. "
    "Captions never cover the UI being shown and are never asserted on.")


def _e2e_note(state: dict) -> str:
    if not state.get("has_e2e_harness"):
        return "- This repo has no e2e harness yet: verify with its existing test suites."
    if state.get("e2e_kind") == "newman":
        return ("- User-facing behaviour needs Postman collections under e2e/collections/, run via "
                "Newman." + _EVIDENCE_CONTRACT)
    return ("- User-facing behaviour needs Playwright tests in e2e/tests/." + _EVIDENCE_CONTRACT
            + _DEMO_TEST_CONTRACT)


def _e2e_report_note(state: dict) -> str:
    if not state.get("has_e2e_harness"):
        return ""
    return "- List the new or changed e2e test files in e2e_specs."


# ---------------------------------------------------------------- phases

def do_pick(state: dict) -> None:
    if config.TARGET_MODE == "workspace":
        import workspace_runner
        return workspace_runner.pick(state)
    # Only tracked, uncommitted changes matter — untracked files (e.g. host-side
    # git-ignored config not excluded inside the container) don't block a checkout.
    if git("status", "--porcelain", "--untracked-files=no"):
        if config.COMM_CHANNEL == "slack":
            if not state.get("dirty_notified"):
                gmail_client.announce(
                    f"[{config.INSTANCE_ID}] The working tree in {config.REPO_PATH} is dirty. "
                    "I will recheck it automatically and start the next task once clean.")
                state["dirty_notified"] = True
        else:
            email(state, "blocked: dirty working tree",
                  "The repo working tree has uncommitted changes; codebot will not start a "
                  "new task. Clean it up and reply to this email to retry.", new_thread=True)
        state["state"] = "WAIT_CLEAN"
        return
    state.pop("dirty_notified", None)
    # Sync to the base branch before detecting target-repo capabilities and seeding
    # any self-healing item, so both reflect its actual current state rather than
    # whatever branch was last checked out.
    git("checkout", config.BASE_BRANCH)
    git("pull", "--ff-only")
    _detect_capabilities(state)
    _seed_self_healing_items(state)
    requested = sorted((h for h in _load_holds() if h.get("requested")),
                       key=lambda h: h.get("requested_at", 0))
    if requested:
        _resume_held_task(state, requested[0])
        if state["state"] != "IDLE":
            return
    items = task_source.list_pending_items()
    log.info("backlog has %d pending item(s)", len(items))
    if not items:
        log.info("no pending items; staying idle")
        state["state"] = "IDLE"
        return
    # A claim of ours that outlived state.json (e.g. the data dir was rebuilt) is
    # resumed before anything new is started, so no task is left claimed but unworked.
    mine = [i for i in items if i.get("claimed_by_me")]
    if mine:
        log.info("resuming from %d item(s) already claimed by this instance", len(mine))
        items = mine
    else:
        items = prioritize_items(items)
    if len(items) == 1:
        slug = re.sub(r"[^a-z0-9]+", "-", items[0]["text"].casefold()).strip("-")[:60]
        choice = {"item": items[0]["text"], "slug": slug or "task"}
    else:
        result = agent_runner.run(prompts.render(
            prompts.PICK, project=config.PROJECT_NAME, items=render_items(items)), contract=False)
        choice = parse_json_reply(result.output)
    if not choice.get("item") or not choice.get("slug"):
        raise ValueError(f"PICK output missing item/slug: {choice!r}")
    # The slug becomes a branch, a directory and file names: model text must not reach
    # them unbounded or with characters a ref or path cannot hold.
    choice["slug"] = re.sub(r"[^a-z0-9]+", "-", str(choice["slug"]).casefold()).strip("-")[:60].strip("-") or "task"
    chosen = next((i for i in items
                   if task_source.normalize(i["text"]) == task_source.normalize(choice["item"])),
                  None)
    if chosen is None:
        # The bullet's own text is the identity mark_done matches on later, so a paraphrase
        # (or a sub-bullet picked as an item) must not become the task. Fail the tick; PICK
        # runs again next time.
        raise ValueError(f"PICK chose text that is not a pending item: {choice['item'][:200]!r}")
    # Claim the item in the backlog BEFORE any local work: the claim write is what
    # keeps two instances off the same task. Losing the race just means repicking.
    if not task_source.claim_task(chosen["text"], chosen.get("id") or None):
        log.info("item was claimed by another instance meanwhile; repicking next tick: %r",
                 chosen["text"][:80])
        return  # stay IDLE
    log.info("coding agent picked %r (slug=%s, %d clarification line(s), %d screenshot(s)): %s",
             chosen["text"][:80], choice["slug"], len(chosen["detail"].splitlines()),
             len(chosen.get("images", [])), choice.get("reason", ""))
    state.update(item=chosen["text"], item_detail=chosen["detail"],
                 item_images=chosen.get("images", []),
                 item_id=chosen.get("id") or None, item_url=chosen.get("url") or None,
                 item_key=chosen.get("key") or None,
                 slug=choice["slug"], thread_id=None)
    if (language := _language_name(choice.get("language"))):
        state["task_language"] = language  # PICK read the item already: no separate detection call
    # Flat prefix (no slash): a "codebot/<slug>" branch would collide with the
    # existing "codebot" branch in git's ref namespace (file-vs-directory, exit 128).
    branch = f"{config.INSTANCE_ID}-{choice['slug']}"
    # -B is idempotent: if do_pick is retried after the branch was already created
    # (state not yet persisted), reset it from the base branch rather than failing on "-b".
    git("checkout", "-B", branch)
    state["branch"] = branch
    # The branch's starting point: later phases measure overreach against it.
    state["base_sha"] = git("rev-parse", "HEAD")
    task_records.migrate(state, phase_checkpoint.store(), config.REPO_PATH)
    repo_provenance.record(phase_checkpoint.store(), state, config.REPO_PATH, initial=True)
    state["state"] = "EXPLORING"
    _ensure_task_language(state)
    agent_runner.set_task_language(state.get("task_language"))
    if config.COMM_CHANNEL == "slack":
        # Checkpoint the correlation nonce before the network call; retry can
        # reconcile a root Slack accepted just before a process crash.
        state["thread_nonce"] = secrets.token_hex(12)
        if state.get("task_language", "English").casefold() != "english":
            intro = f"*{config.INSTANCE_ID}* picked: {state['item']}"
            if state.get("item_url"):
                intro += f"\nSource: {state['item_url']}"
            state["thread_intro"] = _localized(state, intro)
        save_state(state)
        state["thread_id"] = gmail_client.open_thread(state)
        save_state(state)
    log.info("picked %r -> %s", choice["item"], branch)
    trail(state, f"Picked this item; working on branch {branch}", choice.get("reason", ""))
    announce_milestone(state, "exploring", "I'm exploring the codebase.")


def prioritize_items(items: list[dict]) -> list[dict]:
    """Honor the user's Codebot[n] ordering tags: when any pending item carries one,
    only the item(s) with the LOWEST number are offered to PICK, so tagged work is done
    first and in the written order; untagged items wait until no tagged item remains."""
    tagged = [i for i in items if i.get("priority") is not None]
    if not tagged:
        return items
    lowest = min(i["priority"] for i in tagged)
    chosen = [i for i in tagged if i["priority"] == lowest]
    log.info("%d item(s) carry Codebot[n] tags; offering the %d tagged Codebot[%d]",
             len(tagged), len(chosen), lowest)
    return chosen


def render_items(items: list[dict]) -> str:
    """The backlog as a nested bullet list: one line per task, clarifications indented."""
    lines = []
    for item in items:
        lines.append(f"- {item['text']}")
        if item.get("detail"):
            lines.append(item["detail"])
    return "\n".join(lines)


def render_images(paths: list[str]) -> str:
    """The prompt block pointing the session at the item's screenshots ("" when none)."""
    existing = [p for p in paths if Path(p).exists()]
    if len(existing) < len(paths):
        log.warning("%d of %d item screenshot(s) missing on disk",
                    len(paths) - len(existing), len(paths))
    if not existing:
        return ""
    log.info("EXPLORE prompt includes %d screenshot(s): %s", len(existing), existing)
    return ("\nThe user attached screenshot(s) to this item in the backlog. They show "
            "the exact UI/behavior the item refers to — view EACH one with the Read tool "
            "before drawing conclusions:\n"
            + "\n".join(f"- {p}" for p in existing) + "\n")


def do_explore(state: dict) -> None:
    # Picking persists EXPLORING before sending the notice. Recover a missing
    # notice on restart/retry, and record delivery before beginning agent work.
    announce_milestone(state, "exploring", "I'm exploring the codebase.")
    result = agent_runner.run(prompts.render(
        prompts.EXPLORE, project=config.PROJECT_NAME, branch=state["branch"], item=state["item"],
        detail=state.get("item_detail", ""),
        images=render_images(state.get("item_images", []))))
    if handle_result(state, result, "EXPLORING"):
        return
    _continue_exploring(state, result)


def _record_exploration(state: dict, result) -> None:
    """Keep what the exploration decided: brainstorming's scale (drives planning) and a
    PR title, so OPEN_PR needs no separate title call. Absent fields change nothing."""
    data = getattr(result, "data", None) or {}
    if data.get("classification") in ("spike", "bounded", "architectural"):
        state["classification"] = data["classification"]
    title = _clamp_pr_title(data.get("pr_title"), PR_TITLE_TARGET) if data.get("pr_title") else ""
    if title and not state.get("pr_title_guidance"):
        state["pr_title"] = title


def _undo_premature_work(state: dict, phase: str) -> str:
    """Revert work that goes beyond a planning phase (EXPLORING/PROPOSING must not
    implement, commit, or switch branches). Openspec change artifacts are the phases'
    legitimate output and are left alone. Returns a note for the user ('' when clean).

    Best-effort by design: a cleanup error must never fail the phase, so problems are
    reported in the note instead of raised."""
    if config.TARGET_MODE == "workspace":
        return ""  # Shared planning is local; never reset child repos as hygiene.
    notes = []
    base_branch = config.BASE_BRANCH
    try:
        current = git("rev-parse", "--abbrev-ref", "HEAD")
        base = state.get("base_sha") or git("merge-base", "HEAD", base_branch)
        branch = state.get("branch")
        if branch and current != branch:
            # The session wandered off the task branch (worst case: committed on the local
            # base branch). -B recreates/resets the task branch at its base even if the
            # session deleted it; -f discards conflicting tracked changes — premature by
            # definition in a planning phase.
            notes.append(f"the session left the checkout on '{current}'; "
                         f"reset '{branch}' to its base and returned to it")
            git("checkout", "-f", "-B", branch, base)
            if current == base_branch:
                try:
                    upstream = git("rev-parse", f"origin/{base_branch}")
                    if git("rev-parse", base_branch) != upstream:
                        git("branch", "-f", base_branch, upstream)
                        notes.append(f"local {base_branch} had premature commits; reset it "
                                     f"to origin/{base_branch}")
                except Exception:  # noqa: BLE001 — e.g. no origin/<base> ref cached
                    notes.append(f"could not verify local {base_branch} against "
                                 f"origin/{base_branch} — please check it manually")
        else:
            ahead = git("rev-list", "--count", f"{base}..HEAD") or "0"
            if ahead != "0":
                notes.append(f"undid {ahead} premature commit(s)")
                git("reset", "--soft", base)
        # -z: NUL-separated and never C-quoted, so paths with spaces/UTF-8 parse exactly
        # (the quoted form broke both the openspec/ prefix check and the git commands).
        entries = git("status", "--porcelain", "-z", "--untracked-files=no").split("\0")
        offending = []
        i = 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if not entry:
                continue
            code, path = entry[:2], entry[3:]
            paths = [path]  # rename/copy: [destination, source] — source follows as its
            if "R" in code or "C" in code:  # own NUL-separated record
                paths.append(entries[i])
                i += 1
            if all(p.startswith("openspec/") for p in paths):
                continue
            offending.append((code, paths))
        for code, paths in offending:
            if len(paths) == 2:
                dest, src = paths
                if not src.startswith("openspec/"):
                    git("checkout", "HEAD", "--", src)  # undo the deletion side
                if not dest.startswith("openspec/"):  # never delete an openspec artifact
                    git("rm", "-f", "--ignore-unmatch", "--", dest)
            elif "A" in code:  # staged addition: no base version to restore
                git("rm", "-f", "--", paths[0])
            else:
                git("checkout", "HEAD", "--", paths[0])
        if offending:
            notes.append("discarded premature changes outside openspec/: "
                         + ", ".join(p for _c, ps in offending for p in ps))
    except Exception as err:  # noqa: BLE001 — hygiene must not break the phase
        log.exception("premature-work cleanup failed")
        notes.append(f"tried to revert premature work but hit an error: {err}")
    if not notes:
        return ""
    note = (f"Note: I detected work beyond the {phase} phase and cleaned it up "
            f"({'; '.join(notes)}). Implementation starts only after your approval.")
    log.warning("%s", note)
    return note


def do_propose(state: dict) -> None:
    result = agent_runner.resume(state["session_id"], prompts.render(
        prompts.PROPOSE, slug=state["slug"], e2e_note=_e2e_note(state)))
    if handle_result(state, result, "PROPOSING"):
        return
    note = _undo_premature_work(state, "PROPOSING")
    _send_proposal_review(state, result.output, note)


def _send_proposal_review(state: dict, output: str, note: str = "", *,
                          revised: bool = False, feedback: str = "") -> None:
    """One review handoff for normal, revised, and question-continuation paths."""
    try:
        bundle, files = proposal_package.prepare(
            config.DATA_DIR, config.REPO_PATH, _validated_slug(state["slug"]),
            state.get("branch") or state["slug"],
            max(0, config.MAX_ATTACHMENT_BYTES - 65536))
        snapshot = proposal_package.previous(state.get("proposal_snapshot")) if revised else None
    except (ValueError, OSError, json.JSONDecodeError) as error:
        _enter_stuck(state, "PROPOSING", f"The complete OpenSpec review package is unavailable: "
                     f"{error}. I cannot ask you to approve a partial proposal.")
        return
    key = proposal_package.fingerprint(files)
    feedback_key = f"{revised}:{feedback}:{key}"
    if state.get("proposal_sent_key") == feedback_key or state.get("reviewed_proposal") == key:
        state["state"] = "WAIT_APPROVAL"
        return
    summary = ""
    if state.get("replan", {}).get("original_archive"):
        summary = (f"Complementary revision of {state['replan']['original_archive']}. "
                    "Existing implementation and PR are retained.\n")
    changes = proposal_package.changes(snapshot, files).splitlines() if revised else []
    digest = ("What changed since your last review:\n" + "\n".join(line[:180] for line in changes[:3])
              + ("\nFurther changes are in the attached proposal." if len(changes) > 3 else "")
              + "\n\n") if revised else ""
    body = ("The complete OpenSpec proposal is attached; implementation remains paused.\n"
            f"Task: {state['item']}\n\n{digest}{summary}"
            + (f"{note[:300]}\n" if note else "")
            )
    email(state, "revised proposal" if revised else "proposal for review", body, [bundle],
          milestone="approval")
    if state.get("last_delivery", {}).get("complete") is False:
        state["proposal_delivery_pending"] = True
    else:
        state.pop("proposal_delivery_pending", None)
    state["proposal_snapshot"] = str(proposal_package.save_snapshot(
        config.DATA_DIR, state.get("branch") or state["slug"], files))
    state["proposal_sent_key"] = feedback_key
    state["reviewed_proposal"] = key
    state["state"] = "WAIT_APPROVAL"
    save_state(state)


def do_approval_reply(state: dict, reply: str) -> None:
    # Never let the autonomous working session self-declare approval. Classify the
    # user's own words with a fresh, dedicated session (like do_merge_reply) and only
    # advance on an explicit go-ahead; anything else keeps us waiting for real approval.
    verdict = ({"action": "approve"} if re.fullmatch(parallel_conversation.APPROVE, reply)
               else replies.classify("approval", reply, state))
    action = verdict.get("action")
    log.info("classified approval reply as action=%r", action)
    if action == "wait":
        email(state, "approval deferred", "Work is preserved. Implementation remains paused until your explicit approval.")
        return
    if action not in ("approve", "changes", "abort", "complete"):
        email(state, "clarification needed",
              f"I could not tell whether your reply approves the proposal, requests "
              f"changes, marks the task complete, or aborts:\n\n{reply}\n\nPlease reply "
              "again with an explicit approval, the changes you want, 'complete', or 'abort'.")
        return  # stay in WAIT_APPROVAL
    if action == "abort":
        _abort_and_reset(state, "Got it — I'm stopping this task and resetting to a clean slate.")
        return
    if action == "complete":
        _finish_task(state, "You asked me to mark this task complete with no further work.",
                     reset_repo=True)
        return
    if action == "approve":
        if state.get("proposal_delivery_pending"):
            gmail_client.retry_deliveries(state)
            if state.get("proposal_delivery_pending"):
                email(state, "proposal delivery pending", "The complete proposal package is still pending delivery. "
                      "Please approve once you have received the full package.")
                return
        _, current_files = proposal_package.prepare(config.DATA_DIR, config.REPO_PATH,
            _validated_slug(state["slug"]), state.get("branch") or state["slug"],
            max(0, config.MAX_ATTACHMENT_BYTES - 65536))
        current = proposal_package.fingerprint(current_files)
        if state.get("reviewed_proposal") and current != state["reviewed_proposal"]:
            _send_proposal_review(state, "The proposal changed; please review this version.", revised=True)
            return
        trail(state, "Proposal approved by the user", reply)
        replanning.approve(state, current)
        state["approved_task_inventory"] = task_phases.inventory(config.REPO_PATH, state["slug"])
        state["approved_scope_fingerprint"] = proposal_package.scope_fingerprint(current_files)
        state.pop("verified_task_inventory", None)
        # The implementer starts fresh from the approved change, not the planning chat.
        state["planning_session_id"] = state.get("session_id")
        state["fresh_session"] = True
        state["implement_budget"] = _implement_budget(state["slug"])
        announce_milestone(state, "implementing", "Proposal approved; implementation is starting.")
        return
    # changes: revise the proposal in the working session and go back to waiting.
    revision_prompt = prompts.render(prompts.REVISE_PROPOSAL, feedback=verdict.get("feedback", ""), slug=state["slug"])
    if config.TARGET_MODE == "workspace":
        import workspace_runner
        result = workspace_runner.turn(state, revision_prompt)
    else:
        result = agent_runner.resume(state["session_id"], revision_prompt)
    if handle_result(state, result, "PROPOSING"):
        return
    note = "" if state.get("replan") else _undo_premature_work(state, "PROPOSING")
    _send_proposal_review(state, result.output, note, revised=True,
                          feedback=verdict.get("feedback", ""))


def _repo_target(state: dict) -> tuple[lifecycle.Target, dict]:
    """Repo mode as the lifecycle's N=1 case: the checkout and its one record."""
    record = persistence.repository_record(state)
    ops = _pr_ops(record.get("pr_url"))
    return lifecycle.Target(config.REPO_PATH, (ops,)), {ops.name: record}


def _save_record(state: dict) -> None:
    persistence.mirror_record(state)
    save_state(state)


def _rails(state: dict, *, commit_plan: bool = False) -> None:
    """Around implementation turns, best effort: commit the approved OpenSpec change
    (before the first turn), then push progress and open the draft PR on the shared
    seam. Any failure leaves today's behaviour (the PR is opened at OPEN_PR)."""
    if not config.DRAFT_PR:
        return
    target, records = _repo_target(state)
    slug = state.get("slug") or ""
    change = f"openspec/changes/{slug}"
    record = records["."]
    # The approved plan is committed once, for the first draft attempt: later edits to the
    # change folder are the agent's work, never "the approved change".
    if (commit_plan and not record.get("pr_url") and not record.get("plan_committed")
            and record.get("draft_attempts", 0) < 2):
        record["plan_committed"] = True
        try:
            if git("status", "--porcelain", "--untracked-files=all", "--", change):
                git("add", "--", change)
                git("commit", "-m", f"Add approved OpenSpec change {_validated_slug(slug)}", "--", change)
        except Exception as error:  # noqa: BLE001 - the draft opens without the plan commit
            log.warning("could not commit the approved change for the draft PR: %s", error)
    draft = (f"Work in progress: {state['item']}\n\nPlan: `{change}` (approved). Coderbot pushes as it "
             "implements and marks this PR ready after verification and review.")
    opened = lifecycle.progress(target, records, lambda: _pr_title(state), lambda _: draft,
                                lambda: _save_record(state))
    persistence.mirror_record(state)
    if opened:
        log.info("draft PR opened: %s", state["draft_pr_url"])
        email(state, "draft PR", f"I opened a draft PR so you can follow the implementation:\n{state['draft_pr_url']}\n\n"
              "I push after each implementation step and mark it ready once it is verified and reviewed.")


def do_implement(state: dict) -> None:
    if _scope_drift(state):
        return
    _rails(state, commit_plan=True)
    feedback = state.get("implementation_feedback", "")
    plan = config.REPO_PATH / "openspec/changes" / _validated_slug(state["slug"]) / "plan.md"
    if state.get("classification") == "architectural" and not plan.exists():
        # Superpowers' ceremony scales with the task: architectural work gets a written
        # plan first, in the change folder; bounded work goes straight to the checklist.
        feedback = (f"First use writing-plans to write {plan.relative_to(config.REPO_PATH)} "
                    "(Inline Execution, one task per tasks.md item) and commit it; then implement.\n" + feedback)
    result = agent_runner.resume(
        state["session_id"],
        f"Implementation continuation: {state.get('implementation_turn', 0)}\n" + feedback + "\n\n" + prompts.render(prompts.IMPLEMENT, slug=state["slug"], branch=state["branch"],
                       e2e_note=_e2e_note(state), e2e_report_note=_e2e_report_note(state)))
    _rails(state)
    if handle_result(state, result, "IMPLEMENTING"):
        return
    _complete_implementation(state, result)


IMPLEMENT_MAX_TURNS = 12
IMPLEMENT_FRESH_TURNS = 3  # what the one fresh-session escalation gets


def _implement_budget(slug: str) -> int:
    """Implementation turns for a change: one per tasks.md section plus three, at most 12.
    Frozen at approval so a growing checklist cannot extend it."""
    try:
        text = (config.REPO_PATH / "openspec/changes" / _validated_slug(slug) / "tasks.md").read_text()
    except (OSError, ValueError):
        return IMPLEMENT_MAX_TURNS
    return min(IMPLEMENT_MAX_TURNS, len(re.findall(r"(?m)^##\s+\d+", text)) + 3)


def _retry_fresh(state: dict, reason: str) -> None:
    """Superpowers' "blocked: more context" step at feature level: one new implementer
    session, seeded with the approved change and the progress so far, before asking."""
    log.warning("implementer %s; retrying once in a fresh session", reason)
    trail(state, f"Implementation {reason}; continuing in a fresh session")
    state["fresh_retries"] = 1
    state["fresh_session"] = True
    state["implementation_turn"] = state.get("implement_budget", IMPLEMENT_MAX_TURNS)
    state["implementation_no_progress"] = 0
    state["state"] = "IMPLEMENTING"
    save_state(state)


def _complete_implementation(state: dict, result) -> None:
    """A completed agent turn is not a completed plan. Both entry paths use this gate."""
    if _scope_drift(state):
        return
    _scrub_evidence_from_repo(state, "IMPLEMENTING")
    state["implementation_summary"] = "\n".join(
        line for line in result.output.splitlines() if not line.startswith("E2E_SPEC:"))[:1800]
    state["e2e_specs"] = list(dict.fromkeys([
        *state.get("e2e_specs", []), *evidence.reported_specs(result.output)]))
    state["implementation_attachments"] = list(dict.fromkeys([
        *state.get("implementation_attachments", []),
        *[str(path) for path in getattr(result, "attachments", []) if Path(path).is_file()]]))
    log.info("implementation reported %d e2e spec(s): %s",
             len(state["e2e_specs"]), state["e2e_specs"])
    if not state["e2e_specs"] and state.get("has_e2e_harness"):
        log.warning("no E2E_SPEC lines in implementation output — feature may lack tests")
    try:
        slug = _validated_slug(state.get("slug"))
        instructions = json.loads(_run_checked([
            "openspec", "instructions", "apply", "--change", slug, "--json"]))
        plan = task_phases.validate_progress(config.REPO_PATH, slug, instructions)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        _enter_stuck(state, "IMPLEMENTING", "Cannot confirm implementation completion: " + str(error))
        return
    if plan.get("approval"):
        _verification_blocked(state, "approval", "\n".join(plan["approval"]))
        return
    if plan["implementation"]:
        progress = {"pending": plan["implementation"], "complete": plan["complete"],
                    "snapshot": content_snapshot(config.REPO_PATH)}
        previous = state.get("implementation_progress")
        advanced = (previous is None or plan["complete"] > previous["complete"]
                    or progress["pending"] != previous["pending"]
                    or progress["snapshot"] != previous["snapshot"])
        state["implementation_no_progress"] = 0 if advanced else state.get("implementation_no_progress", 0) + 1
        state["implementation_progress"] = progress
        state["implementation_feedback"] = (
            "Continue the approved implementation; the plan is NOT complete. Reuse valid work and evidence.\n"
            f"Checked tasks: {plan['complete']}/{plan['total']}. Pending implementation tasks:\n"
            + "\n".join(plan["implementation"]))
        state["state"] = "IMPLEMENTING"
        state["implementation_turn"] = state.get("implementation_turn", 0) + 1
        save_state(state)
        budget = state.setdefault("implement_budget", _implement_budget(state["slug"]))
        stalled = state["implementation_no_progress"] >= config.QUALITY_GATE_MAX_ROUNDS
        if (stalled or state["implementation_turn"] >= budget) and not state.get("fresh_retries"):
            _retry_fresh(state, "made no progress" if stalled else f"used its {budget} turns")
            return
        if stalled or state["implementation_turn"] >= budget + IMPLEMENT_FRESH_TURNS:
            state["implementation_stall_round"] = config.QUALITY_GATE_MAX_ROUNDS - 1
            _gate_failed(state, "IMPLEMENTING", "implementation_stall_round",
                         "Repeated implementation turns made no observable progress" if stalled else
                         "Implementation used its turn budget, including a fresh session, without finishing",
                         state["implementation_feedback"],
                         [Path(path) for path in state["implementation_attachments"]])
            return
        log.info("implementation partial: %d/%d checked; %d task(s) still pending; continuing turn %d",
                 plan["complete"], plan["total"], len(plan["implementation"]), state["implementation_turn"])
        return
    state["implementation_ready"] = plan
    state["verified_task_inventory"] = task_phases.inventory(config.REPO_PATH, state["slug"])
    state["e2e_round"] = 0
    state.pop("implementation_feedback", None)
    state.pop("implementation_no_progress", None)
    state["verify_round"] = 0
    state.pop("e2e_passed", None)
    state["state"] = "VERIFYING"
    trail(state, "Implementation complete; verifying")
    announce_milestone(state, "verifying", "Implementation is complete; I'm running checks and review.")


GATE_REPORT_MAX_CHARS = 3000
GATE_NOTES_MAX_CHARS = 2500

# Contract fields that hold "<command>: <result>" entries, per gate marker.
_GATE_CHECK_FIELDS = {"QUALITY_GATE": ("commands", "preexisting"),
                      "INTERNAL_REVIEW": ("tests",)}


def _gate_report(output: str, marker: str) -> str:
    """A human-readable account of a rejected gate round for the retry prompt and the
    stuck email: every check the agent reported (failing ones first), the contract's
    other fields, then the agent's own notes (the prose before the contract). The bare
    contract JSON is deliberately NOT what the user sees — a live incident emailed only
    "status is not pass" while the real story (100 lint errors) sat in a JSON list."""
    prefix = marker + ":"
    contract_lines = [line.strip() for line in output.splitlines()
                      if line.strip().startswith(prefix)]
    notes = "\n".join(line for line in output.splitlines()
                      if not line.strip().startswith(prefix)).strip()
    parts: list[str] = []
    value = None
    if contract_lines:
        try:
            value = json.loads(contract_lines[-1][len(prefix):].strip())
        except ValueError:
            value = None
    if isinstance(value, dict):
        checks = []
        for field in _GATE_CHECK_FIELDS.get(marker, ()):
            entries = value.get(field)
            if isinstance(entries, list):
                label = "" if field in ("commands", "tests") else f" [{field}]"
                checks += [(str(e).strip() + label) for e in entries if str(e).strip()]
        if checks:
            failing = [c for c in checks if _looks_failing(c)]
            passing = [c for c in checks if not _looks_failing(c)]
            parts.append("Checks the agent reported (failing first):\n"
                         + "\n".join(f"- {c}" for c in failing + passing))
        others = {k: v for k, v in value.items()
                  if k not in _GATE_CHECK_FIELDS.get(marker, ()) and k != "status"}
        if others:
            parts.append("Other contract fields: "
                         + ", ".join(f"{k}={json.dumps(v)}" for k, v in others.items()))
    elif contract_lines:
        parts.append("Contract line as emitted (malformed): " + contract_lines[-1][:500])
    if notes:
        tail = notes[-GATE_NOTES_MAX_CHARS:]
        if len(notes) > GATE_NOTES_MAX_CHARS:
            tail = "[...]\n" + tail
        parts.append("Agent's notes:\n" + tail)
    return "\n\n".join(parts)[:GATE_REPORT_MAX_CHARS + GATE_NOTES_MAX_CHARS]


def _gate_failed(state: dict, phase: str, counter: str, reason: str, report: str = "",
                 attachments: list[Path] | None = None) -> None:
    diagnostic = diagnostics.report(phase_checkpoint.store(), state, config.REPO_PATH,
        config.DATA_DIR, phase, detail=f"{reason}\n\n{report}")
    state[counter] = state.get(counter, 0) + 1
    # Fed back into the next round's prompt (and the stuck email): a retry that repeats
    # the identical prompt just gets the identical failing report again.
    state[f"{counter}_feedback"] = {"reason": reason, "report": report}
    log.warning("%s result rejected (round %d/%d): %s", phase, state[counter],
                config.QUALITY_GATE_MAX_ROUNDS, reason)
    if state[counter] < config.QUALITY_GATE_MAX_ROUNDS:
        state["state"] = phase
        return
    label = phase.lower().replace('_', ' ')
    body = (f"Task: {state['item']}\n\nThe {label} gate has failed {state[counter]} times. "
            f"Latest reason: {reason}\n\n")
    if report:
        body += f"Reported results:\n{report[:1200]}\n\nFull details are in the diagnostic file: {diagnostic.name}\n\n"
    body += ("Reply with guidance to continue — e.g. tell the agent to fix a failure, or that a "
             "failure is pre-existing on the base branch (it will confirm that there and "
             "report it as pre-existing).")
    files = [diagnostic, *(path for path in (attachments or []) if path.is_file())]
    decision = (f"The {label} gate is blocked: {reason}.\n\n"
                + (f"Reported results:\n{report[:1200]}\n\n" if report else "")
                + "Should I return to implementation to complete or repair the pending work, "
                "or investigate whether this is pre-existing on the base branch? "
                "Reply with guidance; full results and supporting files are attached.")
    email(state, f"{label} stuck - needs your help", body, files, visible_question=decision)
    state["pending_question"] = decision
    state["return_state"] = phase
    if state.get("pending_decision"):
        state["pending_decision"]["origin"] = phase
    state["state"] = "WAIT_REPLY"


def _gate_feedback(state: dict, counter: str) -> str:
    """The rejection notice to prepend to a gate prompt, or "" on a fresh first round."""
    feedback = state.get(f"{counter}_feedback")
    if not state.get(counter) or not isinstance(feedback, dict):
        return ""
    return prompts.render(prompts.GATE_FEEDBACK, round=state[counter],
                          max=config.QUALITY_GATE_MAX_ROUNDS,
                          reason=feedback.get("reason", ""), report=feedback.get("report", ""))


def _verify_prompt(state: dict) -> str:
    path = config.REPO_PATH / "openspec/changes" / _validated_slug(state["slug"]) / "tasks.md"
    reconciliation = task_phases.inspect(config.REPO_PATH, state["slug"])["verification"] if path.is_file() else []
    return (_gate_feedback(state, "verify_round")
            + ("Reconcile these approved verification tasks in this turn; they are not "
               "new implementation or approval requests:\n"
               + prompts.fenced("pending verification tasks", "\n".join(reconciliation)) + "\n"
               if reconciliation else "")
            + ("User requested verification of the existing work, not new planning. "
               "Preserve the current implementation and inspect the cited commit before "
               "doing any further work. Reuse valid existing results.\n"
               + prompts.fenced_authoritative("verification guidance", state["verification_guidance"]) + "\n"
               if state.get("verification_guidance") else "")
            + prompts.render(prompts.VERIFY, slug=state["slug"], base=state.get("base_sha") or config.BASE_BRANCH))


def _review_guide(state: dict) -> str:
    """Superpowers' own reviewer template (requesting-code-review/code-reviewer.md),
    filled in by the controller; a built-in checklist if it is missing (never blocks)."""
    template = config.SUPERPOWERS_PLUGIN_DIR / "skills/requesting-code-review/code-reviewer.md"
    try:
        text = template.read_text()
    except OSError:
        log.info("reviewer template unavailable at %s; using the built-in checklist", template)
        return prompts.REVIEW_GUIDE_FALLBACK
    values = {"[DESCRIPTION]": f"{state.get('item', '')}\n{state.get('implementation_summary', '')}".strip(),
              "[PLAN_OR_REQUIREMENTS]": state.get("archive_path") or f"openspec/changes/{state.get('slug')}/",
              "[BASE_SHA]": state.get("base_sha", "the branch base"), "[HEAD_SHA]": "HEAD"}
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    return "Review guide (Superpowers code reviewer):\n" + text.strip()


def _internal_review_prompt(state: dict) -> str:
    return (_gate_feedback(state, "review_gate_round")
            + (prompts.fenced_authoritative("verification scope", state["verification_guidance"]) + "\n"
               if state.get("verification_guidance") else "")
            + prompts.render(prompts.INTERNAL_REVIEW, slug=state["slug"], review_guide=_review_guide(state),
                             scope=_review_scope(state)))


def _review_scope(state: dict) -> str:
    base = state.get("review_fix_base") or state.get("base_sha", "HEAD~1")
    if state.get("review_fix_base"):
        return prompts.render(prompts.RE_REVIEW_SCOPE, slug=state["slug"], base=base)
    parts = state.get("review_parts")
    if parts:
        n = state.get("review_part", 0)
        return prompts.render(prompts.PART_SCOPE, slug=state["slug"], base=base, part=n + 1, parts=len(parts),
                              files=" ".join(parts[n]))
    return prompts.render(prompts.REVIEW_SCOPE, slug=state["slug"], base=base)


def _review_parts(state: dict) -> list[list[str]] | None:
    """Two halves of the changed files by changed lines, when the diff is too big for
    one review; None otherwise (or when the diff cannot be read)."""
    try:
        rows = [line.split("\t", 2) for line in git("diff", "--numstat", f"{state['base_sha']}...HEAD").splitlines()]
        sizes = [(path, (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)) for a, d, path in rows]
    except (KeyError, ValueError, RuntimeError):
        return None
    total = sum(size for _, size in sizes)
    if total <= config.REVIEW_SPLIT_LINES or len(sizes) < 2:
        return None
    first, running = [], 0
    for path, size in sizes:
        if running >= total / 2 and first:
            break
        first.append(path)
        running += size
    rest = [path for path, _ in sizes if path not in first]
    return [first, rest] if rest else None


def _verification_blocked(state: dict, kind: str, detail: str) -> None:
    """A scope/administrative blocker is a decision, not another coding attempt."""
    guidance = ("Repair the execution environment and request VERIFY again; these results "
                 "do not establish missing implementation." if kind == "check infrastructure" else
                "Provide or obtain the specific missing evidence identified above, then "
                "request VERIFY again. No new spec approval is required; I will not "
                "credit unverified results or restart implementation."
                if kind == "verification evidence" else
                "Reconcile the checklist or explicitly approve a scope change; "
                "I will not restart planning or implementation automatically.")
    question = ("Verification is blocked by " + kind + ":\n" + detail + "\n\n"
                "The existing implementation is preserved. " + guidance)
    state["verification_blocker"] = {"kind": kind, "detail": detail}
    state["pending_question"] = question
    state["return_state"] = "VERIFYING"
    state["state"] = "WAIT_REPLY"
    save_state(state)
    email(state, "Verification blocked", question, visible_question=question)


def _scope_drift(state: dict) -> bool:
    path = config.REPO_PATH / "openspec/changes" / _validated_slug(state["slug"]) / "tasks.md"
    if state.get("approved_scope_fingerprint"):
        directory = (_validated_archive_path(state["archive_path"], state["slug"])
                     if state.get("archive_path") else path.parent)
        current = proposal_package.scope_fingerprint(proposal_package.collect_directory(directory))
        if current != state["approved_scope_fingerprint"]:
            _verification_blocked(state, "scope drift", "Requirements, design or task text changed after approval. "
                                  "Completion checkboxes and archival do not require reapproval; changed approved intent does.")
            return True
    if not path.is_file():
        return False
    actual = task_phases.inventory(config.REPO_PATH, state["slug"])
    expected = state.get("verified_task_inventory", state.get("approved_task_inventory"))
    if expected is not None and actual != expected:
        added = [task for task in actual if task not in expected]
        removed = [task for task in expected if task not in actual]
        _verification_blocked(state, "scope drift", "Added/changed tasks:\n" + "\n".join(added)
                              + "\nRemoved/changed tasks:\n" + "\n".join(removed))
        return True
    return False


def _verify_preflight(state: dict) -> bool:
    if _scope_drift(state):
        return True
    path = config.REPO_PATH / "openspec/changes" / _validated_slug(state["slug"]) / "tasks.md"
    if not path.is_file():
        return False  # Normal strict validation handles a missing change.
    pending = task_phases.inspect(config.REPO_PATH, state["slug"])
    if pending["approval"]:
        _verification_blocked(state, "approval", "\n".join(pending["approval"]))
        return True
    if state.get("verification_guidance") and pending["implementation"]:
        _verification_blocked(state, "unfinished or unclassified tasks", "\n".join(pending["implementation"]))
        return True
    state.pop("verification_blocker", None)
    return False


def _verify_requested_checks(state: dict, result) -> bool:
    """The agent's pass claim cannot override a controller-run unknown/failure."""
    if error := check_plan.reported_error(result.output):
        _gate_failed(state, "VERIFYING", "verify_round", error,
                     "Emit CHECK_PLAN: followed by a valid version-1 JSON check plan; no tests were executed from the malformed plan.", [])
        return True
    if not check_plan.reported(result.output):
        return False
    failed = [check for check in state.get("focused_check_results", [])
               if check.get("gate", {}).get("status") != "pass" and check["status"] not in ("pass", "skipped")]
    if not failed:
        return False
    report = {"status": "fail", "checks": [dict(check, gate={"status": "indeterminate"
              if check["status"] in ("unknown", "infrastructure", "interrupted") else "fail",
              "regressions": list(check.get("failures", {})), "preexisting": []}) for check in failed]}
    _begin_check_repair(state, report, resume="VERIFYING")
    return True


def _complete_verify(state: dict, result) -> None:
    if _verify_preflight(state):
        return
    if handle_result(state, result, "VERIFYING"):
        return
    if _verify_requested_checks(state, result):
        return
    task_path = config.REPO_PATH / "openspec/changes" / _validated_slug(state["slug"]) / "tasks.md"
    if task_path.is_file():
        pending = task_phases.inspect(config.REPO_PATH, state["slug"])
        if pending["verification"]:
            detail = ("Unresolved verification tasks:\n" + "\n".join(pending["verification"])
                      + "\n\nVerifier findings (missing evidence or results):\n"
                      + _gate_report(result.output, "QUALITY_GATE"))
            _verification_blocked(state, "verification evidence", detail)
            return
        if pending["implementation"]:
            state["implementation_return_round"] = state.get("implementation_return_round", 0) + 1
            if state["implementation_return_round"] > config.QUALITY_GATE_MAX_ROUNDS:
                _gate_failed(state, "VERIFYING", "verify_round", "Implementation remains incomplete",
                             "\n".join(pending["implementation"]),
                             [Path(path) for path in getattr(result, "attachments", [])])
                return
            state["implementation_feedback"] = "Complete these approved implementation tasks before verification:\n" + "\n".join(pending["implementation"])
            state["state"] = "IMPLEMENTING"
            trail(state, "Verification returned to implementation: approved tasks remain incomplete",
                  state["implementation_feedback"])
            return
    parsed, reason = parse_quality_gate(result.output)
    if parsed is None:
        _gate_failed(state, "VERIFYING", "verify_round", reason,
                     _gate_report(result.output, "QUALITY_GATE"),
                     [Path(path) for path in getattr(result, "attachments", [])])
        return
    try:
        slug = _validated_slug(state.get("slug"))
        _run_checked(["openspec", "validate", slug, "--strict", "--no-interactive"])
        instructions = json.loads(_run_checked([
            "openspec", "instructions", "apply", "--change", slug, "--json",
        ]))
        progress = instructions.get("progress") if isinstance(instructions, dict) else None
        if task_path.is_file():
            task_phases.validate_progress(config.REPO_PATH, slug, instructions)
        deferred = parsed.get("deferred", 0)
        if deferred:
            owned = task_phases.inspect(config.REPO_PATH, slug)
            if owned["implementation"] or len(owned["final_checks"]) != deferred:
                raise ValueError("Deferred tasks must be explicitly owned by codebot final checks")
        if not isinstance(progress, dict) or instructions.get("state") not in (("all_done", "ready") if deferred else ("all_done",)):
            raise ValueError("OpenSpec apply state is not all_done")
        total = progress.get("total")
        complete = progress.get("complete")
        remaining = progress.get("remaining")
        if any(type(value) is not int for value in (total, complete, remaining)) or \
                 remaining != deferred or complete + deferred != total:
            raise ValueError("OpenSpec apply progress is incomplete")
        if parsed["tasks"] != f"{complete}/{total}":
            raise ValueError("Agent task counts do not match the controller-validated checklist")
    except Exception as error:
        _gate_failed(state, "VERIFYING", "verify_round", str(error),
                     _gate_report(result.output, "QUALITY_GATE"),
                     [Path(path) for path in getattr(result, "attachments", [])])
        return
    state.pop("verify_round", None)
    state.pop("verify_round_feedback", None)
    state["quality_report"] = parsed
    state["quality_snapshot"] = content_snapshot(config.REPO_PATH)
    state["quality_controller_validated"] = True
    evidence_delivery.register_verified(state, config.REPO_PATH, getattr(result, "attachments", []))
    _advance_verified(state)


def _advance_verified(state: dict) -> None:
    state["review_gate_round"] = 0
    reviewed = state.get("internal_review_report", {})
    reusable = (reviewed.get("status") == "pass" and reviewed.get("critical") == 0
                and state.get("reviewed_snapshot") == state["quality_snapshot"])
    state["state"] = "E2E" if reusable else "INTERNAL_REVIEW"
    trail(state, "Verification passed; " + ("reusing content-bound internal review" if reusable else "running internal review"))


def _controller_verify(state: dict) -> bool:
    """Verify without an agent turn whenever ground truth suffices: strict OpenSpec
    validation, the controller's own checklist counts and the focused checks it already
    ran. Returns False (caller falls back to the agent verification turn, today's path)
    when something needs the agent: verification tasks to reconcile, an invalid change,
    or checks that have not run. Failing checks go straight to REPAIR_CHECKS."""
    slug = _validated_slug(state.get("slug"))
    task_path = config.REPO_PATH / "openspec/changes" / slug / "tasks.md"
    if not task_path.is_file():
        return False
    pending = task_phases.inspect(config.REPO_PATH, slug)
    if pending["verification"]:
        return False
    if pending["implementation"]:
        state["implementation_return_round"] = state.get("implementation_return_round", 0) + 1
        if state["implementation_return_round"] > config.QUALITY_GATE_MAX_ROUNDS:
            return False  # the agent turn reports why the tasks stay open
        state["implementation_feedback"] = ("Complete these approved implementation tasks before verification:\n"
                                            + "\n".join(pending["implementation"]))
        state["state"] = "IMPLEMENTING"
        trail(state, "Verification returned to implementation: approved tasks remain incomplete",
              state["implementation_feedback"])
        return True
    results = state.get("focused_check_results") or []
    failed = [check for check in results
              if check.get("gate", {}).get("status") != "pass" and check.get("status") not in ("pass", "skipped")]
    if failed:
        report = {"status": "fail", "checks": [dict(check, gate={"status": "indeterminate"
                  if check.get("status") in ("unknown", "infrastructure", "interrupted") else "fail",
                  "regressions": list(check.get("failures", {})), "preexisting": []}) for check in failed]}
        _begin_check_repair(state, report, resume="VERIFYING")
        return True
    try:
        _run_checked(["openspec", "validate", slug, "--strict", "--no-interactive"])
        instructions = json.loads(_run_checked(["openspec", "instructions", "apply", "--change", slug, "--json"]))
        task_phases.validate_progress(config.REPO_PATH, slug, instructions)
        progress = instructions.get("progress") if isinstance(instructions, dict) else None
        deferred = len(pending["final_checks"])
        total, complete = progress["total"], progress["complete"]
        if instructions.get("state") not in (("all_done", "ready") if deferred else ("all_done",)) \
                or complete + deferred != total:
            return False
    except Exception as error:  # noqa: BLE001 - the agent turn repairs and reports it
        log.info("controller verification needs the agent: %s", error)
        return False
    state["quality_report"] = {"status": "pass", "openspec": "pass", "tasks": f"{complete}/{total}",
                               "commands": [f"{check.get('check') or check.get('id')}: {check.get('status')}"
                                            for check in results] or ["openspec validate --strict: pass"],
                               "preexisting": [], **({"deferred": deferred} if deferred else {})}
    state["quality_snapshot"] = content_snapshot(config.REPO_PATH)
    state["quality_controller_validated"] = True
    state.pop("verify_round", None)
    state.pop("verify_round_feedback", None)
    evidence_delivery.register_verified(state, config.REPO_PATH, [])
    trail(state, "Verified by the controller (OpenSpec, checklist and focused checks); no agent turn needed")
    _advance_verified(state)
    return True


def do_verify(state: dict) -> None:
    if _verify_preflight(state):
        return
    task_path = config.REPO_PATH / "openspec/changes" / _validated_slug(state["slug"]) / "tasks.md"
    reconciliation = task_phases.inspect(config.REPO_PATH, state["slug"])["verification"] if task_path.is_file() else []
    if not reconciliation and state.get("quality_controller_validated") and state.get("quality_report") \
            and state.get("quality_snapshot") == content_snapshot(config.REPO_PATH):
        report, _ = parse_quality_gate("QUALITY_GATE: " + json.dumps(state["quality_report"]))
        if report:
            _advance_verified(state)
            return
    if not state.get("verification_guidance") and _controller_verify(state):
        return
    result = agent_runner.resume(state["session_id"], _verify_prompt(state))
    _complete_verify(state, result)


def _complete_internal_review(state: dict, result) -> None:
    if _scope_drift(state):
        return
    if handle_result(state, result, "INTERNAL_REVIEW"):
        return
    if check_plan.reported(result.output):
        failed = [check for check in state.get("focused_check_results", [])
                  if check.get("gate", {}).get("status") != "pass" and check["status"] not in ("pass", "skipped")]
        if failed:
            report = {"status": "fail", "checks": [dict(check, gate={"status": "fail",
                      "regressions": list(check.get("failures", {})), "preexisting": []}) for check in failed]}
            _begin_check_repair(state, report, resume="INTERNAL_REVIEW")
            return
    review = (getattr(result, "data", None) or {}).get("review") or {}
    parts = state.get("review_parts") or []
    if parts and not state.get("review_fix_base") and review:  # a turn without a review is no clean part
        # Collect the findings of every part; the fix wave and re-review cover them all.
        seen = state.setdefault("review_part_findings", [])
        seen.extend(f for f in review.get("findings") or [] if isinstance(f, str))
        state["review_part_open"] = state.get("review_part_open", 0) + int(review.get("critical") or 0) \
            + int(review.get("important") or 0)
        if state.get("review_part", 0) + 1 < len(parts):
            state["review_part"] = state.get("review_part", 0) + 1
            return  # next part
        review = {"critical": state["review_part_open"], "important": 0, "findings": seen}
    if (review.get("critical") or review.get("important")) and not state.get("review_fix_base"):
        # One fix wave in the implementer's session, then one re-review of its commits.
        state["review_fix_base"] = git("rev-parse", "HEAD")
        state["review_findings"] = [f for f in review.get("findings") or [] if isinstance(f, str)][:30] \
            or [result.output[-3000:]]
        state["state"] = "APPLY_FEEDBACK"
        trail(state, "Internal review found issues; fixing them", "\n".join(state["review_findings"]))
        return
    parsed, reason = parse_internal_review(result.output)
    if parsed is None:
        parsed = _review_with_leftovers(result)
    if parsed is None:
        _gate_failed(state, "INTERNAL_REVIEW", "review_gate_round", reason,
                     _gate_report(result.output, "INTERNAL_REVIEW"),
                     [Path(path) for path in getattr(result, "attachments", [])])
        return
    state.pop("review_gate_round", None)
    state.pop("review_gate_round_feedback", None)
    for key in REVIEW_CYCLE_KEYS:  # this review cycle is over; a later one starts afresh
        state.pop(key, None)
    state["internal_review_report"] = parsed
    state["reviewed_snapshot"] = content_snapshot(config.REPO_PATH)
    evidence_delivery.register_verified(state, config.REPO_PATH, getattr(result, "attachments", []))
    state["state"] = "E2E"  # Final unit/lint/build checks also apply without a UI harness.
    trail(state, "Internal review passed; " +
          ("running the e2e suite" if state.get("has_e2e_harness") else "archiving the change"))
    if state["state"] == "ARCHIVING":
        announce_milestone(state, "archiving", "Checks passed; I'm archiving the OpenSpec change.")


REVIEW_CYCLE_KEYS = ("review_parts", "review_part", "review_part_findings", "review_part_open",
                     "review_fix_base", "review_findings")


def _review_with_leftovers(result) -> dict | None:
    """The feature review already ran its fix wave and re-review in the turn. Open
    Important/Minor findings do not block: they are listed on the PR. Only open Critical
    findings (or a review without evidence) go back through the gate."""
    review = (getattr(result, "data", None) or {}).get("review") or {}
    try:
        critical, important = int(review.get("critical", 1)), int(review.get("important", 0))
    except (TypeError, ValueError):
        return None
    tests = [t for t in review.get("tests") or [] if isinstance(t, str) and t.strip()]
    if critical or not tests:
        return None
    findings = [f for f in review.get("findings") or [] if isinstance(f, str) and f.strip()][:20]
    return {"status": "pass", "critical": 0, "important": important, "tests": tests,
            "concerns": findings or ([f"{important} Important finding(s) left open"] if important else [])}


def _review_concerns(state: dict) -> str:
    concerns = state.get("internal_review_report", {}).get("concerns") or []
    return ("Open review findings (not blocking):\n" + "\n".join(f"- {c}" for c in concerns)) if concerns else ""


def do_internal_review(state: dict) -> None:
    if _scope_drift(state):
        return
    if not state.get("review_fix_base") and "review_parts" not in state:
        state["review_parts"] = _review_parts(state)
    before = (git("rev-parse", "HEAD"), content_snapshot(config.REPO_PATH))
    result = agent_runner.resume(state["session_id"], _internal_review_prompt(state))
    if (git("rev-parse", "HEAD"), content_snapshot(config.REPO_PATH)) != before:
        # The reviewer is read-only; a review whose own edits it judged proves nothing.
        kept = _preserve_uncommitted(f"{state['slug']} reviewer edits")
        log.warning("the read-only reviewer changed the tree; discarding its review (%s)", kept)
        if git("rev-parse", "HEAD") != before[0]:
            state["state"] = "VERIFYING"  # it committed despite the deny rules: verify that code
            return
        _gate_failed(state, "INTERNAL_REVIEW", "review_gate_round", "The reviewer changed files",
                     "Review read-only; the implementer fixes the findings.")
        return
    _complete_internal_review(state, result)


def _tracked_snapshot() -> tuple[str, str]:
    return (git("rev-parse", "HEAD"),
            git("status", "--porcelain", "--untracked-files=no"))


def do_e2e(state: dict) -> None:
    if _scope_drift(state):
        return
    for key in ("e2e_repair_head", "e2e_repair_status"):  # the retired agent-run suite's repair
        state.pop(key, None)
    report = final_checks.run(state, config.REPO_PATH, phase_checkpoint.store())
    state["final_check_report"] = report
    save_state(state)
    if report["status"] != "pass":
        _begin_check_repair(state, report, resume="E2E")
        return
    state.pop("check_repair", None)
    state.pop("final_repair_round", None)
    state.pop("final_check_round", None)
    state["e2e_passed"] = report.get("suite_status") == "pass" and bool(state.get("has_e2e_harness")) and not any(
        w.get("scope") == "e2e:general" for w in state.get("check_waivers", []))
    if state.get("quality_report", {}).get("deferred"):
        _commit_final_check_tasks(state)
    if state.pop("feedback_delivery_pending", False) and state.get("archive_path"):
        _queue_push(state, "feedback", "Requested corrections verified.",
                    feedback=state.get("pending_feedback", ""))
        return
    state["state"] = "ARCHIVING"
    announce_milestone(state, "archiving", "Final checks passed; I'm archiving the change.")


def _begin_check_repair(state: dict, report: dict, *, resume: str) -> None:
    """Every unresolved final result gets diagnosis and repair, not blind retries."""
    previous = state.get("check_repair", {})
    attempt = previous.get("attempt", 0) + 1
    state["check_repair"] = {"attempt": attempt, "resume": resume,
        "checks": check_repair.unresolved(report), "before": content_snapshot(config.REPO_PATH),
        "previous": previous.get("findings", ""), "guidance": previous.get("guidance", "")}
    if previous.get("evidence"):
        state["check_repair"]["evidence"] = True
    state["state"] = "REPAIR_CHECKS"
    save_state(state)
    if attempt > config.QUALITY_GATE_MAX_ROUNDS:
        question = (check_repair.summary(report) + "\n\nBounded automatic repair attempts "
                    "did not produce a controller-verified pass. Last diagnosis:\n"
                    + (previous.get("findings") or "No successful repair diagnosis was recorded.")
                    + "\n1. Continue diagnosis and implement a different fix (Recommended)"
                    "\n2. Provide additional evidence or the missing external prerequisite; include details after ':'"
                    "\n3. Keep waiting and preserve work; do not waive checks"
                    "\nReply with the option number; include any new evidence with option 2.")
        state["pending_question"] = question
        state["return_state"] = "REPAIR_CHECKS"
        state["state"] = "WAIT_REPLY"
        save_state(state)
        files = [Path(item["report"]) for item in state["check_repair"]["checks"]
                 if item.get("report") and Path(item["report"]).is_file()]
        email(state, "final check recovery decision", question, files, visible_question=question)
        return
    email(state, "repairing final checks", check_repair.summary(report) +
          "\nI am diagnosing the logs and implementing a fix. Work is preserved; "
          "no user action is needed. Unaffected valid checks will be reused.")


def do_repair_checks(state: dict) -> None:
    if not state.get("archive_path") and _scope_drift(state):
        return
    context = state.get("check_repair")
    if not isinstance(context, dict):
        raise RuntimeError("Final-check repair context is missing")
    if context["attempt"] > config.QUALITY_GATE_MAX_ROUNDS:
        _begin_check_repair(state, {"checks": context["checks"]}, resume=context["resume"])
        return
    result = agent_runner.resume(state["session_id"], check_repair.prompt(state, context))
    if handle_result(state, result, "REPAIR_CHECKS"):
        return
    # The diagnosis and evidence are part of the conversation, not only logs.
    email(state, "final check repair findings", result.output.strip() or "Repair turn finished; controller verification is next.",
          [Path(p) for p in result.attachments if Path(p).is_file()])
    if state.get("last_delivery", {}).get("complete") is False:
        raise RuntimeError("Repair findings delivery is incomplete")
    _complete_check_repair(state, result)


def _complete_check_repair(state: dict, result) -> None:
    context = state["check_repair"]
    if not state.get("archive_path") and _scope_drift(state):
        return
    context["findings"] = result.output.strip()[-5000:]
    _, status = _tracked_snapshot()
    if status:
        # Never push/archive a repair turn with uncommitted tracked changes.
        context["previous"] = context["findings"] + "\nRepair is incomplete: commit only intended tracked repairs; preserve unrelated work.\n" + status
        context["attempt"] += 1
        state["state"] = "REPAIR_CHECKS"
        save_state(state)
        return
    checks.invalidate_environment(phase_checkpoint.store(), config.REPO_PATH,
                                  [item["check"] for item in context["checks"] if item.get("check")])
    # A clean Git tree is not an environment repair. Re-execute affected checks
    # even when only the ignored venv/service state changed, and never trust prose.
    changed = context["before"] != content_snapshot(config.REPO_PATH)
    for key in ("e2e_passed", "final_check_report", "final_check_round", "post_review_check_round"):
        state.pop(key, None)
    if changed or context["checks"]:
        # Verification is recomputed (cheaply, by the controller). The internal review is
        # kept: it is reused only for the exact content it reviewed (reviewed_snapshot),
        # so a code-changing repair is re-reviewed and an environment-only one is not.
        for key in ("quality_report", "quality_snapshot", "quality_controller_validated"):
            state.pop(key, None)
    active = not state.get("archive_path") and (config.REPO_PATH / "openspec/changes" / state["slug"]).is_dir()
    if context.get("evidence"):
        state["state"] = "PUSHING"  # do_push re-runs the final checks; the PR's review sees the fix
        save_state(state)
        trail(state, "Evidence repair returned to controller verification", context["findings"])
        return
    state["state"] = "VERIFYING" if changed and active else context["resume"]
    if state["state"] == "VERIFYING":
        state["verify_round"] = 0
    if context["resume"] in ("VERIFYING", "INTERNAL_REVIEW"):
        state.pop("focused_check_results", None)
    save_state(state)
    trail(state, "Check repair returned to controller verification", context["findings"])


def _commit_final_check_tasks(state: dict) -> None:
    """Commit only the controller's checkbox delta, including after interruption."""
    if state.get("final_check_report", {}).get("status") != "pass":
        raise RuntimeError("Final tasks require a passing controller report")
    slug = _validated_slug(state["slug"])
    relative = f"openspec/changes/{slug}/tasks.md"
    path = config.REPO_PATH / relative
    committed = git("show", "HEAD:" + relative)
    expected = task_phases.completed_final_checks_text(committed)
    # git() strips trailing whitespace; compare using the same normalization.
    if path.read_text().strip() not in (committed, expected.strip()):
        raise RuntimeError("Final-task completion found unrelated tasks.md edits; preserve them before retrying")
    staged = git("diff", "--cached", "--name-only")
    if staged and (staged != relative or git("show", ":" + relative) != expected.strip()):
        raise RuntimeError("Final-task completion found unrelated staged edits")
    if git("status", "--porcelain", "--untracked-files=no", "--", ".", ":(exclude)" + relative):
        raise RuntimeError("Final-task completion requires other tracked files to be committed")
    if committed == expected.strip():
        return  # Already committed: restart must not regenerate the same delta.
    database = phase_checkpoint.store()
    before = repo_provenance.inspect(config.REPO_PATH)
    task_phases.complete_final_checks(config.REPO_PATH, slug)
    after = repo_provenance.inspect(config.REPO_PATH)
    repo_provenance.record_turn(database, state, config.REPO_PATH, before, after)
    database.record("provenance", state, config.REPO_PATH, "final-tasks:" + after["fingerprint"],
                    {"kind": "controller-final-task-completion", "files": after["files"],
                     "verification_snapshot": state.get("final_check_report", {}).get("snapshot")}, status="complete")
    git("add", "--", relative)
    git("commit", "-m", "chore: complete controller final checks", "--", relative)


def _run_checked(command: list[str]) -> str:
    proc = operations.tool(command, cwd=config.REPO_PATH)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise errors.from_tool(f"{' '.join(command)} exited {proc.returncode}: {detail}", code=f"{command[0]}_failed")
    return proc.stdout.strip()


def _validated_slug(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
        raise ValueError("change slug must be safe kebab-case")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("dated change slug must include a kebab-case name")
    dated = re.match(r"^(\d{4}-\d{2}-\d{2})-", value)
    if dated:
        try:
            date.fromisoformat(dated.group(1))
        except ValueError as error:
            raise ValueError("change slug has an invalid date prefix") from error
    return value


def _validated_archive_path(value, slug: str) -> Path:
    if not isinstance(value, str):
        raise ValueError("archive path must be a relative string")
    relative = Path(value)
    if relative.is_absolute() or relative.parts[:3] != ("openspec", "changes", "archive") or \
            len(relative.parts) != 4:
        raise ValueError("archive path must be a direct child of openspec/changes/archive")
    name = relative.name
    if re.match(r"^\d{4}-\d{2}-\d{2}-", slug):
        if name != slug:
            raise ValueError("archive path does not match the dated change slug")
    else:
        match = re.fullmatch(rf"(\d{{4}}-\d{{2}}-\d{{2}})-{re.escape(slug)}", name)
        if not match:
            raise ValueError("archive path does not match the change slug")
        try:
            date.fromisoformat(match.group(1))
        except ValueError as error:
            raise ValueError("archive path has an invalid date prefix") from error

    repo = config.REPO_PATH.resolve()
    archive_root = repo / "openspec" / "changes" / "archive"
    target = (config.REPO_PATH / relative).resolve()
    if target.parent != archive_root:
        raise ValueError("archive path resolves outside openspec/changes/archive")
    return target


def _archive_target(state: dict) -> Path:
    slug = _validated_slug(state.get("slug"))
    if state.get("archive_path"):
        return _validated_archive_path(state["archive_path"], slug)
    active = config.REPO_PATH / "openspec" / "changes" / slug
    archive_root = config.REPO_PATH / "openspec" / "changes" / "archive"
    if archive_root.resolve() != config.REPO_PATH.resolve() / "openspec" / "changes" / "archive":
        raise ValueError("archive root resolves outside the repository")
    matches = list(archive_root.glob(f"????-??-??-{slug}")) if archive_root.is_dir() else []
    if not active.exists() and len(matches) == 1:
        target = matches[0]
    elif not active.exists() and len(matches) > 1:
        raise RuntimeError(f"multiple archives match change {slug}")
    else:
        name = slug if re.match(r"^\d{4}-\d{2}-\d{2}-", slug) else f"{date.today():%Y-%m-%d}-{slug}"
        target = archive_root / name
    relative = str(target.relative_to(config.REPO_PATH))
    target = _validated_archive_path(relative, slug)
    state["archive_path"] = relative
    save_state(state)
    return target


def _archive_failed(state: dict, error: Exception) -> None:
    path = diagnostics.report(phase_checkpoint.store(), state, config.REPO_PATH,
                              config.DATA_DIR, "ARCHIVING", error=error)
    state["archive_error"] = str(error)
    state["archive_round"] = state.get("archive_round", 0) + 1
    log.warning("archive failed (round %d/%d): %s", state["archive_round"],
                config.ARCHIVE_MAX_ROUNDS, error)
    if state["archive_round"] < config.ARCHIVE_MAX_ROUNDS:
        state["state"] = "ARCHIVING"
        return
    if state.get("session_id") and not state.get("archive_fix_turn"):
        # One automatic repair of the OpenSpec files before asking (work is committed:
        # do_archive refuses to run on a dirty tree).
        state["archive_fix_turn"] = True
        result = agent_runner.resume(state["session_id"], prompts.render(
            prompts.FIX_ARCHIVE, error=str(error), guidance="none"))
        if handle_result(state, result, "ARCHIVING"):
            return
        state.pop("archive_round", None)
        state["state"] = "ARCHIVING"
        return
    email(state, "OpenSpec archival stuck - needs your help",
          f"Task: {state['item']}\n\nArchival failed {state['archive_round']} times. "
          f"{diagnostics.summary(error)}\n\nReply with guidance to retry.", [path])
    state["return_state"] = "ARCHIVING"
    state["pending_question"] = handoffs.render_decision(state["pending_decision"]) if state.get("pending_decision") else "How should I repair the failed archival step?"
    state["state"] = "WAIT_REPLY"


def _unrelated_archive_changes(state: dict, status: str) -> list[str]:
    """Paths in a `git status --porcelain` listing outside the archive's outputs.

    `git()` strips its output, which removes the leading status column of the first
    line (" D path" -> "D path"), so parse each line by the first whitespace after the
    XY status field instead of a fixed offset.
    """
    allowed = (
        "openspec/specs",
        f"openspec/changes/{state['slug']}",
        state["archive_path"],
    )
    unrelated = []
    for line in status.splitlines():
        if not line.strip():
            continue
        match = re.match(r"^ ?[A-Z?!]{1,2} (.*)$", line)
        rest = match.group(1) if match else line
        for path in rest.split(" -> "):
            path = path.strip().strip('"')
            if not any(path == root or path.startswith(root + "/") for root in allowed):
                unrelated.append(path)
    return unrelated


def _archive_commit_paths(state: dict, status: str) -> tuple[str, ...]:
    """Keep deleted tracked paths, omit paths already removed from Git on restart."""
    roots = ("openspec/specs", f"openspec/changes/{state['slug']}", state["archive_path"])
    changed = []
    for line in status.splitlines():
        match = re.match(r"^ ?[A-Z?!]{1,2} (.*)$", line)
        rest = match.group(1) if match else line
        changed.extend(path.strip().strip('"') for path in rest.split(" -> "))
    return tuple(root for root in roots if (config.REPO_PATH / root).exists() or
                 any(path == root or path.startswith(root + "/") for path in changed))


def _validate_archived_change(target: Path) -> None:
    """Strict-validate only the change we just archived.

    `openspec validate --archived` has no per-change form and checks every archived
    change in the repo, so a stale archive with an unticked task would block every
    future task forever. Parse the JSON report and fail only on our own change.
    """
    command = ["openspec", "validate", "--archived", "--strict", "--no-interactive", "--json"]
    with tempfile.TemporaryDirectory(prefix="codebot-archive-validation-") as scratch:
        root = Path(scratch)
        destination = root / "openspec/changes/archive" / target.name
        destination.parent.mkdir(parents=True)
        shutil.copytree(target, destination)
        proc = operations.tool(command, cwd=root)
    try:
        items = json.loads(proc.stdout)["items"]
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise TypeError("items must be a list of objects")
    except (ValueError, KeyError, TypeError) as error:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(
            f"{' '.join(command)} exited {proc.returncode} with an unreadable report: {detail}"
        ) from error
    mine = [item for item in items if item.get("id") == target.name]
    if not mine:
        raise RuntimeError(f"archived change {target.name} is missing from the validation report")
    if not all(item.get("valid") is True for item in mine):
        issues = "; ".join(
            f"{issue.get('path', '?')}: {issue.get('message', '?')}"
            for item in mine for issue in item.get("issues", []) if isinstance(issue, dict)
        ) or "no details reported"
        raise RuntimeError(f"archived change {target.name} failed strict validation: {issues}")
    stale = sorted(item.get("id", "?") for item in items
                   if item.get("id") != target.name and item.get("valid") is not True)
    if stale:
        log.warning("ignoring pre-existing invalid archived changes: %s", ", ".join(stale))


def do_archive(state: dict) -> None:
    try:
        target = _archive_target(state)
        active = config.REPO_PATH / "openspec" / "changes" / state["slug"]
        if active.exists() and target.exists():
            raise RuntimeError("active change and expected archive both exist")
        if active.exists():
            if git("status", "--porcelain", "--untracked-files=no"):
                # Only verified commits are archived and pushed. Keep the stray edits
                # recoverable and re-verify the committed tree.
                kept = _preserve_uncommitted(f"{state['slug']} before archive")
                trail(state, "Uncommitted changes set aside before archiving", kept or "")
                state["state"] = "VERIFYING"
                save_state(state)
                return
            if git("status", "--porcelain", "--untracked-files=all", "--", "openspec/"):
                raise RuntimeError("OpenSpec tree must be clean before archival")
            _run_checked(["openspec", "archive", state["slug"], "-y", "--json"])
        elif not target.is_dir():
            raise RuntimeError("active change and expected archive are both absent")
        if not target.is_dir():
            raise RuntimeError("archive command did not create the expected archive")

        _run_checked(["openspec", "validate", "--specs", "--strict", "--no-interactive"])
        _validate_archived_change(target)
        # After validation, so an unrecognized extra file can never fail strict
        # validation of the change itself; before the commit below, whose path list
        # already covers everything under the archive dir.
        _archive_transcript(state, target)
        status = git("status", "--porcelain", "--untracked-files=all")
        unrelated = _unrelated_archive_changes(state, status) if status else []
        if unrelated:
            raise RuntimeError("archival produced unrelated tracked changes: "
                               + ", ".join(unrelated[:10]))
        if status:
            paths = _archive_commit_paths(state, status)
            git("add", "-A", "--", *paths)
            git("commit", "-m", "chore: archive OpenSpec change", "--", *paths)
        state.pop("archive_round", None)
        state.pop("archive_fix_turn", None)
        state.pop("archive_error", None)
        state["state"] = "OPEN_PR"
        trail(state, "OpenSpec change archived; opening the PR")
    except Exception as error:
        _archive_failed(state, error)


def _https_url(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"https://\S+", value.strip()):
        raise RuntimeError(f"expected one HTTPS URL, got: {value!r}")
    return value.strip()


def _resume_archiving(state: dict, reason: str) -> None:
    log.warning("OPEN_PR archival precondition failed: %s", reason)
    state.pop("pr_url", None)
    state.pop("pr_summary", None)
    state["archive_error"] = reason
    # The archive budget is per task: resetting it here let OPEN_PR and ARCHIVING bounce forever.
    state["state"] = "ARCHIVING"


# GitHub rejects pull-request titles longer than this (GraphQL "Title is too long").
PR_TITLE_HARD_MAX = 256
# What we aim for: a one-line summary, not the backlog bullet.
PR_TITLE_TARGET = 72
_CODEBOT_TAG = re.compile(r"\bcode\s*bot\s*\[\s*\d+\s*\]\s*:?", re.IGNORECASE)


def _clamp_pr_title(title, limit: int = PR_TITLE_HARD_MAX) -> str:
    """One line, tags stripped, never longer than `limit` (cut on a word, with an ellipsis)."""
    text = " ".join(str(title or "").split())
    text = _CODEBOT_TAG.sub("", text).strip(" -:—–")
    if len(text) <= limit:
        return text
    cut = text[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:.-—–(")
    return (cut or text[:limit - 1]) + "…"


def _fallback_pr_title(item: str) -> str:
    """Deterministic summary of a backlog bullet: its first clause, capped at the target.
    Used when the coding agent cannot produce a title (or produces an unusable one)."""
    text = _clamp_pr_title(item)
    for sep in (";", ":", ". ", ", que ", ", which ", " — ", " - "):
        head = text.split(sep, 1)[0].strip()
        if 12 <= len(head) < len(text):
            text = head
            break
    return _clamp_pr_title(text, PR_TITLE_TARGET) or "Codebot change"


def _pr_title(state: dict) -> str:
    """The title used for `gh pr create`: persisted in state["pr_title"] so retries reuse
    it, and regenerated when the user gives guidance (state["pr_title_guidance"]) from a
    WAIT_STUCK reply. Always within GitHub's limit, whatever the agent returns."""
    cached = state.get("pr_title")
    if isinstance(cached, str) and cached.strip():
        return _clamp_pr_title(cached)
    item = state["item"]
    guidance = state.get("pr_title_guidance") or ""
    short = _clamp_pr_title(item)
    if not guidance and len(short) <= PR_TITLE_TARGET:
        title = short
    else:
        title = ""
        try:
            prompt = prompts.render(
                prompts.PR_TITLE, item=item,
                guidance=prompts.render(prompts.PR_TITLE_GUIDANCE, guidance=guidance)
                if guidance else "")
            reply = parse_json_reply(agent_runner.run(prompt, contract=False).output)
            title = _clamp_pr_title(reply.get("title"))
        except Exception:
            log.exception("PR title generation failed; using the fallback title")
        if not title or title == short:
            title = _fallback_pr_title(item)
    title = _clamp_pr_title(title)
    if not title:
        title = _fallback_pr_title(item)
    state["pr_title"] = title
    log.info("PR title: %r", title)
    return title


def _deliver(state: dict, body: str, *, publication: bool = False) -> bool:
    """Push the committed head to the task's PR on the shared seam (lifecycle.deliver): a
    stale URL never targets another branch, a fork or a different base, and nothing is
    forced. Only publication (OPEN_PR) marks a draft ready, and, as before, refuses
    uncommitted tracked changes when the PR already exists. A PR merged on GitHub finishes
    the task; one closed without merging is dropped and the user asked (retry opens a
    fresh one). Returns whether the head was delivered."""
    target, records = _repo_target(state)
    record = records["."]
    value = lifecycle.deliver(target.repos[0], record, lambda: _pr_title(state), lambda: body,
                              lambda: _save_record(state), ready=publication, ready_body=publication,
                              clean=publication and bool(lifecycle.pr_number(record)))
    persistence.mirror_record(state)
    if value["state"] == "MERGED":
        trail(state, f"PR merged on GitHub: {state.get('pr_url')}")
        _finish_task(state, "The PR was merged on GitHub (not by me).", reset_repo=False, merged=True)
        return False
    if value["state"] != "OPEN":
        lifecycle.closed(state, ".", record, resume=state["state"], save=lambda: _save_record(state),
                         ask=_enter_stuck)
        return False
    log.info("PR publication confirmed: %s head=%s", state["pr_url"], state["delivered_sha"])
    return True


def do_open_pr(state: dict) -> None:
    if _scope_drift(state):
        return
    if _conversation_delivery_blocked(state):
        return
    if not state.get("archive_path"):
        state["verify_round"] = 0
        state.pop("review_gate_round", None)
        state["state"] = "VERIFYING"
        return
    slug = _validated_slug(state.get("slug"))
    target = _validated_archive_path(state["archive_path"], slug)
    reference = str(target.relative_to(config.REPO_PATH.resolve()))
    active = config.REPO_PATH / "openspec" / "changes" / slug
    if not target.is_dir():
        _resume_archiving(state, "validated archive directory is missing")
        return
    if active.exists():
        _resume_archiving(state, "active OpenSpec change still exists")
        return
    if git("status", "--porcelain", "--untracked-files=all", "--", "openspec/"):
        _resume_archiving(state, "OpenSpec tree is dirty before PR creation")
        return
    if not git("ls-tree", "-r", "--name-only", "HEAD", "--", reference).strip():
        _resume_archiving(state, "archive is not committed in HEAD")
        return
    body = f"Implements: {state['item']}\n\nOpenSpec archive: `{reference}`"
    if concerns := _review_concerns(state):
        body += "\n\n" + concerns
    if config.TASK_SOURCE == "jira" and state.get("item_url"):
        key = state.get("item_key") or state["item_url"].rstrip("/").rsplit("/", 1)[-1]
        body += f"\n\nCloses {key}\nJira: {state['item_url']}"
    elif state.get("item_url"):
        # A GitHub issue backs the item: let the merge close it.
        body += f"\n\nCloses {state['item_url']}"
    if not _deliver(state, body, publication=True):
        return
    state["pr_summary"] = body
    if state.get("evidence_url"):
        _sync_pr_video(state, state["evidence_url"])
    _publish_progress(state)
    log.info("PR opened: %s", state["pr_url"])
    trail(state, f"PR opened: {state['pr_url']}", body)
    try:
        # Best-effort write-back (GitHub: move the item to review, link the PR on the
        # issue). The PR exists already; a backlog hiccup must not stall the flow.
        task_source.note_pr(state["item"], state.get("item_id"), state["pr_url"])
    except Exception:  # noqa: BLE001
        log.exception("could not link the PR on the backlog item")
    if not state.get("has_code_review"):
        log.info("no Code Review workflow detected for this task; finalizing without a review wait")
        finalize_pr(state)
        return
    # Don't notify the user yet: let the OpenCodeReview action run and address its
    # comments first (do_open_pr -> WAIT_REVIEW -> [ADDRESS_REVIEW -> WAIT_REVIEW]* -> WAIT_MERGE).
    state["review_round"] = 0
    state["review_run_link"] = None            # link of the last Code Review run we processed
    state["review_comment_watermark"] = ""     # only comments created after this are "new"
    _enter_review_wait(state)


def _pr_ref(pr_url) -> tuple[lifecycle.RepoOps, str]:
    """(RepoOps, number) of a PR URL (lifecycle.parse_pr_url); AgentFault when it names no PR."""
    parsed = lifecycle.parse_pr_url(pr_url)
    if not parsed:
        raise errors.AgentFault(f"not a pull-request URL: {str(pr_url)[:200]!r}", code="pr_url")
    return _pr_ops(pr_url), str(parsed[2])


def code_review_check(pr_url: str) -> dict | None:
    """The 'Code Review' workflow check for the PR's head commit, or None if not present yet.

    Returns the gh-pr-checks record (has 'bucket': pass|fail|pending|skipping|cancel,
    and 'link' identifying the specific run). gh exits non-zero while checks are pending
    or failing but still prints JSON, so we parse stdout rather than trust the exit code.
    A genuine gh failure (auth, network) also returns None but is logged at warning, so it
    surfaces promptly instead of masquerading as "not started" and stalling the poll loop
    until the review timeout.
    """
    try:
        ops, number = _pr_ref(pr_url)
    except errors.CoderbotError as error:
        log.warning("gh pr checks: %s", error)
        return None
    proc = ops.gh_quiet("pr", "checks", number, "--json", "name,bucket,workflow,link")
    if proc is None:
        return None
    try:
        checks = json.loads(proc.stdout) if proc.stdout.strip() else []
    except json.JSONDecodeError:
        checks = None
    for c in checks or []:
        if c.get("workflow") == "Code Review" or c.get("name") == "code-review":
            return c
    if checks:
        # Real check data, but the Code Review run just hasn't registered yet.
        log.debug("gh pr checks: Code Review not among %d reported check(s) yet", len(checks))
    else:
        # No usable data. Empty output with a pending / "no checks reported" signal is the
        # normal pre-run state; unparseable output or an unexpected exit is a real tooling
        # failure that must not hide until REVIEW_WAIT_TIMEOUT_SECONDS elapses.
        stderr = proc.stderr.strip()
        benign = checks == [] and (proc.returncode in (0, 8) or "no checks reported" in stderr.lower())
        if benign:
            log.debug("gh pr checks: no checks reported yet (rc=%d)", proc.returncode)
        else:
            log.warning("gh pr checks failed (rc=%d): %s", proc.returncode, stderr[-300:] or "(no stderr)")
    return None


def _pr_ops(pr_url: str | None = None) -> lifecycle.RepoOps:
    """The repository the PR (repo mode: the checkout) lives in; every gh/PR call goes through it."""
    return lifecycle.RepoOps.checkout(config.REPO_PATH, config.BASE_BRANCH, pr_url)


def _pr_body(pr_url: str) -> str:
    ops, number = _pr_ref(pr_url)
    return json.loads(ops.gh("pr", "view", number, "--json", "body"))["body"]


def _bot_comments(endpoint: str) -> list[dict]:
    """github-actions[bot] comments from a PR comments endpoint ([] on any failure)."""
    proc = _pr_ops().gh_quiet("api", "--paginate", f"{endpoint}?per_page=100")
    if proc is None or proc.returncode != 0:
        log.warning("gh api %s failed: %s", endpoint, proc.stderr[-300:] if proc else "")
        return []
    # --paginate prints one JSON array per page, back to back.
    items, decoder, text, at = [], json.JSONDecoder(), proc.stdout.strip(), 0
    try:
        while at < len(text):
            page, at = decoder.raw_decode(text, at)
            items += page if isinstance(page, list) else []
            while at < len(text) and text[at].isspace():
                at += 1
    except json.JSONDecodeError:
        return []
    return [c for c in items if isinstance(c, dict) and c.get("user", {}).get("login") == "github-actions[bot]"]


def review_summary(state: dict) -> str:
    """The most recent OCR sticky-summary issue comment body, for extra context ('' if none)."""
    parsed = lifecycle.parse_pr_url(state.get("pr_url"))
    summaries = _bot_comments(f"repos/{parsed[1]}/issues/{parsed[2]}/comments") if parsed else []
    if not summaries:
        return ""
    return max(summaries, key=lambda c: c.get("updated_at", "")).get("body", "")


def _enter_review_wait(state: dict, expect_new_run: bool = True) -> None:
    state["review_since"] = time.time()  # per-run wait clock (survives restarts via state.json)
    if expect_new_run and state.get("review_head"):
        # Nothing new since the last processed review (e.g. an evidence lap that pushed no
        # code): no run is coming, so waiting for one only idled until the timeout.
        try:
            expect_new_run = git("rev-parse", "HEAD") != state["review_head"]
        except Exception:  # noqa: BLE001 - keep waiting for a run, as before
            pass
    # True when a push should trigger a fresh Code Review run: the poll must then wait
    # for a NEW run link and never conclude from the already-processed run's state.
    state["await_new_run"] = expect_new_run
    state["state"] = "WAIT_REVIEW"


def _render_review_threads(state: dict, key: str = "review_threads") -> str:
    rendered = format_unresolved_threads(state.get(key, []), ids=True,
                                         bot_comment_ids=state.get("bot_comment_ids", []))
    summary = review_summary(state)
    if summary:
        rendered = "Reviewer summary (context):\n" + summary + "\n\n" + rendered
    return rendered


def _begin_evidence_repair(state: dict, *, note: str = "") -> None:
    state.setdefault("push_context", {"continuation": "evidence", "note": note})
    state["evidence_recovery_round"] = state.get("evidence_recovery_round", 0) + 1
    previous = state.setdefault("check_repair", {})
    previous["evidence"] = True
    previous["attempt"] = max(previous.get("attempt", 0), state["evidence_recovery_round"] - 1)
    report = {"checks": [{"check": "e2e", "status": "fail",
        "gate": {"status": "fail", "reason": "Complete playable evidence is required before PR delivery"},
        "recording_diagnostics": evidence_delivery.recovery_details(config.REPO_PATH)}]}
    _begin_check_repair(state, report, resume="PUSHING")


def finalize_pr(state: dict, note: str = "") -> None:
    """Record evidence and email the (now review-clean) PR, then wait for the user."""
    database = phase_checkpoint.store()
    task_id = database.task_identity(state, config.REPO_PATH)
    identity = state["pr_url"] + ":" + content_snapshot(config.REPO_PATH)
    try:
        manifests = config.DATA_DIR / "outbox/evidence"
        adopted = None
        if manifests.exists() and any(manifests.rglob("verified-delivery.json")):
            existing_body = _pr_body(state["pr_url"])
            adopted = evidence_delivery.reconcile(state, config.REPO_PATH, existing_body)
    except Exception:
        log.exception("existing evidence could not be reconciled; not duplicating capture/publication")
        email(state, "evidence reconciliation pending",
              "Existing video delivery could not be verified just now. No new recording or upload was started; retry reconciliation.")
        return
    recorded = [] if adopted else evidence_delivery.review_files(database, state, config.REPO_PATH, identity=identity)
    evidence_files = [Path(p) for p in recorded]
    if state.get("has_e2e_harness") and not adopted and not evidence_files:
        _begin_evidence_repair(state, note=note)
        return
    log.info("recorded %d evidence file(s) to attach", len(evidence_files))
    def upload():
        files, url = _offload_evidence_video(state, evidence_files)
        return {"files": [str(p) for p in files], "url": url}
    upload_key = identity + ":" + digest([config.DRIVE_FOLDER_ID, config.DRIVE_SHARE_MODE, config.DRIVE_REVIEWERS])
    uploaded = {"files": [], "url": adopted["url"]} if adopted else delivery_checkpoint.step(database, task_id, upload_key, "UPLOAD", upload,
        validate=lambda result: bool(result.get("url")))
    evidence_files, video_url = [Path(p) for p in uploaded["files"]], uploaded["url"]
    if video_url:
        # UPLOAD may be replayed after restart, bypassing its state mutation.
        state["evidence_url"] = video_url.splitlines()[0]
        save_state(state)
        _publish_progress(state)
        if not adopted:
            evidence_delivery.sync(database, state, config.REPO_PATH, video_url, _sync_pr_video)
    # Match Gmail's attachment cap before indexing evidence as available. The sender
    # can still explain an omitted file, but the cover must not claim it was attached.
    deliverable, omitted, size = [], [], 0
    for path in evidence_files:
        if not path.is_file() or (config.COMM_CHANNEL == "email" and
                                  size + path.stat().st_size > config.MAX_ATTACHMENT_BYTES):
            omitted.append(path)
            continue
        deliverable.append(path)
        size += path.stat().st_size
    evidence_files = deliverable
    if state.get("has_e2e_harness") and not video_url and not any(
            p.suffix.lower() in (".mp4", ".html") for p in evidence_files):
        _begin_evidence_repair(state, note=note)
        return
    detailed = diagnostics.report(database, state, config.REPO_PATH, config.DATA_DIR,
        "VERIFICATION", detail=verification_ledger.report_details(state))
    state["verification_diagnostic"] = str(detailed)
    evidence_files.append(detailed)
    body = (f"Review cover: {state.get('pr_title') or state['item'][:140]}\n"
            f"Task: {state['item']}\nPR: {state['pr_url']}\n"
            + (f"Source: {state['item_url']}\n" if state.get("item_url") else "")
            + "What changed: Current implementation and verification are documented in the PR.\n"
            + f"{handoffs.concise_verification(state)}\n"
            + (f"Video: {video_url}\n" if video_url else "")
            + "Files: " + ", ".join(path.name for path in evidence_files) + "\n")
    if omitted:
        body += "Evidence unavailable (missing or over attachment limit): " + ", ".join(
            path.name for path in omitted) + "\n\n"
    if note:
        body += note + "\n\n"
    if concerns := _review_concerns(state):
        body += concerns + "\n\n"
    if not video_url and any(path.suffix in (".mp4", ".webm", ".html") for path in evidence_files):
        attachment_desc = ("a Newman run report (html)" if state.get("e2e_kind") == "newman"
                            else "a Playwright video (mp4)" if any(p.suffix.lower() == ".mp4" for p in evidence_files)
                            else "a Playwright source video (webm); MP4 conversion pending")
        body += f"Evidence: {attachment_desc} queued for delivery.\n"
        if state.get("evidence_delivery", {}).get("status") != "complete":
            outcome = state.get("evidence_delivery", {})
            if outcome:
                body += f"Drive delivery pending at {outcome.get('stage')}: {outcome.get('error', outcome.get('status'))}.\n"
    elif not video_url and state.get("has_e2e_harness"):
        body += ("Note: no approved e2e evidence is available for delivery. Capture, validation "
                 "or conversion may be pending; see evidence diagnostics and codebot logs.\n")
    # Surface unresolved threads early — inline comments can land after the review run
    # finishes, and the merge gate re-checks before merging.
    threads = unresolved_review_threads(state["pr_url"])
    if threads is None:
        body += ("Note: I could not check the PR's review threads just now; "
                 "I will re-check before any merge.\n\n")
    elif threads:
        counts = {kind: sum(review_intent.provenance(t) == kind for t in threads)
                  for kind in ("human", "automated", "unknown")}
        body += "Reviewer provenance: " + ", ".join(f"{count} {kind}" for kind, count in counts.items() if count) + ".\n"
        human = [t for t in threads if not lifecycle.is_bot_thread(t)]
        ocr = [t for t in threads if lifecycle.is_bot_thread(t)]
        if human:
            body += (f"Heads up: the PR has {len(human)} unresolved review comment(s); "
                     "I'll address them while waiting, and I will "
                     "not merge until they are resolved (or you say 'merge anyway'):\n\n"
                     "Details are on the PR.\n\n")
        if ocr:
            body += (f"Note: {len(ocr)} automated (OCR) review comment(s) are still "
                     "unresolved; I'll address them automatically — fixing or resolving "
                     "with an explanation — while waiting for your reply.\n\n")
    # A conflict resolution rewrote the PR after the user was invited to reply to it:
    # replies written against the OLD content (worst case a pending 'merge') must not
    # be executed against the new one. Set them aside and say so.
    if state.pop("stale_replies", None) and state.get("thread_id"):
        try:
            drained = 0
            for _ in range(100):
                message = gmail_client.poll_reply(state["thread_id"])
                if not message:
                    break
                message_id, text = message
                if re.fullmatch(r"(?i)\s*merge(?: anyway)?[.! ]*", text):
                    drained += 1
                else:
                    feedback.receive(database, state, config.REPO_PATH, message_id, text)
                gmail_client.mark_processed(message_id)
        except Exception:  # noqa: BLE001 — a gmail hiccup must not block the finalize
            log.exception("could not check the thread for stale replies")
            drained = 0
        if drained:
            body += (f"Note: the PR changed while I resolved merge conflicts with "
                     f"{config.BASE_BRANCH}, so {drained} earlier repl(y/ies) on this thread "
                     "were set aside — please re-send your instruction against the updated "
                     "PR.\n\n")
    body += "Reply with change requests, or tell me to merge."
    notification_identity = identity + ":" + digest([video_url, [str(p) for p in evidence_files]])
    evidence_delivery.notification(database, state, config.REPO_PATH, notification_identity,
        lambda: email(state, "PR ready for review", body, evidence_files, milestone="pr_review"))
    # pr_summary is kept (until the task ends) so a later re-finalize — e.g. after a
    # conflict resolution — doesn't email an empty summary.
    for key in ("review_since", "review_round", "review_run_link", "review_threads",
                "await_new_run"):
        state.pop(key, None)
    state["state"] = "WAIT_MERGE"
    state["reviewed_pr_snapshot"] = content_snapshot(config.REPO_PATH)
    state.pop("evidence_recovery_round", None)
    for row in database.list("feedback", task_id, status="verifying"):
        if state.get("last_delivery", {}).get("complete") is not False:
            database.update("feedback", row, status="complete", outcome="verified delivery")


def handle_review_wait(state: dict) -> None:
    """Poll the Code Review action; when a new run finishes, work the PR's UNRESOLVED
    OCR threads (fix or resolve-with-comment) until none remain, then finalize.

    Unresolved threads — not "comments newer than a watermark" — are the work queue:
    a thread the worker skipped, or one posted after a run was processed, stays in the
    queue instead of silently accumulating until merge time."""
    if time.time() - state.get("review_since", time.time()) > config.REVIEW_WAIT_TIMEOUT_SECONDS:
        finalize_pr(state, note="Automated review wait exhausted its deadline; review status is not established.")
        return
    check = code_review_check(state["pr_url"])
    bucket = check.get("bucket") if check else None
    same_run = check is not None and check.get("link") == state.get("review_run_link")
    if check is None or bucket == "pending" or same_run:
        if same_run and bucket and bucket != "pending" and not state.get("await_new_run"):
            # No new run is coming (a resolution-only round pushes nothing) and the
            # processed run is finished. If every OCR thread is resolved, the PR is
            # clean — finalize now instead of waiting out the review timeout. When a
            # push IS expected (await_new_run), never conclude from the old run: the
            # fixes still need their re-review.
            threads = unresolved_review_threads(state["pr_url"])
            if threads is not None and not [t for t in threads if lifecycle.is_bot_thread(t)]:
                log.info("no unresolved OCR threads on the processed run; PR is clean")
                finalize_pr(state)
                return
        # Not started, still running, or the previous run's result — keep waiting, but
        # don't block the PR forever if the action is stuck or never triggered.
        if time.time() - state.get("review_since", time.time()) > config.REVIEW_WAIT_TIMEOUT_SECONDS:
            log.warning("Code Review did not complete within %ss; notifying user without it",
                        config.REVIEW_WAIT_TIMEOUT_SECONDS)
            finalize_pr(state, note="Note: the automated code review did not finish in time, "
                                    "so I'm sending this without waiting for it.")
        else:
            log.debug("waiting for Code Review (bucket=%s, same_run=%s)", bucket, same_run)
        return
    # A new Code Review run finished (pass/fail/skip/cancel). Read what is unresolved.
    threads = unresolved_review_threads(state["pr_url"])
    if threads is None:
        # Transient query failure: don't record the run as processed, so the next tick
        # re-reads it (the review timeout still bounds the wait).
        log.warning("could not fetch review threads; retrying next tick")
        return
    state["review_run_link"] = check.get("link")
    try:
        state["review_head"] = git("rev-parse", "HEAD")
    except Exception:  # noqa: BLE001
        state.pop("review_head", None)
    ocr = _resolve_repeated_findings(state, [t for t in threads if lifecycle.is_bot_thread(t)])
    if not ocr:
        log.info("no unresolved OCR threads; PR is clean")
        finalize_pr(state)
        return
    if state.get("review_round", 0) >= config.REVIEW_MAX_ROUNDS:
        log.warning("review round limit (%d) reached with %d open thread(s); finalizing",
                    config.REVIEW_MAX_ROUNDS, len(ocr))
        finalize_pr(state, note=(
            f"Note: the automated reviewer still has {len(ocr)} unresolved thread(s) after "
            f"{config.REVIEW_MAX_ROUNDS} rounds of fixes. I've left them on the PR for you."))
        return
    state["review_threads"] = ocr
    state["state"] = "ADDRESS_REVIEW"
    log.info("%d unresolved OCR thread(s); addressing them", len(ocr))


def do_address_review(state: dict) -> None:
    threads = state.get("review_threads", [])
    round = state.get("review_round", 0) + 1
    log.info("addressing %d review thread(s), round %d", len(threads), round)
    trail(state, f"Addressing {len(threads)} automated review thread(s), round "
                 f"{round}")
    head_before = git("rev-parse", state["branch"])
    result = agent_runner.resume(
        state["session_id"],
        prompts.render(prompts.ADDRESS_REVIEW, threads=_render_review_threads(state),
                       branch=state["branch"]) + review_intent.RULES)
    if handle_result(state, result, "ADDRESS_REVIEW"):
        return
    state["review_round"] = round
    _finish_address_review(state, result, head_before)


def _finish_address_review(state: dict, result, head_before: str | None) -> None:
    """Success tail of ADDRESS_REVIEW: new commits are pushed (via PUSHING) and re-reviewed;
    a resolution-only round resolves the threads right away and re-checks the PR."""
    _scrub_evidence_from_repo(state, "ADDRESS_REVIEW")
    committed = head_before is None or git("rev-parse", state["branch"]) != head_before
    if committed:
        # The RESOLVE lines are acted on after the push lands (see do_push), so the
        # thread replies never describe a fix that isn't on GitHub yet.
        _queue_push(state, "review", result.output)
        return
    # Resolution-only round: no new review run is coming. Re-check and either finish
    # or hand the leftovers straight back (the round cap bounds this loop).
    _resolve_review_threads(state, result.output)
    state.pop("review_threads", None)
    remaining = unresolved_review_threads(state["pr_url"])
    if remaining is None:
        # Could not verify — never conclude "clean" from a failed query. The review
        # wait's clean-check retries the query each tick (bounded by its timeout).
        _enter_review_wait(state, expect_new_run=False)
        return
    ocr = [t for t in remaining if lifecycle.is_bot_thread(t)]
    # A resolution-only lap is a review round too; uncounted, it re-entered itself forever.
    state["review_round"] = state.get("review_round", 0) + 1
    if ocr and state["review_round"] < config.REVIEW_MAX_ROUNDS:
        log.info("%d OCR thread(s) still unresolved after the round; re-addressing", len(ocr))
        state["review_threads"] = ocr
        state["state"] = "ADDRESS_REVIEW"
        return
    finalize_pr(state)


# ---------------------------------------------------------------- base-branch conflicts

def _enter_conflict_resolution(state: dict) -> bool:
    """Route the task into RESOLVE_CONFLICTS, bounded by CONFLICT_MAX_ROUNDS: the base
    branch can keep moving while other PRs merge, so a resolution that never converges
    must escalate to the user instead of looping forever. Returns True when the
    resolution state was actually entered (False = escalated to WAIT_STUCK instead)."""
    rounds = state.get("conflict_rounds", 0) + 1
    state["conflict_rounds"] = rounds
    if rounds > config.CONFLICT_MAX_ROUNDS:
        _enter_stuck(state, state["state"],
                     f"The PR still conflicts with {config.BASE_BRANCH} after "
                     f"{config.CONFLICT_MAX_ROUNDS} resolution attempt(s).\nPR: "
                     f"{state.get('pr_url', '?')}\n\n"
                     f"Please resolve the conflicts on the branch yourself (merge "
                     f"{config.BASE_BRANCH} in and push), then reply 'retry'.")
        return False
    log.warning("PR %s conflicts with %s (attempt %d/%d); entering RESOLVE_CONFLICTS",
                state.get("pr_url"), config.BASE_BRANCH, rounds, config.CONFLICT_MAX_ROUNDS)
    # Where a no-op resolution returns to. From WAIT_MERGE the user was already invited
    # to reply on the current PR content; a reply written before the resolution rewrote
    # the PR must not be acted on later — finalize_pr sets those aside (stale_replies).
    state["conflict_return"] = state["state"]
    if state["state"] == "WAIT_MERGE":
        state["stale_replies"] = True
    state["state"] = "RESOLVE_CONFLICTS"
    return True


def check_pr_status(state: dict) -> bool:
    """The PR's standing, read once on the shared seam (lifecycle.standing). Handles the
    PR having been dealt with outside codebot — MERGED by someone on GitHub finishes the
    task exactly as codebot's own merge would; CLOSED without merging is dropped and the
    user asked ('retry' opens a fresh PR, or adopts the PR if they reopened it) — and a
    CONFLICTING PR starts a bounded resolution. Repo-mode policy: a head moved by
    someone else is taken in only at merge time (_pr_head_moved), not followed like a
    workspace PR. Returns True when the state changed; a failed read changes nothing."""
    target, records = _repo_target(state)
    try:
        verdict, value = lifecycle.standing(target.repos[0], records["."])
    except (errors.CoderbotError, ValueError) as error:
        log.warning("could not read the PR: %s", str(error)[-300:])
        return False
    persistence.mirror_record(state)
    if verdict == "MERGED":
        log.info("PR %s was merged on GitHub outside codebot; finishing the task", state["pr_url"])
        trail(state, f"PR merged on GitHub: {state['pr_url']}")
        _finish_task(state, "The PR was merged on GitHub (not by me).", reset_repo=False, merged=True)
        return True
    if verdict == "CLOSED":
        log.warning("PR %s was closed without merging; asking the user", state["pr_url"])
        lifecycle.closed(state, ".", records["."], resume="OPEN_PR", save=lambda: _save_record(state),
                         ask=_enter_stuck)
        return True
    if value.get("mergeable") == "CONFLICTING":  # the conflict budget is per task, not per burst
        _enter_conflict_resolution(state)
        return True
    return False


def poll_pr(state: dict) -> None:
    """Repo mode's PR waits (WAIT_REVIEW / WAIT_MERGE): the inbox every WAIT_MERGE tick,
    the PR at most once per poll interval, as in workspace mode — its standing first,
    then the Code Review wait or the PR's review threads."""
    if state["state"] == "WAIT_MERGE":
        handle_wait(state)
        if state["state"] != "WAIT_MERGE":
            return
    if not state.get("pr_url") or not lifecycle.due(persistence.repository_record(state)) or check_pr_status(state):
        return
    (handle_review_wait if state["state"] == "WAIT_REVIEW" else handle_merge_wait)(state)


def do_resolve_conflicts(state: dict) -> None:
    """Merge the base branch into the task branch in the working session. A conflict
    with genuinely different reasonable resolutions comes back as a NEED_USER_INPUT
    question and is emailed like any other; a clean resolution is pushed and goes
    through the automated review again before it can merge."""
    # Persisted (not a local) so the WAIT_REPLY question detour can also tell whether
    # the answer-informed session ended up committing.
    state.setdefault("conflict_head", git("rev-parse", state["branch"]))
    conflicts = _start_base_merge()
    if not conflicts:
        _leave_conflict_resolution(state)  # merged cleanly: no agent turn needed
        return
    result = agent_runner.resume(
        state["session_id"],
        prompts.render(prompts.RESOLVE_CONFLICTS, branch=state["branch"],
                       base_branch=config.BASE_BRANCH, paths="\n".join(conflicts)))
    if handle_result(state, result, "RESOLVE_CONFLICTS"):
        return
    _leave_conflict_resolution(state)


def _start_base_merge() -> list[str]:
    """coderbot fetches and merges the base itself (the agent has no remote credentials);
    returns the conflicted paths left for the agent. A merge already in progress (a
    retried turn) is kept as it is."""
    def merging():  # git-path: .git is a file in a worktree
        return (config.REPO_PATH / git("rev-parse", "--git-path", "MERGE_HEAD")).exists()
    if not merging():
        git("fetch", "origin", config.BASE_BRANCH)
        if _git_quiet("merge", "--no-edit", "origin/" + config.BASE_BRANCH):
            return []
        if not merging():
            raise RuntimeError(f"git could not start merging origin/{config.BASE_BRANCH}; see `git status`")
    return [path for path in git("diff", "--name-only", "--diff-filter=U").splitlines() if path] or \
        ["(merge in progress; see `git status`)"]


def _leave_conflict_resolution(state: dict) -> None:
    """Exit RESOLVE_CONFLICTS based on whether the session committed. A commit is pushed
    (PUSHING) and goes through the automated review again (never merge unreviewed
    commits). No commit returns to the state the conflict preempted — NOT to WAIT_REVIEW,
    which after a finalize (review_run_link already popped) would misread the old
    finished run as new and re-finalize with a duplicate email; the per-tick conflict
    check re-detects a leftover conflict from either wait state, bounded by the cap."""
    committed = git("rev-parse", state["branch"]) != state.get("conflict_head")
    state.pop("conflict_head", None)
    if committed:
        log.info("conflict resolution produced new commits; pushing them for re-review")
        trail(state, "Merge conflicts resolved; pushing for re-review")
        state.pop("conflict_return", None)
        _queue_push(state, "conflicts")
        return
    origin = state.pop("conflict_return", "WAIT_MERGE")
    log.warning("conflict-resolution session committed nothing; returning to %s", origin)
    if origin == "WAIT_REVIEW":
        _enter_review_wait(state, expect_new_run=False)
    else:
        state.pop("stale_replies", None)  # the PR did not change; replies are not stale
        state["state"] = origin


# ---------------------------------------------------------------- pre-merge thread resolution

def unresolved_review_threads(pr_url: str) -> list[dict] | None:
    """The PR's unresolved review threads (lifecycle.review_threads); None when unknown."""
    try:
        return lifecycle.review_threads(*_pr_ref(pr_url))
    except errors.CoderbotError as error:  # could not verify: callers fail closed
        log.warning("review threads: %s", error)
        return None


def _comment_label(comment: dict, thread: dict, bot_ids: set) -> tuple[str, bool]:
    """(label, is_bot). The bot's GitHub login is often the operator's own account, so
    only replies coderbot itself posted (ids recorded in state) are "codebot (you)"; any
    other comment from that login is flagged as coming from the shared account — the
    operator giving instructions through it."""
    author = (comment.get("author") or "?").strip() or "?"
    if comment.get("comment_id") in bot_ids:
        return "codebot (you)", True
    login = (thread.get("bot_login") or "").strip()
    if login and author == login:
        # Replies posted before their ids were tracked: recognise the RESOLVE-reason shape.
        if _BOT_REPLY_SHAPE.match((comment.get("body") or "").strip()):
            return "codebot (you)", True
        return f"{author} (your account)", False
    return author, False


_BOT_REPLY_SHAPE = re.compile(
    r"^(fixed|answered|no change|not applicable|covered|already correct|resolved by codebot)\b",
    re.IGNORECASE)


def format_unresolved_threads(threads: list[dict], ids: bool = False,
                              bot_comment_ids=()) -> str:
    """`ids=False`: the compact opener-only listing for emails. `ids=True`: the worker
    view — thread id plus EVERY comment in order, the bot's own replies labelled as its
    own and the latest human comment marked as the one to respond to, so a reviewer's
    follow-up (and the fact that the bot already answered once) is never lost."""
    bot_ids = set(bot_comment_ids or ())
    lines = []
    for i, t in enumerate(threads, 1):
        loc = f"{t['path']}:{t['line']}" if t.get("line") else (t.get("path") or "(general)")
        tag = " [outdated code]" if t.get("outdated") else ""
        head = f"[{i}] {loc} ({t.get('author', '?')}){tag}"
        if not ids:
            body = (t.get("body") or "").strip()
            if len(body) > 400:
                body = body[:400] + "…"
            lines.append(f"{head}\n{body}")
            continue
        head += f"\nthread_id: {t.get('id')}"
        comments = t.get("comments") or [{"author": t.get("author", "?"), "body": t.get("body", "")}]
        labels = [_comment_label(c, t, bot_ids) for c in comments]
        latest = max((j for j, (_, is_bot) in enumerate(labels) if not is_bot), default=None)
        rendered = []
        for j, (c, (who, _)) in enumerate(zip(comments, labels)):
            if j == latest and len(comments) > 1:
                who += " (latest — respond to this)"
            rendered.append(f"{who}: {(c.get('body') or '').strip()}")
        lines.append(head + "\n" + "\n".join(rendered))
    return "\n\n".join(lines)


def _resolve_review_threads(state: dict, output: str, key: str = "review_threads") -> int:
    """Answer and resolve the threads state[key] holds from the worker's RESOLVE lines
    (lifecycle.answer_threads), with the task's receipts and thread watermark."""
    if not state.get(key):
        return 0
    try:
        ops, number = _pr_ref(state.get("pr_url"))
    except errors.CoderbotError as error:  # the next round or the merge gate catches them again
        log.warning("cannot answer review threads: %s", error)
        return 0
    return lifecycle.answer_threads(ops, number, state[key], output, state,
                                    state.setdefault("pr_threads_seen", {}), lambda: save_state(state))


def _resolve_repeated_findings(state: dict, threads: list[dict]) -> list[dict]:
    """Reply to and resolve, without an agent turn, bot threads that repeat an
    already-declined finding or sit on code that has since changed (outdated); return
    the threads that still need the agent."""
    declined = state.get("declined_findings") or {}
    reasons = {}
    for t in threads:
        if lifecycle.finding_key(t) in declined:
            reasons[t["id"]] = declined[lifecycle.finding_key(t)] + " (already evaluated in an earlier round)"
        elif t.get("outdated"):
            reasons[t["id"]] = "fixed: the code this comment refers to has changed since (outdated)."
    if not reasons:
        return threads
    saved = state.get("review_threads")
    state["review_threads"] = [t for t in threads if t["id"] in reasons]
    try:
        _resolve_review_threads(state, "\n".join(f"RESOLVE: {tid} {reason}" for tid, reason in reasons.items()))
    finally:
        if saved is None:
            state.pop("review_threads", None)
        else:
            state["review_threads"] = saved
    log.info("resolved %d repeated or outdated bot finding(s) without an agent turn", len(reasons))
    return [t for t in threads if t["id"] not in reasons]


def do_merge_reply(state: dict, reply: str) -> None:
    if re.fullmatch(r"(?i)\s*(merge(?: anyway)?|fusiona(?: de todos modos)?)[.!]?\s*", reply):
        verdict = {"action": "merge", "force": bool(re.search(r"(?i)anyway|de todos modos", reply))}
    else:
        verdict = replies.classify("pr", reply, state)
    action = verdict.get("action")
    log.info("classified PR reply as action=%r force=%r", action, verdict.get("force"))
    if action not in ("merge", "changes", "abort", "complete"):
        # Merging is irreversible: never fall through to it on unexpected output.
        email(state, "clarification needed",
              f"I could not tell whether your reply asks for changes, an abort, a "
              f"merge, or to mark the task complete:\n\n{reply}\n\nPlease reply again "
              "with the change requests, 'abort', 'complete', or an explicit 'merge'.")
        return  # stay in WAIT_MERGE
    if action == "complete":
        _finish_task(state, "You asked me to mark this task complete without merging the PR "
                            f"myself.\nPR: {state.get('pr_url', '-')}", reset_repo=True)
        return
    if action == "abort":
        pr_url = state.get("pr_url", "-")
        _abort_and_reset(state, "Got it — I'm stopping this task and resetting to a clean "
                                f"slate.\n\nPR: {pr_url}")
        return
    if action == "changes":
        feedback.receive(phase_checkpoint.store(), state, config.REPO_PATH,
            "pr-feedback-" + hashlib.sha256(reply.encode()).hexdigest(), verdict.get("feedback") or reply)
        _dispatch_feedback(state)
        return
    # merge
    if state.get("replan") and not state.get("approved_proposal"):
        email(state, "merge blocked", "The revised proposal requires approval before merge.")
        return
    if _conversation_delivery_blocked(state):
        return
    if state.get("reviewed_pr_snapshot") and content_snapshot(config.REPO_PATH) != state["reviewed_pr_snapshot"]:
        email(state, "merge blocked", "The PR content changed since your review. Review the updated PR before merging.")
        state.pop("reviewed_pr_snapshot", None)
        return
    target, records = _repo_target(state)
    try:  # the shared pre-merge read, without the workspace's CI-green and ready gates
        blocked = {verdict: pr for _, verdict, _, pr in lifecycle.precheck(target, records, ["."], gates=False)}
    except (errors.CoderbotError, ValueError) as error:
        log.warning("could not check the PR before merging: %s", error)
        email(state, "merge blocked — could not check the PR",
              f"I did not merge: I couldn't query the PR's state just now.\nPR: "
              f"{state['pr_url']}\n\nReply 'merge' to retry.")
        return  # stay in WAIT_MERGE
    persistence.mirror_record(state)
    if records["."].get("merge", {}).get("status") != "MERGED":
        if any(pr.get("mergeable") == "CONFLICTING" for pr in blocked.values()):
            # The base branch moved ahead and the branch no longer merges cleanly.
            # Resolve the conflicts in the working session; genuinely ambiguous
            # resolutions come back as an emailed question. The resolution push then
            # needs its re-review before the user's next 'merge'. The announcement is
            # sent only if resolution actually starts — past the attempt cap
            # _enter_conflict_resolution emails the (opposite) stuck instructions instead.
            log.warning("PR %s conflicts with %s; resolving before the merge",
                        state["pr_url"], config.BASE_BRANCH)
            if _enter_conflict_resolution(state):
                email(state, "merge conflict — resolving automatically",
                      f"`{config.BASE_BRANCH}` has changed and this PR now conflicts with "
                      f"it, so I can't merge it yet.\nPR: {state['pr_url']}\n\n"
                      "I'm resolving the conflicts now. If any conflict has genuinely "
                      "different reasonable resolutions I'll send you the alternatives; "
                      "otherwise the automated review re-runs on the resolved branch and "
                      "I'll message you when it's clean — reply 'merge' then.")
            return
        if "CLOSED" in blocked:
            lifecycle.closed(state, ".", records["."], resume="OPEN_PR", save=lambda: _save_record(state),
                             ask=_enter_stuck)
            return
        # Review-thread gate: inline review threads may appear at any time, including
        # after the "PR ready" email (handle_merge_wait addresses them proactively, but
        # only up to its round cap). Never squash-merge over unresolved feedback unless
        # the user explicitly forces it. Fails CLOSED: a failed thread query blocks the
        # merge, never waves it through. The strict `is True` matters — classifier JSON
        # isn't schema-validated, and a string like "false" is truthy.
        if verdict.get("force") is not True:
            threads = unresolved_review_threads(state["pr_url"])
            if threads is None:
                email(state, "merge blocked — could not verify review comments",
                      f"I did not merge: I couldn't query the PR's review threads to check "
                      f"for unresolved comments.\nPR: {state['pr_url']}\n\n"
                      "Reply 'merge' to have me retry the check, or 'merge anyway' to merge "
                      "without it.")
                return  # stay in WAIT_MERGE
            if threads:
                log.warning("merge blocked: %d unresolved review thread(s)", len(threads))
                email(state, "merge blocked — unresolved review comments",
                      f"I did not merge: the PR has {len(threads)} unresolved review "
                      f"thread(s).\nPR: {state['pr_url']}\n\n"
                       "The unresolved findings are listed on the PR.\n\n"
                      "Options: reply with change requests and I'll address them; resolve "
                      "the threads on GitHub and reply 'merge' again; or reply 'merge "
                      "anyway' to merge despite them.")
                state["pr_thread_notified"] = True  # don't re-address them every tick
                return  # stay in WAIT_MERGE
        # Merge only the head the user was shown. delivered_sha is recorded on every
        # push; legacy states without it merge exactly as before.
        expected = state.get("delivered_sha")
        if "MOVED" in blocked and _pr_head_moved(state, expected, blocked["MOVED"]["headRefOid"]):
            return  # stay in WAIT_MERGE
        log.info("merging PR %s (squash%s)", state["pr_url"],
                 f", head {expected[:12]}" if expected else "")
        report = lifecycle.merge(target, records, ["."], lambda: _save_record(state))
        if report["failed"] and expected and _pr_head_moved(state, expected, _remote_pr_head(state)):
            return  # GitHub refused because the head moved; stay in WAIT_MERGE
        if report["failed"]:
            # A late-breaking conflict or other GitHub rejection (e.g. branch behind the
            # base branch). Don't crash into the retry loop — surface it and wait for the
            # user to fix it.
            log.warning("gh pr merge failed: %s", report["error"])
            email(state, "merge failed — needs your help",
                  f"I couldn't merge the PR; GitHub reported:\n\n{report['error']}\n\n"
                  f"PR: {state['pr_url']}\n\nThis usually means {config.BASE_BRANCH} moved ahead "
                  "or there are conflicts. If it's a merge conflict I'll detect it and start "
                  "resolving automatically on my next check; otherwise please resolve it on "
                  "the branch and reply 'merge' to retry.")
            return  # stay in WAIT_MERGE
        trail(state, f"PR merged: {state['pr_url']}")
    _finish_task(state, "PR merged.", reset_repo=False, merged=True)


def _remote_pr_head(state: dict) -> str | None:
    """The PR's current head SHA, or None when it can't be read (never raises)."""
    try:
        target, records = _repo_target(state)
        return lifecycle.pinned(target.repos[0], records["."])["headRefOid"]
    except Exception as error:  # noqa: BLE001 — the merge itself still pins the head
        log.warning("could not read the PR head before merging: %s", error)
        return None


def _pr_head_moved(state: dict, expected: str, head: str | None) -> bool:
    """When the PR head differs from the one presented for review, tell the user, adopt
    the new head as the one to review, and return True (the caller must not merge)."""
    if not head or head == expected:
        return False
    log.warning("PR head moved since review: expected %s, found %s", expected[:12], head[:12])
    email(state, "merge blocked — the PR changed since review",
          f"I did not merge: the PR branch moved after I presented it "
          f"({expected[:12]} -> {head[:12]}).\nPR: {state['pr_url']}\n\n"
          "Please review the new commits and reply 'merge' again to merge them.")
    state["delivered_sha"] = head
    state.pop("reviewed_pr_snapshot", None)
    return True


def _finish_address_pr_threads(state: dict) -> None:
    """After the fixes are pushed: reply on + resolve the threads the worker declared
    handled (RESOLVE lines, kept in the push context). Threads without a RESOLVE line
    stay open and are picked up again next tick, bounded by PR_THREAD_MAX_ROUNDS."""
    output = (state.get("push_context") or {}).get("output", "")
    resolved = _resolve_review_threads(state, output, key="pr_threads")
    threads = state.pop("pr_threads", [])
    answered = sum(1 for t in threads if t.get("id") in (state.get("pr_threads_seen") or {}))
    if resolved + answered < len(threads):
        log.warning("%d/%d review thread(s) still unresolved after the round",
                    len(threads) - resolved - answered, len(threads))
    # The repaired content has passed the push gates and is now the PR presented
    # for review. Do not compare the user's next merge against the pre-repair PR.
    state["reviewed_pr_snapshot"] = content_snapshot(config.REPO_PATH)
    state["state"] = "WAIT_MERGE"


def do_address_pr_threads(state: dict) -> None:
    threads = state.get("pr_threads", [])
    round = state.get("pr_thread_round", 0) + 1
    log.info("addressing %d unresolved review thread(s), round %d", len(threads), round)
    result = agent_runner.resume(
        state["session_id"],
        prompts.render(prompts.ADDRESS_PR_THREADS,
                       threads=_render_review_threads(state, key="pr_threads"),
                       branch=state["branch"]) + review_intent.RULES)
    if handle_result(state, result, "ADDRESS_PR_THREADS"):
        return
    state["pr_thread_round"] = round
    _scrub_evidence_from_repo(state, "ADDRESS_PR_THREADS")
    _queue_push(state, "threads", result.output)


def _queue_push(state: dict, continuation: str, output: str = "",
                attachments: list[Path] | None = None, *, feedback: str = "") -> None:
    state["push_context"] = {
        "continuation": continuation,
        "output": str(output),
        "attachments": [str(path) for path in attachments or []],
        "feedback": feedback,
    }
    state["state"] = "PUSHING"
    save_state(state)


def do_push(state: dict) -> None:
    context = state.get("push_context")
    if not isinstance(context, dict):
        raise RuntimeError("PUSHING state is missing push_context")
    continuation = context.get("continuation")
    if continuation not in ("review", "feedback", "threads", "conflicts", "evidence"):
        raise RuntimeError(f"invalid push continuation: {continuation!r}")

    report = final_checks.run(state, config.REPO_PATH, phase_checkpoint.store())
    state["final_check_report"] = report
    save_state(state)
    if report["status"] != "pass":
        _begin_check_repair(state, report, resume="PUSHING")
        return
    state.pop("check_repair", None)
    if not _deliver(state, state.get("pr_summary") or state.get("item", "")):
        return
    if continuation == "review":
        # The fix is on GitHub now: reply on + resolve the threads the worker declared
        # handled, then wait for the re-review the push triggers.
        _resolve_review_threads(state, context.get("output", ""))
        state.pop("review_threads", None)
        state.pop("review_comments", None)
        _enter_review_wait(state)
    elif continuation == "conflicts":
        if state.get("has_code_review"):
            _enter_review_wait(state)
        else:
            finalize_pr(state)
    elif continuation == "evidence":
        # A failed new recording may queue another repair; retain its new context.
        state.pop("push_context", None)
        if state.get("has_code_review"):
            _enter_review_wait(state)
        else:
            finalize_pr(state, note=context.get("note", ""))
        return
    elif continuation == "feedback":
        recorded = evidence_delivery.review_files(phase_checkpoint.store(), state, config.REPO_PATH) if state.get("has_e2e_harness") else []
        if state.get("has_e2e_harness") and not recorded:
            _begin_evidence_repair(state)
            return
        attachments = [Path(path) for path in context.get("attachments", [])]
        attachments.extend(path for path in recorded if path not in attachments)
        attachments, video_url = _offload_evidence_video(state, attachments)
        if video_url:
            _publish_progress(state)
            store = phase_checkpoint.store()
            evidence_delivery.sync(store, state, config.REPO_PATH, video_url, _sync_pr_video)
        body = (f"PR updated: {state['pr_url']}\n\n"
                f"Requested: {context.get('feedback') or '(see earlier message)'}\n\n"
                f"Changed (implementation report): {context.get('output', '')}\n\n"
                + handoffs.evidence_index(attachments, video_url))
        body += "\n\n" + handoffs.concise_verification(state)
        detailed = diagnostics.report(phase_checkpoint.store(), state, config.REPO_PATH, config.DATA_DIR,
            "FEEDBACK_VERIFICATION", detail=verification_ledger.report_details(state))
        attachments.append(detailed)
        if video_url:
            body += f"\n\nVideo: {video_url}"
        store = phase_checkpoint.store()
        evidence_delivery.notification(store, state, config.REPO_PATH,
            digest([state["pr_url"], str(state.get("delivered_sha")), video_url, body]),
            lambda: email(state, "PR updated", body, attachments))
        state.pop("pending_feedback", None)
        state["state"] = "WAIT_MERGE"
        state["reviewed_pr_snapshot"] = (state.get("final_check_report") or {}).get("snapshot")
        state.pop("evidence_recovery_round", None)
    else:
        _finish_address_pr_threads(state)
    state.pop("push_context", None)


# ---------------------------------------------------------------- silence check-ins

# A task thread that stays quiet for longer than the current back-off interval gets a
# short check-in, so the user can tell a long-running step from a wedged bot. The clock
# lives in state.json (survives restarts) and restarts on any REAL email in either
# direction on the task thread: every email() call and every consumed reply. Check-ins
# themselves bypass email() so they neither restart the clock nor displace last_email,
# which STATUS and the check-ins quote as "what I'm waiting on". They are sent from the
# tick loop, so one can lag while a single long agent call runs (bounded by
# CODEBOT_AGENT_TIMEOUT); it goes out as soon as that call returns.
# Waits whose next move is the user's: the check-in spells out what is expected in full.
USER_SIDE_WAITS = {"WAIT_APPROVAL", "WAIT_MERGE", "WAIT_REPLY", "WAIT_STUCK", "WAIT_CLEAN"}


def _fmt_dur(seconds: float) -> str:
    total = max(0, int(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days} d")
    if hours:
        parts.append(f"{hours} h")
    if minutes or not parts:
        parts.append(f"{minutes} min")
    return " ".join(parts)


def _note_contact(state: dict) -> None:
    """A real email was sent or received on the task thread: restart the silence clock."""
    state["last_contact"] = time.time()
    state.pop("ping_count", None)
    state.pop("last_ping_at", None)


def _ping_due(state: dict, now: float) -> bool:
    if state.get("state") not in USER_SIDE_WAITS:
        return False
    decision = hashlib.sha256(json.dumps([state.get("state"), state.get("reviewed_proposal"),
        state.get("pending_question"), state.get("stuck_return"), state.get("pr_url")]).encode()).hexdigest()
    if state.get("reminded_decision") == decision:
        return False
    state["pending_decision_id"] = decision
    if (not config.PING_SCHEDULE_SECONDS or state.get("state") == "IDLE"
            or not state.get("thread_id")):
        return False
    anchor = state.get("last_ping_at")
    if anchor is None:
        anchor = state.get("last_contact")
    if anchor is None:
        # A task that predates the check-ins (state.json from an older build): start the
        # clock now rather than pinging on the spot.
        state["last_contact"] = now
        return False
    return now - anchor >= 86400


def _ping_body(state: dict, now: float, count: int) -> str:
    spanish = state.get("task_language", "English").casefold() == "spanish"
    if state.get("pending_reply_delivery"):
        return ("La respuesta está preparada; estoy reintentando solo la entrega de archivos. No necesito una decisión ni volveré a ejecutar al agente."
                 if spanish else "The reply is ready; I am retrying file delivery only. No decision is needed and the agent will not run again.")
    if state["state"] == "WAIT_APPROVAL":
        return ("Aprobación pendiente: revisa la propuesta ya enviada y responde con aprobación o cambios. La implementación sigue pausada."
                if spanish else "Approval pending: review the proposal already sent and reply with approval or changes. Implementation remains paused.")
    decision = state.get("pending_decision")
    if isinstance(decision, dict) and decision.get("wait_state") == state["state"]:
        return handoffs.render_decision(decision)
    question = state.get("pending_question")
    if state["state"] == "WAIT_APPROVAL":
        action = "Revisa la propuesta y responde con aprobación o cambios." if spanish else "Review the proposal and reply with approval or changes."
    elif state["state"] == "WAIT_MERGE":
        action = ("Revisa el PR y responde con cambios o 'merge'. " if spanish else
                  "Review the PR and reply with changes or 'merge'. ") + state.get("pr_url", "")
        if state.get("pr_thread_notified"):
            action += "\n" + ("Hay comentarios pendientes; indica cómo resolverlos." if spanish else
                                "Review comments remain; give guidance on their resolution.")
    elif question:
        action = str(question)[:500]
    else:
        action = "Responde con instrucciones para resolver el bloqueo o 'retry'." if spanish else "Reply with instructions to resolve the blocker or 'retry'."
    return ("Decisión pendiente: " if spanish else "Decision pending: ") + action


def _maybe_ping(state: dict) -> None:
    """Every tick: send a silence check-in when one is due. Never raises — a check-in is a
    courtesy and must not burn the current state's failure budget."""
    try:
        now = time.time()
        if not _ping_due(state, now):
            return
        count = state.get("ping_count", 0) + 1
        body = _ping_body(state, now, count)
        _send_localized(state, subject(state, "check-in"), body, state["thread_id"])
        state["ping_count"] = count
        state["last_ping_at"] = now
        state["reminded_decision"] = state["pending_decision_id"]
        log.info("sent silence check-in #%d in %s (thread quiet for %s)", count, state["state"],
                 _fmt_dur(now - state.get("last_contact", now)))
    except Exception:  # noqa: BLE001
        log.exception("could not send the silence check-in; will retry next tick")


# ---------------------------------------------------------------- loop

PHASES = {
    "REPAIR_CHECKS": do_repair_checks,
    "REPLANNING": lambda state: do_replan(state),
    "APPLY_FEEDBACK": lambda state: do_apply_feedback(state),
    "IDLE": do_pick,
    "EXPLORING": do_explore,
    "PROPOSING": do_propose,
    "IMPLEMENTING": do_implement,
    "VERIFYING": do_verify,
    "INTERNAL_REVIEW": do_internal_review,
    "E2E": do_e2e,
    "ARCHIVING": do_archive,
    "OPEN_PR": do_open_pr,
    "ADDRESS_REVIEW": do_address_review,
    "ADDRESS_PR_THREADS": do_address_pr_threads,
    "RESOLVE_CONFLICTS": do_resolve_conflicts,
    "PUSHING": do_push,
}

# WAIT_REVIEW polls the Code Review action rather than the inbox, but shares the
# post-tick sleep, so it lives in WAITS and is dispatched ahead of the inbox waits.
# WAIT_MERGE does too: it also checks the PR for unresolved review threads before
# falling through to the inbox check (see handle_merge_wait).
WAITS = {"WAIT_APPROVAL", "WAIT_MERGE", "WAIT_REPLY", "WAIT_CLEAN", "WAIT_REVIEW", "WAIT_STUCK"}


def check_clean_checkout(state: dict) -> None:
    """Slack's pre-task block retries itself; email keeps waiting for a reply."""
    if not git("status", "--porcelain", "--untracked-files=no"):
        state.pop("dirty_notified", None)
        state["state"] = "IDLE"


# ---------------------------------------------------------------- credentials
#
# The agent may ask for a credential it lacks (CREDENTIAL_REQUEST line). The user can
# send it through a one-time PrivateBin page the bot opens, continue without it, or
# pause. Off unless CODEBOT_CREDENTIAL_REQUESTS is on. Every helper here either acts or
# returns control to the existing code path; none of them can stall the loop.

def _credential_key(state: dict) -> str:
    """Stable for the whole task: recovery isolation rewrites the branch, not the item."""
    return str(state.get("item_id") or state.get("item") or "")


def _skipped_credentials(state: dict) -> list[str]:
    return [name for name, decision in (state.get("credentials") or {}).items()
            if isinstance(decision, dict) and decision.get("decision") == "skip"]


def _credential_text(state: dict, kind: str, **fields) -> str:
    return message_templates.operational(kind, state.get("task_language") or "English").format(**fields)


def _maybe_request_credential(state: dict, result, phase: str) -> bool:
    """Pause for a credential the agent asked for. False leaves handle_result unchanged."""
    try:
        request = secret_store.parse_request(getattr(result, "output", ""))
        if not request or phase not in CONTINUATIONS:
            return False
        env = request["env"]
        if env in _skipped_credentials(state) or secret_store.available(env):
            return False  # already decided or already present: the agent just continues
        rounds = state.get("question_rounds", 0) + 1
        if rounds > config.QUESTION_MAX_ROUNDS:
            return False
        text = _credential_text(state, "credential_ask", **request)
        email(state, "credential needed", text, localized=True)
    except Exception:  # noqa: BLE001 — the phase continues exactly as before
        log.warning("could not request a credential; continuing without pausing", exc_info=True)
        return False
    state["question_rounds"] = rounds
    state["credential_request"] = {**request, "phase": phase, "asked_at": time.time(), "session": None,
                                   "question": text}
    state["pending_question"] = text
    state["return_state"] = phase
    state["state"] = "WAIT_REPLY"
    _transcript_note(state, f"asked the user for credential {env} via {config.COMM_CHANNEL}")
    return True


_CREDENTIAL_CHOICE = re.compile(r"\(?([123])\)?[.):]?")
_CREDENTIAL_WORDS = (
    ("skip", re.compile(r"\b(sin (la |el )?(clave|key|credencial|token)|contin[uú]a sin|sigue sin|"
                       r"without (it|the)|skip|omit|om[ií]tel[ao]|no (la )?tengo|i don'?t have)\b")),
    ("pause", re.compile(r"\b(pausa|pause|espera|wait|hold|luego|later|m[aá]s tarde)\b")),
    ("send_link", re.compile(r"\b(env[ií]a|m[aá]nda|send|link|enlace|p[aá]gina|page|s[ií]|yes|ok|dale)\b")),
)


def _classify_credential_reply(state: dict, reply: str) -> str:
    text = reply.strip().casefold()
    choice = _CREDENTIAL_CHOICE.fullmatch(text)
    if choice:
        return {"1": "send_link", "2": "skip", "3": "pause"}[choice.group(1)]
    if len(text) <= 120:
        for action, pattern in _CREDENTIAL_WORDS:
            if pattern.search(text):
                return action
    request = state["credential_request"]
    return replies.classify("credential", reply, env=request["env"], purpose=request["purpose"],
                            service=request["service"])["action"]


def _credential_action(state: dict, msg_id: str, reply: str):
    """A callable that handles this reply as a credential reply, or None (today's path).

    Deciding never raises; acting may (like any reply handler) and is then retried."""
    try:
        if not secret_store.enabled():
            return None
        command = parse_command(reply)
        # HOLD, ABORT, STATUS... keep their existing meaning. "continue without it" parses
        # as CONTINUE plus a note, which in a task thread is only ever an answer.
        if (command and not (command[0] == "CONTINUE" and command[2])) or re.fullmatch(
                r"(?i)\s*(retry|abort|complete|merge(?: anyway)?)\s*[.!]?\s*", reply):
            return None
        links = secret_store.take_links(msg_id)
        request = state.get("credential_request")
        if (request and not request.get("resume")
                and state.get("pending_question") != request.get("question")):
            # The agent has since asked something else: this reply answers that question.
            privatebin.stop(request.get("session"))
            state.pop("credential_request", None)
            request = None
        if not (request and state["state"] == "WAIT_REPLY" and state.get("item")):
            if links:  # never opened; the rest of the message still takes today's path
                try:
                    email(state, "secure link not used",
                          _credential_text(state, "credential_unexpected"), localized=True)
                except Exception:  # noqa: BLE001
                    log.warning("could not send the unused-link notice", exc_info=True)
            return None
        if request.get("resume"):
            return lambda: _credential_resume(state)  # a previous resume failed: retry it
        if links or secret_store.available(request["env"]):
            return lambda: _credential_provided(state, links)
        action = _classify_credential_reply(state, reply)
        return {"send_link": lambda: _credential_send_link(state),
                "skip": lambda: _credential_skip(state),
                "pause": lambda: email(state, "credential paused", _credential_text(
                    state, "credential_paused", env=request["env"]), localized=True)}.get(action)
    except Exception:  # noqa: BLE001
        log.warning("could not classify a credential reply; handling it normally", exc_info=True)
        return None


def _credential_send_link(state: dict) -> None:
    request = state["credential_request"]
    session = request.get("session")
    if not privatebin.running(session):
        privatebin.stop(session)
        request["session"] = None
        try:
            session = privatebin.start(config.INSTANCE_ID, config.DATA_DIR / "privatebin",
                                       ttl=config.PRIVATEBIN_TTL_SECONDS)
        except privatebin.Unavailable as error:
            allowed = (" (" + ", ".join(config.PRIVATEBIN_INSTANCES) + ")") if config.PRIVATEBIN_INSTANCES else ""
            email(state, "credential page unavailable", _credential_text(
                state, "credential_unavailable", reason=str(error), allowed=allowed,
                env=request["env"]), localized=True)
            return
        request["session"] = session
        save_state(state)  # a retry after a failed email must find, not orphan, the page
    email(state, "credential link", _credential_text(
        state, "credential_link", url=session["public_url"], env=request["env"],
        minutes=max(1, int((session["expires_at"] - time.time()) // 60))), localized=True)


def _credential_provided(state: dict, links: list[str]) -> None:
    request = state["credential_request"]
    env = request["env"]
    if not secret_store.available(env):  # already stored when retrying after a crash
        accepted = privatebin.accepted_bases(request.get("session"), config.PRIVATEBIN_INSTANCES)
        problem = "it is not a PrivateBin link I can open."
        value = None
        for link in links:
            try:
                fetch_url, key = privatebin.parse_link(link, accepted)
                value = privatebin.decode(privatebin.fetch(fetch_url), key)
                break
            except privatebin.LinkUnusable as error:
                problem = str(error)
        if value is None:
            email(state, "credential not received", _credential_text(
                state, "credential_failed", problem=problem, env=env), localized=True)
            return
        secret_store.put(_credential_key(state), env, value, ttl=config.SECRET_TTL_SECONDS,
                         source="privatebin")
    privatebin.stop(request.get("session"))
    request["session"] = None
    state.setdefault("credentials", {})[env] = {"decision": "provided", "at": time.time()}
    request["resume"] = (f"The credential {env} is now set as environment variable {env} for "
                         "you and for controller-run checks. Do not print, log or commit its "
                         "value. Continue the work.")
    save_state(state)
    email(state, "credential received", _credential_text(
        state, "credential_received", env=env, hours=max(1, config.SECRET_TTL_SECONDS // 3600)),
        localized=True)
    _credential_resume(state)


def _credential_skip(state: dict) -> None:
    request = state["credential_request"]
    env = request["env"]
    privatebin.stop(request.get("session"))
    request["session"] = None
    state.setdefault("credentials", {})[env] = {"decision": "skip", "at": time.time()}
    request["resume"] = (f"The user chose to continue without {env}; do not ask for it again. "
                         f"Checks that need it must list it in env_keys with optional: true; "
                         "they will be reported as skipped. Continue with everything else.")
    save_state(state)
    email(state, "continuing without credential", _credential_text(
        state, "credential_skipped", env=env), localized=True)
    _credential_resume(state)


def _credential_resume(state: dict) -> None:
    """Resume the session after a decision. The request is kept (with its resume message)
    until the resume returns, so a transient failure is retried on the next poll instead
    of stranding the user; a new request raised by the resumed turn is left in place."""
    request = state["credential_request"]
    _answer_question(state, request["resume"])
    if state.get("credential_request") is request:
        state.pop("credential_request", None)


def _credential_tick(state: dict) -> None:
    """Select the task's credentials and close a page that expired or died. Never raises."""
    try:
        secret_store.activate(_credential_key(state) if state.get("item") else None)
        request = state.get("credential_request")
        if not request:
            return
        if state.get("state") != "WAIT_REPLY" or not state.get("item"):
            privatebin.stop(request.get("session"))
            state.pop("credential_request", None)
            return
        session = request.get("session")
        if session and not privatebin.running(session):
            privatebin.stop(session)
            request["session"] = None
            email(state, "credential page closed", _credential_text(
                state, "credential_page_closed", env=request["env"]), localized=True)
    except Exception:  # noqa: BLE001
        log.warning("credential housekeeping failed", exc_info=True)


def _release_credentials(state: dict, *, drop: bool) -> None:
    """Close any open page; on task end also delete the task's stored values. Never raises."""
    try:
        request = state.get("credential_request") or {}
        privatebin.stop(request.get("session"))
        if request:
            request["session"] = None
        if drop and state.get("item"):
            secret_store.drop_task(_credential_key(state))
    except Exception:  # noqa: BLE001
        log.warning("could not release task credentials", exc_info=True)


def _startup_credentials() -> None:
    """Background: remove pages left by a previous run (unless one is still serving the
    pending request) and expired values. Never raises."""
    try:
        if not secret_store.enabled():
            return
        session = (load_state().get("credential_request") or {}).get("session")
        if not privatebin.running(session):
            privatebin.cleanup_stale(config.INSTANCE_ID)
        secret_store.sweep()
    except Exception:  # noqa: BLE001
        log.warning("credential startup cleanup failed", exc_info=True)


def handle_wait(state: dict) -> None:
    polled = gmail_client.poll_reply(state["thread_id"]) if state.get("thread_id") else None
    if polled is None:
        return
    msg_id, reply = polled
    if config.COMM_CHANNEL == "slack" and state.get("slack_last_handled_id") == msg_id:
        gmail_client.mark_processed(msg_id)
        return
    target = gmail_client.foreign_command(reply)
    if target:
        # A command addressed to ANOTHER instance, replied on our thread. That
        # instance picks it up mailbox-wide; classifying it here as the user's answer
        # could abort or complete OUR task on a command that was never for us.
        log.info("setting aside command for instance %r replied on our thread", target)
        gmail_client.mark_processed(msg_id)
        return
    log.info("reply received in %s: %r", state["state"], reply[:200])
    _note_contact(state)
    # Before the conversation queue: queuing marks the message processed, which forgets
    # its captured secure links. None means "not a credential reply": today's path.
    credential_action = _credential_action(state, msg_id, reply)
    if credential_action is None and _queue_conversation(state, msg_id, reply):
        return
    if credential_action is None and state["state"] in ("WAIT_REPLY", "WAIT_STUCK") and any(
            token in reply.casefold() for token in ("skip e2e", "saltarnos las pruebas e2e", "omitir e2e")):
        verification_ledger.waiver(state, reply, "e2e:general", content_snapshot(config.REPO_PATH))
    reply_phase = state["state"]
    try:
        if credential_action is not None:
            credential_action()
        else:
            _dispatch_wait_reply(state, msg_id, reply)
    except AgentContentFilterError as error:
        state["state"] = reply_phase
        _content_filter_stop(state, reply_phase, error=error, user_text=reply)
    if config.COMM_CHANNEL == "slack":
        # Persist the state transition before clearing the durable inbox entry.
        # If the process dies between those writes, restart skips reapplying it.
        state["slack_last_handled_id"] = msg_id
        save_state(state)
    # Consume only successful replies or content-filter rejections. Transient
    # failures still leave the message available for a later attempt.
    gmail_client.mark_processed(msg_id)


def _accept_current_scope(state: dict) -> None:
    """The user accepted the drifted requirements/tasks as the approved scope: re-baseline
    and resume. Without this path a scope drift could only loop back to the same block."""
    slug = _validated_slug(state["slug"])
    directory = (_validated_archive_path(state["archive_path"], slug) if state.get("archive_path")
                 else config.REPO_PATH / "openspec/changes" / slug)
    state["approved_scope_fingerprint"] = proposal_package.scope_fingerprint(proposal_package.collect_directory(directory))
    if (config.REPO_PATH / "openspec/changes" / slug / "tasks.md").is_file():
        inventory = task_phases.inventory(config.REPO_PATH, slug)
        state["approved_task_inventory"] = inventory
        if "verified_task_inventory" in state:
            state["verified_task_inventory"] = inventory
    state.pop("verification_blocker", None)
    resume = state.get("return_state") or "VERIFYING"
    state.pop("pending_question", None)
    state.pop("pending_decision", None)
    state["state"] = resume if resume in PHASES else "VERIFYING"
    trail(state, "User accepted the current requirements and tasks as the approved scope")


def _dispatch_wait_reply(state: dict, msg_id: str, reply: str) -> None:
    selection = handoffs.selected_reply(state, reply)
    if selection is not None and selection.get("reply") == handoffs.ACCEPT_SCOPE:
        _accept_current_scope(state)
        return
    if selection is not None:
        if selection.get("error") or selection.get("wait"):
            message = selection.get("error") or "Work is preserved. I will keep waiting; no action was authorized."
            email(state, "decision selection", message + "\n\n" + handoffs.render_decision(state["pending_decision"]))
            return
        # Bare choices have an exact controller meaning. Prose must still pass
        # scope assessment in post-planning question/recovery waits, while the
        # approval and PR handlers already classify the complete reply themselves.
        if (not selection.get("prose") or state["state"] not in ("WAIT_REPLY", "WAIT_STUCK")
                or state.get("return_state") in ("EXPLORING", "PROPOSING")):
            _handle_reply(state, selection["reply"])
            return
    if config.TARGET_MODE == "workspace":
        return _handle_reply(state, reply)
    if (state["state"] == "WAIT_MERGE" or state.get("return_state") == "FEEDBACK_QUESTION" or
            state.get("execution_task_id") and state["state"] in ("WAIT_REPLY", "WAIT_STUCK")) and not re.fullmatch(
            r"(?i)\s*(merge(?: anyway)?|fusiona(?: de todos modos)?|abort|complete|retry)[.! ]*", reply):
        database = phase_checkpoint.store()
        row = feedback.receive(database, state, config.REPO_PATH, msg_id, reply)
        if state.get("feedback_question_id"):
            database.update("feedback", row, question_reply_to=state["feedback_question_id"])
        _dispatch_feedback(state)
    else:
        _handle_reply(state, reply)


def handle_merge_wait(state: dict) -> None:
    """A WAIT_MERGE poll: check the open PR for unresolved review threads (e.g. a human
    reviewer's) and address them, so live feedback gets fixed before the user even says
    'merge'."""
    threads = unresolved_review_threads(state["pr_url"])
    if threads is not None:
        # A human thread the bot already answered (without a code change) is left open
        # for the reviewer to close; it re-enters the queue only once they post again.
        seen = state.get("pr_threads_seen") or {}
        threads = [t for t in threads if lifecycle.has_new_comments(t, seen)]
    if threads is None:
        log.warning("could not fetch review threads; checking the inbox only this tick")
    elif not threads:
        state.pop("pr_thread_notified", None)  # the round budget is per task, not per burst
    elif state.get("pr_thread_notified"):
        log.debug("%d unresolved review thread(s) still open; already notified the user",
                  len(threads))
    elif state.get("pr_thread_round", 0) >= config.PR_THREAD_MAX_ROUNDS:
        log.warning("review-thread round limit (%d) reached with %d open thread(s); "
                    "leaving them for the user", config.PR_THREAD_MAX_ROUNDS, len(threads))
        report = diagnostics.report(phase_checkpoint.store(), state, config.REPO_PATH, config.DATA_DIR,
            "PR_REVIEW", detail=format_unresolved_threads(threads))
        summary = agent_runner.run("Read remaining review findings. Do not use tools or change code. "
            "Give at most three prioritized blockers, impact, recommendation and the precise "
            "decision needed. Do not suggest an unexplained merge bypass.\n" + format_unresolved_threads(threads),
            contract=False).output
        email(state, "unresolved review threads need your help",
              f"There are still {len(threads)} unresolved review conversation(s) on the PR "
              f"after {config.PR_THREAD_MAX_ROUNDS} round(s) of fixes.\nPR: {state['pr_url']}"
              f"\n{summary}\nReply with guidance on the remaining findings; use 'merge anyway' only "
              "to explicitly merge despite them.", [report])
        state["pr_thread_notified"] = True
    else:
        state["pr_threads"] = threads
        state["state"] = "ADDRESS_PR_THREADS"
        log.info("PR has %d new unresolved review thread(s); addressing before merge", len(threads))
        return


def _receive_feedback(state: dict) -> bool:
    if not state.get("thread_id") or not state.get("item"):
        return False
    polled = gmail_client.poll_reply(state["thread_id"])
    if not polled:
        return False
    message_id, text = polled
    if parse_command(text):
        return False  # command loop retains ownership, especially ABORT/HOLD/STATUS
    if gmail_client.foreign_command(text):
        gmail_client.mark_processed(message_id)
        return False
    if _queue_conversation(state, message_id, text):
        return True
    row = feedback.receive(phase_checkpoint.store(), state, config.REPO_PATH, message_id, text)
    if row is None:
        raise RuntimeError("Feedback receipt could not be recorded")
    if not row["data"].get("acknowledged"):
        gmail_client.send(subject(state, "feedback received"),
            message_templates.operational("received", state.get("task_language", "English")),
            state["thread_id"])
        phase_checkpoint.store().update("feedback", row, acknowledged=True)
    task_records.contact(phase_checkpoint.store(), state, config.REPO_PATH,
                         message_id, state["thread_id"])
    gmail_client.mark_processed(message_id)
    return True


def _dispatch_feedback(state: dict) -> bool:
    database = phase_checkpoint.store()
    parallel_conversation.transfer_changes(database, state, config.REPO_PATH)
    parallel_conversation.sync_inputs(database, state, config.REPO_PATH)
    rows = feedback.pending(database, state, config.REPO_PATH)
    if not rows:
        return False
    row = rows[0]
    if config.TARGET_MODE == "workspace":
        state["pending_feedback"] = row["data"]["text"]
        if state["state"] not in ("EXPLORING", "PROPOSING", "WAIT_APPROVAL"):
            state["state"] = "APPLY_FEEDBACK"
        else:
            state["implementation_feedback"] = row["data"]["text"]
        database.update("feedback", row, status="complete")
        save_state(state)
        return True
    original_phase = state["state"]
    try:
        return _apply_received_feedback(state, database, row)
    except AgentContentFilterError as error:
        state["state"] = original_phase
        database.update("feedback", row, status="blocked", outcome="content-filter")
        _content_filter_stop(state, original_phase, error=error, user_text=row["data"]["text"])
        return True


def _apply_received_feedback(state: dict, database, row: dict) -> bool:
    text = row["data"]["text"]
    original_phase = state["state"]
    lateral_change = bool(row["data"].get("conversation_input_id"))
    phase = (state.get("return_state") if original_phase == "WAIT_REPLY" else original_phase)
    # A PR already exists, but a supplementary proposal can still be awaiting
    # approval. Classify delivery without interpreting it as that approval.
    if original_phase == "WAIT_APPROVAL" and state.get("pr_url") and not row["data"].get("assessment"):
        classified = agent_runner.run("Classify only whether this entire message requests evidence delivery "
            "without product changes or approval. No tools. Return JSON {\"delivery_only\":true|false}. "
            "Mixed product changes, approval replies and ambiguous messages are false.\n" + prompts.fenced("message", text), contract=False)
        if parse_json_reply(classified.output).get("delivery_only") is True:
            row = database.update("feedback", row, assessment=feedback.validate({"action": "delivery",
                "answer": "Deliver existing evidence", "reason": "Delivery does not approve the proposal",
                "references": "Authorized user request: " + text}))
    delivery_only = row["data"].get("assessment", {}).get("action") == "delivery"
    if lateral_change and (phase in ("EXPLORING", "PROPOSING") or
                           original_phase == "WAIT_APPROVAL" and not state.get("approved_proposal")) and not delivery_only:
        # A lateral requirement is not an answer to the working question. Update
        # initial planning with phase guardrails, preserving an unanswered wait.
        saved_wait = {key: copy.deepcopy(state[key]) for key in
                      ("state", "pending_question", "pending_decision", "return_state", "question_rounds")
                      if key in state}
        planning_phase = phase if phase in ("EXPLORING", "PROPOSING") else "PROPOSING"
        result = agent_runner.resume(state["session_id"],
            "Incorporate this newly received requirement into initial planning only. "
            "It does NOT answer any existing question or authorize implementation. "
            "Preserve existing unanswered decisions. Do not edit application code, commit, push or merge.\n"
            + prompts.PHASE_RULES.get(planning_phase, "") + prompts.fenced("user requirement", text))
        state["session_id"] = result.session_id
        if original_phase == "WAIT_REPLY":
            state.update(saved_wait)
            email(state, "planning context incorporated", result.output)
        elif original_phase == "WAIT_APPROVAL":
            _send_proposal_review(state, result.output, revised=True, feedback=text)
        elif handle_result(state, result, planning_phase):
            pass
        else:
            email(state, "planning context incorporated", result.output)
        save_state(state)
        database.update("feedback", row, status="complete", outcome="initial planning updated")
        return True
    if phase in ("EXPLORING", "PROPOSING"):
        # These phases are still defining the initial requirements. There is no
        # approved contract to investigate or replan, even when an answer changes
        # a suggested option or arrived through the active-turn feedback queue.
        if original_phase == "WAIT_REPLY":
            _handle_reply(state, text)
        else:
            state["return_state"] = phase
            state["state"] = "WAIT_REPLY"
            save_state(state)
            do_question_reply(state, text)
        database.update("feedback", row, status="complete", outcome=state["state"])
        return True
    interrupted_wait = None
    if lateral_change and state["state"] in ("WAIT_STUCK", "WAIT_REPLY", "WAIT_APPROVAL", "WAIT_CLEAN"):
        state["state"] = state.get("stuck_return") or state.get("return_state") or "VERIFYING"
        interrupted_wait = original_phase
    elif state["state"] in ("WAIT_STUCK", "WAIT_REPLY") and state.get("return_state") != "FEEDBACK_QUESTION":
        # Explicit recovery commands/answers keep their existing continuation;
        # a newly asserted feature requirement must be investigated as feedback.
        intent = agent_runner.run("Classify this authorized message without tools. Return ONLY JSON "
            'with kind "answer", "delivery" or "product_feedback". Requests solely to capture, '
            'convert or publish evidence, or add its Drive link to the PR, are delivery, '
            'not product_feedback or an answer authorizing implementation/merge. A missing feature, new requirement or '
            "design objection is product_feedback even if phrased as a question. An answer "
            "to the pending question or retry/repair guidance is answer. Selecting, "
            "explaining or qualifying an offered alternative is an answer even if "
            "it starts with '3. <explanation>', unless the complete reply adds or "
            "changes requirements outside the approved scope; that remains product_feedback.\n"
            + handoffs.classification_context(state)
            + prompts.fenced("pending question", state.get("pending_question") or state.get("stuck_error", ""))
            + prompts.fenced("message", text), contract=False)
        kind = parse_json_reply(intent.output).get("kind")
        if kind not in ("answer", "product_feedback", "delivery"):
            raise ValueError("Invalid waiting feedback intent")
        if kind in ("product_feedback", "delivery"):
            state["state"] = state.get("stuck_return") or state.get("return_state") or "VERIFYING"
            interrupted_wait = original_phase
        if kind == "delivery":
            row = database.update("feedback", row, assessment={"action": "delivery",
                "answer": "Deliver requested evidence", "reason": "Controller delivery operation",
                "references": "Authorized user request: " + text})
    if row["data"].get("question_reply_to"):
        question = database.get("feedback", row["data"]["question_reply_to"])
        if question:
            row = database.update("feedback", row, text=question["data"]["text"] +
                "\nUser clarification: " + text)
            if row is None:
                raise RuntimeError("Feedback clarification could not be saved")
    if state["state"] in USER_SIDE_WAITS and row["data"].get("phase") not in USER_SIDE_WAITS:
        interrupted_wait = state["state"]
        state["state"] = row["data"].get("phase", "VERIFYING")
    if state["state"] in ("WAIT_STUCK", "WAIT_REPLY") and any(
            token in text.casefold() for token in ("skip e2e", "saltarnos las pruebas e2e", "omitir e2e")):
        verification_ledger.waiver(state, text, "e2e:general", content_snapshot(config.REPO_PATH))
    if state["state"] in ("WAIT_APPROVAL", "WAIT_REPLY", "WAIT_STUCK", "WAIT_CLEAN") and not (
            row["data"].get("assessment", {}).get("action") == "delivery"):
        if state.get("return_state") == "FEEDBACK_QUESTION":
            state.pop("pending_question", None)
            state.pop("return_state", None)
            state["state"] = state.pop("feedback_question_origin", "VERIFYING")
        else:
            _handle_reply(state, row["data"]["text"])
            database.update("feedback", row, status="complete", outcome=state["state"])
            return True
    assessment = row["data"].get("assessment")
    if not assessment:
        database.update("feedback", row, status="investigating")
        try:
            result = agent_runner.read(feedback.investigation(state, row))
            assessment = feedback.validate(parse_json_reply(result.output))
        except Exception:
            state["state"] = original_phase
            raise
        row = database.update("feedback", row, assessment=assessment)
        if row is None:
            raise RuntimeError("Feedback assessment could not be saved")
    if assessment["action"] == "delivery":
        # Delivery is controller work, never an application implementation phase.
        # Preserve the pending decision and resume it after success or a retryable failure.
        try:
            outcome = evidence_delivery.deliver(database, state, config.REPO_PATH,
                sync_pr=_sync_pr_video, notify=_notify_evidence, operations=assessment.get("operations"),
                read_pr=lambda s: _pr_body(s["pr_url"]))
            if outcome["status"] != "complete":
                if (state.get("pr_url") and state.get("has_e2e_harness") and
                        original_phase == "WAIT_MERGE" and
                        outcome.get("stage") in ("capture/validation", "conversion", "video")):
                    database.update("feedback", row, status="pending", delivery_error=str(outcome),
                                    retry_after=time.time() + 60)
                    _begin_evidence_repair(state)
                    return True
                raise RuntimeError(f"Evidence delivery pending at {outcome['stage']}: {outcome.get('error', outcome['status'])}")
        except Exception as exc:
            database.update("feedback", row, status="pending", delivery_error=str(exc), retry_after=time.time() + 60)
            if not row["data"].get("delivery_error"):
                detail = str(exc) + "\nNo product replanning or merge is authorized."
                if "access" in str(exc):
                    detail += ("\nConfirm the destination folder and its existing access settings. "
                               "Codebot will not create or modify permissions; do not send credentials here.")
                email(state, "evidence delivery pending", detail)
        else:
            database.update("feedback", row, status="complete", outcome="evidence delivered")
        state["state"] = original_phase
        save_state(state)
        return True
    if assessment.get("delivery_operations") and not row["data"].get("delivery_requirement_id"):
        delivery_assessment = feedback.validate({"action": "delivery", "answer": "Complete requested evidence delivery",
            "reason": "Delivery requirement retained separately from product feedback",
            "references": assessment["references"], "operations": assessment["delivery_operations"]})
        retained = feedback.receive(database, state, config.REPO_PATH, "delivery:" + row["id"], text)
        database.update("feedback", retained, status="pending", assessment=delivery_assessment,
                        after_product_feedback=row["id"], requires_product_completion=True)
        row = database.update("feedback", row, delivery_requirement_id=retained["id"])
    if not row["data"].get("assessment_delivered"):
        try:
            if lateral_change and assessment["action"] == "question":
                # Do not let email's decision decorator overwrite the working
                # pending_decision for a clarification owned by lateral chat.
                import message_delivery
                message_delivery.deliver(database, state, config.REPO_PATH, config.DATA_DIR,
                    gmail_client._backend(), subject(state, "request clarification"),
                    assessment["answer"] + "\n" + assessment["reason"] + "\nRequest: " +
                    row["data"]["conversation_input_id"], state.get("thread_id"),
                    identity="request-clarification:" + row["id"])
            else:
                answer, files = feedback.attachments(assessment)
                email(state, "feedback investigated", answer + "\n" + assessment["reason"], files,
                      visible_question=assessment["answer"] if assessment["action"] == "question" else None)
                if files and state.get("last_delivery", {}).get("complete") is False:
                    raise RuntimeError("Feedback evidence attachment delivery remains incomplete")
        except Exception:
            state["state"] = original_phase
            raise
        row = database.update("feedback", row, assessment_delivered=True)
        if row is None:
            raise RuntimeError("Feedback delivery checkpoint failed")
    if assessment["action"] == "replan":
        replanning.begin(state, row)
        save_state(state)
        database.update("feedback", row, status="planning")
    elif assessment["action"] == "correction":
        state["pending_feedback"] = row["data"]["text"]
        state["feedback_origin"] = state["state"]
        state["state"] = "APPLY_FEEDBACK"
        state["active_feedback_id"] = row["id"]
        save_state(state)
        database.update("feedback", row, status="repairing")
    elif assessment["action"] == "question":
        if lateral_change:
            request = database.get("conversation_input", row["data"]["conversation_input_id"])
            database.update("conversation_input", request, status="clarifying", question=assessment["answer"])
            database.update("feedback", row, status="waiting")
            state["state"] = original_phase
            save_state(state)
            return True
        state["feedback_question_origin"] = state["state"]
        state["feedback_question_id"] = row["id"]
        state["pending_question"] = assessment["answer"]
        state["return_state"] = "FEEDBACK_QUESTION"
        if state.get("pending_decision"):
            state["pending_decision"]["origin"] = "FEEDBACK_QUESTION"
        state["state"] = "WAIT_REPLY"
        save_state(state)
        database.update("feedback", row, status="waiting")
    else:
        if interrupted_wait:
            state["state"] = interrupted_wait
        database.update("feedback", row, status="complete", outcome="answered")
    if row["data"].get("question_reply_to"):
        question = database.get("feedback", row["data"]["question_reply_to"])
        if question:
            database.update("feedback", question, status="complete", outcome="clarification incorporated")
        state.pop("feedback_question_id", None)
    return True


def do_replan(state: dict) -> None:
    prompt = replanning.prompt(state, config.REPO_PATH)
    save_state(state)
    result = agent_runner.resume(state["session_id"], prompt)
    if handle_result(state, result, "REPLANNING"):
        return
    _send_proposal_review(state, result.output, revised=True, feedback=state["replan"]["feedback"])
    row = phase_checkpoint.store().get("feedback", state["replan"]["feedback_id"])
    if row is None:
        raise RuntimeError("Replanning feedback is missing")
    phase_checkpoint.store().update("feedback", row, status="planning" if state.get("proposal_delivery_pending") else "complete",
                                    outcome="revised proposal delivery queued")


def _continue_replanning(state, result):
    archive = state.get("replan", {}).get("original_archive")
    if archive:
        # The historical archive is immutable; the complementary artifacts carry
        # the new decisions. Detect an agent accidentally editing historical files.
        if git("status", "--porcelain", "--untracked-files=all", "--", archive):
            raise RuntimeError("Replanning modified the historical archive; preserve it and repair the complementary change")
    _run_checked(["openspec", "validate", state["slug"], "--strict", "--no-interactive"])
    _send_proposal_review(state, result.output, revised=True, feedback=state["replan"]["feedback"])
    database = phase_checkpoint.store()
    row = database.get("feedback", state["replan"]["feedback_id"])
    if row:
        database.update("feedback", row, status="planning" if state.get("proposal_delivery_pending") else "complete",
                        outcome="revised proposal delivery queued")


def do_apply_feedback(state: dict) -> None:
    if state.get("review_findings"):
        prompt = prompts.render(prompts.FIX_REVIEW, slug=state["slug"], findings="\n".join(state["review_findings"]))
    else:
        prompt = prompts.render(prompts.APPLY_PR_FEEDBACK, feedback=state["pending_feedback"],
                                branch=state["branch"]) + review_intent.RULES
    result = agent_runner.resume(state["session_id"], prompt)
    if handle_result(state, result, "APPLY_FEEDBACK"):
        return
    _complete_local_feedback(state, result)


def _complete_local_feedback(state, result):
    if state.pop("review_findings", None) is not None:
        state.pop("reviewed_snapshot", None)
        state["state"] = "VERIFYING"  # then the one re-review of the fix commits
        return
    state["verify_round"] = 0
    state.pop("reviewed_snapshot", None)
    state.pop("final_check_report", None)
    state.pop("coverage_report", None)
    if state.get("pr_url"):
        state["feedback_delivery_pending"] = True
    state["state"] = "VERIFYING"
    if state.get("archive_path") and state.get("pr_url"):
        _queue_push(state, "feedback", result.output, feedback=state.get("pending_feedback", ""))
    if state.get("active_feedback_id"):
        database = phase_checkpoint.store()
        row = database.get("feedback", state["active_feedback_id"])
        if row is None:
            raise RuntimeError("Feedback context is missing")
        database.update("feedback", row, status="verifying", outcome="corrected; verifying")
        state.pop("active_feedback_id", None)


# ---------------------------------------------------------------- abort / reset

def _abort_in_progress_ops() -> None:
    """Abort a half-finished merge/rebase/cherry-pick/am, if any. Checks git's own
    state files first so the no-op case stays silent instead of logging four
    "fatal: nothing in progress" errors every time."""
    git_dir = Path(git("rev-parse", "--absolute-git-dir"))
    checks = (
        (("merge", "--abort"), ["MERGE_HEAD"]),
        (("rebase", "--abort"), ["rebase-merge", "rebase-apply"]),
        (("cherry-pick", "--abort"), ["CHERRY_PICK_HEAD"]),
        (("am", "--abort"), ["rebase-apply/applying"]),
    )
    for op, markers in checks:
        if any((git_dir / m).exists() for m in markers):
            log.warning("git %s in progress; aborting it", op[0])
            _git_quiet(*op)


def _git_quiet(*args: str) -> bool:
    """Run a git command, returning success. Never raises — for the best-effort reset."""
    try:
        proc = operations.tool(["git", *args], cwd=config.REPO_PATH)
    except (subprocess.TimeoutExpired, TimeoutError):
        log.debug("git %s timed out after %ss", " ".join(args), config.SUBPROCESS_TIMEOUT_SECONDS)
        return False
    except OSError as error:  # e.g. argument list too long; the docstring promises no raise
        log.debug("git %s could not start: %s", " ".join(args[:3]), error)
        return False
    if proc.returncode != 0:
        log.debug("git %s -> rc=%d: %s", " ".join(args), proc.returncode, proc.stderr.strip()[:200])
    return proc.returncode == 0


def _uncommitted_paths() -> list[str]:
    """Modified, staged, unmerged and untracked (non-ignored) paths, parsed from -z output
    so renames and odd file names are handled."""
    status = operations.tool(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                             cwd=config.REPO_PATH).stdout
    paths, entries = [], [e for e in status.split("\0") if e]
    index = 0
    while index < len(entries):
        entry = entries[index]
        paths.append(entry[3:])
        if entry[:2].strip() and entry[0] in "RC":  # rename/copy: the next record is the source
            index += 1
        index += 1
    return [p for p in paths if p]


def _preserve_uncommitted(label: str) -> str | None:
    """Before a destructive reset, put whatever is uncommitted somewhere recoverable.

    Stash first (tracked + untracked, minus evidence); if git refuses (e.g. unmerged
    paths mid-merge), copy the files and a patch under data/backups. Never raises and
    never prevents the reset: the next task must still start clean. Returns a note for
    the user, or None when there was nothing to preserve."""
    try:
        paths = _uncommitted_paths()
        keep = [p for p in paths if not _looks_like_evidence(p)]
        if not keep:
            return None
        title = f"codebot: {label}"
        # Without evidence to exclude, stash everything with no pathspec: thousands of
        # untracked paths on the command line could exceed ARG_MAX and lose them to clean.
        pathspec = ["--", *keep] if len(keep) < len(paths) else []
        if _git_quiet("stash", "push", "--include-untracked", "-m", title, *pathspec):
            return (f"uncommitted changes ({len(keep)} path(s)) were saved in git stash "
                    f"'{title}' before the reset; `git stash list` shows it")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = config.DATA_DIR / "backups" / f"{re.sub(r'[^A-Za-z0-9_.-]+', '-', label)[:60]}-{stamp}"
        dest.mkdir(parents=True, exist_ok=True)
        budget, copied = 200 * 1024 * 1024, 0
        for rel in keep:
            source = config.REPO_PATH / rel
            if not source.is_file():
                continue
            budget -= source.stat().st_size
            if budget < 0:
                break
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)
            copied += 1
        patch = operations.tool(["git", "diff", "HEAD", "--binary"], cwd=config.REPO_PATH, text=False)
        if patch.returncode == 0 and patch.stdout:
            (dest / "changes.patch").write_bytes(patch.stdout)
        return (f"git could not stash the uncommitted changes; {copied} of {len(keep)} file(s) "
                f"were copied to {dest} before the reset")
    except Exception as error:  # noqa: BLE001 — preservation is best-effort, the reset is not optional
        log.warning("could not preserve uncommitted changes before reset: %s", error)
        return f"could not preserve uncommitted changes before the reset: {error}"


def _reset_to_base_branch(label: str = "reset") -> list[str]:
    """Best-effort LOCAL reset: save anything uncommitted (stash or backup), abort any
    half-finished git op, discard changes, land on a clean, up-to-date base branch. Never
    touches remote branches or PRs. Returns notes about preserved work and about any step
    that left the tree unclean (empty list when there was nothing to report)."""
    problems = []
    preserved = _preserve_uncommitted(label)
    if preserved:
        problems.append(preserved)
    _abort_in_progress_ops()  # a half-finished merge/rebase would block the checkout
    _git_quiet("reset", "--hard")  # drop staged/unstaged tracked changes
    # The shared release lands on the base; branches may have merged/moved while we were
    # away, so it resets to the fetched origin base (best effort: do_pick pulls too).
    target, records = _repo_target({})
    lifecycle.release(target, records, discard=True)
    ops = target.repos[0]
    branch = ops.quiet("rev-parse", "--abbrev-ref", "HEAD")
    if branch != config.BASE_BRANCH:
        problems.append(f"could not switch to {config.BASE_BRANCH} (still on {branch or 'unknown'})")
    if ops.quiet("status", "--porcelain", "--untracked-files=no"):
        problems.append("tracked changes remain after reset")
    return problems


# Every task-scoped state key (persistence.TASK_KEYS): cleared when a task ends, kept
# with a held task.
RESET_KEYS = persistence.TASK_KEYS


def _finish_task(state: dict, note: str, reset_repo: bool, *, merged: bool = False) -> None:
    """End the task successfully: mark the item done in the backlog, optionally reset the
    checkout to a clean base branch (needed when finishing before a merge — the tree may
    hold proposal artifacts or a stale branch), email confirmation, and land in IDLE."""
    _release_credentials(state, drop=True)
    item = state.get("item", "")
    struck = task_source.mark_done(item, state.get("item_id")) if item else False
    if struck:
        log.info("marked item done in the backlog; task complete")
        body = f"{note}\n\nThe item was marked done in the backlog:\n\n{item}"
        subj = "task complete"
    else:
        log.warning("could not locate item in the backlog to mark done: %r", item[:80])
        # Also release the claim: a leftover self-claim would make do_pick's resume
        # filter re-pick this FINISHED task deterministically on the next idle tick,
        # long before the user can act on this email.
        try:
            task_source.unclaim_task(item, state.get("item_id"))
        except Exception:  # noqa: BLE001 — best-effort; the email below covers it
            log.exception("could not unclaim the unmatched item")
        subj = "task complete — mark the item manually"
        body = (f"{note}\n\nBut I could not find this item in the backlog to mark it "
                f"done:\n\n{item}\n\nPlease mark it done (or delete it) promptly "
                "— until then it may be picked again.")
    if reset_repo and config.TARGET_MODE != "workspace":
        problems = _reset_to_base_branch(f"finished {state.get('slug') or state.get('branch') or 'task'}")
        if problems:
            body += ("\n\nNotes from the repo reset (I'll re-check before the "
                     "next task):\n- " + "\n- ".join(problems))
    body += "\n\nI'll pick up the next pending item from the backlog."
    if merged:
        email(state, subj, body, milestone="merged")
    else:
        email(state, subj, body)
    _publish_progress(state, "completed")
    if config.COMM_CHANNEL == "slack" and state.get("thread_id"):
        state["retire_thread_id"] = state["thread_id"]
    for key in RESET_KEYS:
        state.pop(key, None)
    if config.TARGET_MODE == "workspace":
        state.pop("workspace", None)
        state.pop("workspace_review", None)
    if state.get("original_workspace"):
        config.REPO_PATH = Path(state.pop("original_workspace"))
        state.pop("workspace_path", None)
    state["state"] = "IDLE"


def _content_filter_stop(state: dict, failed_state: str, *, error=None, user_text=None) -> None:
    """Reject this attempt, preserve the task, and request new text without an LLM."""
    state.setdefault("task_language", "English")
    if failed_state not in USER_SIDE_WAITS:
        state["stuck_return"] = failed_state
        state["state"] = "WAIT_STUCK"
    state.pop("technical_retry", None)
    state.setdefault("failures", {}).pop(failed_state, None)
    state["provider_block"] = diagnostics.safe({"source": "user reply" if user_text is not None else "bot-generated request",
        "phase": failed_state, "user_text": user_text, "request": getattr(error, "request", ""),
        "partial_response": getattr(error, "partial_response", ""),
        "provider_detail": getattr(error, "provider_detail", "")})
    body = handoffs.provider_notice(state["provider_block"], state.get("task_language", "English"))
    if failed_state not in USER_SIDE_WAITS:
        state["stuck_error"] = diagnostics.summary(error=error, detail=body)
    store = phase_checkpoint.store()
    state["failure_context"] = diagnostics.failure_context(store, state, config.REPO_PATH, str(error or body))
    files = []
    if error is not None or user_text is not None:
        path = diagnostics.report(store, state, config.REPO_PATH, config.DATA_DIR, failed_state, error=error, detail=body)
        state["stuck_diagnostic"] = str(path)
        files.append(path)
    save_state(state)
    log.warning("provider content filter blocked %s; waiting with recorded request evidence", failed_state)
    label = "texto bloqueado" if state.get("task_language", "").casefold() == "spanish" else "content blocked"
    email(state, label, body, files, localized=True)


LOOP_REPEATS = 4  # above every per-phase cap, so it only catches loops the caps miss


def _loop_guard(state: dict, prev: str) -> None:
    """Stop a task that keeps making the same transition on the same repository state."""
    if not state.get("item") or prev in WAITS or state["state"] in WAITS or state["state"] == prev:
        return
    try:
        head, status = _tracked_snapshot()
    except Exception:  # noqa: BLE001 - the guard never fails a tick
        return
    signature = hashlib.sha256(f"{prev}>{state['state']}>{head}>{status}".encode()).hexdigest()[:16]
    seen = state.setdefault("loop_seen", {})
    seen[signature] = seen.get(signature, 0) + 1
    if len(seen) > 50:
        for key in list(seen)[:-50]:
            seen.pop(key)
    if seen[signature] >= LOOP_REPEATS:
        seen.pop(signature)
        _enter_stuck(state, prev, f"Loop detected: {prev} -> {state['state']} repeated {LOOP_REPEATS} times "
                     "without any change to the repository. Stopping instead of repeating it again.")


def _work_preserving_phase(state: dict) -> str:
    """Where to resume a task whose recorded phase is unknown (corrupt state, or a name
    from another coderbot version), keeping its work: never IDLE, which drops the task."""
    if not state.get("item"):
        return "IDLE"
    if state.get("pr_url"):
        return "WAIT_MERGE"
    if state.get("approved_scope_fingerprint"):
        return "VERIFYING"
    return "EXPLORING"


def _enter_stuck(state: dict, failed_state: str, detail: str) -> None:
    """Stop retrying, remember the failed state, e-mail the user, and enter WAIT_STUCK.

    The resume target lives in stuck_return, NOT return_state: return_state is the
    WAIT_REPLY phase pointer, and clobbering it here (e.g. when WAIT_REPLY itself
    escalates after repeated classifier failures) would strand the pending question —
    the answer would later resolve to an unknown phase and the task's context is lost."""
    state["stuck_return"] = failed_state
    state["failure_context"] = diagnostics.failure_context(phase_checkpoint.store(), state, config.REPO_PATH, detail)
    path = diagnostics.report(phase_checkpoint.store(), state, config.REPO_PATH,
                              config.DATA_DIR, failed_state, detail=detail)
    state["stuck_error"] = diagnostics.summary(detail=state["failure_context"]["cause"])
    state["stuck_diagnostic"] = str(path)
    email(state, f"stuck in {failed_state}",
          f"Task: {state.get('item', '?')}\nState: {failed_state}\n\n"
          + handoffs.stopped_notice(state["failure_context"]), [path])
    state["state"] = "WAIT_STUCK"


def _clear_stuck(state: dict, failed_state: str) -> None:
    if state.get("workspace"):
        state["workspace"].pop("verification_rounds", None)
    if state.get("reported_check_plan_error"):
        state.pop("check_plan_repair_round", None)
        state["check_plan_repair_revision"] = state.get("check_plan_repair_revision", 0) + 1
    # return_state is deliberately KEPT: when resuming WAIT_REPLY it still points at the
    # phase whose question is pending, and handle_result refreshes it on new questions.
    if "failures" in state:
        state["failures"].pop(failed_state, None)
    _grant_budget(state, f"retry of {failed_state}", "e2e_round", "verify_round", "review_gate_round",
                  "archive_round", "pr_thread_round", "question_rounds", "conflict_rounds")
    state.pop("stuck_return", None)
    state.pop("stuck_error", None)
    state.pop("failure_context", None)
    state.pop("provider_block", None)
    state.pop("technical_retry", None)
    # A human cleared this failure: re-run the phase's agent turn instead of replaying
    # the recorded output that led to it (same prompt + unchanged repo would replay).
    if state.get("item"):
        try:
            phase_checkpoint.retire(state, failed_state)
        except Exception:  # noqa: BLE001 - best effort; never block the retry itself
            log.exception("could not retire %s checkpoints before retrying", failed_state)


def _escalate(state: dict, failed_state: str) -> bool:
    """After the failure budget is exhausted for a state, e-mail the user and enter
    WAIT_STUCK. Returns True if the escalation was delivered. Never re-escalates WAIT_STUCK
    (its own failures just back off; ABORT is the escape hatch)."""
    if failed_state == "WAIT_STUCK":
        return False
    try:
        _enter_stuck(state, failed_state,
                     f"I've failed this step {config.MAX_STATE_FAILURES} times in a row. "
                     f"Last error:\n\n{traceback.format_exc()}")
        log.warning("escalated %s to WAIT_STUCK after %d failures",
                    failed_state, config.MAX_STATE_FAILURES)
        return True
    except Exception:
        log.exception("failed to escalate stuck state %s; will keep retrying", failed_state)
        return False


def do_stuck_reply(state: dict, reply: str) -> None:
    """Handle the user's reply while WAIT_STUCK: retry / abort / complete / instructions."""
    keyword = {"retry": "retry", "reintenta": "retry", "reintentar": "retry", "try again": "retry",
               "abort": "abort", "aborta": "abort", "complete": "complete"}.get(
        reply.strip().casefold().rstrip(".! "))
    verdict = {"action": keyword} if keyword else replies.classify("stuck", reply, state)
    action = verdict.get("action")
    log.info("classified stuck reply as action=%r", action)
    failed_state = state.get("stuck_return", "IDLE")
    if action == "abort":
        _abort_and_reset(state, "Got it — I'm giving up on this task and resetting to a "
                                "clean slate.")
        return
    if action == "complete":
        _finish_task(state, "You asked me to mark the stuck task complete with no further "
                            "work.", reset_repo=True)
        return
    if action not in ("retry", "instructions"):
        email(state, "clarification needed",
              f"I could not tell whether you want me to retry, abort, or follow instructions:"
              f"\n\n{reply}\n\nPlease reply 'retry', 'abort', 'complete', or with explicit "
              "instructions.")
        return  # stay in WAIT_STUCK
    if action == "instructions" and failed_state == "OPEN_PR":
        # OPEN_PR is orchestrator-driven: the PR title/body never pass through the coding
        # session, so guidance about them must be applied HERE or it would be silently
        # ignored (a live incident: "use a shorter title" followed by the same failure).
        state["pr_title_guidance"] = (verdict.get("feedback") or reply)[:2000]
        state.pop("pr_title", None)
        log.info("recorded PR title guidance; the title is regenerated on resume")
    if action == "instructions" and state.get("session_id"):
        # Apply the guidance in the working session, then let the failed state re-run — which
        # handles any follow-up question through its own do_* handler.
        log.info("applying user instructions to session before resuming %s", failed_state)
        result = agent_runner.resume(state["session_id"], prompts.render(
            prompts.ANSWER_REPLY, reply=verdict.get("feedback") or reply,
            rules=prompts.PHASE_RULES.get(failed_state, "")))
        state["session_id"] = result.session_id
        if handle_result(state, result, failed_state):
            return
    # retry, or instructions applied: clear the failure history for the state and resume it.
    _clear_stuck(state, failed_state)
    if failed_state == "IDLE":
        # The unrecoverable-context fallback resumes at IDLE, which means "no task":
        # drop every task key so session/PR/spec state can't leak into the next pick.
        for key in RESET_KEYS:
            state.pop(key, None)
    state["state"] = failed_state


def _abort_and_reset(state: dict, note: str, new_thread: bool = False) -> None:
    """Shared abort machinery: reset the local checkout, email a confirmation, clear all
    task state, and return to IDLE. Used both by the mailbox-wide last-resort 'ABORT'
    email and by an explicit abort classified from a WAIT_APPROVAL/WAIT_MERGE reply.
    Never touches remote branches or PRs — those are left for the user to clean up."""
    _release_credentials(state, drop=True)
    aborted_task = state.get("item", "-")
    prev_state = state["state"]
    log.warning("ABORT: resetting to IDLE (was state=%s task=%r)", prev_state, aborted_task[:80])
    problems = (["Workspace files and child repository branches were preserved; no reset was performed"]
                if config.TARGET_MODE == "workspace" else
                _reset_to_base_branch(f"aborted {state.get('slug') or state.get('branch') or 'task'}"))
    # The task goes back to the pool, so its claim marker must go too — otherwise no
    # instance (including this one, if its state was wiped) would ever pick it again.
    if aborted_task and aborted_task != "-":
        try:
            if not task_source.unclaim_task(aborted_task, state.get("item_id")):
                problems.append("could not find the backlog item to release its "
                                "claim — release it manually")
        except Exception:  # noqa: BLE001 — a backlog hiccup must not block the reset
            log.exception("unclaim failed during reset")
            problems.append("could not release the item's claim in the backlog — "
                            "release it manually or no instance will pick the task")
    status = (f"The working tree was reset to a clean, up-to-date `{config.BASE_BRANCH}`." if not problems
              else "Notes from the reset (I'll re-check the tree before starting the "
                   "next task):\n- " + "\n- ".join(problems))
    email(state, "aborted — reset to IDLE",
          f"{note}\n\nWas working on: {aborted_task}\nPrevious state: {prev_state}\n\n{status}\n\n"
          "Remote branches and PRs were left untouched. I'll pick up the next pending item "
          "from the backlog.", new_thread=new_thread)
    _publish_progress(state, "aborted")
    if config.COMM_CHANNEL == "slack" and state.get("thread_id"):
        state["retire_thread_id"] = state["thread_id"]
    for key in RESET_KEYS:
        state.pop(key, None)
    if config.TARGET_MODE == "workspace":
        state.pop("workspace", None)
        state.pop("workspace_review", None)
    state["state"] = "IDLE"


# ---------------------------------------------------------------- on-hold tasks

def _load_holds() -> list[dict]:
    """Unparseable holds.json is quarantined (and reported), never read as empty and
    then overwritten; an unreadable one raises rather than losing the holds."""
    holds = persistence.read_json(config.HOLDS_PATH, [])
    if not isinstance(holds, list):
        return persistence.quarantine(config.HOLDS_PATH, [])
    for hold in holds:
        if hold.get("saved"):
            hold["saved"] = persistence.forward(_validate_target_state(hold["saved"]))
            task_records.migrate(hold["saved"], phase_checkpoint.store(), config.REPO_PATH)
    return holds


def _save_holds(holds: list[dict]) -> None:
    persistence.write_json(config.HOLDS_PATH, holds, indent=2)


def _commit_pending_work(state: dict) -> list[str]:
    """Leave nothing dangling before parking a task: abort any half-finished git
    operation, then commit every tracked change and every untracked file that is not
    evidence on the task branch. Returns notes about anything that could not be saved."""
    notes = []
    branch = state.get("branch")
    if not branch:
        return notes
    _abort_in_progress_ops()
    if git("rev-parse", "--abbrev-ref", "HEAD") != branch:
        if not _git_quiet("checkout", branch):
            notes.append(f"could not switch back to {branch} to save pending work")
            return notes
    _git_quiet("add", "-u")  # every tracked change
    untracked = [p for p in git("ls-files", "--others", "--exclude-standard").splitlines()
                 if p.strip()]
    secrets_found = [p for p in untracked if _looks_like_secret(p)]
    kept = [p for p in untracked if not _looks_like_evidence(p) and p not in secrets_found]
    if secrets_found:
        notes.append("left out of the commit because they look like credentials: "
                     + ", ".join(secrets_found[:10]))
    if kept:
        _git_quiet("add", "--", *kept)
    if git("status", "--porcelain", "--untracked-files=no"):
        if _git_quiet("commit", "-q", "-m", "wip: task put on hold by the user"):
            notes.append("committed pending work as 'wip: task put on hold by the user'")
        else:
            notes.append("could not commit the pending work — it stays in the working tree")
    return notes


def _hold_task(state: dict, thread_id: str | None) -> None:
    """Park the current task: save its work and state, mark it on hold in the backlog,
    and return to IDLE so the next task can start. CONTINUE on the task's thread
    brings it back with top priority."""
    _release_credentials(state, drop=False)
    item = state.get("item", "")
    notes = (["Workspace changes remain saved locally; no blanket commit or reset was performed"]
             if config.TARGET_MODE == "workspace" else _commit_pending_work(state))
    try:
        if not task_source.hold_task(item, state.get("item_id")):
            notes.append("could not mark the item on hold in the backlog — another "
                         "instance may pick it up; mark it on hold by hand")
    except Exception:  # noqa: BLE001 — a backlog hiccup must not lose the hold
        log.exception("hold marker failed")
        notes.append("could not update the backlog (see logs)")
    resume_state = state["state"]
    saved = {k: state[k] for k in RESET_KEYS if k in state}
    if config.TARGET_MODE == "workspace":
        saved["workspace"] = copy.deepcopy(state["workspace"])
    saved["state"] = resume_state
    holds = [h for h in _load_holds() if h.get("thread_id") != state.get("thread_id")]
    holds.append({"thread_id": state.get("thread_id") or thread_id, "item": item,
                  "item_id": state.get("item_id"),
                  "slug": state.get("slug"), "branch": state.get("branch"),
                  "held_at": time.time(), "requested": False, "note": "", "saved": saved})
    _save_holds(holds)
    body = (f"Task on hold: {item}\nBranch: {state.get('branch', '-')} (all work "
            f"committed there)\nIt was in state {resume_state}.\n\n"
            "I'm moving on to the next backlog item. Reply 'continue' on THIS thread "
            "(optionally followed by instructions) and I'll pick this task back up "
             "first thing after my current work.")
    if config.TARGET_MODE == "workspace":
        body = (f"Task on hold: {item}\nWorkspace files and child branches remain local; "
                "no blanket commit or reset was performed.\n"
                f"It was in state {resume_state}. Reply 'continue' on this thread to resume.")
    if notes:
        body += "\n\nNotes:\n- " + "\n- ".join(notes)
    problems = [] if config.TARGET_MODE == "workspace" else _reset_to_base_branch(f"hold {state.get('slug') or state.get('branch') or 'task'}")
    if problems:
        log.warning("reset after hold left notes: %s", problems)
        body += "\n\nRepo reset:\n- " + "\n- ".join(problems)
    email(state, "on hold", body)
    _publish_progress(state, "paused")
    with _status_command_lock:
        for key in RESET_KEYS:
            state.pop(key, None)
        if config.TARGET_MODE == "workspace":
            state.pop("workspace", None)
        state["state"] = "IDLE"
    log.info("task on hold: %r (resume at %s)", item[:80], resume_state)


def _request_continue(thread_id: str, note: str) -> bool:
    """Flag the held task on this thread as requested; it is restored at the next pick.
    False when no held task lives on the thread."""
    holds = _load_holds()
    for hold in holds:
        if hold.get("thread_id") == thread_id:
            hold["requested"] = True
            hold["note"] = note
            hold["requested_at"] = time.time()
            _save_holds(holds)
            log.info("continue requested for held task %r", hold.get("item", "")[:80])
            return True
    return False


def _resume_held_task(state: dict, hold: dict) -> None:
    """Restore a held task into `state` (branch, session, wait/phase state) and apply
    the user's CONTINUE note, if any, as the reply the task was waiting for."""
    saved = dict(hold.get("saved") or {})
    saved = _validate_target_state(saved)
    resume_state = saved.pop("state", "EXPLORING")
    note = hold.get("note", "")
    item = hold.get("item", "")
    log.info("resuming held task %r at %s", item[:80], resume_state)
    item_id = hold.get("item_id")
    try:
        task_source.unhold_task(item, item_id)
    except Exception:  # noqa: BLE001
        log.exception("could not remove the hold marker")
    if not task_source.claim_task(item, item_id):
        log.warning("held task could not be re-claimed (struck or claimed elsewhere); dropping "
                    "the hold: %r", item[:80])
        _save_holds([h for h in _load_holds() if h is not hold and h.get("thread_id") != hold.get("thread_id")])
        return
    branch = hold.get("branch") or saved.get("branch")
    if branch and config.TARGET_MODE != "workspace":
        git("checkout", branch)
    state.update(saved)
    state["state"] = resume_state
    # New holds carry their persisted language. A legacy hold is classified on
    # the next FSM tick, rather than starting another classifier mid-resume.
    agent_runner.set_task_language(state.get("task_language"))
    save_state(state)
    _save_holds([h for h in _load_holds() if h.get("thread_id") != hold.get("thread_id")])
    if note and state["state"] in WAITS and state["state"] != "WAIT_REVIEW":
        # The note answers whatever the task was waiting on (approval, question, merge).
        email(state, "resumed", f"Resuming this task with your note:\n\n{note}")
        _handle_reply(state, note)
        return
    if note and state.get("session_id"):
        result = agent_runner.resume(state["session_id"], prompts.render(
            prompts.ANSWER_REPLY, reply=f"(task resumed after a pause) {note}",
            rules=prompts.PHASE_RULES.get(state["state"], "")))
        if handle_result(state, result, state["state"]):
            return
    last = state.get("last_email") or {}
    if state["state"] in WAITS:
        email(state, "resumed",
              "Resuming this task; I'm still waiting on your reply to my last message"
              + (f":\n\n{last.get('body', '')}" if last.get("body") else "."))
    else:
        email(state, "resumed", f"Resuming this task from state {state['state']}.")


def _is_transient_network_error(error: BaseException) -> bool:
    """A service or environment outage, as opposed to a fault in the step (see errors.kind)."""
    return errors.kind(error) == errors.TRANSIENT


def _request_verification(state: dict, note: str = "") -> None:
    """Move an active change to verification without changing Git or approvals."""
    if not state.get("item") or not state.get("session_id"):
        raise ValueError("VERIFY requires an active task and coding session")
    slug = _validated_slug(state.get("slug"))
    if state.get("archive_path") or not (config.REPO_PATH / "openspec/changes" / slug / "tasks.md").is_file():
        raise ValueError("VERIFY requires an active, unarchived OpenSpec change")
    restore_commit, note = verification_recovery.guidance(note)
    if restore_commit:
        state["verification_plan_recovery"] = verification_recovery.restore(
            config.REPO_PATH, slug, restore_commit, config.DATA_DIR)
        state["verified_task_inventory"] = task_phases.inventory(config.REPO_PATH, slug)
    # Removing only the in-memory context lets task_records.migrate replay these
    # durable transitions on the next tick. Retain their history, but explicitly
    # supersede the old continuation before saving the new phase.
    database = phase_checkpoint.store()
    task_id = database.task_identity(state, config.REPO_PATH)
    # Prior VERIFY versions cleared these fields. Recover only an actual clean
    # review outcome bound to precisely the current content, never an agent claim
    # that an old review is still applicable.
    review_rows = [row for row in database.list("checkpoint", task_id)
                   if row["status"] in ("complete", "consumed")
                   and row["data"].get("phase") == "INTERNAL_REVIEW"
                   and row["data"].get("output")]
    if review_rows:
        snapshot = content_snapshot(config.REPO_PATH)
        for row in review_rows:
            if row["data"].get("snapshot") == snapshot:
                report, _ = parse_internal_review(row["data"]["output"])
                if report:
                    state["internal_review_report"] = report
                    state["reviewed_snapshot"] = snapshot
                    break
    if state.get("quality_snapshot") and state["quality_snapshot"] != content_snapshot(config.REPO_PATH):
        state.pop("quality_report", None)
        state.pop("quality_snapshot", None)
        state.pop("quality_controller_validated", None)
    for row in database.list("feedback", task_id):
        if row["status"] in ("planning", "repairing"):
            database.update("feedback", row, status="superseded", outcome="verify-requested")
    state["verification_requested_from"] = state["state"]
    for key in ("replan", "pending_question", "return_state", "stuck_return",
                "active_feedback_id", "pending_feedback", "feedback_origin",
                "coverage_report", "verification_blocker",
                "final_check_report", "e2e_passed", "implementation_feedback",
                "verify_round_feedback", "review_gate_round_feedback"):
        state.pop(key, None)
    state["verification_guidance"] = note or "Verify the existing implementation and continue the normal verification gates."
    _grant_budget(state, "VERIFY command", "verify_round", "review_gate_round", "implementation_return_round")
    state["state"] = "VERIFYING"
    save_state(state)


def check_commands(state: dict) -> bool:
    """Handle a mailbox-wide user command (ABORT / STATUS / DONE). Returns True only when
    the tick should skip normal dispatch (an ABORT reset or a DONE completion).

    Checked every tick regardless of state and mailbox-wide, so it works even when the agent
    is stuck waiting on a thread — or in a state that never polls the inbox (WAIT_REVIEW).
    Only commands from CODEBOT_USER_EMAIL are honored. A gmail hiccup here must not break
    the tick, so polling failures are swallowed. ABORT and DONE reset local state only —
    remote branches and PRs are untouched.
    """
    try:
        polled = gmail_client.poll_command()
    except Exception as error:
        if _is_transient_network_error(error):
            log.warning("command check could not poll gmail (%s: %s); skipping this tick",
                        type(error).__name__, error)
        else:
            log.exception("command check could not poll gmail; skipping this tick")
        return False
    if polled is None:
        return False
    msg_id, thread_id, command, targeted, note = polled
    if config.COMM_CHANNEL == "slack" and state.get("slack_last_handled_id") == msg_id:
        gmail_client.mark_processed(msg_id)
        return False
    if (config.COMM_CHANNEL == "slack" and thread_id != state.get("thread_id")
            and not any(h.get("thread_id") == thread_id for h in _load_holds())):
        # A completed/aborted hold may still have a queued event before its root
        # is retired. Never let that event target an unrelated active task.
        gmail_client.mark_processed(msg_id)
        return False
    if config.COMM_CHANNEL == "slack" and _handle_held_command(msg_id, thread_id, command, note):
        return False  # command addressed a held task, not the active one
    if command == "VERIFY":
        if thread_id != state.get("thread_id") and not targeted:
            gmail_client.mark_processed(msg_id)
            return False
        try:
            _request_verification(state, note)
        except ValueError as error:
            _send_localized(state, subject(state, "verify"), str(error), thread_id)
            gmail_client.mark_processed(msg_id)
            return False
        if config.COMM_CHANNEL == "slack":
            state["slack_last_handled_id"] = msg_id
            save_state(state)
        gmail_client.mark_processed(msg_id)
        email(state, "Verification requested", "I will verify the existing work without repeating planning or implementation. "
              "Verification, internal review and final checks still apply.")
        return True
    if command == "KICK":
        # The independent supervisor interrupts live turns; here there is no
        # running agent to restart. Never fall through to ABORT.
        gmail_client.mark_processed(msg_id)
        body = ("No hay un turno del agente en ejecución; la tarea sigue igual."
                if state.get("task_language", "English").casefold() == "spanish" else
                "No coding-agent turn is running; the task is unchanged.")
        gmail_client.send(subject(state, "kick"), body, thread_id)
        return False
    if command == "CONTINUE":
        if not _request_continue(thread_id, note):
            if thread_id and thread_id == state.get("thread_id"):
                # "continue" on the ACTIVE task thread is ordinary conversation
                # ("carry on"); leave it for handle_wait's classifiers.
                log.debug("CONTINUE-shaped reply on the active thread; deferring")
                return False
            gmail_client.mark_processed(msg_id)
            _send_localized(_language_state_for_thread(state, thread_id),
                            f"{config.SUBJECT_PREFIX} general — nothing on hold here",
                            "CONTINUE received, but no task is on hold on this thread.",
                            thread_id)
            return False
        if config.COMM_CHANNEL == "email":
            gmail_client.mark_processed(msg_id)
        _send_localized(_language_state_for_thread(state, thread_id),
                        f"{config.SUBJECT_PREFIX} general — will continue",
                        "Got it — this task is next: I'll resume it as soon as my current "
                        "work is done (right away if I'm idle).", thread_id)
        if config.COMM_CHANNEL == "slack":
            state["slack_last_handled_id"] = msg_id
            save_state(state)
        if config.COMM_CHANNEL == "slack":
            gmail_client.mark_processed(msg_id)
        return False
    if command == "HOLD":
        if not state.get("item"):
            log.warning("HOLD command received but no task is in progress; ignoring")
            gmail_client.mark_processed(msg_id)
            _send_localized(_language_state_for_thread(state, thread_id),
                            f"{config.SUBJECT_PREFIX} general — nothing to hold",
                            "HOLD received, but no task is in progress.", thread_id)
            return False
        # Consume only AFTER handling (multi-step side effects, like DONE).
        _hold_task(state, thread_id)
        if config.COMM_CHANNEL == "slack":
            state["slack_last_handled_id"] = msg_id
            save_state(state)
        gmail_client.mark_processed(msg_id)
        return True
    if command == "DONE":
        if not targeted and thread_id and thread_id == state.get("thread_id"):
            # A BARE "Done"-bodied reply on the ACTIVE task thread is normal
            # conversation ("Done" = "I did what you asked"), not the command — leave
            # it unprocessed for handle_wait and its classifiers. "DONE <this-instance>"
            # is explicit and IS the command.
            log.debug("bare DONE-shaped reply on the active thread; deferring to the "
                      "thread classifier")
            return False
        if not state.get("item"):
            log.warning("DONE command received but no task is in progress; ignoring")
            gmail_client.mark_processed(msg_id)
            _send_localized(_language_state_for_thread(state, thread_id),
                            f"{config.SUBJECT_PREFIX} general — nothing to complete",
                            "DONE received, but no task is in progress.", thread_id)
            return False
        # Consume only AFTER handling: _finish_task has multi-step network side effects
        # (docs strike-through, repo reset, email); a failure mid-way must leave the
        # DONE unprocessed so the next tick retries it. Each step is idempotent enough
        # to re-run (a struck item just reports "mark manually" on the retry).
        _finish_task(state, "DONE command received: marking the current task complete.",
                     reset_repo=True)
        if config.COMM_CHANNEL == "slack":
            state["slack_last_handled_id"] = msg_id
            save_state(state)
        gmail_client.mark_processed(msg_id)
        return True
    # Gmail retains its mailbox-wide consume-first semantics. Slack's SQLite inbox
    # instead checkpoints successful handling before marking an event consumed.
    if config.COMM_CHANNEL == "email":
        gmail_client.mark_processed(msg_id)
    if command == "STATUS":
        _send_status(state, thread_id)
        if thread_id and thread_id == state.get("thread_id"):
            _note_contact(state)  # a STATUS exchange on the task thread is contact too
        if config.COMM_CHANNEL == "slack":
            state["slack_last_handled_id"] = msg_id
            save_state(state)
            gmail_client.mark_processed(msg_id)
        return False  # STATUS is read-only; let the tick proceed normally
    _abort_and_reset(state, "ABORT received: I stopped the task in progress and reset myself "
                            "to a clean slate.", new_thread=True)
    if config.COMM_CHANNEL == "slack":
        state["slack_last_handled_id"] = msg_id
        save_state(state)
        gmail_client.mark_processed(msg_id)
    return True


def _handle_held_command(msg_id: str, thread_id: str, command: str, note: str) -> bool:
    """A held Slack thread addresses its own task, never the unrelated active task."""
    hold = next((h for h in _load_holds() if h.get("thread_id") == thread_id), None)
    if not hold:
        return False
    item, item_id = hold["item"], hold.get("item_id")
    if command == "CONTINUE":
        _request_continue(thread_id, note)
        response = "I'll resume this task after the current work is done."
    elif command == "STATUS":
        response = (f"{config.INSTANCE_ID} · on hold: {item}\n"
                    f"Branch: {hold.get('branch', '-')}\n"
                    f"Resume requested: {bool(hold.get('requested'))}")
    elif command == "HOLD":
        response = f"Task is already on hold: {item}"
    elif command == "VERIFY":
        response = "Resume this held task with CONTINUE before requesting VERIFY."
    elif command == "ABORT":
        task_source.unhold_task(item, item_id)
        if not task_source.unclaim_task(item, item_id):
            raise RuntimeError(f"could not release held task {item!r} from backlog")
        _save_holds([h for h in _load_holds() if h.get("thread_id") != thread_id])
        response = f"Released held task to the backlog: {item}"
    elif command == "DONE":
        if not task_source.mark_done(item, item_id):
            raise RuntimeError(f"could not mark held task {item!r} done in backlog")
        _save_holds([h for h in _load_holds() if h.get("thread_id") != thread_id])
        response = f"Marked held task done: {item}"
    else:
        return False
    _send_localized(hold.get("saved") or {},
                    f"{config.SUBJECT_PREFIX} held task — {command}", response, thread_id)
    gmail_client.mark_processed(msg_id)
    if command in ("ABORT", "DONE"):
        held_state = {**(hold.get("saved") or {}), "thread_id": thread_id}
        _publish_progress(held_state, "aborted" if command == "ABORT" else "completed")
        gmail_client.close_thread(thread_id)
    return True


def _fmt_ts(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(epoch))


def _send_status(state: dict, thread_id: str) -> None:
    """Reply to a STATUS command with a snapshot of the FSM. Never mutates state."""
    body = _short_status(state, agent_runner.turn_snapshot(), time.time())
    holds = _load_holds()
    if holds:
        body += "\nOn hold: " + "; ".join(str(h.get("slug") or h.get("item", "?")) +
            (" (continue requested)" if h.get("requested") else "") for h in holds[:3])
    _send_localized(state, subject(state, "status"), body, thread_id)
    task_records.contact(phase_checkpoint.store(), state, config.REPO_PATH,
                         "status-" + str(time.time_ns()), thread_id)


def _short_status(state: dict, turn: dict, now: float) -> str:
    """A factual snapshot of the task and the last observable agent event."""
    turn = activity.snapshot(state, turn, now)
    spanish = state.get("task_language", "English").casefold() == "spanish"
    phase = state.get("return_state") if state.get("state") == "WAIT_REPLY" else state.get("state", "IDLE")
    title = str(state.get("item") or ("Sin tarea activa" if spanish else "No active task"))[:140]
    started = (turn.get("started_at") if turn.get("active") else
               state.get("last_transition") or _liveness.get("tick_started"))
    elapsed = _fmt_dur(now - started) if isinstance(started, (float, int)) else "?"
    last = turn.get("last_activity") or state.get("last_effective_progress")
    counts = turn.get("todos") or {}
    children = turn.get("children") or []
    if spanish:
        lines = [f"Tarea: {title}", f"Fase: {phase} (inició hace {elapsed})."]
        if turn.get("current_task"):
            lines.append(f"Ahora: {turn['current_task']}.")
        if counts:
            lines.append(f"Plan: {counts.get('in_progress', 0)} en curso, "
                         f"{counts.get('pending', 0)} pendientes, {counts.get('completed', 0)} terminadas.")
        if children:
            lines.append("Subagentes: " + "; ".join(
                f"{child['title']} (último evento hace {_fmt_dur(now - child['at'])})"
                for child in children) + ".")
        if last:
            lines.append(f"Última actividad observada: {turn.get('activity', 'agente')} "
                         f"(hace {_fmt_dur(now - last)}).")
        if turn.get("process_dead"):
            lines.append("El proceso del agente no aparece activo.")
    else:
        lines = [f"Task: {title}", f"Phase: {phase} (started {elapsed} ago)."]
        if turn.get("current_task"):
            lines.append(f"Now: {turn['current_task']}.")
        if counts:
            lines.append(f"Plan: {counts.get('in_progress', 0)} in progress, "
                         f"{counts.get('pending', 0)} pending, {counts.get('completed', 0)} done.")
        if children:
            lines.append("Subagents: " + "; ".join(
                f"{child['title']} (last event {_fmt_dur(now - child['at'])} ago)"
                for child in children) + ".")
        if last:
            lines.append(f"Last observed activity: {turn.get('activity', 'agent')} "
                         f"({_fmt_dur(now - last)} ago).")
        if turn.get("process_dead"):
            lines.append("The agent process is no longer running.")
    message_status = (parallel_conversation.status_summary(phase_checkpoint.store(), state, config.REPO_PATH)
                      if state.get("execution_task_id") and state.get("thread_id") else "")
    if message_status:
        lines.append(message_status)
    for operation in operations.snapshot()[:4]:
        elapsed = _fmt_dur(now - operation["started_at"])
        lines.append(("Operación" if spanish else "Operation") + ": "
                     + f"{operation['kind']} / {operation['label']} ({elapsed}); "
                     + ("presupuesto restante" if spanish else "budget remaining")
                     + f": {operation['remaining']:.0f}s")
    if not message_status:
        # An in-flight or failed message is not a request for another workflow reply.
        if state.get("state") in USER_SIDE_WAITS and not turn.get("active"):
            lines.append(_ping_body(state, now, 0))
        elif not turn.get("active") and not operations.snapshot():
            lines.append("No hay ejecución activa; siguiente paso o reintento pendiente." if spanish else
                         "No active execution; next step or retry pending.")
        else:
            lines.append("Estoy trabajando; no necesito nada de ti." if spanish else "Working; no action needed.")
    return "\n".join(lines[:10])


# What "this phase finished via a WAIT_REPLY answer" means, per phase. Mirrors each
# do_* function's own success tail so a phase completed through a question detour ends
# up in exactly the same place as one completed directly.
def _continue_exploring(state: dict, result) -> None:
    _record_exploration(state, result)
    _undo_premature_work(state, "EXPLORING")
    state["state"] = "PROPOSING"
    announce_milestone(state, "proposing", "Exploration is complete; I'm preparing the OpenSpec proposal.")


def _continue_proposing(state: dict, result) -> None:
    note = _undo_premature_work(state, "PROPOSING")
    _send_proposal_review(state, result.output, note)


def _grant_budget(state: dict, reason: str, *counters: str) -> None:
    """The one place a human decision resets per-task attempt counters. Counters only
    reset on their own when the step they count succeeds; nothing else grants a budget."""
    used = {counter: state.pop(counter) for counter in counters if state.get(counter)}
    if used:
        log.info("budget granted by %s: %s", reason, used)
        trail(state, f"Attempt counters reset by {reason}", ", ".join(f"{k}={v}" for k, v in used.items()))


def _continue_implementing(state: dict, result) -> None:
    _grant_budget(state, "answer during IMPLEMENTING", "implementation_no_progress", "implementation_turn")
    _complete_implementation(state, result)


def _continue_verifying(state: dict, result) -> None:
    _grant_budget(state, "answer during VERIFYING", "verify_round")
    _complete_verify(state, result)


def _continue_internal_review(state: dict, result) -> None:
    _grant_budget(state, "answer during INTERNAL_REVIEW", "review_gate_round")
    _complete_internal_review(state, result)


def _continue_e2e(state: dict, result) -> None:
    _grant_budget(state, "answer during E2E", "e2e_round", "final_check_round", "final_repair_round")
    if state.get("check_repair"):
        _complete_check_repair(state, result)
        return
    # Upgrade legacy WAIT_REPLY final-check blockers into repair before rerunning.
    if state.get("final_check_report", {}).get("status") != "pass" and state.get("final_check_report"):
        _begin_check_repair(state, state["final_check_report"], resume="E2E")
        state["check_repair"]["guidance"] = "Prior user-guided investigation:\n" + result.output
        save_state(state)
        return
    state["state"] = "E2E"  # re-run the final checks on the (answer-informed) fix


def _continue_archiving(state: dict, result) -> None:
    _grant_budget(state, "answer during ARCHIVING", "archive_round")
    state["state"] = "ARCHIVING"


def _continue_address_review(state: dict, result) -> None:
    # Whether the answer-informed session committed is unknown on this detour path
    # (no head snapshot): push regardless — a no-op push is harmless, and the review
    # wait then either sees a new run or concludes from the clean-check.
    state["review_round"] = state.get("review_round", 0) + 1
    _finish_address_review(state, result, head_before=None)


def _continue_resolve_conflicts(state: dict, result) -> None:
    # conflict_head was persisted when the resolution session started, so this detour
    # can make the same committed-or-not exit decision as the direct path.
    _leave_conflict_resolution(state)


def _continue_address_pr_threads(state: dict, result) -> None:
    _scrub_evidence_from_repo(state, "ADDRESS_PR_THREADS")
    state["pr_thread_round"] = state.get("pr_thread_round", 0) + 1
    _queue_push(state, "threads", result.output)


def _continue_apply_pr_feedback(state: dict, result) -> None:
    extra_attachments = _scrub_evidence_from_repo(state, "APPLY_PR_FEEDBACK")
    attachments = _collect_attachments(
        result, state.get("e2e_specs", []), state.get("e2e_kind"))
    existing = {path.resolve() for path in attachments}
    attachments += [path for path in extra_attachments if path.resolve() not in existing]
    _queue_push(state, "feedback", result.output, attachments,
                feedback=state.get("pending_feedback", ""))


CONTINUATIONS = {
    "REPAIR_CHECKS": _complete_check_repair,
    "REPLANNING": _continue_replanning,
    "APPLY_FEEDBACK": _complete_local_feedback,
    "FEEDBACK_QUESTION": lambda state, result: state.update(state="VERIFYING"),
    "EXPLORING": _continue_exploring,
    "PROPOSING": _continue_proposing,
    "IMPLEMENTING": _continue_implementing,
    "VERIFYING": _continue_verifying,
    "INTERNAL_REVIEW": _continue_internal_review,
    "E2E": _continue_e2e,
    "ARCHIVING": _continue_archiving,
    "ADDRESS_REVIEW": _continue_address_review,
    "ADDRESS_PR_THREADS": _continue_address_pr_threads,
    "APPLY_PR_FEEDBACK": _continue_apply_pr_feedback,
    "RESOLVE_CONFLICTS": _continue_resolve_conflicts,
}


def _reply_prompt(state: dict, phase: str, reply: str) -> str:
    """The prompt that resumes the working session with the user's answer. Gate phases
    re-issue their own contract prompt (the answer alone would not make the session emit
    the completion contract again); every other phase gets the answer plus its rules."""
    if phase == "REPAIR_CHECKS":
        context = state["check_repair"]
        context["guidance"] = reply
        context["attempt"] = 1
        return check_repair.prompt(state, context)
    if phase == "E2E" and state.get("final_check_report", {}).get("status") != "pass" and state.get("final_check_report"):
        state["check_repair"] = {"attempt": 1, "resume": "E2E",
            "checks": check_repair.unresolved(state["final_check_report"]),
            "before": content_snapshot(config.REPO_PATH), "guidance": reply}
        return check_repair.prompt(state, state["check_repair"])
    if phase in ("VERIFYING", "INTERNAL_REVIEW"):
        gate_prompt = _verify_prompt if phase == "VERIFYING" else _internal_review_prompt
        return f"User recovery guidance:\n{reply}\n\n" + gate_prompt(state)
    if phase == "ARCHIVING":
        return prompts.render(prompts.FIX_ARCHIVE,
                              error=state.get("archive_error", "unknown archival failure"),
                              guidance=reply)
    return (prompts.render(prompts.ANSWER_REPLY, reply=reply,
                           rules=prompts.PHASE_RULES.get(phase, ""))
            + handoffs.classification_context(state))


def do_question_reply(state: dict, reply: str) -> None:
    """Handle the user's answer while WAIT_REPLY. The reply is classified first — with a
    fresh session, like the other waits — because control-flow instructions ("mark it
    done and move on", "abandon this") must act on the FSM, not be forwarded to the
    working session, which has no lever on the FSM and can only loop asking questions."""
    if state.get("pending_reply_delivery"):
        _continue_reply_delivery(state)
        return
    verdict = replies.classify("question", reply, state,
                               question=state.get("pending_question", "(not recorded)"))
    action = verdict.get("action")
    log.info("classified WAIT_REPLY reply as action=%r", action)
    if action == "complete":
        _finish_task(state, "You asked me to mark this task complete with no further work.",
                     reset_repo=True)
        return
    if action == "abort":
        _abort_and_reset(state, "Got it — I'm stopping this task and resetting to a clean slate.")
        return
    if action == "resume_implementation":
        blocker = state.get("verification_blocker", {})
        if (state.get("return_state") == "VERIFYING" and
                blocker.get("kind") == "unfinished or unclassified tasks"):
            if _scope_drift(state):
                return
            # Do not let an LLM verdict approve a new inventory or bypass approval.
            inventory = task_phases.inventory(config.REPO_PATH, state["slug"])
            approved = state.get("verified_task_inventory", state.get("approved_task_inventory"))
            pending = task_phases.inspect(config.REPO_PATH, state["slug"])
            if approved is None or inventory != approved:
                _verification_blocked(state, "scope drift",
                                      "Cannot confirm pending tasks belong to the approved inventory.")
                return
            if pending["approval"]:
                _verification_blocked(state, "approval", "\n".join(pending["approval"]))
                return
            if pending["implementation"]:
                state["implementation_feedback"] = (
                    "The user explicitly authorized completing only these pending approved "
                    "implementation tasks. Preserve existing work and Git history; do not "
                    "replan, expand scope, push or claim unexecuted checks. After completion, "
                    "continue normal verification and evidence reconciliation.\n"
                    + "\n".join(pending["implementation"]) + "\n"
                    + prompts.fenced_authoritative("user continuation authorization", reply))
                for key in ("verification_guidance", "verification_blocker", "pending_question",
                            "return_state", "question_rounds", "implementation_no_progress",
                            "implementation_progress", "quality_controller_validated"):
                    state.pop(key, None)
                state["state"] = "IMPLEMENTING"
                save_state(state)
                trail(state, "User authorized continuation of pending approved implementation",
                      state["implementation_feedback"])
                return
    # answer (the default): resume the working session.
    _answer_question(state, reply)


def _answer_question(state: dict, reply: str) -> None:
    """Resume the working session with the user's answer, restating the phase rules the
    raw reply lacks — sessions have overreached (implemented/pushed while PROPOSING)
    exactly on these unframed resumes."""
    # Read return_state without popping: if a later step here raises, the state stays
    # WAIT_REPLY with return_state intact so the retry re-runs cleanly.
    phase = state.get("return_state")
    if phase not in CONTINUATIONS:
        resume = _work_preserving_phase(state)
        _enter_stuck(state, resume,
                     f"Internal error: I was waiting on a question for unknown phase "
                     f"{phase!r} and cannot resume it. 'retry' resumes the task at {resume} "
                     "with its work and branch intact; 'abort' resets me.")
        return
    pending_question = state.get("pending_question")
    question_rounds = state.get("question_rounds")
    trail(state, f"Answer received (resuming {phase})", reply)
    result = agent_runner.resume(state["session_id"], _reply_prompt(state, phase, reply))
    if handle_result(state, result, phase):
        return  # re-questioned (or question cap hit); return_state already updated
    reply_files = [Path(p) for p in result.attachments if Path(p).is_file()]
    if result.output.strip() or reply_files:
        # A WAIT_REPLY turn answers a person, regardless of the phase's normal
        # completion notice. Deliver the answer/evidence before its continuation
        # can replace it with a progress banner or silently discard attachments.
        state["pending_reply_delivery"] = {
            "phase": phase, "output": result.output, "session_id": result.session_id,
            "attachments": [str(p) for p in reply_files],
            "question": pending_question, "question_rounds": question_rounds,
        }
        if pending_question is not None:
            state["pending_question"] = pending_question
        if question_rounds is not None:
            state["question_rounds"] = question_rounds
        save_state(state)
        _continue_reply_delivery(state)
        return
    CONTINUATIONS[phase](state, result)
    if state["state"] != "WAIT_REPLY":  # a continuation may itself have re-questioned
        state.pop("return_state", None)
        state.pop("pending_question", None)


def _continue_reply_delivery(state: dict) -> None:
    """Resume only transport/continuation; never regenerate a completed agent reply."""
    from types import SimpleNamespace
    pending = state["pending_reply_delivery"]
    phase = pending["phase"]
    receipt_id = pending.get("notification_id")
    if receipt_id:
        receipt = phase_checkpoint.store().get("delivery_receipt", receipt_id)
        if not receipt or receipt["status"] != "complete":
            log.info("reply delivery pending for %s; waiting for transport confirmation", phase)
            return
    elif not pending.get("delivered"):
        state.pop("last_delivery", None)
        try:
            label = "exploration reply" if phase == "EXPLORING" else f"reply during {phase}"
            email(state, label, pending["output"].strip() or "The requested supporting files are attached.",
                  [Path(p) for p in pending["attachments"]])
        except Exception as error:
            # Delivery receipts may already exist even if the send raised.
            log.warning("reply delivery unavailable for %s; preserving completed answer: %s", phase, error)
            save_state(state)
            return
        delivery = state.get("last_delivery", {})
        if delivery.get("notification_id"):
            pending["notification_id"] = delivery["notification_id"]
        if delivery.get("complete") is False:
            save_state(state)
            log.info("reply delivery incomplete for %s; retrying transport only", phase)
            return
        pending["delivered"] = True
        save_state(state)
    result = SimpleNamespace(session_id=pending["session_id"], output=pending["output"],
                             attachments=pending["attachments"], question=None, preamble="")
    CONTINUATIONS[phase](state, result)
    state.pop("pending_reply_delivery", None)
    if state["state"] != "WAIT_REPLY":
        state.pop("return_state", None)
        state.pop("pending_question", None)
    save_state(state)


def _handle_reply(state: dict, reply: str) -> None:
    if config.TARGET_MODE == "workspace":
        import workspace_runner
        return workspace_runner.reply(state, reply)
    if state["state"] == "WAIT_APPROVAL":
        do_approval_reply(state, reply)
    elif state["state"] == "WAIT_STUCK":
        do_stuck_reply(state, reply)
    elif state["state"] == "WAIT_MERGE":
        do_merge_reply(state, reply)
    elif state["state"] == "WAIT_CLEAN":
        state["state"] = "IDLE"
    elif state["state"] == "WAIT_REPLY":
        do_question_reply(state, reply)


# Loop liveness for the heartbeat thread: the main loop stamps this at the start of every
# tick. The daemon touches the heartbeat file only while a tick is younger than the max
# plausible duration, so a legit multi-hour agent call stays healthy but a wedged loop
# eventually lets the heartbeat go stale (see config.HEARTBEAT_MAX_TICK_SECONDS).
_liveness = {"tick_started": time.time()}
_work_active = threading.Event()
_status_command_lock = threading.RLock()
_progress_lock = threading.RLock()
_current_state: dict | None = None  # main-thread owner; status worker reads a snapshot only
_lock_handle = None  # kept alive for the process lifetime so the flock is held
_conversation_worker = None
_conversation_state = None


def _queue_conversation(state, message_id, text):
    """Receipt first; commands retain their existing owner outside /btw."""
    if parse_command(text):
        return False
    store = phase_checkpoint.store()
    row = parallel_conversation.receive(store, state, config.REPO_PATH, message_id, text)
    fixed = parallel_conversation.deterministic(state, text)
    if config.TARGET_MODE == "workspace" and state.get("state") == "WAIT_REPLY" and parse_btw(text) is None:
        # Route to the question handler, which classifies the actual reply before
        # acting. Do not make an unrelated lateral-delivery worker a dependency.
        fixed = {"intent": "answer", "resolves_pending_question": True, "inputs": []}
    if fixed and row["status"] == "pending":
        parallel_conversation.save_classification(store, row, fixed)
    task_records.contact(store, state, config.REPO_PATH, message_id, state["thread_id"])
    if _conversation_worker:
        parallel_conversation._notify(row, "received", _conversation_worker.notify)
    gmail_client.mark_processed(message_id)
    if _conversation_worker:
        _conversation_worker.wake.set()
    return True


def _dispatch_conversation(state):
    store = phase_checkpoint.store()
    if config.TARGET_MODE == "workspace" and state.get("state") == "WAIT_REPLY":
        # Recover receipts already taken out of Slack's inbox by an older build.
        # Only unchanged active question/thread receipts qualify; /btw remains lateral.
        for pending in store.list("conversation", store.task_identity(state, config.REPO_PATH)):
            view = pending["data"].get("snapshot", {})
            if (pending["status"] in ("pending", "retry") and not pending["data"].get("classification")
                    and not pending["data"].get("explicit_btw") and view.get("state") == "WAIT_REPLY"
                    and view.get("thread_id") == state.get("thread_id")
                    and view.get("decision_version") == parallel_conversation.decision_version(state)):
                parallel_conversation.save_classification(store, pending,
                    {"intent": "answer", "resolves_pending_question": True, "inputs": []})
    rows = parallel_conversation.flow_pending(store, state, config.REPO_PATH)
    if not rows:
        return False
    rows = [r for r in rows if not (r["data"].get("deferred_pr_decision") or r["data"].get("queued_pr_url"))
            or state["state"] == "WAIT_MERGE"]
    if not rows:
        return False
    row = rows[0]
    if row["data"].get("queued_pr_url"):
        if row["data"]["queued_pr_url"] != parallel_conversation._prs(state):
            store.update("conversation", row, status="complete", outcome="stale PR; not authorized")
            email(state, "PR instruction needs confirmation", "The PR changed. Please send your instruction again; I did not merge.")
            return True
        if not row["data"].get("deferred_pr_decision"):
            row = store.update("conversation", row, snapshot=parallel_conversation.snapshot(store, state, config.REPO_PATH))
    if row["data"].get("deferred_pr_decision"):
        if state["state"] != "WAIT_MERGE":
            return False  # Repairs and push continue; lateral worker does not consume it.
        if row["data"]["deferred_pr_decision"] != parallel_conversation.pr_decision_key(state):
            store.update("conversation", row, status="complete", outcome="stale PR decision; not authorized")
            email(state, "PR decision needs confirmation", "The displayed PR decision changed. Please review it and send your decision again; I did not merge.")
            return True
        # Only phase drift is permitted. The existing merge handler still validates
        # reviewed content, conflicts, pending requirements and GitHub protections.
        row = store.update("conversation", row, snapshot=parallel_conversation.snapshot(store, state, config.REPO_PATH))
    if row["data"].get("flow_applied") or state.get("conversation_flow_id") == row["id"]:
        store.update("conversation", row, status="complete", outcome=state["state"])
        state.pop("conversation_flow_id", None)
        return True
    if row["data"]["snapshot"]["decision_version"] != parallel_conversation.decision_version(state):
        store.update("conversation", row, status="pending", route=None, classification=None,
                     snapshot=parallel_conversation.snapshot(store, state, config.REPO_PATH))
        # Reusing a bare choice against a different decision is not safe.
        if parallel_conversation.deterministic(state, row["data"]["text"]):
            parallel_conversation.save_classification(store, store.get("conversation", row["id"]),
                {"intent": "ambiguous", "resolves_pending_question": False, "inputs": []})
        return True
    phase = state["state"]
    try:
        _dispatch_wait_reply(state, row["identity"], row["data"]["original_text"])
    except AgentContentFilterError as error:
        state["state"] = phase
        _content_filter_stop(state, phase, error=error, user_text=row["data"]["original_text"])
        store.update("conversation", row, status="failed", last_error="content-filter")
        return True
    state["conversation_flow_id"] = row["id"]
    save_state(state)
    state.pop("conversation_flow_id", None)
    store.update("conversation", row, status="complete", outcome=state["state"])
    return True


def _deliver_conversation(row):
    return parallel_conversation.delivery(phase_checkpoint.store(), row, row["data"]["snapshot"].get("repo", config.REPO_PATH),
                                          config.DATA_DIR, gmail_client._backend())


def _deliver_conversation_status(row, stage):
    return parallel_conversation.delivery(phase_checkpoint.store(), row, row["data"]["snapshot"].get("repo", config.REPO_PATH),
        config.DATA_DIR, gmail_client._backend(), notice=stage)


def _conversation_delivery_blocked(state):
    store = phase_checkpoint.store()
    # Drain authorized messages before evaluating the handoff, not after publishing.
    if state.get("thread_id"):
        with _status_command_lock:
            _receive_feedback(state)
    parallel_conversation.sync_inputs(store, state, config.REPO_PATH)
    if not parallel_conversation.delivery_blockers(store, state, config.REPO_PATH):
        state.pop("conversation_delivery_notified", None)
        return False
    if not state.get("conversation_delivery_notified"):
        email(state, "pending conversation changes", "There are pending requests or messages to evaluate before "
              "publishing or merging. Work is preserved; no publication or merge was authorized by the lateral chat.")
        state["conversation_delivery_notified"] = True
    return True


def _status_supervisor_once() -> bool:
    """Serve one inquiry without touching the FSM or starting another agent turn."""
    state = dict(_current_state) if _current_state is not None else load_state()
    working = _work_active.is_set() and state.get("state") in PHASES
    turn = agent_runner.turn_snapshot()
    if operations.snapshot():
        turn["active"] = True
    thread = state.get("thread_id") if _work_active.is_set() and turn.get("active") else None
    with _status_command_lock:
        # Interrupt only the live process; the main thread consumes VERIFY and owns
        # the state transition on its next tick. Never mutate shared FSM state here.
        if turn.get("active") and thread and state.get("item") and state.get("session_id"):
            active = config.REPO_PATH / "openspec/changes" / str(state.get("slug", "")) / "tasks.md"
            if not state.get("archive_path") and active.is_file():
                verify = gmail_client.poll_verify(thread)
                if isinstance(verify, tuple) and len(verify) == 5:
                    if turn_control.request_kick():
                        return True  # Leave the durable command queued for check_commands.
        kick = gmail_client.poll_kick(thread) if turn.get("active") or config.COMM_CHANNEL == "slack" else None
        if kick:
            msg_id, reply_thread = kick
            if turn_control.request_kick():
                gmail_client.mark_processed(msg_id)
                text = ("Reiniciando este turno del agente; conservaré la tarea y el trabajo ya guardado."
                        if state.get("task_language", "English").casefold() == "spanish" else
                        "Restarting this agent turn; keeping the task and saved work.")
                try:
                    gmail_client.send(subject(state, "kick"), text, reply_thread)
                    task_records.contact(phase_checkpoint.store(), state, config.REPO_PATH,
                                         msg_id, reply_thread)
                except Exception:
                    log.exception("turn kicked but acknowledgment could not be sent")
                return True
            if not turn.get("active"):
                gmail_client.mark_processed(msg_id)
                text = ("No hay un turno del agente en ejecución; la tarea sigue igual."
                        if state.get("task_language", "English").casefold() == "spanish" else
                        "No coding-agent turn is running; the task is unchanged.")
                gmail_client.send(subject(state, "kick"), text, reply_thread)
                return True
            return False  # process is starting; leave KICK queued
        if config.COMM_CHANNEL == "email" and not working:
            return False  # normal mailbox polling owns STATUS between turns
        request = gmail_client.poll_status(state.get("thread_id") if working else None)
        if not request:
            if (working or _work_active.is_set()) and state.get("thread_id"):
                _receive_feedback(copy.deepcopy(_conversation_state or state))
            return False
        if config.COMM_CHANNEL == "email" and not _work_active.is_set():
            return False  # let the normal command loop consume it
        msg_id, reply_thread = request
        snapshot = dict(_current_state) if _current_state is not None else state
        turn = agent_runner.turn_snapshot()
        body = _short_status(snapshot, turn, time.time())
        subj = subject(snapshot, "brief status")
        gmail_client.send(subj, body, reply_thread)
        task_records.contact(phase_checkpoint.store(), snapshot, config.REPO_PATH,
                             msg_id, reply_thread)
        gmail_client.mark_processed(msg_id)
        log.info("served brief STATUS in %s without interrupting %s",
                 reply_thread, snapshot.get("state"))
        return True


def _status_supervisor_loop() -> None:
    while True:
        try:
            _status_supervisor_once()
        except Exception:  # noqa: BLE001 — supervisory polling never crashes codebot
            log.exception("brief status supervisor failed; will retry")
        try:
            _refresh_progress()
        except Exception:
            log.exception("progress supervisor failed; will retry")
        time.sleep(3 if config.COMM_CHANNEL == "slack" else 15)


def _refresh_progress() -> None:
    """Publish independently of STATUS requests, including user-side waits."""
    with _progress_lock:
        state = dict(_current_state) if _current_state is not None else load_state()
        if state.get("item") and state.get("thread_id"):
            _publish_progress(state)
        gmail_client.flush_progress()


def _publish_progress(state: dict, situation: str | None = None) -> None:
    # Its own lock: a slow progress update must not hold up commands or state saves.
    with _progress_lock:
        try:
            # Reply handlers retain wait states for safe retries while running an
            # agent. Polling a wait alone is not work; a live turn is.
            active = (bool(agent_runner.turn_snapshot().get("active")) or
                      (_work_active.is_set() and state.get("state") in PHASES
                       and state.get("state") != "IDLE"))
            gmail_client.update_progress(state.get("thread_id"),
                                         progress.snapshot(state, active=active, situation=situation))
        except Exception:
            log.exception("could not publish task progress; task execution continues")


def _heartbeat_loop() -> None:
    while True:
        try:
            if (not operations.expired() and
                    (operations.snapshot() or time.time() - _liveness["tick_started"] < config.HEARTBEAT_MAX_TICK_SECONDS)):
                selfheal_events.emit("heartbeat", _current_state or {})
                config.HEARTBEAT_PATH.write_text(str(int(time.time())))
        except Exception:
            log.exception("heartbeat write failed")
        time.sleep(config.POLL_INTERVAL_SECONDS)


class ShutdownRequested(BaseException):
    """Raised in the main thread by the SIGTERM/SIGINT handler. A BaseException (not
    Exception) so it passes through every `except Exception` in the phases, and so
    subprocess.run() kills whatever child is running (an agent call, e2e, git) on its
    way out instead of waiting for it."""


_shutdown = threading.Event()


def _request_shutdown(signum, _frame) -> None:
    """Only a flag and the interrupting raise: taking a lock here could deadlock against
    the main thread that holds it. main()'s finally stops the other children."""
    if not _shutdown.is_set():
        _shutdown.set()
        raise ShutdownRequested(signal.Signals(signum).name)


def _install_shutdown_handlers() -> None:
    """Python is PID 1 in the container and has no default SIGTERM handler there, so a
    plain `docker stop` (Spot interruption, host shutdown, redeploy) would be ignored
    until Docker SIGKILLs us. With the handler we drop everything at once and exit;
    the last completed tick is already on disk, so the next start resumes from it."""
    _shutdown.clear()
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)


def _release_single_instance_lock() -> None:
    global _lock_handle
    if _lock_handle is not None:
        try:
            _lock_handle.close()
        except OSError:
            pass
        _lock_handle = None


def _acquire_single_instance_lock() -> None:
    """Refuse to start a second codebot against the same data/ dir — two processes would
    interleave state.json writes. The lock is released automatically when the process exits."""
    global _lock_handle
    lock_path = config.DATA_DIR / "state.lock"
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _lock_handle = open(lock_path, "w")
    try:
        fcntl.flock(_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(f"another codebot already holds {lock_path}; refusing to start")


def validate_managed_runtime() -> None:
    required = [
        ("Superpowers skill tree",
         config.SUPERPOWERS_PLUGIN_DIR / "skills/using-superpowers/SKILL.md"),
        ("bridge manifest", config.BRIDGE_PLUGIN_DIR / ".claude-plugin/plugin.json"),
        ("bridge skill",
         config.BRIDGE_PLUGIN_DIR / "skills/coderbot-openspec-workflow/SKILL.md"),
    ]
    if config.AGENT == "claude":
        required.extend([
            ("Superpowers Claude manifest",
             config.SUPERPOWERS_PLUGIN_DIR / ".claude-plugin/plugin.json"),
            ("OpenSpec skills manifest",
             config.OPENSPEC_SKILLS_DIR / ".claude-plugin/plugin.json"),
            ("OpenSpec Claude skills",
             config.OPENSPEC_SKILLS_DIR / "skills/openspec-explore/SKILL.md"),
        ])
    elif config.AGENT == "opencode":
        required.extend([
            ("OpenSpec OpenCode skills",
             config.OPENSPEC_SKILLS_DIR / "opencode/openspec-explore/SKILL.md"),
            ("Superpowers OpenCode entrypoint",
             config.SUPERPOWERS_PLUGIN_DIR / ".opencode/plugins/superpowers.js"),
            ("bridge OpenCode package", config.BRIDGE_PLUGIN_DIR / "package.json"),
            ("bridge OpenCode entrypoint",
             config.BRIDGE_PLUGIN_DIR / ".opencode/plugins/coderbot-openspec.js"),
        ])

    for label, path in required:
        if not path.is_file():
            raise SystemExit(f"managed runtime unavailable: missing {label} at {path}")


def main() -> None:
    # No credential threads, backlog validation, channel delivery, or agent turn
    # may start against a mismatched target. Do not migrate durable records yet.
    raw_state = json.loads(config.STATE_PATH.read_text()) if config.STATE_PATH.exists() else {"state": "IDLE"}
    try:
        _validate_target_state(raw_state)
    except ValueError as error:
        raise SystemExit(f"Target state unavailable: {error}") from error
    if config.AGENT not in ("claude", "opencode"):
        raise SystemExit("CODEBOT_AGENT must be 'claude' or 'opencode'")
    if config.AGENT == "opencode" and (
            "/" not in config.OPENCODE_MODEL or config.OPENCODE_MODEL.endswith("/")):
        raise SystemExit("OPENCODE_MODEL must be set to provider/model when CODEBOT_AGENT=opencode")
    validate_managed_runtime()
    if config.COMM_CHANNEL not in ("email", "slack"):
        raise SystemExit("CODEBOT_COMM_CHANNEL must be 'email' or 'slack'")
    if config.COMM_CHANNEL == "email" and not config.USER_EMAIL:
        raise SystemExit("CODEBOT_USER_EMAIL must be set in .env")
    if config.COMM_CHANNEL == "slack":
        for setting, value in (("CODEBOT_SLACK_CHANNEL_ID", config.SLACK_CHANNEL_ID),
                               ("CODEBOT_SLACK_BOT_TOKEN", config.SLACK_BOT_TOKEN),
                               ("CODEBOT_SLACK_APP_TOKEN", config.SLACK_APP_TOKEN)):
            if not value:
                raise SystemExit(f"{setting} must be set when CODEBOT_COMM_CHANNEL=slack")
        if not re.fullmatch(r"C[A-Z0-9]+", config.SLACK_CHANNEL_ID):
            raise SystemExit("CODEBOT_SLACK_CHANNEL_ID must be a public channel ID (C...)")
    if config.TASK_SOURCE not in task_source.SOURCES:
        raise SystemExit("CODEBOT_TASK_SOURCE must be one of: " + ", ".join(task_source.SOURCES))
    if config.TASK_SOURCE == "gdoc" and not config.DOC_ID:
        raise SystemExit("CODEBOT_DOC_ID must be set in .env (the backlog Google Doc id)")
    if config.TASK_SOURCE == "github" and not (config.GH_PROJECT_OWNER and config.GH_PROJECT_NUMBER):
        raise SystemExit("CODEBOT_GH_PROJECT_URL (or CODEBOT_GH_PROJECT_OWNER and "
                         "CODEBOT_GH_PROJECT_NUMBER) must be set in .env when "
                         "CODEBOT_TASK_SOURCE=github")
    if config.TASK_SOURCE == "jira":
        for setting, value in (("CODEBOT_JIRA_URL", config.JIRA_URL),
                               ("CODEBOT_JIRA_PROJECT_KEY", config.JIRA_PROJECT_KEY),
                               ("CODEBOT_JIRA_EMAIL", config.JIRA_EMAIL),
                               ("CODEBOT_JIRA_API_TOKEN", config.JIRA_API_TOKEN),
                               ("CODEBOT_JIRA_PICK_LABEL", config.JIRA_PICK_LABEL)):
            if not value:
                raise SystemExit(f"{setting} must be set when CODEBOT_TASK_SOURCE=jira")
        if not re.fullmatch(r"https://[^/\s]+\.atlassian\.net", config.JIRA_URL):
            raise SystemExit("CODEBOT_JIRA_URL must be an https://<site>.atlassian.net URL")
        for setting, value in (("CODEBOT_JIRA_PICK_STATUS", config.JIRA_PICK_STATUS),
                               ("CODEBOT_JIRA_ACTIVE_STATUS", config.JIRA_ACTIVE_STATUS),
                               ("CODEBOT_JIRA_REVIEW_STATUS", config.JIRA_REVIEW_STATUS),
                               ("CODEBOT_JIRA_DONE_STATUS", config.JIRA_DONE_STATUS)):
            if not value:
                raise SystemExit(f"{setting} must name a Jira workflow status")
    # .exists() not .is_dir(): in a git worktree .git is a file.
    if config.TARGET_MODE != "workspace" and not (config.REPO_PATH / ".git").exists():
        raise SystemExit(
            f"CODEBOT_REPO_PATH ({config.REPO_PATH}) is not a git checkout; set it in .env")
    try:
        task_source.validate()
    except RuntimeError as err:
        raise SystemExit(f"backlog ({config.TASK_SOURCE}) unavailable: {err}") from err
    _acquire_single_instance_lock()
    threading.Thread(target=_startup_credentials, daemon=True).start()
    gmail_client.start()
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    global _conversation_worker
    _conversation_worker = parallel_conversation.Worker(phase_checkpoint.store, config.REPO_PATH,
        agent_runner.converse, _deliver_conversation, notify=_deliver_conversation_status)
    _conversation_worker.thread.start()
    threading.Thread(target=_status_supervisor_loop, daemon=True).start()
    log.info("codebot starting; instance=%s agent=%s repo=%s source=%s %s",
             config.INSTANCE_ID, config.AGENT, config.REPO_PATH, config.TASK_SOURCE,
             task_source.describe())
    # Re-assert the backlog claim for an in-flight task: one picked before claim markers
    # existed (or whose marker was hand-deleted) is invisible protection-wise, and a
    # newly spawned instance could pick it too. Best-effort — a claim now held by
    # ANOTHER instance is only logged (claim_task does); the user sorts out that
    # pre-existing split-brain.
    try:
        startup_state = load_state()
        if startup_state.get("item"):
            task_source.claim_task(startup_state["item"], startup_state.get("item_id"))
    except Exception:  # noqa: BLE001 — an unreachable backlog must not block startup
        log.exception("could not re-assert the in-flight task's claim at startup")
    _install_shutdown_handlers()
    threading.Thread(target=_probe_runtime, name="runtime-probe", daemon=True).start()
    try:
        selfheal_events.emit("ready", load_state())
        _run_loop()
    except ShutdownRequested as sig:
        # Deliberately no save_state: the tick in flight may have half-mutated the
        # state dict, while state.json still holds the last completed tick (phases
        # that need a mid-tick checkpoint save one themselves). Resuming from that
        # re-runs the interrupted phase, which is the same recovery as a crash.
        log.info("received %s; stopping (the in-flight tick is discarded, state.json "
                 "keeps the last completed one)", sig)
    finally:
        if _conversation_worker:
            _conversation_worker.close()
        if _shutdown.is_set():
            turn_control.request_kick(None)
        _release_single_instance_lock()


def _run_loop() -> None:
    global _current_state, _conversation_state
    backoff = 1
    while True:
        _liveness["tick_started"] = time.time()
        try:
            state = load_state()
            _report_quarantined()
            if retention.due(config.DATA_DIR / ".retention-stamp"):
                retention.maybe_sweep(config.DATA_DIR, config.REPO_PATH, lambda: _retention_keep(state),
                                      phase_checkpoint.store())
            if state.get("workspace_path"):
                config.REPO_PATH = Path(state["workspace_path"])
        except (ValueError, OSError):
            # Corrupt/unreadable state.json, or a workspace target that no longer matches
            # it (a child repo lost origin/HEAD, a new child, `git init` at the root): never
            # crash into a restart loop — log, wait, and retry once an operator fixes it.
            log.exception("could not load %s; retrying in %ss", config.STATE_PATH,
                          config.POLL_INTERVAL_SECONDS)
            time.sleep(config.POLL_INTERVAL_SECONDS)
            continue
        if state.get("state") not in PHASES and state.get("state") not in WAITS \
                and config.TARGET_MODE != "workspace":
            unknown, state["state"] = state.get("state"), _work_preserving_phase(state)
            log.error("unknown state %r in %s; resuming at %s", unknown, config.STATE_PATH,
                      state["state"])
        prev = state["state"]
        attempt_task_id = state.get("execution_task_id")
        selfheal_events.emit("heartbeat", state)
        _current_state = state
        _conversation_state = copy.deepcopy(state)
        try:
            if state.get("item") and state["state"] != "IDLE":
                agent_runner.set_task_context(state)
                _ensure_task_language(state)
            else:
                agent_runner.set_task_context(None)
            _credential_tick(state)
            agent_runner.set_task_language(state.get("task_language"))
            if config.COMM_CHANNEL == "slack" and state.get("retire_thread_id"):
                gmail_client.close_thread(state["retire_thread_id"])
                state.pop("retire_thread_id")
                save_state(state)
            if (config.COMM_CHANNEL == "slack" and state.get("item")
                    and state["state"] != "IDLE" and not state.get("thread_id")):
                state["thread_id"] = gmail_client.open_thread(state)
                save_state(state)
            if prev not in WAITS:
                log.debug("tick: state=%s task=%r", prev, state.get("item", "-"))
            _announce_state(state)
            with _status_command_lock:
                handled_command = check_commands(state)
            task_records.apply_contacts(phase_checkpoint.store(), state, config.REPO_PATH)
            try:
                gmail_client.retry_deliveries(state)
            except Exception:
                if not state.get("pending_reply_delivery"):
                    raise
                log.exception("reply transport retry failed; keeping completed answer pending")
            receipt_id = state.get("last_delivery", {}).get("notification_id")
            if receipt_id:
                receipt = phase_checkpoint.store().get("delivery_receipt", receipt_id)
                if receipt:
                    state["last_delivery"]["complete"] = receipt["status"] == "complete"
            if state.get("item") and not handled_command and state["state"] not in USER_SIDE_WAITS and state.get("execution_task_id"):
                with _status_command_lock:
                    _receive_feedback(state)
            dispatched_feedback = False
            if not handled_command and _dispatch_conversation(state):
                dispatched_feedback = True
            elif state.get("pending_reply_delivery") and not handled_command:
                _continue_reply_delivery(state)
                dispatched_feedback = True
            elif state.get("item") and not handled_command:
                _work_active.set()
                try:
                    dispatched_feedback = _dispatch_feedback(state)
                finally:
                    _work_active.clear()
            retry_due = (state["state"] != "WAIT_STUCK" and state.get("technical_retry", {}).get("phase") == state["state"]
                         and state.get("technical_retry", {}).get("next_at", 0) > time.time())
            if handled_command or dispatched_feedback:
                pass  # reset to IDLE; skip normal dispatch this tick
            elif retry_due:
                save_state(state)
                remaining = state["technical_retry"]["next_at"] - time.time()
                gmail_client.wait(max(0, min(config.POLL_INTERVAL_SECONDS, remaining)))
                continue
            else:
                _announce_state(state)
                _work_active.set()
                try:
                    if config.TARGET_MODE == "workspace":
                        import workspace_runner
                        workspace_runner.run_phase(state)
                    elif state["state"] in ("WAIT_REVIEW", "WAIT_MERGE"):
                        poll_pr(state)
                    elif state["state"] == "WAIT_CLEAN" and config.COMM_CHANNEL == "slack":
                        check_clean_checkout(state)
                    elif state["state"] in WAITS:
                        handle_wait(state)
                    else:
                        with performance.span(str(state.get("branch", "idle")), "phase", state["state"]):
                            PHASES[state["state"]](state)
                finally:
                    _work_active.clear()
            # Success: clear this state's consecutive-failure counter.
            if not dispatched_feedback:
                state.pop("technical_retry", None)
            if state.get("failures", {}).get(prev):
                state["failures"][prev] = 0
            if prev in PHASES and prev != "IDLE" and not dispatched_feedback and state["state"] != prev:
                state["last_effective_progress"] = time.time()
            if state["state"] != prev:
                log.info("state transition: %s -> %s", prev, state["state"])
                state["last_transition"] = time.time()
            _loop_guard(state, prev)
            _maybe_ping(state)
            save_state(state)
            if state["state"] != prev:
                selfheal_events.emit("phase_completed", state, origin=prev, task_id=attempt_task_id)
            _announce_state(state)
            if state.get("item") and state["state"] != prev:
                phase_checkpoint.retire(state, prev)
            if config.COMM_CHANNEL == "slack" and state.get("retire_thread_id"):
                gmail_client.close_thread(state["retire_thread_id"])
                state.pop("retire_thread_id")
                save_state(state)
            backoff = 1
        except turn_control.TurnKicked:
            # Discard only this tick's in-memory state. Last completed checkpoint
            # and actual files remain authoritative; retry the same phase.
            state = load_state()
            if state.get("item"):
                state["kick_pending"] = True
                state["kick_count"] = state.get("kick_count", 0) + 1
                save_state(state)
            _current_state = state
            log.info("user kicked agent turn; resuming %s without consuming failure budget",
                     state.get("state"))
            backoff = 1
            continue
        except AgentContentFilterError as error:
            _content_filter_stop(state, prev, error=error)
            backoff = 1
            gmail_client.wait(config.POLL_INTERVAL_SECONDS)
            continue
        except Exception as error:
            if state.get("item"):
                diagnostics.report(phase_checkpoint.store(), state, config.REPO_PATH,
                    config.DATA_DIR, prev, error=error)
            if _is_transient_network_error(error):
                if state.get("technical_retry", {}).get("phase") != prev:
                    state["technical_retry"] = {"attempts": 0, "phase": prev}
                retry = state["technical_retry"]
                retry["attempts"] += 1
                backoff = min(max(2 ** min(retry["attempts"] - 1, 12), getattr(error, "retry_after", None) or 0), 3600)
                retry["next_at"] = time.time() + backoff
                if retry["attempts"] >= config.MAX_STATE_FAILURES and not retry.get("notified"):
                    path = diagnostics.report(phase_checkpoint.store(), state, config.REPO_PATH,
                        config.DATA_DIR, prev, error=error)
                    email(state, "service unavailable", message_templates.operational("unavailable", state.get("task_language", "English")), [path])
                    retry["notified"] = True
                    state["stuck_return"] = prev
                    state["state"] = "WAIT_STUCK"
                    selfheal_events.incident({**state, "state": prev}, error)
                if state["state"] == "WAIT_STUCK":
                    log.warning("cycle failed in %s on a transient network error (%s: %s); "
                                "automatic retries paused after %d attempts; waiting for user reply",
                                prev, type(error).__name__, error, retry["attempts"])
                else:
                    log.warning("cycle failed in %s on a transient network error (%s: %s); "
                                "retrying in %ss", prev, type(error).__name__, error, backoff)
            elif errors.kind(error) == errors.FATAL and state.get("item") and prev != "WAIT_STUCK":
                # Retrying cannot fix a missing credential or a wrong target: ask now.
                log.error("cycle failed in %s on a fatal error (%s: %s)", prev, type(error).__name__, error)
                selfheal_events.incident({**state, "state": prev}, error)
                try:
                    _enter_stuck(state, prev, f"This step cannot continue without your help: {error}")
                except Exception:
                    log.exception("failed to report fatal error in %s", prev)
            else:
                failures = state.setdefault("failures", {})
                failures[prev] = failures.get(prev, 0) + 1
                log.exception("cycle failed in %s (%d/%d); retrying in %ss",
                              prev, failures[prev], config.MAX_STATE_FAILURES, backoff)
                if failures[prev] >= config.MAX_STATE_FAILURES and _escalate(state, prev):
                    if state["state"] != prev:
                        state["last_transition"] = time.time()
                    save_state(state)
                    selfheal_events.incident({**state, "state": prev}, error,
                                             binding=selfheal_events.state_binding(state))
                    _announce_state(state)
                    backoff = 1
                    time.sleep(config.POLL_INTERVAL_SECONDS)
                    continue
            _maybe_ping(state)  # a failing step is exactly when "is it stuck?" gets asked
            save_state(state)
            _announce_state(state)
            if _is_transient_network_error(error):
                gmail_client.wait(config.POLL_INTERVAL_SECONDS if state["state"] == "WAIT_STUCK" else backoff)
            else:
                time.sleep(backoff)
            backoff = min(backoff * 2, 3600)
            continue
        if state["state"] in WAITS or state["state"] == "IDLE":
            gmail_client.wait(min(config.POLL_INTERVAL_SECONDS, 2) if state.get("item")
                              else config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
