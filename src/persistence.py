"""Persisted files: one atomic writer, one reader that quarantines corruption, and the
state schema (task-scoped vs global keys) that drives task reset and hold/restore."""
import json
import os
import tempfile
import threading
import time
from pathlib import Path

_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}
# (path, quarantined copy) pairs the loop has not reported to the user yet.
quarantined: list[tuple[str, str]] = []


def _lock(path: Path) -> threading.Lock:
    with _guard:
        return _locks.setdefault(os.path.abspath(path), threading.Lock())


def atomic_write(path, data, *, mode=0o644) -> None:
    """Write via a unique temp file in the same directory, fsync, then rename: readers
    see the old or the new content, never a torn file, and concurrent writers never
    share a temp name."""
    path = Path(path)
    payload = data.encode() if isinstance(data, str) else data
    with _lock(path):
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
    try:
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        pass


def write_json(path, value, **dump) -> None:
    atomic_write(path, json.dumps(value, **dump))


def read_json(path, default):
    """The parsed file, or `default` when it does not exist. A file that does not parse
    is moved aside to <name>.corrupt-<stamp> and reported (see `quarantined`) instead
    of being read as empty and then overwritten. Other read errors propagate."""
    path = Path(path)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return quarantine(path, default)


def quarantine(path, default):
    path = Path(path)
    moved = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        os.replace(path, moved)
    except OSError:
        moved = path
    quarantined.append((str(path), str(moved)))
    return default


# ---------------------------------------------------------------- state schema

# Every task-scoped key: cleared whenever a task ends (merge, DONE, abort) so the next
# pick starts from a clean slate, and saved/restored with a held task. Add new task
# state here.
TASK_KEYS = ("sent_messages", "declined_findings", "review_head", "draft_pr_url", "draft_pr_attempts", "draft_pr_ready", "loop_seen", "classification", "review_session_id", "credential_request", "credentials", "item", "item_id", "item_url", "item_key", "trail_ref", "item_detail", "item_images", "task_language", "task_language_failures", "thread_intro", "base_sha", "slug", "thread_nonce",
             "execution_task_id", "conversation_version", "conversation_flow_id", "conversation_delivery_notified",
             "recovery_id", "replan", "approved_proposal", "approved_scope_fingerprint",
             "reviewed_proposal", "reviewed_pr_snapshot", "active_feedback_id", "coverage_report",
             "check_waivers", "technical_retry", "stuck_diagnostic", "feedback_origin",
             "last_delivery", "verification_diagnostic", "proposal_delivery_pending",
             "feedback_question_origin", "remote_branch", "feedback_delivery_pending", "recovery_resume",
             "pending_decision_id", "reminded_decision", "last_effective_progress",
             "feedback_question_id",
             "isolated_merge_heads",
             "branch", "pending_question", "question_rounds", "stuck_return", "stuck_error", "failures", "session_id", "thread_id", "pr_url", "e2e_specs",
             "return_state", "review_since", "review_round", "review_run_link",
             "review_comment_watermark", "review_comments", "pr_summary", "pr_title",
             "pr_title_guidance",
             "pr_threads", "pr_thread_round", "pr_thread_notified", "pr_threads_seen",
             "bot_comment_ids", "verify_round",
             "review_gate_round", "archive_round", "archive_fix_turn", "planning_session_id", "fresh_session",
             "implement_budget", "fresh_retries", "archive_path", "e2e_repair_head",
             "e2e_repair_status", "push_context", "archive_error", "e2e_round",
             "review_threads", "await_new_run", "conflict_rounds", "conflict_return",
             "conflict_head", "stale_replies", "last_contact", "ping_count", "last_ping_at",
             "evidence_url", "milestones_announced", "banner_state", "banner_stage", "proposal_snapshot", "proposal_sent_key",
             "quality_report", "internal_review_report", "e2e_passed",
             "implementation_summary", "pending_feedback", "architecture_report",
             "kick_pending", "kick_count", "has_e2e_harness", "has_code_review", "e2e_kind",
             "execution_attempt_id", "execution_checkpoint_id", "phase_sessions",
             "session_context_tokens", "focused_check_results", "reported_check_plan", "final_check_report",
             "reported_check_plan_error", "check_plan_repair_round", "check_plan_repair_revision",
             "rejected_check_plan", "failure_context", "provider_block",
             "reviewed_snapshot", "delivered_sha", "localized_messages", "final_check_round", "final_repair_round",
             "post_review_check_round", "architecture_attempt_head", "implementation_feedback",
             "implementation_return_round", "implementation_turn", "implementation_progress",
             "implementation_no_progress", "implementation_stall_round", "implementation_stall_round_feedback",
             "implementation_attachments", "implementation_ready", "pending_decision", "check_repair", "pending_reply_delivery",
             "verification_guidance", "verification_requested_from", "verification_blocker",
             "verification_plan_recovery", "quality_snapshot", "quality_controller_validated",
             "approved_task_inventory", "verified_task_inventory", "evidence_recovery_round", "focused_scope_reconciled")
# Keys that outlive a task, with the value a missing key means.
GLOBAL_KEYS = {"state": "IDLE", "retire_thread_id": None, "slack_last_handled_id": None,
               "last_email": None, "last_transition": None, "dirty_notified": None,
               "evidence_delivery": None, "workspace": None, "workspace_path": None,
               "original_workspace": None}
# Removed state names -> where a task saved under them resumes. Old state.json and
# holds.json files keep loading after a state is deleted.
FORWARD_STATES = {"RECOVERING": "VERIFYING", "EVIDENCE_REVIEW": "VERIFYING"}
_STATE_POINTERS = ("state", "stuck_return", "return_state", "conflict_return", "recovery_resume",
                   "banner_state", "feedback_origin", "verification_requested_from")


def forward(state: dict) -> dict:
    """Every key that names a phase, not only `state`: a WAIT_STUCK task whose
    stuck_return is a deleted phase must resume somewhere real."""
    for key in _STATE_POINTERS:
        if state.get(key) in FORWARD_STATES:
            state[key] = FORWARD_STATES[state[key]]
    repair = state.get("check_repair")
    if isinstance(repair, dict) and repair.get("resume") in FORWARD_STATES:
        repair["resume"] = FORWARD_STATES[repair["resume"]]
    if isinstance(state.get("pending_decision"), dict) and state["pending_decision"].get("origin") in FORWARD_STATES:
        state["pending_decision"]["origin"] = FORWARD_STATES[state["pending_decision"]["origin"]]
    return state


def read_state(path) -> dict | None:
    """Pure read of state.json: None when absent; corruption is quarantined and the
    default (IDLE) returned; removed state names are mapped forward."""
    state = read_json(path, None)
    if state is None:
        return None
    if not isinstance(state, dict):
        return quarantine(path, {"state": GLOBAL_KEYS["state"]})
    state.setdefault("state", GLOBAL_KEYS["state"])
    return forward(state)
