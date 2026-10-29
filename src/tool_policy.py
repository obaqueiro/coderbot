"""Lifecycle guardrails enforced by the agent runtime instead of prompt prose.

Coderbot alone pushes, opens/merges PRs, archives OpenSpec changes and creates
worktrees; planning phases write only OpenSpec artifacts; implementation turns do not
fan out to subagents. Claude Code gets `--settings` deny rules plus a PreToolUse guard
(agent-plugin/runtime/guard.py) for wrapped forms the deny rules miss (`git -C . push`,
`bash -c "…"`, live check C4). OpenCode gets bash permission patterns. Everything here
fails open: a policy error must never block a turn (CODEBOT_TOOL_POLICY=off disables).
"""
import json

import config

# Commands only coderbot runs. Matched on the normalized command (see guard.py).
DENIED = ("git push", "gh pr create", "gh pr merge", "gh pr close", "gh pr ready",
          "gh pr edit", "openspec archive", "git worktree add")
# Planning phases produce OpenSpec artifacts only; coderbot commits them.
PLANNING = {"EXPLORING", "PROPOSING", "REPLANNING"}
PLANNING_DENIED = ("git commit", "git reset", "git checkout", "git switch", "git merge",
                   "git rebase", "git stash")
# Work turns that must finish in this session rather than delegating.
NO_SUBAGENTS = {"IMPLEMENTING", "REPAIR_CHECKS", "E2E", "APPLY_FEEDBACK", "APPLY_PR_FEEDBACK",
                "ADDRESS_REVIEW", "ADDRESS_PR_THREADS", "RESOLVE_CONFLICTS", "RECOVERING",
                "ARCHIVING", "VERIFYING", "INTERNAL_REVIEW", "EVIDENCE_REVIEW"}
GUARD = config.BRIDGE_PLUGIN_DIR / "runtime" / "guard.py"


def current_phase() -> str | None:
    """The FSM phase of the agent turn being started (None outside a task)."""
    import agent_runner
    return (agent_runner._task_context.get() or {}).get("state")


def enabled() -> bool:
    return config.TOOL_POLICY


def denied(phase: str | None) -> tuple[str, ...]:
    return DENIED + (PLANNING_DENIED if phase in PLANNING else ())


def claude_args(phase: str | None) -> list[str]:
    """Extra `claude` flags for a working turn in `phase`."""
    if not enabled():
        return []
    rules = [f"Bash({command}{suffix})" for command in denied(phase) for suffix in (":*", " *")]
    settings = {"permissions": {"deny": rules}, "hooks": {"PreToolUse": [{
        "matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit",
        # `|| true`: a missing interpreter or script must not exit 2 (exit 2 blocks).
        "hooks": [{"type": "command", "command": f"python3 {GUARD} || true", "timeout": 5}]}]}}
    args = ["--settings", json.dumps(settings)]
    tools = []
    if phase == "EXPLORING":
        tools += ["Edit", "Write", "MultiEdit", "NotebookEdit"]
    if phase in NO_SUBAGENTS and config.EXECUTION != "subagent":
        tools += ["Task", "Agent"]
    return args + (["--disallowedTools", *tools] if tools else [])


def environment(phase: str | None) -> dict:
    """Environment for the agent process: what guard.py enforces, plus runtime caps."""
    if not enabled():
        return {}
    policy = {"phase": phase, "repo": str(config.REPO_PATH), "outbox": str(config.DATA_DIR / "outbox"),
              "denied": list(denied(phase)), "write_scope": ["openspec"] if phase in PLANNING else None}
    if config.TARGET_MODE == "workspace":
        # The root holds the shared plan (<root>/openspec) and the controller's manifest.
        policy.update(workspace=True, protected=[str(config.REPO_PATH / path) for path in
                      (config.WORKSPACE_MANIFEST, ".codebot/checks.json") if path])
    return {"CODEBOT_POLICY": json.dumps(policy),
            # Subagents never spawn subagents; background jobs die with the turn anyway.
            "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1",
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}


def opencode_bash(phase: str | None) -> dict:
    """OpenCode `permission.bash`: allow everything except coderbot-only commands."""
    rules = {"*": "allow"}
    if enabled():
        for command in denied(phase):
            rules[command + "*"] = "deny"
            rules["git -C * " + command[4:] + "*" if command.startswith("git ") else command + " *"] = "deny"
    return rules
