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
symlink directories, and supports worktree `.git` files. It never scans inside
repositories or other folders for additional repositories. Each repository needs
`origin` and an available default branch, from `origin/HEAD` or a manifest override.
The current checkout branch is never guessed as the base. GitHub Projects needs
`CODEBOT_GH_ISSUE_REPO=owner/repo` because the workspace root has no origin.

Optional manifest:

```json
{
  "exclude": ["scratch"],
  "base_branches": {"api": "develop", "frontend": "main"},
  "shared_inputs": ["openspec", "docs", "scripts", "e2e", "*compose*.y*ml"],
  "checks": [
    {"id": "integration", "argv": ["python3", "scripts/integration_test.py"], "cwd": ".", "scope": "full"}
  ],
  "docker": {"context": "desktop-linux", "host_root": "/absolute/path/to/feature-workspace"}
}
```

Agent-emitted `CHECK_PLAN` uses workspace-relative `cwd`. Repository checks are
discovered automatically; verification also requires a shared root/E2E check.
Repository and root source content contributes to evidence/cache identity,
including unchanged dependency repositories.
Child repositories contribute tracked files and non-ignored untracked source,
using their own Git ignore rules. Ignored build caches, such as TypeScript
incremental state, do not invalidate verification; tracked changes, deletions,
dependencies and shared root source still do. Failed checks remain visible to
the repair agent even when concurrent source changes invalidate their evidence.

The controller collapses identical check executions regardless of their IDs;
different arguments, input selections, credentials, reporters or preparation
remain separate. E2E `run.sh` checks share a workspace resource lock even when
the agent omits `resources`. Their initial execution budget is at least 1800
seconds, capped at 3600. A recorded execution timeout raises the next execution
budget up to that ceiling automatically, preserving partial output for diagnosis.
Once an execution times out at the ceiling, unchanged reruns remain blocked with
the failure report until source or execution-environment identity changes.
Waiting for controller resources is bounded separately from execution. Explicit
manifest and agent timeout settings are retained within the E2E ceiling.

Agents must not ask users to deduplicate checks or increase controller timeouts.
These are operational repairs, not product approvals. Human questions should
contain one concrete decision and consequences; transport contracts and attachment
paths are parsed separately, not embedded in the visible question. Actual test
failures still block verification; increasing a timeout does not waive them.

## Workflow

- One source task, shared root OpenSpec plan, and communication thread.
- Agent-selected feature branches use each repository's base. Additional repos
  need no selection approval. Existing functional proposal approval still applies.
- Independent branch, publication, review, and merge progress for each changed repo.
- Checks and independent internal review precede publication.
- Each PR is announced with reports/available attachments in the feature thread.
- Completion requires all required merges and valid integrated verification.
- Shared edits have private eligible-content recovery journals under Coderbot data.
  Delivery lists local-only files: **merging PRs does not deliver those files**.

## Safety and operational limits

No repository sandbox is imposed. Picking requires clean child repositories;
mismatched/unattributed work pauses rather than resets it. Abort/hold preserve
workspace files and branches. State from another target requires explicit reset or
migration. Do not run older single-repository builds against workspace state.

Local Docker socket tests use the mounted host paths without extra configuration.
Remote Docker tests require a host-root mapping; it is not file synchronization:
remote paths must already exist and match harness binds. An optional configured
context must match the active context.

Compose `down --volumes` is allowed only with explicit test/e2e project and file
arguments and a resolved configuration with project-scoped volumes/networks.
External volumes/networks, shared resource names, and global prune are rejected.
Harness blockers prompt an agent repair before verification retries. Arbitrary
shell commands are not a security sandbox. Guard report opening with a Darwin
check and `command -v open`; Linux should print the report path instead.

Cross-repository merges are independent, not atomic. Changed PR heads, closed PRs,
conflicts, and failed checks pause completion. Verified root-only changes can be
delivered locally without inventing a PR.
