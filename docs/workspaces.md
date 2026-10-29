# Multi-repository feature workspaces

Coderbot coordinates one backlog feature across repositories beneath a non-Git
directory. The entire workspace is writable; selection is automatic change/PR
tracking, not a permission boundary. Root files remain local; no root Git is created.

## Configuration

```dotenv
CODEBOT_REPO_PATH=/absolute/path/to/feature-workspace
# Optional, relative to the workspace root:
CODEBOT_WORKSPACE_MANIFEST=workspace.json
```

Mode is detected automatically: `.git` at the target root (file or directory)
means single-repository mode; without it, the target is a workspace. No mode
setting is required, and an old `CODEBOT_TARGET_MODE` setting is ignored.

The parent Compose bind mount exposes child repositories at their host paths.
Rebuild after entrypoint changes. Starting the bot starts real backlog work;
review configuration and saved state first.

Discovery checks **only immediate child folders**, excludes generated folders and
symlink directories, and supports worktree `.git` files. Each repository needs
`origin` and a default branch, from `origin/HEAD` or a manifest override; the current
checkout branch is never guessed as the base. A child without them is left out with a
warning; a saved task that references a skipped repository pauses with the reason.
GitHub Projects needs `CODEBOT_GH_ISSUE_REPO=owner/repo` because the workspace root
has no origin.

Optional manifest:

```json
{
  "exclude": ["scratch"],
  "base_branches": {"api": "develop", "frontend": "main"},
  "merge_order": ["api", "frontend"],
  "shared_inputs": ["openspec", "docs", "scripts", "e2e", "*compose*.y*ml",
                    "AGENTS.md", "INSTRUCTIONS.md", "requirements.md"],
  "checks": [
    {"id": "integration", "argv": ["python3", "scripts/integration_test.py"], "cwd": ".", "scope": "full"}
  ],
  "docker": {"context": "desktop-linux", "host_root": "/absolute/path/to/feature-workspace",
             "compose_files": ["docker-compose.e2e.yaml"]}
}
```

`shared_inputs` above is the default. `max_depth` is still accepted (1-16) but
discovery always uses 1. A check may list the `repositories` it covers.

Checks the agent declares in its turn result use workspace-relative `cwd`.
Repository checks are discovered automatically; verification also requires a shared
root/E2E check. Repository and root source content contributes to evidence/cache
identity, including unchanged dependency repositories. Child repositories contribute
tracked files and non-ignored untracked source, using their own Git ignore rules.

The controller runs identical check commands once, whatever their ids. E2E `run.sh`
checks share a workspace resource lock. Their execution budget is at least 1800
seconds, capped at 3600; a recorded timeout raises the next budget up to that ceiling.
Actual test failures still block verification.

## Workflow

Workspace mode runs the lifecycle in `src/lifecycle.py` over the list of repositories
(`Target{root, repos}` plus `RepoOps`). `RepoOps` is the only code that runs git and
gh for a repository: every call is time-bounded and its failures are typed
(transient, agent fault, fatal). Single-repository mode still uses its own handlers
in `src/main.py`.

- One backlog task, one shared root OpenSpec plan and one conversation thread.
- **The controller owns branches.** At pick time it fetches each repository, returns
  clean children left on an old coderbot branch to their base, and creates a unique
  branch per attempt (`<instance>-<slug>-<6 hex>`). `base_sha` is the merge-base with
  the fetched base. The agent stays on the checked-out branch; commits it makes on the
  base branch are moved onto the feature branch. The agent cannot push (see the
  README's guardrails).
- **Draft PRs open lazily**, one per repository, after the first implementation turn
  that commits past the merge-base (at most 2 attempts per repository;
  `CODEBOT_DRAFT_PR=off` disables them). Progress is pushed after every
  implementation turn. PRs are pinned by number: number, URL, head and base must still
  match, or the task pauses.
- **Verification and review** run before any PR is marked ready. The review is one
  fresh, read-only session over every repository's diff, starting from at most
  `CODEBOT_REVIEW_CONTEXT_CHARS` (12000) characters of diff stats. Critical or
  Important findings get one fix wave in the implementer session, then one
  re-review. Critical findings left after that pause the task; Important ones are
  listed as "Review notes left open" in the PR status.
- **PR changes while waiting.** A head moved by someone else is fast-forwarded in and
  re-verified; a diverged head or a closed PR pauses the task with retry, complete or
  abort. Conflicts get a RESOLVE_CONFLICTS turn. Review threads from all PRs are
  handled in one wave, bounded by `CODEBOT_PR_THREAD_MAX_ROUNDS`.
- **Merges are ordered and pre-checked.** A `merge` reply merges in `merge_order`
  first, then the rest. Before the first merge every PR must be open, not a draft, at
  the verified head, mergeable, green and free of unresolved threads (unless the
  reply forces it); otherwise nothing is merged. Each merge uses
  `gh pr merge --squash --match-head-commit`. A partial merge is recorded, reported
  in the thread and retried by replying `merge` again.
- The task completes when every PR is merged; children then return to their base.
  Verified root-only changes are delivered locally without a PR. **Merging PRs does
  not deliver root-local files.**

## Safety and operational limits

No repository sandbox is imposed. A pick with dirty children notifies once and then
waits in WAIT_CLEAN until they are clean. Hold and abort preserve workspace files and
branches. Workspace and target errors (lost `origin/HEAD`, a new child without origin,
a mismatched saved target) are fatal errors: during a task they pause it in
WAIT_STUCK instead of crashing the loop. State from another target requires explicit
reset or migration. Do not run older single-repository builds against workspace
state.

The runtime guard also enforces workspace rules: no `git init` at the root, no
mutating `gh api` calls (non-GET methods, GraphQL mutations, field flags), and no
writes to the manifest or any `.codebot/checks.json`. Planning phases may write only
under `<root>/openspec`. The guard fails open.

Local Docker socket tests use the mounted host paths without extra configuration.
Remote Docker tests require a host-root mapping; it is not file synchronization:
remote paths must already exist and match harness binds. An optional configured
context must match the active context.

Compose `down --volumes` is allowed only with explicit test/e2e project and file
arguments and a resolved configuration with project-scoped volumes/networks.
External volumes/networks, shared resource names, and global prune are rejected.

Cross-repository merges are independent, not atomic.
