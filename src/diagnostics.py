"""Complete redacted incident artifacts, independent of successful evidence runs."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import traceback

import redaction


def redact(text, environ=None):
    """Incident redaction: the shared value/shape redactor plus the caller's explicit secrets.

    With ``environ`` given, secret-named entries of that mapping are stripped too. The
    default process environment is scanned conservatively (long, non-numeric values only)
    so ordinary settings such as ``..._TOKEN_BUDGET=80000`` are never redacted.
    """
    text = str(text)
    explicit = environ is not None
    env = environ if explicit else os.environ
    minimum = redaction.MIN_LENGTH if explicit else 16
    secrets = {value for key, value in env.items()
               if re.search(r"TOKEN|PASSWORD|SECRET|API_KEY|CREDENTIAL", key, re.I)
               and isinstance(value, str) and len(value) >= minimum and not value.isdigit()}
    for value in sorted(secrets, key=len, reverse=True):
        for form in {value, json.dumps(value, ensure_ascii=True)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}:
            text = text.replace(form, redaction.PLACEHOLDER)
    return redaction.redact(text)


def safe(value):
    """Redact string values before JSON escaping or presentation truncation."""
    if isinstance(value, dict):
        return {redact(k): safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe(v) for v in value]
    return redact(value) if isinstance(value, str) else value


def failure_context(store, state, repo, detail=""):
    """Facts only, including legacy checkpoint evidence; never infer a green suite."""
    import check_plan
    context = {"cause": state.get("reported_check_plan_error") or summary(detail=detail or state.get("stuck_error", "")),
               "recovery": {"recorded_attempts": state.get("check_plan_repair_round"),
                            "revision": state.get("check_plan_repair_revision"),
                            "workspace_rounds": state.get("workspace", {}).get("verification_rounds", {})},
               "verification": state.get("final_check_report", {})}
    rejected = state.get("rejected_check_plan")
    source = "recorded rejected plan"
    if not rejected and state.get("reported_check_plan_error"):
        for row in store.list("checkpoint", store.task_identity(state, repo)):
            output = row["data"].get("output", "")
            if row["data"].get("phase") == "VERIFYING" and check_plan.reported_error(output):
                rejected = check_plan.reported_payload(output)
                source = "historical checkpoint " + row["id"] + "; not a new repair attempt"
                break
    if rejected:
        context.update(rejected_check_plan=rejected, evidence_source=source)
        validation = check_plan.reported_error("CHECK_PLAN: " + rejected.removeprefix("CHECK_PLAN: "))
        if validation:
            context["cause"] = validation
        context["impact"] = "Invalid check metadata, not a failed product test. No malformed plan was executed."
    if state.get("provider_block"):
        context["provider_block"] = state["provider_block"]
    return safe(context)


def report(store, state, repo, directory, phase, *, error=None, detail="", identity=None):
    trace = "".join(traceback.format_exception(error)) if error is not None else ""
    context = failure_context(store, state, repo, detail)
    attempt = state.get("execution_attempt_id") or state.get("check_plan_repair_round") or "-"
    fields = {"task": state.get("item"), "phase": phase,
              "attempt": attempt, "detail": detail,
              "traceback": trace}
    if error is not None:
        for key in ("cmd", "returncode", "stdout", "stderr", "output", "request", "partial_response", "provider_detail"):
            value = getattr(error, key, None)
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            if value is not None:
                fields[key] = value
    fields = safe(fields)
    # Keep the incident identity independent of its presentation format.
    text = redact(f"Task: {state.get('item', '-')}\nPhase: {phase}\n"
                  f"Attempt: {attempt}\n\n{detail}")
    text += "\n\nRecorded context:\n" + json.dumps(context, ensure_ascii=False, sort_keys=True)
    for key in ("cmd", "returncode", "stdout", "stderr", "output", "request", "partial_response", "provider_detail"):
        if key in fields:
            text += "\n\n" + key + ":\n" + redact(fields[key])
    if trace:
        text += "\n\nFull exception chain:\n" + redact(trace)
    key = identity or hashlib.sha256(text.encode()).hexdigest()
    row = store.record("incident", state, repo, key, {"phase": phase}, status="recorded")
    folder = Path(directory) / "diagnostics"
    folder.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^a-zA-Z0-9_-]", "-", phase.lower())
    path = folder / f"diagnostic-{name}-{row['id'][:12]}.md"
    if not path.is_file():
        timestamp = datetime.fromtimestamp(row["created"], timezone.utc).isoformat()
        document = (f"# Diagnostic report\n\n"
                    f"- Incident: {row['id']}\n"
                    f"- Time: {timestamp}\n"
                    f"- Task: {state.get('item', '-')}\n"
                    f"- Phase: {phase}\n"
                    f"- Attempt: {attempt}\n")
        if detail:
            document += "\n## Details\n\n" + str(detail) + "\n"
        document += "\n## Cause and recorded recovery\n\n" + _code_block(json.dumps(
            {k: v for k, v in context.items() if k not in ("rejected_check_plan", "verification", "provider_block")},
            ensure_ascii=False, indent=2))
        for name, heading in (("rejected_check_plan", "Rejected CHECK_PLAN"),
                              ("verification", "Recorded verification"), ("provider_block", "Provider-blocked attempt")):
            if context.get(name):
                value = context[name]
                document += "\n## " + heading + "\n\n" + _code_block(
                    json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, dict) else value)
        for key in ("cmd", "returncode", "stdout", "stderr", "output", "request", "partial_response", "provider_detail"):
            if key in fields:
                document += f"\n## {key}\n\n" + _code_block(fields[key])
        if trace:
            document += "\n## Full exception chain\n\n" + _code_block(trace)
        path.write_text(redact(document), encoding="utf-8")
    store.update("incident", row, path=str(path), hash=hashlib.sha256(path.read_bytes()).hexdigest())
    return path


def _code_block(value):
    """Fence raw output without letting embedded backticks close the block."""
    text = str(value)
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", text)), default=0))
    return f"{fence}text\n{text}" + ("" if text.endswith("\n") else "\n") + f"{fence}\n"


def summary(error=None, detail=""):
    value = redact(str(error) if error else detail)
    try:
        structured = json.loads(value)
        if isinstance(structured, dict):
            value = next((structured[k] for k in ("reason", "message", "error", "detail")
                          if isinstance(structured.get(k), str) and structured[k].strip()),
                         "Structured failure details recorded; no concise cause was supplied.")
    except (ValueError, TypeError):
        pass
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        return "Outcome unavailable; diagnostic report recorded."
    final = next((line for line in lines if not line.startswith(("Task:", "State:", "PR:"))), lines[-1])
    if "Traceback" in value:
        final = lines[-1]
    return final if len(final) <= 240 else final[:237] + "..."
