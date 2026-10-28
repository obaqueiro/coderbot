"""Durable feedback intake and validated investigation contracts."""
import os
import time

import prompts
import handoffs
import config
from pathlib import Path


def receive(store, state, repo, message_id, text):
    return store.record("feedback", state, repo, message_id,
        {"text": text, "original_text": text, "received_at": time.time(), "phase": state.get("state"),
         "thread_id": state.get("thread_id"), "acknowledged": False})


def pending(store, state, repo):
    rows = store.list("feedback", store.task_identity(state, repo))
    def ready(row):
        parent = row["data"].get("after_product_feedback")
        if not parent:
            return True
        product = store.get("feedback", parent)
        return bool(product) and product["status"] == "complete" and state.get("state") in (
            "WAIT_MERGE", "WAIT_REVIEW", "ADDRESS_PR_THREADS", "ADDRESS_REVIEW")
    return sorted([row for row in rows if row["status"] in ("pending", "investigating")
                   and row["data"].get("retry_after", 0) <= time.time() and ready(row)],
                  key=lambda row: row["created"])


def validate(value):
    if value.get("action") not in ("answer", "correction", "replan", "question", "delivery"):
        raise ValueError("Invalid feedback assessment action")
    for key in ("answer", "reason", "references"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"Feedback assessment requires {key}")
    if value.get("action") == "delivery":
        operations = value.setdefault("operations", ["record", "convert", "upload", "pr_sync", "notify"])
        if not isinstance(operations, list) or not operations or any(
                operation not in ("record", "convert", "upload", "pr_sync", "notify") for operation in operations):
            raise ValueError("Invalid evidence delivery operations")
    return value


def attachments(value):
    """Extract actual outbox files, never arbitrary paths from answer prose."""
    raw_paths = value.get("attachments", [])
    if not isinstance(raw_paths, list):
        raise ValueError("Invalid feedback attachments")
    paths = list(raw_paths)
    if not all(isinstance(p, str) for p in paths):
        raise ValueError("Invalid feedback attachments")
    text = value.get("answer", "")
    paths.extend(line[len("ATTACH:"):].strip().strip("`") for line in text.splitlines()
                 if line.startswith("ATTACH:"))
    root = (config.DATA_DIR / "outbox").resolve()
    valid, missing = [], []
    for raw in paths:
        p = Path(raw).resolve()
        if p.is_relative_to(root) and os.path.isfile(p) and os.path.getsize(p):
            if p not in valid:
                valid.append(p)
        else:
            missing.append(raw)
    text = "\n".join(line for line in text.splitlines() if not line.startswith("ATTACH:"))
    if missing:
        text += "\nAttachments unavailable or outside authorized outbox: " + ", ".join(missing)
    return text, valid


def investigation(state, row):
    return ("Investigate this authorized user feedback against actual approved OpenSpec "
        "proposal, design, specs and implementation. Read files and inspect code as needed. "
        "Do not edit code, commit, push or merge. Answer any factual question and preserve "
        "any requirement assertion even when phrased as a question. Classify material "
        "Evidence capture/conversion/Drive publication/PR video links are existing controller "
        "delivery capabilities, not product scope changes. Return action delivery for requests "
        "limited to these operations; do not replan solely for publication. For mixed requests "
        "preserve product changes under correction/replan and return delivery_operations separately "
        "so the controller can retain the requested delivery after product changes. "
        f"Drive enabled: {config.EVIDENCE_UPLOAD}; configured folder: {bool(config.DRIVE_FOLDER_ID)}. "
        "omissions, scope/architecture changes or invalidated assumptions as replan; "
        "localized unambiguous defects within approved intent as correction. Ask only "
        "a concrete unresolved product decision.\n" + handoffs.DECISION_INSTRUCTIONS + "\n"
        f"Task: {state.get('item')}\nChange: {state.get('slug')}\n"
        f"Archive: {state.get('archive_path')}\n"
        + prompts.fenced("user feedback", row["data"]["text"])
        + '\nReturn ONLY JSON: {"action":"answer|correction|replan|question|delivery",'
        '"answer":"factual answer or concrete question", "reason":"impact reasoning",'
        '"references":"actual inspected file/requirement references",'
        '"attachments":["authorized outbox file paths"],'
        '"delivery_operations":[], '
        '"operations":["record","convert","upload","pr_sync","notify"]}.')
