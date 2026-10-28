## 1. Configuration and discovery

- [x] 1.1 Add explicit repo/workspace target mode and validated optional workspace manifest while preserving repo-mode defaults.
- [x] 1.2 Implement immutable workspace/repository contexts, bounded Git-root discovery, worktree support, symlink containment, and nested-root exclusions.
- [x] 1.3 Resolve remote identities and per-repository default base branches with override support and actionable failures.
- [x] 1.4 Add discovery/configuration tests for mixed folders, ancestor Git roots, caches, worktrees, nested repositories, and differing base branches.

## 2. Durable feature and repository state

- [x] 2.1 Introduce versioned feature state with workspace identity and independent repository records/checkpoints.
- [x] 2.2 Extend task identities and execution-store records to distinguish workspace features and repository operations.
- [x] 2.3 Validate active target/inventory identity before migration, delivery replay, inbox dispatch, sessions, or work; permit blank IDLE initialization.
- [x] 2.4 Add migration/restart tests for legacy repo mode, changed target, changed remote, missing repository, and partial repository progress.

## 3. Autonomous enrollment and runner boundaries

- [ ] 3.1 Add structured initial/later repository enrollment from workspace exploration without repository-selection approval.
- [ ] 3.2 Implement controller-owned clean-tree/operation-marker checks and safe per-repository branch preparation without resetting user branches.
- [ ] 3.3 Add effective runner execution isolation for unselected repositories/external paths, including shell subprocess writes; fail closed for unsupported configurations.
- [ ] 3.4 Keep one workspace coding context, load applicable instructions, and record selection reasons and before/after inventory provenance.
- [ ] 3.5 Test subset selection, late enrollment, dirty repositories, unchanged excluded checkouts, and adversarial write/symlink bypasses for supported runners.

## 4. Shared planning and local edit recovery

- [ ] 4.1 Resolve shared OpenSpec root and repository/shared-area task ownership independently of Git roots; retain existing feature approval behavior.
- [ ] 4.2 Implement private local edit journals with before/after content, modes, deletion/new-file records, and credential/generated-file exclusions.
- [ ] 4.3 Implement conflict-aware local recovery that refuses to overwrite divergent user content and never initializes workspace Git.
- [ ] 4.4 Test shared plan/harness changes, secret exclusions, local-only features, and concurrent user edit preservation.

## 5. Repository-specific lifecycle and recovery

- [ ] 5.1 Introduce explicit repository Git/GitHub helpers and a repo-mode adapter; audit global cwd and scalar branch/base/PR assumptions.
- [ ] 5.2 Update entrypoint authentication/safe directories and setup validation for all discovered repositories.
- [ ] 5.3 Connect feature coordinator and repository subrecords through implementation, replanning, held-task resume, review fixes, conflicts, and recovery.
- [ ] 5.4 Adapt provenance/ownership and recovery isolation to repository contexts plus the shared local edit journal.
- [ ] 5.5 Test lifecycle paths with two changed repositories and an untouched third; ensure no fallback Git operation targets the workspace root.

## 6. Composite checks and evidence

- [ ] 6.1 Implement composite snapshots for changed/test-dependency repositories, shared inputs, and private environment identity.
- [ ] 6.2 Discover and execute repository-specific and shared checks with collision-free identities and compatible baseline vectors.
- [ ] 6.3 Update quality gates, waivers, reported test paths, review snapshots, evidence storage/publication, and cache invalidation for composite content.
- [ ] 6.4 Add explicit Docker daemon/host-path preflight and isolated test projects/resources; reject unsafe cleanup and unsupported arbitrary harness scripts.
- [ ] 6.5 Provide documented container-compatible workspace test adapter behavior without macOS open or guessed Azure paths.
- [ ] 6.6 Test dependency/local-input invalidation, indeterminate baseline handling, zero-test gates, unsafe cleanup rejection, and optional isolated Docker execution.

## 7. Multi-PR delivery and completion

- [ ] 7.1 Create/reconcile one PR per changed repository using explicit remote identity and independent durable publication checkpoints.
- [ ] 7.2 Track review, conflicts, merge order, and partial merges independently while retaining feature-level coordination.
- [ ] 7.3 Announce each opened PR in the existing thread and publish consolidated PR/local-only changes and dependency status with durable notification deduplication.
- [ ] 7.4 Gate backlog completion on required merges and delivered-content integration verification; handle local-only features without inventing a PR.
- [ ] 7.5 Test accepted PR creation followed by crash/message failure, partial merges, changed merged content, and final aggregated delivery.

## 8. Regression validation and rollout

- [ ] 8.1 Ensure tests always isolate data and never consume/write live state, invoke live backlog/channel adapters, or modify the real feature workspace.
- [ ] 8.2 Add controlled end-to-end synthetic workspace tests covering discovery through integrated verification, multiple PRs, restart, and completion.
- [ ] 8.3 Run existing repo-mode regression suites and complete the audit of all lifecycle/config.REPO_PATH/scalar PR assumptions before enabling workspace support.
- [ ] 8.4 Document workspace configuration, autonomous selection, local-only delivery, Docker safety, state mismatch handling, and rollback limitations.
- [ ] 8.5 Validate the OpenSpec change and report implementation/test results while leaving the current bot container stopped.
