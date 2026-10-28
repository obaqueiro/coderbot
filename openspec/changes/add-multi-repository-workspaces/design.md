## Context

See proposal.md for motivation. The reference workspace contains seven independent Git repositories and a non-Git root holding OpenSpec, Compose, scripts, and E2E assets. Current config.REPO_PATH is simultaneously agent cwd, Git root, planning root, check root, and durable task identity. main.py stores scalar branch/base_sha/pr_url; execution_identity.snapshot uses git ls-files; provenance and recovery inspect one Git root; entrypoint prepares one repository's authentication.

This is a cross-cutting data-model, execution-safety, and recovery change, so a design artifact is required.

## Goals / Non-Goals

**Goals:** Separate workspace coordination from repository operations without replacing existing lifecycle guarantees. Make selection autonomous and all mutation/recovery explicitly scoped. Preserve shared local changes without turning the root into Git.

**Non-Goals:** Parallel coding workers, atomic cross-repository merges, deployment automation, automatic remote file synchronization, or modifying the user's real workspace while building Coderbot. The bot is not restarted by implementation/testing.

## Decisions

### 1. Explicit mode and immutable execution contexts

Keep CODEBOT_REPO_PATH as the target path for compatibility; add CODEBOT_TARGET_MODE=repo|workspace, default repo. Workspace mode accepts an optional manifest path for discovery exclusions, base overrides, shared input patterns, checks, and Docker mappings; repository discovery and affected selection remain autonomous. Introduce workspace/repository context objects and pass explicit contexts to operations rather than mutating global config.REPO_PATH per repository. A one-repository adapter preserves the current path.

Discovery walks bounded workspace directories, skips dependency/build caches, recognizes .git files and directories, verifies exact Git top-level roots, and canonicalizes symlinks. Nested Git roots have explicit ownership boundaries; parent operations exclude child roots. Record normalized remote identity and relative location; do not infer base from the currently checked-out feature branch. Resolve base from configured override or origin's default branch, with an actionable pause if neither can be determined.

Alternative: auto-detect mode or monkeypatch global paths. Rejected because ambiguous ancestor Git discovery and concurrent utility/conversation turns make these unsafe.

### 2. One feature with independent repository checkpoints

Version workspace state with target identity, inventory, selected/verification repositories, shared plan, workspace session, and local edit journal. Repository records contain relative path, remote identity, base branch/SHA, branch, mutation ownership, check snapshots, PR URL, review/merge status, and publication checkpoints. Durable records use workspace+feature identity plus repository identity where appropriate. Schema migration retains single-repo legacy behavior but never silently adopts active state into a different target.

Validate identity before task migration, inbox processing, outbox replay, sessions, or phase dispatch. IDLE without task records can initialize against a new target; active mismatches pause. Inventory drift affecting enrolled repositories also pauses instead of rebinding a PR or session.

Alternative: separate independent bot tasks per repository. Rejected because it fragments shared approval, cross-service verification, and completion.

### 3. Autonomous enrollment with mutation boundaries

Run one agent session at the workspace root for shared exploration and planning. Emit structured repository-selection/enrollment results; the controller validates each selected Git root and safely prepares its branch before granting implementation writes. Later dependency enrollment uses the same controller path without selection approval. Keep existing approval for functional changes; adding a repository is not itself an approval trigger.

Configure the runner/tool permission boundary so candidate repositories are read-only until enrolled, and external roots are not writable. Prompt instructions alone are not an enforcement mechanism. Generic shell commands must not bypass the boundary: the selected runner must use a restricted execution mechanism or expose scoped execution tools; unsupported enforcement configurations fail closed. Test both supported runner paths and subprocess writes. Observe changes across the inventory before/after turns as defense-in-depth and report unexpected mutations without deleting them.

Check selected repositories for user changes and Git operation markers before checkout; do not reset existing branches. Per-repository Git helpers and explicit gh --repo owner/name avoid root/remote ambiguity. Configure safe.directory and HTTPS aliases for discovered repositories in container startup. Excluded repositories remain usable as read-only integration dependencies.

Alternative: freely writable workspace with post-turn diff checks only. Rejected because detecting unintended edits after the fact does not protect user work.

### 4. Shared local planning and recoverable edits

Resolve OpenSpec from the workspace root and associate tasks with repo ids or shared areas. Maintain one feature plan and existing proposal workflow. Workspace root never gets git init, commit, or PR.

Journal eligible shared files in private Coderbot data, with before/after hashes, file modes, existence markers, and content versions. Default eligible areas cover OpenSpec, docs, scripts, E2E source, and Compose configuration; exclude secret patterns, user credential material, generated outputs, and external links. Use existing redaction plus explicit secret-file exclusion; content suspected to contain secrets is excluded from recoverable bodies and public output. Hash excluded secret inputs only through private HMAC when they influence environment identity. Recovery requires current content to match the recorded bot version before restoring; never auto-restore divergent files. Journal before enabling a turn and finalize only observed bot changes, avoiding indiscriminate workspace backups.

Alternative: make root a repository. Explicitly rejected by the user. Local journals are recovery aids, not portable delivery or source control.

### 5. Composite verification and isolated test infrastructure

Retain existing per-repo snapshot/baseline logic behind repository contexts. Add composite workspace identity: sorted repo content identities for changed and test-dependency repositories, plus eligible shared-input identity and environment identity. Identify checks by scope/repo/command to avoid collisions. Before implementation capture a compatible baseline vector for integration comparisons; unsupported baseline recreation reports indeterminate rather than comparing unrelated checkout combinations.

Discover required checks per changed repository and shared workspace checks. Workspace tests can depend on unmodified repos; their content is still part of evidence identity. Map reported E2E paths to the shared or repository harness explicitly. Quality gates, waivers, review snapshots, and cached results cannot use a single repository's SHA as the whole-feature identity.

Use an explicit test daemon and host-path mapping with a task-specific Compose project/resources. Analyze configured test commands and reject unsafe cleanup scripts; do not silently rewrite arbitrary scripts. Provide a dedicated container-compatible test adapter where needed: no macOS open, no development volumes/project cleanup, and no guessed Azure mappings. Actual Docker execution remains opt-in in tests.

### 6. PR aggregation rather than atomic delivery

Keep the feature lifecycle as coordinator; repository subrecords advance through publication, review, conflicts, and merge independently. Publish only repos with deliverable changes, with all PRs linking to the shared feature and explaining integration evidence and dependency order. Reconcile remote publication before retries. Track notifications independently so accepted PR creation is not repeated after message failure.

Announce each PR as opened, then update consolidated status in the existing feature thread. Root-only work has no invented PR. Completion requires all required merges and verification covering delivered content; if merged content differs, rerun relevant checks. Partial merge is visible and recoverable, never treated as a rollback-able transaction. Automatic merging respects existing policy and declared compatibility order.

### 7. Incremental integration with explicit coverage

Introduce contexts/state/discovery first, then connect planning/enrollment, Git/recovery, checks/evidence, and multi-PR coordination. Do not advertise workspace mode as supported until all lifecycle paths—including replanning, review fixes, conflict resolution, held tasks, and recovery—use the correct contexts. A guard rejects unsupported phases during development rather than falling back to root-level Git.

## Risks / Trade-offs

- [Global single-repo assumptions are widespread] → Audit all config.REPO_PATH, scalar PR/branch, and cwd usages; preserve repo-mode regressions and add end-to-end controlled workspace flows.
- [Tool permissions might not constrain shell writes] → Require effective execution isolation, include adversarial subprocess/symlink tests, and fail closed where unavailable.
- [Shared local changes are not delivered by PRs] → Keep recoverable non-secret records and prominent local-only file reporting.
- [Autonomous enrollment broadens scope] → Limit to approved functional scope and configured workspace; journal selection reasons, protect user changes, and expose affected repositories in delivery.
- [Partial merges break compatibility] → Declare contract/merge ordering and backwards-compatible transitions; pause blocked delivery rather than promise atomicity.
- [Integration cleanup can destroy development data] → Explicit daemon/mapping, isolated projects/volumes, and rejection of unsafe scripts.
- [Full workspace scans are expensive] → Bound discovery and content selection; exclude generated assets and retain dependency-aware evidence invalidation.

## Migration Plan

1. Ship repo-mode compatibility and workspace state validation first; never reinterpret existing active state automatically.
2. Implement workspace orchestration behind explicit target mode and documented preflight.
3. Validate using temporary synthetic repositories, fake GitHub/channel adapters, and opt-in isolated Docker tests; no live backlog, PR, or workspace mutation.
4. Document configuration and local-only delivery limitations. Leave the current container stopped; require explicit user authorization to enable/run it.
5. Rollback by disabling workspace mode only after pausing/archiving the workspace feature; retain journals/checkpoints and credentials. Older builds must not consume workspace state as single-repo state.
