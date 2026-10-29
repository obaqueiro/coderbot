"""Additive task migration and durable conversation events, owned by the FSM."""
import time


def migrate(state, store, repo):
    if not state.get("item"):
        return state
    task_id = store.task_identity(state, repo)
    state.setdefault("execution_task_id", task_id)
    state.setdefault("conversation_version", 1)
    # Never infer that old work was created by the bot from a dirty checkout alone.
    if state.get("pending_question"):
        store.record("feedback", state, repo, "legacy-question", {
            "text": state["pending_question"], "kind": "pending_question",
            "phase": state.get("return_state"), "thread_id": state.get("thread_id")},
            status="waiting")
    # Replay only durable pending transitions, never infer consent from a phase.
    pending = store.list("feedback", task_id)
    if state.get("state") not in ("WAIT_APPROVAL", "WAIT_REPLY", "WAIT_STUCK"):
        planning = next((row for row in pending if row["status"] == "planning"), None)
        repairing = next((row for row in pending if row["status"] == "repairing"), None)
        if planning and not state.get("replan"):
            import replanning
            replanning.begin(state, planning)
        elif repairing and not state.get("active_feedback_id"):
            state["active_feedback_id"] = repairing["id"]
            state["pending_feedback"] = repairing["data"]["text"]
            state["feedback_origin"] = state["state"]
            state["state"] = "APPLY_FEEDBACK"
    return state


def contact(store, state, repo, identity, thread_id, *, at=None):
    if thread_id != state.get("thread_id") or not state.get("item"):
        return
    return store.record("contact", state, repo, identity,
                        {"at": time.time() if at is None else at})


def apply_contacts(store, state, repo):
    task_id = store.task_identity(state, repo)
    rows = store.list("contact", task_id, status="pending")
    if not rows:
        return
    latest = max(row["data"]["at"] for row in rows)
    state["last_contact"] = max(state.get("last_contact", 0), latest)
    state.pop("ping_count", None)
    state.pop("last_ping_at", None)
    for row in rows:
        store.update("contact", row, status="complete")
