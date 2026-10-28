## Purpose

Enable one feature to span independently versioned repositories and local shared infrastructure without assuming the workspace root is a Git repository.

## ADDED Requirements

### Requirement: Explicit workspace discovery and compatibility
The system SHALL support an explicit workspace mode alongside unchanged single-repository behavior. It MUST discover independent Git roots beneath the configured workspace, support Git worktrees, identify repository remotes and base branches, exclude generated directories, and reject writable paths resolving outside the workspace. It MUST NOT treat arbitrary folders or an ancestor Git repository as child repositories.

#### Scenario: Non-Git root with mixed children
- **WHEN** a workspace contains repositories, a worktree, shared test folders, and caches
- **THEN** the system inventories actual repositories and shared areas without running root-level Git operations or treating caches as repositories

#### Scenario: Single-repository installation
- **WHEN** workspace mode is not enabled
- **THEN** the existing single-repository lifecycle and configuration remain supported

### Requirement: Autonomous affected repository scope
The system SHALL discover and select the repositories needed for a feature without a repository-selection approval gate. It SHALL permit later selection expansion, record the reason, and apply existing feature-level approval policies without adding a repository-specific approval requirement. Unselected repositories MUST remain unmodified by bot operations.

#### Scenario: Feature spans a subset
- **WHEN** exploration determines that two of seven repositories require changes
- **THEN** the bot selects those two autonomously and does not branch, commit, or edit the other five

#### Scenario: Additional dependency discovered
- **WHEN** implementation identifies another repository necessary for the approved feature
- **THEN** the bot enrolls it safely without requesting repository-selection approval and records why it was included

### Requirement: Safe local shared workspace edits
The system SHALL permit edits to shared workspace files without initializing Git or creating a PR for the workspace root. It SHALL retain recoverable before/after versions of eligible bot edits and report local-only changes. It MUST exclude credentials, secret files, external symlink targets, and generated artifacts from recoverable content and public reports. It MUST preserve concurrent user edits rather than overwriting them during recovery.

#### Scenario: Integration harness changes
- **WHEN** the bot changes shared Compose, E2E, or OpenSpec files
- **THEN** those changes remain local, eligible changes have recoverable records, and delivery explicitly states that merging repository PRs does not deliver them

#### Scenario: Concurrent user change
- **WHEN** a user modifies a file after the bot recorded its version
- **THEN** recovery refuses to overwrite the user's version and identifies the conflict

### Requirement: Repository-specific execution and durable recovery
The system SHALL maintain independent repository identity, base commit, working branch, checks, PR, review, and merge progress under one feature. All Git and PR operations MUST target an explicit repository. Existing user work and unfinished Git operations MUST be protected, and restart MUST resume completed checkpoints without repeating externally accepted effects.

#### Scenario: Dirty selected repository
- **WHEN** a selected repository contains preexisting uncommitted changes
- **THEN** the bot does not reset or switch that checkout and reports the blocker

#### Scenario: Restart after first PR
- **WHEN** one repository PR is created and the bot restarts before publishing the second
- **THEN** it reconciles the first PR and continues the remaining work without creating a duplicate

### Requirement: Workspace-bound state
The system MUST validate persisted task workspace identity and repository identities before executing work, delivering queued task messages, or resuming sessions. A changed target MUST cause a safe actionable pause rather than silent task migration.

#### Scenario: Configured target changed
- **WHEN** saved state belongs to a different workspace from the configured target
- **THEN** the bot pauses before task execution or delivery and asks for explicit reset or migration

### Requirement: Shared planning and integrated verification
The system SHALL use one workspace-level OpenSpec plan and coordinated coding context, honoring workspace and repository instructions. It SHALL run required checks for affected repositories and shared integration checks. Verification and evidence MUST identify the exact relevant repository content and local shared inputs, including read-only dependency repositories used by integration tests. Changed inputs MUST invalidate previous verification.

#### Scenario: Cross-service feature
- **WHEN** a feature changes frontend and API repositories
- **THEN** the bot verifies both repositories and their integrated behavior before declaring the feature ready

#### Scenario: Dependency changes after evidence
- **WHEN** a repository or shared file used by the integration suite changes after a successful run
- **THEN** the earlier integration evidence is not accepted for the new content combination

### Requirement: Isolated integration execution
The system MUST identify the Docker daemon and host mount mapping used for integration tests and isolate test project resources from development infrastructure. It MUST NOT execute cleanup against an unverified daemon, development project, or shared persistent volume.

#### Scenario: Remote daemon without mapping
- **WHEN** test infrastructure targets a remote daemon but its host paths are not configured
- **THEN** the bot blocks execution with an actionable configuration error instead of guessing paths or cleaning up resources

### Requirement: Coordinated PR reporting and completion
The system SHALL publish one PR per changed repository requiring delivery and announce each in the feature's existing communication thread. It SHALL report all PRs, local-only changes, dependency order, and partial merge status together. It MUST NOT mark the backlog feature done until all required PRs are merged and required integration verification is valid. Local-only features SHALL complete after required verification and explicit local-change delivery reporting without inventing a PR.

#### Scenario: Partial merge
- **WHEN** one of two required PRs merges
- **THEN** the feature remains incomplete and the communication thread identifies the outstanding PR

#### Scenario: Entire feature delivered
- **WHEN** every required PR is merged and required verification covers the delivered content
- **THEN** the bot marks the feature complete and reports any remaining local-only infrastructure changes
