"""Jira Cloud REST v3 backlog backend.

All task mutations address a stable Jira issue ID; search results are used to
discover work, never as the authoritative owner after a write.
"""
import base64
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import config
import errors
import ownership
import safe_download
import task_source
from task_text import normalize, priority_of

log = logging.getLogger(__name__)

_TIMEOUT = 60
IMAGES_DIR = config.DATA_DIR / "jira_images"


def _request(method: str, path: str, payload: dict | None = None) -> dict | list:
    """Call Jira Cloud v3, returning JSON (or {} for no-content responses)."""
    credentials = base64.b64encode(
        f"{config.JIRA_EMAIL}:{config.JIRA_API_TOKEN}".encode()).decode()
    url = f"{config.JIRA_URL}/rest/api/3{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Basic {credentials}", "Accept": "application/json",
        "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                raw = response.read()
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:500]
            try:
                delay = min(30, max(1, int(error.headers.get("Retry-After", "1"))))
            except (ValueError, TypeError, AttributeError):
                delay = 1
            if error.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(delay)
                continue
            raise errors.from_http(error.code, f"Jira {method} {path}: HTTP {error.code}: {detail}",
                                   resource="jira", retry_after=delay) from error
        except urllib.error.URLError as error:
            raise errors.Transient(f"Jira {method} {path}: connection failed: {error.reason}",
                                   code="jira_unreachable", resource="jira") from error
    raise errors.Transient(f"Jira {method} {path}: retry budget exhausted", resource="jira")


def _jql_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _search(jql: str, fields: list[str] | None = None) -> list[dict]:
    """Enhanced paginated JQL search; /search is being removed by Atlassian."""
    results, cursor = [], None
    while True:
        body = {"jql": jql, "fields": fields or ["summary", "description", "status",
                                               "labels", "attachment"], "maxResults": 100}
        if cursor:
            body["nextPageToken"] = cursor
        page = _request("POST", "/search/jql", body)
        if not isinstance(page, dict):
            raise RuntimeError("Jira /search/jql returned a non-object response")
        results.extend(task_source.require_list(page, "issues", "Jira /search/jql"))
        next_cursor = page.get("nextPageToken")
        if page.get("isLast", not next_cursor) or not next_cursor:
            return results
        if cursor == next_cursor:
            raise RuntimeError("Jira /search/jql repeated its pagination token")
        cursor = next_cursor


def adf_text(node: dict | str | None) -> str:
    """Render Jira's ADF into readable text for the agent, retaining links/lists."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""

    def walk(value: dict, depth: int = 0) -> str:
        kind = value.get("type")
        if kind == "text":
            text = value.get("text", "")
            for mark in value.get("marks", []):
                if mark.get("type") == "link" and (url := mark.get("attrs", {}).get("href")):
                    text += f" ({url})"
            return text
        if kind == "inlineCard":
            return value.get("attrs", {}).get("url", "")
        if kind == "hardBreak":
            return "\n"
        if kind == "media":
            return ""  # attachments are downloaded separately
        parts = [walk(child, depth + (kind in ("bulletList", "orderedList")))
                 for child in value.get("content", []) if isinstance(child, dict)]
        if kind in ("bulletList", "orderedList"):
            return "\n".join(f"{'  ' * depth}{i + 1 if kind == 'orderedList' else '-'}"
                             f"{'.' if kind == 'orderedList' else ''} {part.strip()}"
                             for i, part in enumerate(parts) if part.strip())
        if kind in ("doc", "listItem"):
            return "\n".join(part for part in parts if part)
        if kind in ("paragraph", "heading", "blockquote", "codeBlock"):
            return "".join(parts) + "\n"
        return "".join(parts)

    return walk(node).strip()


def _download_images(issue: dict) -> list[str]:
    """Best-effort download of image attachments visible on this Jira issue."""
    images = []
    for attachment in issue.get("fields", {}).get("attachment") or []:
        if not attachment.get("mimeType", "").startswith("image/"):
            continue
        url = attachment.get("content", "")
        if urllib.parse.urlparse(url).netloc != urllib.parse.urlparse(config.JIRA_URL).netloc:
            log.warning("skipping attachment outside configured Jira site: %s", url)
            continue
        name = re.sub(r"[^a-zA-Z0-9_.-]", "_", attachment.get("filename", "image"))
        dest = IMAGES_DIR / f"{attachment.get('id', 'image')}-{name}"
        try:
            token = base64.b64encode(f"{config.JIRA_EMAIL}:{config.JIRA_API_TOKEN}".encode()).decode()
            # Basic auth goes only to the configured Jira site; a redirect to a media CDN
            # (signed URL) continues without it.
            data = safe_download.download(
                url, auth_hosts=(urllib.parse.urlparse(config.JIRA_URL).netloc,),
                authorization=f"Basic {token}", timeout=_TIMEOUT, content_type_prefix="image/")
            IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            images.append(str(dest))
        except (OSError, ValueError) as error:
            log.warning("could not download Jira attachment %s: %s", name, error)
    return images


def claim_label() -> str:
    return f"codebot-claim-{config.INSTANCE_ID}-{config.INSTANCE_FINGERPRINT}"


def hold_label() -> str:
    return f"codebot-hold-{config.INSTANCE_ID}-{config.INSTANCE_FINGERPRINT}"


def _owner_labels(issue: dict) -> set[str]:
    return {label for label in issue.get("fields", {}).get("labels", [])
            if label.startswith(("codebot-claim-", "codebot-hold-"))}


def _issue(item_id: str) -> dict:
    result = _request("GET", f"/issue/{urllib.parse.quote(str(item_id), safe='')}?fields=summary,description,status,labels,attachment")
    if not isinstance(result, dict):
        raise RuntimeError(f"Jira issue {item_id} returned an invalid response")
    return result


def _pending(issues: list[dict]) -> list[dict]:
    seen = set()
    pending = []
    for issue in issues:
        if issue["id"] in seen:
            continue
        seen.add(issue["id"])
        fields = issue.get("fields") or {}
        status = (fields.get("status") or {}).get("name", "")
        labels = _owner_labels(issue)
        if hold_label() in labels or labels - {claim_label()}:
            continue
        mine = claim_label() in labels
        if not mine and (status.casefold() != config.JIRA_PICK_STATUS.casefold()
                         or config.JIRA_PICK_LABEL not in (fields.get("labels") or [])):
            continue
        if mine and status.casefold() == config.JIRA_DONE_STATUS.casefold():
            continue
        pending.append({"id": issue["id"], "key": issue["key"],
                        "url": f"{config.JIRA_URL}/browse/{issue['key']}",
                        "text": fields.get("summary", "").strip(),
                        "detail": adf_text(fields.get("description")),
                        "images": _download_images(issue), "claimed_by_me": mine,
                        "priority": priority_of(fields.get("summary", ""))})
    return pending


def list_pending_items() -> list[dict]:
    project = _jql_quote(config.JIRA_PROJECT_KEY)
    ready = _search(f"project = {project} AND status = {_jql_quote(config.JIRA_PICK_STATUS)} "
                    f"AND labels = {_jql_quote(config.JIRA_PICK_LABEL)}")
    owned = _search(f"project = {project} AND labels = {_jql_quote(claim_label())} "
                    f"AND status != {_jql_quote(config.JIRA_DONE_STATUS)}")
    return _pending(ready + owned)


def _find_task(item_text: str, item_id: str | None = None) -> dict | None:
    if item_id:
        return _issue(item_id)
    project = _jql_quote(config.JIRA_PROJECT_KEY)
    candidates = _search(f"project = {project}")
    matches = [item for item in candidates if normalize(item.get("fields", {}).get("summary", ""))
               == normalize(item_text)]
    if not matches:
        return None
    matches.sort(key=lambda item: (claim_label() not in _owner_labels(item),
                                   bool(_owner_labels(item)),
                                   (item.get("fields", {}).get("status") or {}).get("name", "").casefold()
                                   == config.JIRA_DONE_STATUS.casefold()))
    return _issue(matches[0]["id"])


def _label(issue: dict, action: str, label: str) -> None:
    labels = set(issue.get("fields", {}).get("labels") or [])
    if (action == "add" and label in labels) or (action == "remove" and label not in labels):
        return
    _request("PUT", f"/issue/{issue['id']}", {"update": {"labels": [{action: label}]}})


def _foreign_owners(issue: dict) -> set[str]:
    return _owner_labels(issue) - {claim_label(), hold_label()}


def _transition(issue: dict, target: str) -> None:
    current = (issue.get("fields", {}).get("status") or {}).get("name", "")
    if current.casefold() == target.casefold():
        return
    response = _request("GET", f"/issue/{issue['id']}/transitions")
    if not isinstance(response, dict):
        raise RuntimeError(f"Jira returned invalid transitions for {issue['key']}")
    options = response.get("transitions", [])
    transition = next((t for t in options
                       if t.get("to", {}).get("name", "").casefold() == target.casefold()), None)
    if not transition:
        names = [t.get("to", {}).get("name", "?") for t in options]
        raise RuntimeError(f"Jira issue {issue['key']} cannot transition from {current!r} "
                           f"to {target!r}; available targets: {names}")
    _request("POST", f"/issue/{issue['id']}/transitions",
             {"transition": {"id": transition["id"]}})
    issue["fields"]["status"] = {"name": target}


def validate() -> None:
    if not config.JIRA_PICK_LABEL:
        raise RuntimeError("CODEBOT_JIRA_PICK_LABEL must name the opt-in label for Jira tasks")
    _request("GET", "/myself")
    project = urllib.parse.quote(config.JIRA_PROJECT_KEY, safe="")
    _request("GET", f"/project/{project}")
    response = _request("GET", f"/project/{project}/statuses")
    if not isinstance(response, list):
        raise RuntimeError("Jira project returned invalid workflow status data")
    available = {s.get("name", "").casefold()
                 for issue_type in response for s in issue_type.get("statuses", [])}
    configured = {"CODEBOT_JIRA_PICK_STATUS": config.JIRA_PICK_STATUS,
                  "CODEBOT_JIRA_ACTIVE_STATUS": config.JIRA_ACTIVE_STATUS,
                  "CODEBOT_JIRA_REVIEW_STATUS": config.JIRA_REVIEW_STATUS,
                  "CODEBOT_JIRA_DONE_STATUS": config.JIRA_DONE_STATUS}
    for key, name in configured.items():
        if name.casefold() not in available:
            raise RuntimeError(f"{key}={name!r} is not a status in Jira project "
                               f"{config.JIRA_PROJECT_KEY}; available: {sorted(available)}")
    try:
        _search(f"project = {_jql_quote(config.JIRA_PROJECT_KEY)}",
                fields=["summary", "status"])
    except task_source.MalformedPayload as error:
        # Startup must not depend on the search payload's shape: the loop retries that.
        log.warning("Jira search returned an unexpected payload during startup: %s", error)


def describe() -> str:
    return f"jira={config.JIRA_URL}/browse/{config.JIRA_PROJECT_KEY}"


def claim_task(item_text: str, item_id: str | None = None) -> bool:
    issue = _find_task(item_text, item_id)
    if not issue:
        return False
    labels = _owner_labels(issue)
    if _foreign_owners(issue) or hold_label() in labels:
        return False
    status = (issue.get("fields", {}).get("status") or {}).get("name", "")
    already_claimed = claim_label() in labels
    if not already_claimed and (status.casefold() != config.JIRA_PICK_STATUS.casefold()
                                or config.JIRA_PICK_LABEL not in (issue["fields"].get("labels") or [])):
        return False
    if status.casefold() == config.JIRA_DONE_STATUS.casefold():
        return False
    if not already_claimed:
        _label(issue, "add", claim_label())
        issue = _issue(issue["id"])
        if _foreign_owners(issue):
            _label(issue, "remove", claim_label())
            return False
        if claim_label() not in _owner_labels(issue):
            return False
        if (config.JIRA_PICK_LABEL not in (issue["fields"].get("labels") or []) or
                (issue["fields"].get("status") or {}).get("name", "").casefold()
                != config.JIRA_PICK_STATUS.casefold()):
            _label(issue, "remove", claim_label())
            return False
    _transition(issue, config.JIRA_ACTIVE_STATUS)
    return True


def unclaim_task(item_text: str, item_id: str | None = None) -> bool:
    issue = _find_task(item_text, item_id)
    if not issue or _foreign_owners(issue):
        return False
    _label(issue, "remove", claim_label())
    _transition(issue, config.JIRA_PICK_STATUS)
    return True


def hold_task(item_text: str, item_id: str | None = None) -> bool:
    issue = _find_task(item_text, item_id)
    if not issue or _foreign_owners(issue):
        return False
    if hold_label() not in _owner_labels(issue):
        if claim_label() not in _owner_labels(issue):
            return False
        _label(issue, "add", hold_label())
    _label(issue, "remove", claim_label())
    return True


def unhold_task(item_text: str, item_id: str | None = None) -> bool:
    issue = _find_task(item_text, item_id)
    if not issue or _foreign_owners(issue):
        return False
    if hold_label() in _owner_labels(issue):
        _label(issue, "add", claim_label())
        issue = _issue(issue["id"])
        if _foreign_owners(issue):
            _label(issue, "remove", claim_label())
            return False
    _label(issue, "remove", hold_label())
    return True


def mark_done(item_text: str, item_id: str | None = None) -> bool:
    issue = _find_task(item_text, item_id)
    if not issue or _foreign_owners(issue):
        return False
    _transition(issue, config.JIRA_DONE_STATUS)
    _label(issue, "remove", claim_label())
    _label(issue, "remove", hold_label())
    return True


def _adf(text: str) -> dict:
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": line}] if line else []}
        for line in text.splitlines() or [""]]}


def _comment(issue: dict, message: str) -> None:
    _request("POST", f"/issue/{issue['id']}/comment", {"body": _adf(message)})


def note_activity(item_text: str, item_id: str | None, message: str,
                  ref: str | None = None) -> str | None:
    issue = _find_task(item_text, item_id)
    if not issue:
        log.warning("no Jira issue to note activity on: %r", item_text[:80])
        return None
    _comment(issue, message)
    return None


def note_pr(item_text: str, item_id: str | None, pr_url: str) -> None:
    issue = _find_task(item_text, item_id)
    if not issue:
        log.warning("no Jira issue to link PR to: %r", item_text[:80])
        return
    _transition(issue, config.JIRA_REVIEW_STATUS)
    _comment(issue, f"Pull request: {pr_url}")


def ensure_item(item_text: str) -> bool:
    project = _jql_quote(config.JIRA_PROJECT_KEY)
    wanted = normalize(item_text)
    for issue in _search(f"project = {project}", fields=["summary", "description", "status"]):
        fields = issue.get("fields") or {}
        if wanted in (normalize(fields.get("summary", "")),
                      normalize(adf_text(fields.get("description")))):
            return False
    title = item_text.strip()
    detail = ""
    if len(title) > 200:
        title = re.split(r"(?<=[.!?])\s", title, maxsplit=1)[0][:200].rstrip()
        detail = item_text.strip()
    response = _request("POST", "/issue", {"fields": {
        "project": {"key": config.JIRA_PROJECT_KEY},
        "issuetype": {"name": config.JIRA_ISSUE_TYPE},
        "summary": title,
        "labels": [config.JIRA_PICK_LABEL],
        "description": _adf(detail) if detail else None}})
    if not isinstance(response, dict) or "id" not in response:
        raise RuntimeError("Jira did not return the created issue's ID")
    _transition(_issue(response["id"]), config.JIRA_PICK_STATUS)
    return True
