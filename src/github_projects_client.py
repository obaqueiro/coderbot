"""Backlog backend: a GitHub Projects v2 board, driven entirely through the `gh` CLI.

Tasks are the board's items whose content is an ISSUE in the target repository (the
checkout's origin, or CODEBOT_GH_ISSUE_REPO). Draft cards and pull-request cards are
skipped: ownership is recorded as labels on the issue, and labels do not exist on
drafts. Status moves through the board's Status field:

    pending   Status in GH_PROJECT_PICK_STATUSES (default "Ready"), no codebot label
    claimed   Status = GH_PROJECT_ACTIVE_STATUS, label "<prefix>:<instance>"
    PR open   Status = GH_PROJECT_REVIEW_STATUS, PR linked on the issue
    on hold   Status unchanged, label "<prefix>-hold:<instance>"
    aborted   back to the first pick status, label removed
    done      Status = GH_PROJECT_DONE_STATUS, labels removed

Multi-instance coordination: GitHub has no compare-and-swap on labels, so a claim is
add-then-verify — add my label, re-read the issue, and if another instance's claim
label is present too, remove mine and report the claim lost. Both racers back off and
repick on their next tick; Status only moves after the verified claim.
"""
import json
import logging
import os
import re
import urllib.parse
import urllib.request

import config
import operations
import ownership
import safe_download
import task_source

# Hosts that may receive the GitHub token when fetching issue images.
GITHUB_IMAGE_HOSTS = ("github.com", "api.github.com", "githubusercontent.com")
MAX_IMAGES = 10
from task_text import normalize, priority_of

log = logging.getLogger(__name__)

# Images referenced from issue bodies are downloaded here for the exploration session.
IMAGES_DIR = config.DATA_DIR / "gh_images"

_SCOPE_MARKERS = ("INSUFFICIENT_SCOPES", "read:project", "Resource not accessible",
                  "does not have permission", "Could not resolve to a ProjectV2")
_SCOPE_HINT = (" — GH_TOKEN must carry the 'project' scope (classic PAT; 'read:project' is "
               "not enough) or Projects read/write (fine-grained PAT), be authorized for "
               "the organization's SSO if it uses one, and the project owner/number must "
               "be right.")

_cache: dict = {}


# ---------------------------------------------------------------- gh plumbing

def _gh(*args: str) -> str:
    """Run a gh command and return stdout; RuntimeError (with a token-scope hint when
    the error looks like one) on failure."""
    proc = operations.tool(["gh", *args])
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip())[-2000:]
        message = f"gh {' '.join(args[:2])} failed (exit {proc.returncode}): {detail}"
        if any(marker in detail for marker in _SCOPE_MARKERS):
            message += _SCOPE_HINT
        raise RuntimeError(message)
    return proc.stdout


def _graphql(query: str, **variables) -> dict:
    """POST a GraphQL document with variables (ints via -F so they stay ints) and
    return its `data`."""
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if value is None:
            continue
        flag = "-F" if isinstance(value, int) and not isinstance(value, bool) else "-f"
        args += [flag, f"{key}={value}"]
    out = _gh(*args)
    try:
        payload = json.loads(out)
    except ValueError as err:
        raise RuntimeError(f"gh api graphql returned non-JSON output: {out[:500]!r}") from err
    if payload.get("errors"):
        detail = json.dumps(payload["errors"])[:2000]
        message = f"GraphQL errors: {detail}"
        if any(marker in detail for marker in _SCOPE_MARKERS):
            message += _SCOPE_HINT
        raise RuntimeError(message)
    return payload.get("data") or {}


def _target_repo() -> str:
    """'owner/repo' whose issues are the tasks: CODEBOT_GH_ISSUE_REPO or the target
    checkout's origin remote."""
    if "repo" in _cache:
        return _cache["repo"]
    repo = config.GH_ISSUE_REPO
    if not repo:
        proc = operations.tool(["git", "-C", str(config.REPO_PATH), "remote", "get-url", "origin"])
        if proc.returncode != 0:
            raise RuntimeError("cannot determine the issue repository: no origin remote in "
                               f"{config.REPO_PATH} (set CODEBOT_GH_ISSUE_REPO=owner/repo)")
        # The remote may use an SSH host alias (git@github-work:owner/repo.git), so only
        # the owner/repo tail is parsed; the repo is on GitHub by construction (gh pr).
        m = re.search(r"[:/]([^/:\s]+)/([^/\s]+?)(?:\.git)?/?$", proc.stdout.strip())
        if not m:
            raise RuntimeError(f"cannot parse owner/repo from the origin remote "
                               f"{proc.stdout.strip()!r} (set CODEBOT_GH_ISSUE_REPO=owner/repo)")
        repo = f"{m.group(1)}/{m.group(2)}"
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
        raise RuntimeError(f"CODEBOT_GH_ISSUE_REPO must be owner/repo, got {repo!r}")
    _cache["repo"] = repo
    return repo


# ---------------------------------------------------------------- the project

_PROJECT_QUERY = """
query($owner: String!, $number: Int!) {
  repositoryOwner(login: $owner) {
    ... on ProjectV2Owner {
      projectV2(number: $number) {
        id title
        fields(first: 50) {
          nodes {
            ... on ProjectV2SingleSelectField { id name options { id name } }
          }
        }
      }
    }
  }
}"""


def _parse_project(data: dict) -> dict:
    project = (data.get("repositoryOwner") or {}).get("projectV2")
    if not project:
        raise RuntimeError(f"project {config.GH_PROJECT_OWNER}/{config.GH_PROJECT_NUMBER} "
                           f"not found for this token{_SCOPE_HINT}")
    fields = {}
    for field in project.get("fields", {}).get("nodes", []):
        if field and field.get("options") is not None:
            fields[field["name"].lower()] = {
                "id": field["id"], "name": field["name"],
                "options": {o["name"].lower(): o for o in field["options"]},
                "order": [o["name"].lower() for o in field["options"]]}
    status = fields.get("status")
    if not status:
        raise RuntimeError(f"project {project.get('title')!r} has no single-select Status field")
    return {"id": project["id"], "title": project.get("title", ""), "status": status,
            "fields": fields}


def _project() -> dict:
    if "project" not in _cache:
        _cache["project"] = _parse_project(_graphql(
            _PROJECT_QUERY, owner=config.GH_PROJECT_OWNER, number=config.GH_PROJECT_NUMBER))
    return _cache["project"]


def _status_option(name: str) -> dict:
    options = _project()["status"]["options"]
    option = options.get(name.lower())
    if option is None:
        raise RuntimeError(f"Status option {name!r} does not exist on the project; it has "
                           f"{[o['name'] for o in options.values()]}")
    return option


# ---------------------------------------------------------------- items

_ITEM_FIELDS = """
  id
  fieldValues(first: 30) {
    nodes {
      ... on ProjectV2ItemFieldSingleSelectValue {
        name
        field { ... on ProjectV2SingleSelectField { name } }
      }
    }
  }
  content {
    __typename
    ... on Issue {
      id number title body url state
      labels(first: 50) { nodes { name } }
      repository { nameWithOwner }
    }
  }"""

_ITEMS_QUERY = """
query($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on ProjectV2 {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {%s
        }
      }
    }
  }
}""" % _ITEM_FIELDS

_ITEM_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on ProjectV2Item {%s
    }
  }
}""" % _ITEM_FIELDS


def _parse_item(node: dict) -> dict:
    """A board item as a flat record: {item_id, kind, content_id, number, title, body,
    url, state, labels, repo, status, fields}."""
    content = node.get("content") or {}
    fields = {}
    for value in (node.get("fieldValues") or {}).get("nodes", []):
        if value and value.get("field", {}).get("name"):
            fields[value["field"]["name"].lower()] = value.get("name")
    return {
        "item_id": node["id"],
        "kind": content.get("__typename") or "None",
        "content_id": content.get("id"),
        "number": content.get("number"),
        "title": (content.get("title") or "").strip(),
        "body": content.get("body") or "",
        "url": content.get("url") or "",
        "state": content.get("state") or "",
        "labels": {l["name"] for l in (content.get("labels") or {}).get("nodes", []) if l},
        "repo": (content.get("repository") or {}).get("nameWithOwner", ""),
        "status": fields.get("status"),
        "fields": fields,
    }


def _items() -> list[dict]:
    """Every item on the board, in board order."""
    project_id = _project()["id"]
    items, cursor = [], None
    while True:
        data = _graphql(_ITEMS_QUERY, id=project_id, cursor=cursor)
        node = data.get("node")
        if not isinstance(node, dict) or not isinstance(node.get("items"), dict):
            raise task_source.MalformedPayload(
                "GitHub Projects items query returned no project node (payload keys: "
                f"{sorted(data)})")
        page = node["items"]
        items.extend(_parse_item(n) for n in task_source.require_list(page, "nodes", "GitHub Projects items") if n)
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return items
        cursor = info.get("endCursor")


def _item(item_id: str) -> dict | None:
    node = _graphql(_ITEM_QUERY, id=item_id).get("node")
    return _parse_item(node) if node and node.get("id") else None


# ---------------------------------------------------------------- labels

def claim_label(instance: str = config.INSTANCE_ID) -> str:
    owner = ownership.me() if instance == config.INSTANCE_ID else instance
    return f"{config.GH_LABEL_PREFIX}:{owner}"


def hold_label(instance: str = config.INSTANCE_ID) -> str:
    owner = ownership.me() if instance == config.INSTANCE_ID else instance
    return f"{config.GH_LABEL_PREFIX}-hold:{owner}"


def claimed_by(labels: set[str]) -> str | None:
    """The instance named by the issue's claim label, or None."""
    prefix = f"{config.GH_LABEL_PREFIX}:"
    owners = sorted(l[len(prefix):].strip().lower() or "?" for l in labels if l.startswith(prefix))
    return owners[0] if owners else None


def held_by(labels: set[str]) -> str | None:
    prefix = f"{config.GH_LABEL_PREFIX}-hold:"
    holders = sorted(l[len(prefix):].strip().lower() or "?" for l in labels if l.startswith(prefix))
    return holders[0] if holders else None


def _foreign_claims(labels: set[str]) -> set[str]:
    prefix = f"{config.GH_LABEL_PREFIX}:"
    return {l for l in labels
            if l.startswith(prefix) and l[len(prefix):].strip().lower() != ownership.me()}


def _add_label(task: dict, label: str) -> None:
    _gh("api", "-X", "POST", f"repos/{_target_repo()}/issues/{task['number']}/labels",
        "-f", f"labels[]={label}")
    log.info("added label %r to issue #%s", label, task["number"])


def _remove_label(task: dict, label: str) -> None:
    """Remove a label from the issue; a label that is not on it is not an error."""
    try:
        _gh("api", "-X", "DELETE",
            f"repos/{_target_repo()}/issues/{task['number']}/labels/{urllib.parse.quote(label, safe='')}")
    except RuntimeError as err:
        if "404" in str(err) or "Label does not exist" in str(err):
            return
        raise
    log.info("removed label %r from issue #%s", label, task["number"])


def _ensure_labels() -> None:
    repo = _target_repo()
    for name, color, description in (
            (claim_label(), "1d76db", f"being implemented by codebot instance {config.INSTANCE_ID}"),
            (hold_label(), "fbca04", f"on hold by codebot instance {config.INSTANCE_ID}")):
        _gh("label", "create", name, "--repo", repo, "--force", "--color", color,
            "--description", description)


# ---------------------------------------------------------------- status

_SET_STATUS = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId, itemId: $itemId, fieldId: $fieldId,
    value: { singleSelectOptionId: $optionId }
  }) { projectV2Item { id } }
}"""


def _set_status(task: dict, name: str) -> None:
    if (task.get("status") or "").lower() == name.lower():
        return
    project = _project()
    _graphql(_SET_STATUS, projectId=project["id"], itemId=task["item_id"],
             fieldId=project["status"]["id"], optionId=_status_option(name)["id"])
    log.info("issue #%s: Status %r -> %r", task["number"], task.get("status"), name)
    task["status"] = name


def _pick_statuses() -> list[str]:
    return [s.lower() for s in config.GH_PROJECT_PICK_STATUSES]


# ---------------------------------------------------------------- pending items

def _pending(tasks: list[dict], target_repo: str) -> list[dict]:
    """Pickable tasks: open issues of the target repo in a pick status with no codebot
    label, plus any not-done issue this instance already claimed (claimed_by_me)."""
    me = ownership.me()
    picks, done = _pick_statuses(), config.GH_PROJECT_DONE_STATUS.lower()
    pending, skipped, foreign = [], {"draft/PR": 0, "other repo": 0, "closed": 0}, set()
    for t in tasks:
        if t["kind"] != "Issue":
            skipped["draft/PR"] += 1
            continue
        if t["repo"].lower() != target_repo.lower():
            skipped["other repo"] += 1
            continue
        if t["state"].upper() == "CLOSED":
            skipped["closed"] += 1
            continue
        status = (t.get("status") or "").lower()
        owner, holder = claimed_by(t["labels"]), held_by(t["labels"])
        if holder:
            continue
        if (owner == me or ownership.legacy_marker(owner, t["title"], t["item_id"])) \
                and not (_foreign_claims(t["labels"]) - {
                    f"{config.GH_LABEL_PREFIX}:{config.INSTANCE_ID}"}):
            if status != done:
                pending.append(dict(t, claimed_by_me=True))
            continue
        if owner:
            foreign.add(owner)
            continue
        if status in picks:
            pending.append(dict(t, claimed_by_me=False))
    if any(skipped.values()):
        log.info("board cards ignored (only open issues of %s are tasks): %s", target_repo,
                 ", ".join(f"{n} {k}" for k, n in skipped.items() if n))
    if foreign:
        log.info("%d board item(s) claimed by other instance(s) %s; not pickable",
                 sum(1 for t in tasks if claimed_by(t["labels"]) in foreign), sorted(foreign))
    return pending


def _priority(task: dict) -> int | None:
    """Codebot[n] in the title, else the 1-based position of the item's value in a
    single-select "Priority" field when the board has one."""
    tagged = priority_of(task["title"])
    if tagged is not None:
        return tagged
    field = _project()["fields"].get("priority")
    value = (task.get("fields") or {}).get("priority")
    if field and value and value.lower() in field["order"]:
        return field["order"].index(value.lower()) + 1
    return None


_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((\S+?)(?:\s+\"[^\"]*\")?\)|<img[^>]+src=[\"']([^\"']+)[\"']",
                       re.IGNORECASE)


def image_urls(body: str) -> list[str]:
    urls = []
    for m in _IMAGE_RE.finditer(body or ""):
        url = m.group(1) or m.group(2)
        if url and url.startswith("http") and url not in urls:
            urls.append(url)
    return urls


def _download_images(task: dict) -> list[str]:
    urls = image_urls(task["body"])
    if not urls:
        return []
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    paths = []
    if len(urls) > MAX_IMAGES:
        log.warning("issue #%s has %d images; downloading the first %d", task["number"], len(urls), MAX_IMAGES)
    for index, url in enumerate(urls[:MAX_IMAGES]):
        ext = re.sub(r"[^A-Za-z0-9.]", "", os.path.splitext(urllib.parse.urlparse(url).path)[1])[:8] or ".png"
        dest = IMAGES_DIR / f"issue{task['number']}-{index}{ext}"
        try:
            # The token goes only to GitHub-owned hosts (never to "https://evil/github.png")
            # and is dropped if GitHub redirects to a signed URL elsewhere.
            data = safe_download.download(url, auth_hosts=GITHUB_IMAGE_HOSTS,
                                          authorization=f"Bearer {token}" if token else None,
                                          content_type_prefix="image/")
            IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            paths.append(str(dest))
        except Exception as error:  # noqa: BLE001 — a lost screenshot must not block the backlog
            log.warning("could not download image %s for issue #%s: %s", url, task["number"], error)
    log.info("downloaded %d/%d image(s) for issue #%s", len(paths), len(urls), task["number"])
    return paths


def _as_item(task: dict) -> dict:
    return {"id": task["item_id"], "url": task["url"], "text": task["title"],
            "detail": task["body"].strip(), "images": _download_images(task),
            "claimed_by_me": bool(task.get("claimed_by_me")), "priority": _priority(task)}


def list_pending_items() -> list[dict]:
    repo = _target_repo()
    tasks = _items()
    items = [_as_item(t) for t in _pending(tasks, repo)]
    log.debug("project %s: %d pending item(s) of %d board item(s); pick statuses %s",
              _project()["title"], len(items), len(tasks), config.GH_PROJECT_PICK_STATUSES)
    return items


# ---------------------------------------------------------------- finding a task

def _find_task(item_text: str, item_id: str | None) -> dict | None:
    """The board item for a task: by node id when known, else by title. Duplicate
    titles prefer the copy this instance claimed, then an unclaimed open copy, then a
    foreign-claimed one, then a done/closed one."""
    if item_id:
        task = _item(item_id)
        if task is not None and task["kind"] == "Issue":
            return task
        log.warning("board item %s is gone or is not an issue; matching by title", item_id)
    me, done = ownership.me(), config.GH_PROJECT_DONE_STATUS.lower()

    def rank(task: dict) -> int:
        if task["state"].upper() == "CLOSED" or (task.get("status") or "").lower() == done:
            return 3
        owner = claimed_by(task["labels"]) or held_by(task["labels"])
        if owner == me or ownership.legacy_marker(owner, task["title"], task["item_id"]):
            return 0
        return 1 if owner is None else 2

    wanted = normalize(item_text)
    matches = [t for t in _items() if t["kind"] == "Issue" and normalize(t["title"]) == wanted]
    return min(matches, key=rank) if matches else None


def _is_done(task: dict) -> bool:
    return (task.get("status") or "").lower() == config.GH_PROJECT_DONE_STATUS.lower()


# ---------------------------------------------------------------- the interface

def claim_task(item_text: str, item_id: str | None = None) -> bool:
    task = _find_task(item_text, item_id)
    if task is None or _is_done(task) or task["state"].upper() == "CLOSED":
        log.warning("no open board issue to claim for: %r", item_text[:80])
        return False
    owner = claimed_by(task["labels"])
    if owner == ownership.me():
        if _foreign_claims(task["labels"]):
            log.warning("task %r has competing claim labels; refusing ownership", item_text[:80])
            return False
        log.info("task already claimed by this instance: %r", item_text[:80])
        if (task.get("status") or "").lower() in _pick_statuses():
            # Label landed but the status move did not (crash between the two).
            _set_status(task, config.GH_PROJECT_ACTIVE_STATUS)
        return True
    if ownership.legacy_marker(owner, item_text, task["item_id"]):
        # Install the fingerprinted marker first, then retire the legacy one.
        # Never adopt a legacy label merely because the readable name matches.
        _add_label(task, claim_label())
        fresh = _item(task["item_id"]) or task
        rivals = _foreign_claims(fresh["labels"]) - {f"{config.GH_LABEL_PREFIX}:{owner}"}
        if rivals:
            _remove_label(task, claim_label())
            return False
        _remove_label(task, f"{config.GH_LABEL_PREFIX}:{owner}")
        if (fresh.get("status") or "").lower() in _pick_statuses():
            _set_status(fresh, config.GH_PROJECT_ACTIVE_STATUS)
        return True
    if owner:
        log.info("task already claimed by %r: %r", owner, item_text[:80])
        return False
    _add_label(task, claim_label())
    fresh = _item(task["item_id"]) or task
    rivals = _foreign_claims(fresh["labels"])
    if rivals:
        log.info("lost the claim race for %r to %s; backing off", item_text[:80], sorted(rivals))
        _remove_label(task, claim_label())
        return False
    _set_status(fresh, config.GH_PROJECT_ACTIVE_STATUS)
    log.info("claimed board issue #%s for %s: %r", task["number"], config.INSTANCE_ID,
             item_text[:80])
    return True


def unclaim_task(item_text: str, item_id: str | None = None) -> bool:
    task = _find_task(item_text, item_id)
    if task is None:
        log.warning("no board issue to unclaim for: %r", item_text[:80])
        return False
    owner = claimed_by(task["labels"])
    holder = held_by(task["labels"])
    if holder and not ownership.mine(holder, item_text, task["item_id"]):
        return False
    if _foreign_claims(task["labels"]) - {f"{config.GH_LABEL_PREFIX}:{config.INSTANCE_ID}"}:
        return False
    if owner and not ownership.mine(owner, item_text, task["item_id"]):
        return False
    if ownership.legacy_marker(owner, item_text, task["item_id"]):
        _remove_label(task, f"{config.GH_LABEL_PREFIX}:{owner}")
    _remove_label(task, claim_label())
    if not _is_done(task) and task["state"].upper() != "CLOSED":
        _set_status(task, config.GH_PROJECT_PICK_STATUSES[0])
    return True


def hold_task(item_text: str, item_id: str | None = None) -> bool:
    task = _find_task(item_text, item_id)
    if task is None or _is_done(task):
        log.warning("no open board issue to put on hold for: %r", item_text[:80])
        return False
    owner = claimed_by(task["labels"])
    holder = held_by(task["labels"])
    if holder:
        return ownership.mine(holder, item_text, task["item_id"])
    if owner and not ownership.mine(owner, item_text, task["item_id"]):
        return False
    if hold_label() not in task["labels"]:
        _add_label(task, hold_label())
    _remove_label(task, claim_label())
    if ownership.legacy_marker(owner, item_text, task["item_id"]):
        _remove_label(task, f"{config.GH_LABEL_PREFIX}:{owner}")
    log.info("put board issue #%s on hold for %s", task["number"], config.INSTANCE_ID)
    return True


def unhold_task(item_text: str, item_id: str | None = None) -> bool:
    task = _find_task(item_text, item_id)
    if task is None:
        log.warning("no board issue to take off hold for: %r", item_text[:80])
        return False
    holder = held_by(task["labels"])
    if holder and not ownership.mine(holder, item_text, task["item_id"]):
        return False
    _remove_label(task, hold_label())
    if ownership.legacy_marker(holder, item_text, task["item_id"]):
        _remove_label(task, f"{config.GH_LABEL_PREFIX}-hold:{holder}")
    return True


def mark_done(item_text: str, item_id: str | None = None) -> bool:
    task = _find_task(item_text, item_id)
    if task is None:
        log.warning("no board issue matched item text: %r", item_text[:80])
        return False
    owner = claimed_by(task["labels"]) or held_by(task["labels"])
    if owner and not ownership.mine(owner, item_text, task["item_id"]):
        return False
    if _is_done(task):
        log.info("board issue #%s already Done", task["number"])
    else:
        _set_status(task, config.GH_PROJECT_DONE_STATUS)
    _remove_label(task, claim_label())
    _remove_label(task, hold_label())
    if ownership.legacy_marker(owner, item_text, task["item_id"]):
        prefix = (f"{config.GH_LABEL_PREFIX}:" if claimed_by(task["labels"])
                  else f"{config.GH_LABEL_PREFIX}-hold:")
        _remove_label(task, prefix + (owner or ""))
    return True


_ADD_ITEM = """
mutation($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: { projectId: $projectId, contentId: $contentId }) {
    item { id }
  }
}"""

SEED_TITLE_MAX = 200


def _split_seed(text: str) -> tuple[str, str]:
    """Issue title/body for a seeded item. GitHub caps titles, and the self-healing
    texts are paragraphs: long texts use their first sentence as the title and carry
    the full text in the body (has_item matches on either)."""
    text = text.strip()
    if len(text) <= SEED_TITLE_MAX:
        return text, ""
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    if len(first) > SEED_TITLE_MAX:
        first = first[:SEED_TITLE_MAX - 1].rstrip() + "…"
    return first, text


def has_item(item_text: str) -> bool:
    wanted = normalize(item_text)
    return any(t["kind"] == "Issue" and wanted in (normalize(t["title"]), normalize(t["body"]))
               for t in _items())


def add_item(item_text: str) -> None:
    repo = _target_repo()
    title, body = _split_seed(item_text)
    url = _gh("issue", "create", "--repo", repo, "--title", title, "--body", body).strip()
    number = url.rstrip("/").rsplit("/", 1)[-1]
    content_id = json.loads(_gh("issue", "view", number, "--repo", repo, "--json", "id"))["id"]
    data = _graphql(_ADD_ITEM, projectId=_project()["id"], contentId=content_id)
    item_id = data["addProjectV2ItemById"]["item"]["id"]
    _set_status({"item_id": item_id, "number": number, "status": None},
                config.GH_PROJECT_PICK_STATUSES[0])
    log.info("added backlog issue #%s to the board: %r", number, title[:80])


def ensure_item(item_text: str) -> bool:
    if has_item(item_text):
        return False
    add_item(item_text)
    return True


def note_pr(item_text: str, item_id: str | None, pr_url: str) -> None:
    """Move the item to review (the PR link itself lands on the issue through the
    activity trail's "PR opened" note and the PR body's Closes line)."""
    task = _find_task(item_text, item_id)
    if task is None:
        log.warning("no board issue to move to review for: %r", item_text[:80])
        return
    _set_status(task, config.GH_PROJECT_REVIEW_STATUS)


def note_activity(item_text: str, item_id: str | None, message: str,
                  ref: str | None = None) -> str | None:
    """Append a note as a comment on the task's issue. Issue comments need no thread
    handle, so the returned ref is always None."""
    task = _find_task(item_text, item_id)
    if task is None:
        log.warning("no board issue to note activity on: %r", item_text[:80])
        return None
    _gh("issue", "comment", str(task["number"]), "--repo", _target_repo(), "--body", message)
    log.debug("noted activity on issue #%s (%d chars)", task["number"], len(message))
    return None


def validate() -> None:
    """Startup checks: the repo, the project and its Status options, and this
    instance's labels. RuntimeError with an actionable message otherwise."""
    if not (config.GH_PROJECT_OWNER and config.GH_PROJECT_NUMBER):
        raise RuntimeError("CODEBOT_GH_PROJECT_URL (or CODEBOT_GH_PROJECT_OWNER and "
                           "CODEBOT_GH_PROJECT_NUMBER) must be set")
    repo = _target_repo()
    project = _project()
    wanted = [*config.GH_PROJECT_PICK_STATUSES, config.GH_PROJECT_ACTIVE_STATUS,
              config.GH_PROJECT_REVIEW_STATUS, config.GH_PROJECT_DONE_STATUS]
    missing = [s for s in wanted if s.lower() not in project["status"]["options"]]
    if missing:
        raise RuntimeError(f"project {project['title']!r} has no Status option(s) {missing}; "
                           f"it has {[o['name'] for o in project['status']['options'].values()]}")
    _ensure_labels()
    log.info("backlog: project %r (%s/%s), issues in %s, pick statuses %s",
             project["title"], config.GH_PROJECT_OWNER, config.GH_PROJECT_NUMBER, repo,
             config.GH_PROJECT_PICK_STATUSES)


def describe() -> str:
    return f"project={config.GH_PROJECT_OWNER}/{config.GH_PROJECT_NUMBER}"
