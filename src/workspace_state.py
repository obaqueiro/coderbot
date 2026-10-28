"""Versioned feature state, validated before any durable replay or migration."""
from copy import deepcopy

from workspace import WorkspaceError


SCHEMA_VERSION = 1


class TargetMismatch(WorkspaceError):
    """Operator must explicitly reset/migrate state; no automatic rebinding."""


def bind_and_validate(state, workspace):
    """Return an independent state copy; never alter a saved cursor on failure."""
    state = deepcopy(state)
    saved = state.get("workspace")
    blank = state.get("state", "IDLE") == "IDLE" and not any(
        state.get(key) for key in ("item", "branch", "session_id", "execution_task_id",
                                  "thread_id", "pending_reply_delivery", "replan"))
    if saved is None:
        if workspace.mode == "repo":
            return state  # Existing legacy repo states retain their migration behavior.
        if not blank:
            raise TargetMismatch("Active task has no workspace identity; explicitly reset or migrate state")
        saved = {"schema_version": SCHEMA_VERSION, "identity": workspace.identity,
                 "root": str(workspace.root), "mode": workspace.mode,
                 "inventory": {r.relative_path: {"identity": r.identity, "remote": r.remote,
                              "base_branch": r.base_branch} for r in workspace.repositories},
                 "repositories": {}, "verification_repositories": [], "local_edits": [],
                 "session_id": None}
        state["workspace"] = saved
        return state
    if not isinstance(saved, dict) or saved.get("schema_version") != SCHEMA_VERSION:
        raise TargetMismatch("Unsupported workspace state schema; explicitly migrate before resuming")
    if (saved.get("identity") != workspace.identity or saved.get("root") != str(workspace.root)
            or saved.get("mode") != workspace.mode):
        raise TargetMismatch("Saved task belongs to a different target; explicitly reset or migrate state")
    inventory = saved.get("inventory")
    records = saved.get("repositories")
    dependencies = saved.get("verification_repositories")
    if not isinstance(inventory, dict) or not isinstance(records, dict) or not isinstance(dependencies, list):
        raise TargetMismatch("Malformed workspace repository state; explicitly repair before resuming")
    current = {repo.relative_path: repo for repo in workspace.repositories}
    for name in set(records) | set(dependencies):
        repo = current.get(name)
        old = inventory.get(name, {})
        if repo is None or old.get("identity") != repo.identity:
            raise TargetMismatch(f"Repository identity changed or missing: {name}; explicitly migrate state")
        if old.get("base_branch") != repo.base_branch:
            raise TargetMismatch(f"Repository base branch changed: {name}; explicitly migrate state")
        if name in records and records[name].get("identity") != repo.identity:
            raise TargetMismatch(f"Repository checkpoint identity mismatch: {name}")
    # Refresh only unenrolled candidates; completed repository checkpoints remain intact.
    saved["inventory"] = {repo.relative_path: {"identity": repo.identity, "remote": repo.remote,
                          "base_branch": repo.base_branch} for repo in workspace.repositories}
    return state


def repository_record(state, workspace, name, *, reason):
    """Create an enrollment cursor, not a grant to edit or a Git operation."""
    repo = workspace.repository(name)
    validated = bind_and_validate(state, workspace)
    if workspace.mode != "workspace":
        raise WorkspaceError("Repository feature records require workspace mode")
    if not isinstance(reason, str) or not reason.strip():
        raise WorkspaceError("Repository enrollment requires a reason")
    records = validated["workspace"]["repositories"]
    if name not in records:
        records[name] = {"identity": repo.identity, "reason": reason.strip(),
                         "base_branch": repo.base_branch, "base_sha": None,
                         "branch": None, "state": "DISCOVERED", "checks": {},
                         "pr_url": None, "publication": {}, "review": {},
                         "merge": {}, "ownership": {}}
    return validated
