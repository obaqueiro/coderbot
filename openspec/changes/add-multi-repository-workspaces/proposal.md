## Why

Features can span independent repositories and shared local integration infrastructure. Coderbot currently assumes its target is one Git repository, preventing safe planning, implementation, verification, and delivery from a non-Git workspace root.

## What Changes

- Add explicit workspace mode while preserving existing single-repository installations.
- Discover Git repositories autonomously, including worktrees; select and extend the affected subset without repository-selection approval.
- Coordinate one feature, shared OpenSpec plan, coding session, and communication thread with independent repository branches, checks, PRs, reviews, and merge checkpoints.
- Permit shared workspace edits outside Git; preserve recoverable non-secret bot edits locally and disclose that PR merges do not deliver those files.
- Verify affected repositories and the assembled workspace using composite content identities and isolated integration infrastructure.
- Validate persisted workspace identity before resuming; protect existing user changes and unselected repositories.
- Announce each PR and consolidated local-only changes; complete a feature only after required PR merges and valid integrated verification.

## Capabilities

### New Capabilities
- `multi-repository-workspaces`: Discovery, autonomous repository scope, local workspace edits, coordinated execution, verification, delivery, and restart safety.

### Modified Capabilities
None. Existing quality and communication requirements remain applicable; the new capability specifies their workspace orchestration.

## Impact

Configuration/setup, container startup, main lifecycle, agent runners and prompts, task records/execution store/checkpoints, Git provenance and recovery, OpenSpec integration, content identity, check discovery/baselines/evidence, GitHub PR orchestration, and Slack/email status reporting. Add unit and controlled multi-repository integration tests. No changes to target repositories or live workspace infrastructure are authorized as part of implementing Coderbot itself.
