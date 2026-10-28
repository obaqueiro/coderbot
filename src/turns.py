"""The structured result of a working agent turn.

The agent returns ONE JSON object (Claude Code enforces RESULT_SCHEMA via
`--json-schema`; OpenCode is asked for a fenced JSON block, because its json_schema
output format breaks message reads up to 1.19.5, docs/runtime-checks.md C10). The
controller validates it and renders canonical control lines (NEED_USER_INPUT, ATTACH,
CHECK_PLAN, QUALITY_GATE, ...) for the existing parsers, so a model never has to get a
free-text sentinel's exact format right. A turn without a valid object falls back to
its raw text, i.e. today's behaviour.
"""
import json
import re

STATUSES = ("done", "done_with_concerns", "needs_input", "needs_approval", "blocked")
_STRINGS = {"type": "array", "items": {"type": "string"}}

RESULT_SCHEMA = {
    "type": "object",
    "required": ["status", "summary"],
    "properties": {
        "status": {"type": "string", "enum": list(STATUSES)},
        "summary": {"type": "string"},
        "question": {"type": "object", "required": ["text"], "properties": {
            "text": {"type": "string"}, "options": {**_STRINGS, "maxItems": 4}}},
        "concerns": _STRINGS,
        "attachments": _STRINGS,
        "checks": {"type": "array", "items": {"type": "object"}},
        "e2e_specs": _STRINGS,
        "credential": {"type": "object", "required": ["env", "service", "purpose"], "properties": {
            "env": {"type": "string"}, "service": {"type": "string"}, "purpose": {"type": "string"}}},
        "classification": {"type": "string", "enum": ["spike", "bounded", "architectural"]},
        "language": {"type": "string"},
        "pr_title": {"type": "string"},
        "verification": {"type": "object", "required": ["status", "commands", "tasks"], "properties": {
            "status": {"type": "string", "enum": ["pass", "fail"]},
            "commands": _STRINGS, "preexisting": _STRINGS,
            "openspec": {"type": "string", "enum": ["pass", "fail"]},
            "tasks": {"type": "string"}, "deferred": {"type": "integer"}}},
        "review": {"type": "object", "required": ["critical", "important"], "properties": {
            "critical": {"type": "integer"}, "important": {"type": "integer"},
            "minor": {"type": "integer"}, "tests": _STRINGS, "findings": _STRINGS}},
        "threads": {"type": "array", "items": {"type": "object", "required": ["id", "action", "reply"], "properties": {
            "id": {"type": "string"}, "action": {"type": "string", "enum": ["fixed", "answered", "declined"]},
            "reply": {"type": "string"}}}},
    },
}
SCHEMA_JSON = json.dumps(RESULT_SCHEMA, separators=(",", ":"))

# Field -> the control line it replaces. A marker line the model typed itself into the
# summary is dropped when the structured field exists, so each line appears once.
MARKERS = {"attachments": "ATTACH:", "e2e_specs": "E2E_SPEC:", "checks": "CHECK_PLAN:",
           "credential": "CREDENTIAL_REQUEST:", "threads": "RESOLVE:",
           "verification": "QUALITY_GATE:", "review": "INTERNAL_REVIEW:", "question": "NEED_USER_INPUT:"}

PROTOCOL = """End every turn with the result object (JSON):
- status: done | done_with_concerns | needs_input (one question for the user) | needs_approval | blocked (cannot proceed; say why).
- summary: what you did and found, for the human, in the task language. Plain prose.
- question: {"text": ..., "options": [2-4 short choices, recommended first]} only with needs_input/needs_approval.
- attachments: absolute paths of evidence files you saved under the outbox.
- checks: focused checks for coderbot to run, each {"id", "argv": [...], "cwd", "scope": "focused", "inputs": [...]}; add "kind": "e2e" and "reporter" for E2E.
- Fill the phase's extra field when the card asks for it (verification, review, threads, classification, pr_title)."""


def valid(data) -> bool:
    """The minimal contract the controller relies on; unknown keys are ignored."""
    if not isinstance(data, dict) or data.get("status") not in STATUSES or not isinstance(data.get("summary"), str):
        return False
    question = data.get("question")
    return question is None or (isinstance(question, dict) and isinstance(question.get("text"), str))


def extract(text: str):
    """The last JSON object in `text` that is a valid result (fenced or bare), else None."""
    if not isinstance(text, str):
        return None
    decoder = json.JSONDecoder()
    found = None
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if valid(value):
            found = value
    return found


def _clean_summary(summary: str, data: dict) -> str:
    drop = tuple(marker for field, marker in MARKERS.items() if data.get(field))
    lines = [line for line in summary.splitlines() if not (drop and line.lstrip().startswith(drop))]
    return "\n".join(lines).strip()


def render(data: dict) -> str:
    """Canonical control text for a validated result (see the module docstring)."""
    lines = [_clean_summary(data["summary"], data)]
    if data.get("concerns"):
        lines.append("Concerns:\n" + "\n".join(f"- {c}" for c in data["concerns"]))
    lines += [f"ATTACH: {path}" for path in data.get("attachments") or []]
    lines += [f"E2E_SPEC: {spec}" for spec in data.get("e2e_specs") or []]
    if data.get("checks"):
        lines.append("CHECK_PLAN: " + json.dumps({"version": 1, "checks": data["checks"]}, ensure_ascii=False))
    if data.get("credential"):
        lines.append("CREDENTIAL_REQUEST: " + json.dumps(data["credential"], ensure_ascii=False))
    for thread in data.get("threads") or []:
        if isinstance(thread, dict) and thread.get("id"):
            reply = " ".join(str(thread.get("reply", "")).split())
            lines.append(f"RESOLVE: {thread['id']} {thread.get('action', 'answered')}: {reply}")
    question = data.get("question") if data["status"] in ("needs_input", "needs_approval") else None
    if question and str(question.get("text", "")).strip():
        options = [o for o in question.get("options") or [] if str(o).strip()]
        lines.append("NEED_USER_INPUT: " + question["text"].strip()
                     + "".join(f"\n{i}. {o}" for i, o in enumerate(options, 1)))
    elif isinstance(data.get("verification"), dict):
        gate = data["verification"]
        contract = {"status": gate.get("status"), "commands": gate.get("commands") or [],
                    "preexisting": gate.get("preexisting") or [], "openspec": gate.get("openspec", "pass"),
                    "tasks": gate.get("tasks", "")}
        if "deferred" in gate:
            contract["deferred"] = gate["deferred"]
        lines.append("QUALITY_GATE: " + json.dumps(contract, ensure_ascii=False))
    elif isinstance(data.get("review"), dict):
        review = data["review"]
        critical, important = int(review.get("critical") or 0), int(review.get("important") or 0)
        lines.append("INTERNAL_REVIEW: " + json.dumps(
            {"status": "pass" if not critical and not important else "fail", "critical": critical,
             "important": important, "tests": review.get("tests") or []}, ensure_ascii=False))
    return "\n".join(line for line in lines if line)


def finalize(output: str, structured=None):
    """(rendered output, data) for a working turn: the CLI's structured result, else a
    result object found in the text, else the raw text unchanged (data None)."""
    data = structured if valid(structured) else extract(output)
    return (render(data), data) if data else (output, None)


# Phases whose controller step reads a structured field (gate verdict, review counts,
# per-thread actions). Elsewhere a prose-only reply is today's normal output, so a
# missing object is not worth an extra repair call.
STRUCTURED_PHASES = {"VERIFYING", "INTERNAL_REVIEW", "EVIDENCE_REVIEW", "ADDRESS_REVIEW", "ADDRESS_PR_THREADS"}


def needs_result(phase) -> bool:
    return phase in STRUCTURED_PHASES


REPAIR_PROMPT = ("Your last message did not end with the result object. Reply now with ONLY that JSON "
                 "object for the turn you just completed (status, summary and any fields the card asked for). "
                 "Do not run tools or change files.")
