"""Durable lateral conversation. Workers never own the task state machine."""
import copy
import hashlib
import json
import re
import time
import threading
import logging
from pathlib import Path

import handoffs
import prompts
import redaction
from command_text import parse_command, parse_btw


INTENTS = {"answer", "conversation", "change_request", "ambiguous"}
INPUT_KINDS = {"context", "change"}
OPEN_INPUTS = {"ready", "clarifying", "waiting_approval", "processing"}
log = logging.getLogger(__name__)


def _notify(row, stage, callback):
    """Status delivery is independent of interpretation; transport failure never drops input."""
    if callback is not None:
        try:
            callback(row, stage)
        except Exception as error:
            import diagnostics
            log.warning("conversation status delivery pending: %s", diagnostics.summary(error=error))


def status_text(row, stage):
    import diagnostics
    spanish = row["data"]["snapshot"].get("task_language", "").casefold() == "spanish"
    kind = stage.split(":", 1)[0]
    attempts = row["data"].get("attempts", 0)
    classification = row["data"].get("processing_stage", "classification") == "classification"
    action = ("interpretar el mensaje" if classification else "preparar la respuesta") if spanish else (
        "classify the message" if classification else "prepare the response")
    if kind == "received":
        body = ("Recibí tu mensaje y lo guardé en cola; aún no se ha aplicado." if spanish else
                "Received and durably queued your message; it has not been applied yet.")
    elif kind == "classifying":
        body = (f"Estoy interpretando tu mensaje, intento {attempts + 1}/3." if spanish else
                f"Classifying your message, attempt {attempts + 1}/3.")
    elif kind == "responding":
        body = ("Estoy preparando la respuesta a tu mensaje." if spanish else
                "Preparing the response to your message.")
    elif kind == "queued":
        body = ("Mensaje interpretado; el despacho por el controlador está pendiente." if spanish else
                "Message classified; controller dispatch is pending.")
    else:
        cause = row["data"].get("last_error", "Outcome unavailable")
        body = (f"No pude {action}: {cause}." if spanish else f"Could not {action}: {cause}.")
        if kind == "retry":
            delay = min(2 ** attempts, 30)
            body += (f" Conservo el mensaje; reintento automático {attempts + 1}/3 en {delay}s. No lo reenvíes." if spanish else
                     f" Message retained; automatic retry {attempts + 1}/3 in {delay}s. Do not resend it.")
        else:
            body += (f" Pausé el procesamiento tras {attempts} intentos fallidos. El mensaje sigue guardado. "
                     "Consulta STATUS; cuando el servicio se recupere puedes reenviar la misma solicitud sin reformularla." if spanish else
                     f" Processing paused after {attempts} failed attempts. The message remains saved. "
                     "Use STATUS; once the service recovers you can resend the same request without rephrasing it.")
        if classification:
            body += (" No se llegó a interpretar ni aplicar esta solicitud." if spanish else
                     " This request was not classified or applied.")
    body += (" No se autorizó ninguna acción sobre la tarea por este aviso." if spanish else
             " This notice authorizes no workflow action.")
    excerpt = diagnostics.redact(row["data"]["text"])[:100].replace("\n", " ")
    return body + "\n\n" + ("Mensaje: " if spanish else "Message: ") + excerpt


def status_summary(store, state, repo):
    """STATUS exposes routing/retry progress separately from the coding phase."""
    rows = [row for row in store.list("conversation", store.task_identity(state, repo))
            if row["status"] in ("pending", "classifying", "retry", "failed", "generating", "classified")
            and row["data"]["snapshot"].get("thread_id") == state.get("thread_id")
            and not row["data"].get("superseded_by")]
    if not rows:
        return ""
    row = max(rows, key=lambda item: item["created"])
    stage = {"retry": "retry", "failed": "blocked", "classifying": "classifying",
             "generating": "responding", "classified": "queued"}.get(row["status"], "received")
    return status_text(row, stage)


def process_one(store, repo, generate, deliver, *, notify=None):
    """One worker owns lateral records; the controller alone consumes flow records."""
    with store.connection() as db:
        records = db.execute("SELECT * FROM conversation WHERE status IN "
                              "('pending','classifying','classified','generating','generated','retry','failed') ORDER BY created").fetchall()
    rows = [store._decode(r) for r in records]
    blocked_tasks = set()
    row = None
    for candidate in rows:
        if candidate["task_id"] in blocked_tasks or candidate["data"].get("route") == "flow":
            continue
        if candidate["status"] == "failed" and (not candidate["data"].get("generation_failed") or
                                                 candidate["data"].get("failure_delivered")):
            continue
        if candidate["data"].get("retry_at", 0) > time.time():
            blocked_tasks.add(candidate["task_id"])
            continue
        row = candidate
        break
    if row is None:
        return False
    try:
        if row["data"].get("generation_failed"):
            receipt = deliver(row)
            store.update("conversation", row, failure_delivered=receipt["complete"],
                         notification_id=receipt["notification_id"], retry_at=time.time() + 30)
            return True
        def remember(session_id):
            current = store.get("conversation", row["id"])
            store.update("conversation", current, session_id=session_id)
        if not row["data"].get("classification"):
            fixed = deterministic(row["data"]["snapshot"], row["data"]["original_text"])
            if fixed is None:
                import agent_runner
                row = store.update("conversation", row, status="classifying", processing_stage="classification")
                _notify(row, "classifying:" + str(row["data"].get("attempts", 0) + 1), notify)
                with agent_runner.routing():
                    result = generate(classification_prompt(row), None)
                fixed = json.loads(result.output)
            row = save_classification(store, row, fixed)
        if row["data"]["route"] == "flow":
            return True
        inputs = register_inputs(store, row, repo)
        if row["data"].get("output") is None:
            ready = [r for r in inputs if r["data"].get("message_id") == row["id"]]
            if (row["data"]["classification"]["intent"] == "change_request"
                    and ready and all(r["status"] == "ready" for r in ready)):
                spanish = row["data"]["snapshot"].get("task_language", "").casefold() == "spanish"
                row = store.update("conversation", row, status="generated", output=(
                    "OK, entendido. La instrucción quedó en cola para el flujo principal; aún no se ha aplicado."
                    if spanish else "OK, understood. The instruction is queued for the main workflow; it has not been applied yet."))
        if row["data"].get("output") is None:
            sessions = [r["data"].get("session_id") for r in store.list("conversation", row["task_id"])
                        if r["data"].get("session_id")]
            row = store.update("conversation", row, status="generating", processing_stage="response")
            result = generate(response_prompt(row, inputs), row["data"].get("session_id") or
                              (sessions[0] if sessions else None), on_session=remember)
            row = store.update("conversation", row, status="generated", output=result.output,
                               session_id=result.session_id)
        receipt = deliver(row)
        store.update("conversation", row, status="complete" if receipt["complete"] else "generated",
                      notification_id=receipt["notification_id"], last_error=receipt.get("error"),
                      retry_at=0 if receipt["complete"] else time.time() + 30)
    except Exception as error:
        from agent_errors import AgentContentFilterError
        import diagnostics
        import subprocess
        row = store.get("conversation", row["id"])
        attempts = row["data"].get("attempts", 0) + 1
        if row["data"].get("output") is not None:
            status = "generated"
        else:
            status = "failed" if isinstance(error, AgentContentFilterError) or attempts >= 3 else "retry"
        cause = diagnostics.summary(error=error)
        if isinstance(error, subprocess.TimeoutExpired):
            cause = f"execution deadline exceeded ({error.timeout:.2f}s limit)"
            if getattr(error, "elapsed", None) is not None:
                cause += f"; elapsed {error.elapsed:.2f}s"
        row = store.update("conversation", row, status=status, attempts=attempts,
                           retry_at=time.time() + min(2 ** attempts, 30),
                           last_error=f"{type(error).__name__}: {cause}")
        if status == "retry":
            _notify(row, "retry:" + str(attempts), notify)
        if status == "failed":
            spanish = row["data"]["snapshot"].get("task_language", "").casefold() == "spanish"
            notice = status_text(row, "blocked")
            if isinstance(error, AgentContentFilterError):
                import diagnostics
                view = row["data"]["snapshot"]
                block = diagnostics.safe({"source": "user reply", "phase": "parallel conversation",
                    "user_text": row["data"]["original_text"], "request": error.request,
                    "partial_response": error.partial_response, "provider_detail": error.provider_detail})
                view = {**view, "provider_block": block}
                notice = handoffs.provider_notice(block, view.get("task_language", "English"))
                notice += "\n\n" + ("La tarea y la decisión pendiente no cambiaron." if spanish else
                                        "The task and pending decision are unchanged.")
                row = store.update("conversation", row, snapshot=view)
            current = store.update("conversation", row, output=notice, generation_failed=True)
            try:
                receipt = deliver(current)
                store.update("conversation", current, notification_id=receipt["notification_id"],
                             failure_delivered=receipt["complete"])
            except Exception:
                log.exception("could not deliver lateral failure notice")
        log.exception("lateral conversation failed; work is unchanged")
    return True


class Worker:
    def __init__(self, store_factory, repo, generate, deliver, *, notify=None):
        self.store_factory, self.repo = store_factory, repo
        self.generate, self.deliver = generate, deliver
        self.notify = notify
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self.run, name="task-conversation", daemon=True)

    def run(self):
        while not self.stop.is_set():
            try:
                processed = process_one(self.store_factory(), self.repo, self.generate, self.deliver, notify=self.notify)
            except Exception:
                log.exception("conversation worker failed")
                processed = False
            self.wake.wait(.2 if processed else 2)
            self.wake.clear()

    def close(self):
        self.stop.set()
        self.wake.set()
        import turn_control
        turn_control.request_kick("conversation")


def flow_pending(store, state, repo):
    return sorted([r for r in store.list("conversation", store.task_identity(state, repo))
                   if r["status"] == "classified" and r["data"].get("route") == "flow"],
                  key=lambda r: r["created"])


def delivery(store, row, repo, directory, backend, *, notice=None):
    import message_delivery
    view = row["data"]["snapshot"]
    files = []
    if notice is None and view.get("provider_block"):
        import diagnostics
        files.append(diagnostics.report(store, view, repo, directory, "parallel conversation",
            detail=row["data"]["output"], identity="conversation-failure:" + row["id"]))
    return message_delivery.deliver(store, view, repo, directory, backend,
        "Task conversation", status_text(row, notice) if notice else row["data"]["output"], view.get("thread_id"), files,
        identity=("conversation-status:" + row["id"] + ":" + notice) if notice else "conversation:" + row["id"])


def _safe(value):
    """Bound model context and redact common credential forms, not original receipts."""
    if isinstance(value, dict):
        return {k: _safe(v) for k, v in value.items()
                if not re.search(r"secret|password|token|credential|api_key", k, re.I)}
    if isinstance(value, list):
        return [_safe(v) for v in value[:20]]
    if isinstance(value, str):
        value = redaction.redact(value)
        value = re.sub(r"(?i)(bearer\s+|(?:api[_-]?key|password|secret)\s*[:=]\s*)\S+",
                        r"\1[redacted]", value)
        return value[:5000]
    return value


def decision_version(state):
    values = [state.get(k) for k in ("state", "pending_question", "pending_decision",
                                    "return_state", "stuck_return", "stuck_error",
                                    "reviewed_proposal", "proposal_sent_key", "pr_url")]
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def snapshot(store, state, repo):
    keys = ("item", "item_detail", "item_id", "branch", "slug", "state", "thread_id",
            "execution_task_id", "task_language", "pending_question", "pending_decision",
            "return_state", "stuck_return", "stuck_error", "implementation_summary",
            "verification_blocker", "final_check_report", "internal_review_report",
            "archive_path", "pr_url", "execution_checkpoint_id", "workspace_path",
            "last_effective_progress", "quality_report", "proposal_snapshot")
    view = _safe(copy.deepcopy({k: state[k] for k in keys if k in state}))
    if state.get("stuck_error") or state.get("reported_check_plan_error") or state.get("provider_block"):
        import diagnostics
        view["failure_context"] = _safe(copy.deepcopy(state.get("failure_context") or
            diagnostics.failure_context(store, state, repo)))
    view["execution_task_id"] = store.task_identity(state, repo)
    view["decision_version"] = decision_version(state)
    view["captured_at"] = time.time()
    view["repo"] = str(repo)
    # The proposal review package is frozen outside the live checkout. Never read
    # files which the working session may currently be editing as verified facts.
    view["proposal"] = {}
    if state.get("proposal_snapshot"):
        try:
            path = Path(state["proposal_snapshot"])
            if path.stat().st_size <= 1024 * 1024:
                contents = json.loads(path.read_text())
                if isinstance(contents, dict):
                    view["proposal"] = _safe({k: v for k, v in contents.items()
                                              if k in {"proposal.md", "design.md", "tasks.md"}})
        except (OSError, ValueError):
            pass
    history = store.list("conversation", view["execution_task_id"])
    view["history"] = _safe([{"id": r["id"], "user": r["data"]["text"],
                              "bot": r["data"].get("output", "")}
                             for r in sorted(history, key=lambda r: r["created"])[-6:]])
    view["inputs"] = _safe([{"id": r["id"], "text": r["data"]["text"],
                             "kind": r["data"]["kind"], "status": r["status"],
                             "question": r["data"].get("question")}
                             for r in store.list("conversation_input", view["execution_task_id"])[:20]])
    return view


def receive(store, state, repo, message_id, text):
    row = store.record("conversation", state, repo, message_id, {
        "original_text": text, "text": parse_btw(text) if parse_btw(text) is not None else text,
        "explicit_btw": parse_btw(text) is not None, "snapshot": snapshot(store, state, repo),
        "route": None, "session_id": None, "classification": None,
        "output": None, "notification_id": None})
    # A new message can reformulate a permanently rejected one. Keep the old
    # receipt, but do not let its unknown intent block all future delivery.
    for old in store.list("conversation", row["task_id"], status="failed"):
        if old["created"] < row["created"] and old["data"].get("generation_failed"):
            store.update("conversation", old, superseded_by=row["id"])
    return row


def validate(value):
    if not isinstance(value, dict) or value.get("intent") not in INTENTS:
        raise ValueError("Invalid conversation intent")
    if type(value.get("resolves_pending_question")) is not bool:
        raise ValueError("Conversation classification requires a resolution boolean")
    if type(value.get("action_depends_on_change", False)) is not bool:
        raise ValueError("Invalid action dependency")
    inputs = value.get("inputs", [])
    if not isinstance(inputs, list) or len(inputs) > 10:
        raise ValueError("Invalid conversation inputs")
    for item in inputs:
        if (not isinstance(item, dict) or item.get("kind") not in INPUT_KINDS
                or not isinstance(item.get("text"), str) or not item["text"].strip()
                or type(item.get("needs_clarification", False)) is not bool):
            raise ValueError("Invalid conversation input")
        for key in ("replaces", "clarifies"):
            if item.get(key) is not None and not isinstance(item[key], str):
                raise ValueError("Invalid input reference")
    action = value.get("requested_action")
    if action is not None and not isinstance(action, str):
        raise ValueError("Invalid requested action")
    return value


def classification_snapshot(view):
    """Routing needs the current decision and input references, not reports or history."""
    result = _safe({k: view[k] for k in ("item", "slug", "state", "task_language", "return_state",
        "stuck_return", "stuck_error", "pr_url", "decision_version") if k in view})
    result = {k: v[:256] if isinstance(v, str) else v for k, v in result.items()}
    incomplete = False
    def clip(text, limit):
        nonlocal incomplete
        text = _safe(text)
        if len(text) > limit:
            incomplete = True
        return text[:limit]
    result["pending_question"] = clip(view.get("pending_question") or "", 1200)
    decision = view.get("pending_decision") or {}
    result["pending_decision"] = {k: decision[k] for k in ("binary", "wait_state", "origin") if k in decision}
    result["pending_decision"]["question"] = clip(decision.get("question", ""), 800)
    options = decision.get("options", [])
    incomplete |= len(options) > 4
    result["pending_decision"]["options"] = [{"label": clip(option.get("label", ""), 600),
        "wait": option.get("wait", False), "details": option.get("details", False)} for option in options[:4]]
    result["inputs"] = [{k: v[:240] if isinstance(v, str) else v for k, v in item.items()
        if k in ("id", "kind", "status", "question", "text")} for item in _safe(view.get("inputs", []))[:6]]
    result["routing_context_incomplete"] = incomplete
    return _safe(result)


def classification_prompt(row):
    return ("Classify an authorized task-thread message BEFORE any action. No tools. "
            "Use the entire message, negations, conditions and displayed decision. "
            "An explanatory question does not resolve a pending decision. A vague 'continue' "
            "during recovery is ambiguous, not retry. New requirements are change_request, "
            "not approval. Preserve context and changes even in mixed messages. A reply to "
            "a lateral clarification references the input ID, not the workflow question. "
            "Only use replaces/clarifies for an unambiguous existing pending input ID. "
            "If an action depends on a change, do not authorize the action. /btw is always "
             "lateral and cannot authorize workflow actions. Without a pending decision, "
             "non-command messages are conversation/context/change, not answers. "
             "Explicit instructions must not be classified as mere conversation: populate "
             "requested_action for workflow actions and inputs of kind change for requests "
             "to modify, verify, generate or deliver work. They will be queued for the controller. "
             "Do not infer an action from a question, negation or hypothetical discussion. "
             "If routing_context_incomplete is true, do not resolve or authorize a workflow decision; ask for clarification. "
            "Return ONLY JSON: {\"intent\":\"answer|conversation|change_request|ambiguous\","
            "\"resolves_pending_question\":false,\"requested_action\":null,"
            "\"action_depends_on_change\":false,\"inputs\":[{\"kind\":\"context|change\","
            "\"text\":\"exact user assertion\",\"needs_clarification\":false,"
            "\"replaces\":null,\"clarifies\":null}]}. Do not invent user assertions.\n"
            + prompts.fenced("routing snapshot", json.dumps(classification_snapshot(row["data"]["snapshot"]), ensure_ascii=False))
            + prompts.fenced("explicit btw", str(row["data"]["explicit_btw"]))
            + prompts.fenced("user message", _safe(row["data"]["text"])))


def deterministic(state, text):
    if parse_btw(text) is not None:
        return None
    if deferred_pr_reply(state, text):
        return {"intent": "answer", "resolves_pending_question": True, "inputs": []}
    if parse_command(text) or re.fullmatch(r"(?i)\s*(retry|abort|complete|merge(?: anyway)?)\s*[.!]?\s*", text):
        return {"intent": "answer", "resolves_pending_question": True, "inputs": []}
    selection = handoffs.selected_reply(state, text)
    if selection is not None and not selection.get("prose"):
        return {"intent": "answer", "resolves_pending_question": True, "inputs": []}
    return None


def pr_decision_key(state):
    """Stable across repair phases, never across another displayed PR decision."""
    decision = state.get("pending_decision", {})
    if decision.get("wait_state") != "WAIT_MERGE" or decision.get("task") != state.get("slug") or not state.get("pr_url"):
        return None
    return hashlib.sha256(json.dumps([state["pr_url"], decision], sort_keys=True).encode()).hexdigest()


def deferred_pr_reply(view, text):
    if parse_btw(text) is not None or not pr_decision_key(view):
        return False
    if view.get("state") not in ("ADDRESS_PR_THREADS", "ADDRESS_REVIEW", "PUSHING", "WAIT_REVIEW"):
        return False
    if re.fullmatch(r"(?i)\s*merge(?: anyway)?[.!]?\s*", text):
        return True
    selection = handoffs.selected_reply({**view, "state": "WAIT_MERGE"}, text)
    return bool(selection and not selection.get("prose") and not selection.get("error"))


def save_classification(store, row, value):
    value = validate(value)
    if classification_snapshot(row["data"]["snapshot"])["routing_context_incomplete"]:
        value = {**value, "intent": "ambiguous", "resolves_pending_question": False, "requested_action": None}
        return store.update("conversation", row, status="classified", route="lateral", classification=value)
    if row["data"]["explicit_btw"]:
        value = {**value, "intent": "change_request" if value.get("inputs") else "conversation",
                 "resolves_pending_question": False, "requested_action": None}
    if value["intent"] == "change_request" and not value.get("inputs"):
        value = {**value, "intent": "ambiguous", "resolves_pending_question": False}
    view = row["data"]["snapshot"]
    # Operational instructions are controller work, even when captured between
    # waits. Bind an early merge to this PR, not to whichever PR exists later.
    merge = (re.fullmatch(r"(?i)\s*(merge(?: anyway)?|fusiona(?: de todos modos)?)[.!]?\s*",
                         row["data"]["original_text"]) or value.get("requested_action") == "merge")
    if (merge and view.get("pr_url") and not row["data"]["explicit_btw"]
            and not value.get("inputs") and not value.get("action_depends_on_change")
            and len(row["data"]["text"]) <= 5000):
        return store.update("conversation", row, status="classified", route="flow",
            classification={**value, "intent": "answer", "resolves_pending_question": True},
            queued_pr_url=view["pr_url"], deferred_pr_decision=pr_decision_key(view))
    if (value.get("requested_action") and not value.get("inputs") and not row["data"]["explicit_btw"]
            and not (value["intent"] == "answer" and value["resolves_pending_question"]
                     and view.get("state") in {"WAIT_REPLY", "WAIT_STUCK", "WAIT_APPROVAL", "WAIT_MERGE", "WAIT_CLEAN"})):
        # Preserve instructions the classifier identified instead of answering
        # that the lateral assistant cannot act. Feedback assesses their meaning.
        value = {**value, "intent": "change_request", "resolves_pending_question": False,
                 "inputs": [{"kind": "change", "text": row["data"]["text"]}]}
    if deferred_pr_reply(view, row["data"]["original_text"]) and not value.get("inputs") and not value.get("action_depends_on_change"):
        return store.update("conversation", row, status="classified", route="flow",
            classification={"intent": "answer", "resolves_pending_question": True, "inputs": []},
            deferred_pr_decision=pr_decision_key(view))
    if view.get("state") not in {"WAIT_REPLY", "WAIT_STUCK", "WAIT_APPROVAL", "WAIT_MERGE", "WAIT_CLEAN"}:
        value = {**value, "intent": "change_request" if value.get("inputs") else "conversation",
                 "resolves_pending_question": False}
    if len(row["data"]["text"]) > 5000:
        value = {**value, "intent": "ambiguous", "resolves_pending_question": False,
                 "requested_action": None, "inputs": []}
    route = "flow" if (value["intent"] == "answer" and value["resolves_pending_question"]
                        and not value.get("action_depends_on_change") and not value.get("inputs")) else "lateral"
    if value["intent"] == "answer" and route == "lateral":
        value = {**value, "intent": "ambiguous", "resolves_pending_question": False}
    return store.update("conversation", row, status="classified", classification=value, route=route)


def context_prompt(store, state, repo, invocation=None):
    rows = [r for r in store.list("conversation_input", store.task_identity(state, repo))
            if r["data"]["kind"] == "context" and
            (r["status"] == "ready" or r["status"] == "incorporated" and invocation is not None and
             r["data"].get("invocation") == invocation)]
    rows.sort(key=lambda r: r["created"])
    if not rows:
        return "", []
    if invocation is not None:
        rows = [store.update("conversation_input", r, invocation=invocation) for r in rows]
    return ("\nUser context received since the previous turn. Incorporate within current phase "
            "and approved scope; this is not authorization to expand scope or bypass checks.\n"
            + prompts.fenced("user context", json.dumps([
                {"id": r["id"], "text": r["data"]["text"]} for r in rows], ensure_ascii=False))), rows


def incorporated(store, rows, checkpoint):
    for row in rows:
        current = store.get("conversation_input", row["id"])
        if current["status"] == "ready":
            store.update("conversation_input", current, status="incorporated", checkpoint=checkpoint)


def transfer_changes(store, state, repo):
    import feedback
    rows = store.list("conversation_input", store.task_identity(state, repo))
    changed = False
    for row in rows:
        if row["status"] == "ready" and row["data"]["kind"] == "change":
            event = feedback.receive(store, state, repo, "conversation:" + row["id"], row["data"]["text"])
            store.update("feedback", event, conversation_input_id=row["id"])
            store.update("conversation_input", row, status="processing", feedback_id=event["id"])
            changed = True
        elif row["status"] == "replaced" and row["data"].get("feedback_id"):
            event = store.get("feedback", row["data"]["feedback_id"])
            if event and event["status"] in {"pending", "investigating"}:
                store.update("feedback", event, status="complete", outcome="replaced")
    return changed


def sync_inputs(store, state, repo):
    """Expose controller outcomes without treating a queued request as applied."""
    for row in store.list("conversation_input", store.task_identity(state, repo)):
        if row["status"] not in {"processing", "waiting_approval"} or not row["data"].get("feedback_id"):
            continue
        event = store.get("feedback", row["data"]["feedback_id"])
        if not event:
            continue
        action = event["data"].get("assessment", {}).get("action")
        if event["status"] == "planning" or action == "replan" and not state.get("approved_proposal"):
            store.update("conversation_input", row, status="waiting_approval")
        elif action == "replan" and state.get("approved_proposal"):
            store.update("conversation_input", row, status="incorporated", approved=True)
        elif event["status"] == "verifying" and state.get("state") in {"OPEN_PR", "WAIT_REVIEW", "WAIT_MERGE"}:
            store.update("conversation_input", row, status="applied", outcome="corrected; verification reached delivery")
        elif event["status"] == "complete":
            outcome = "applied" if action == "correction" else "incorporated"
            store.update("conversation_input", row, status=outcome, outcome=event["data"].get("outcome"))


def delivery_blockers(store, state, repo):
    """Unclassified receipts cannot silently race an irreversible handoff."""
    task = store.task_identity(state, repo)
    pending = [r for r in store.list("conversation", task)
               if r["status"] in {"pending", "classifying", "generating", "failed", "retry", "classified"}
               and not r["data"].get("superseded_by")
               and (not r["data"].get("classification") or
                    r["data"].get("classification", {}).get("inputs") and
                    not store.list("conversation_input", task, identity=r["id"] + ":0"))]
    pending += [r for r in store.list("conversation_input", task)
                if r["data"]["kind"] == "change" and r["status"] in OPEN_INPUTS]
    return pending


def register_inputs(store, row, repo):
    state = row["data"]["snapshot"]
    for index, value in enumerate(row["data"]["classification"].get("inputs", [])):
        identity = row["id"] + ":" + str(index)
        existing = store.list("conversation_input", row["task_id"], identity=identity)
        if existing:
            reference = existing[0]["data"].get("reference")
            if reference and existing[0]["status"] == "ready":
                old = store.get("conversation_input", reference)
                if old and old["task_id"] == row["task_id"] and old["status"] in {"ready", "clarifying"}:
                    store.update("conversation_input", old, status="replaced", replaced_by=existing[0]["id"])
            continue  # Do not replay a replacement or duplicate clarification.
        reference = value.get("replaces") or value.get("clarifies")
        old = store.get("conversation_input", reference) if reference else None
        valid = old and old["task_id"] == row["task_id"] and old["status"] in {"ready", "clarifying"}
        if old and old["status"] == "processing" and old["data"].get("feedback_id"):
            event = store.get("feedback", old["data"]["feedback_id"])
            valid = old["task_id"] == row["task_id"] and event and event["status"] in {"pending", "waiting"}
        status = "clarifying" if value.get("needs_clarification") or reference and not valid else "ready"
        kind = old["data"]["kind"] if valid and value.get("clarifies") else value["kind"]
        text = (old["data"]["text"] + "\nUser clarification: " + value["text"]
                if valid and value.get("clarifies") else value["text"])
        item = store.record("conversation_input", state, repo, identity,
                            {"text": text, "original_text": value["text"], "kind": kind,
                             "message_id": row["id"], "reference": reference,
                             "snapshot": state, "feedback_id": None}, status=status)
        if valid:
            store.update("conversation_input", old, status="replaced", replaced_by=item["id"])
            if old["data"].get("feedback_id"):
                event = store.get("feedback", old["data"]["feedback_id"])
                if event and event["status"] in {"pending", "waiting"}:
                    store.update("feedback", event, status="complete", outcome="replaced by clarification")
    return store.list("conversation_input", row["task_id"])


def response_prompt(row, inputs):
    return ("You are the task's lateral conversation assistant, not its coding agent/controller. "
            "Answer the user's question clearly using ONLY supplied confirmed context. No tools. "
            "Explain the blocker and alternatives if requested. Do not claim unverified work, "
            "approve changes, retry, cancel, merge or resolve the workflow decision. "
            "Existing workflow questions remain pending. Ask a lateral clarification only when "
             "needed; identify its request ID. Describe registered changes as received, NOT applied. "
             "Instructions are forwarded to the controller, not rejected because this chat cannot act. "
            "Material scope changes require proposal review and explicit approval; context is "
            "incorporated at the next safe point. If a change invalidates current work, offer "
            "pause/reorientation but do not interrupt. If intent is ambiguous, ask what action "
            "is intended rather than guessing. Use the task language. Do not expose secrets.\n"
            + prompts.fenced("task snapshot", json.dumps(row["data"]["snapshot"], ensure_ascii=False))
            + prompts.fenced("classification", json.dumps(row["data"]["classification"], ensure_ascii=False))
            + prompts.fenced("registered requests", json.dumps(_safe([
                {"id": r["id"], "text": r["data"]["text"], "status": r["status"]}
                for r in inputs[:20]]), ensure_ascii=False))
            + prompts.fenced("user message", _safe(row["data"]["text"])))
