## MODIFIED Requirements

### Requirement: Conditional E2E gate
Codebot SHALL only run the e2e suite and require it to pass when the target repo
was detected to have an e2e harness for this task. When no harness was detected,
codebot SHALL proceed from internal review to OpenSpec archival without invoking
an e2e run, and SHALL NOT treat the absent harness as a test failure. Pull-request
creation SHALL remain blocked until the applicable e2e path and archival gate
have completed.

#### Scenario: E2E harness present
- **WHEN** internal review completes and an e2e harness was detected for this task
- **THEN** codebot runs the e2e suite and only proceeds to OpenSpec archival once it passes

#### Scenario: E2E harness absent
- **WHEN** internal review completes and no e2e harness was detected for this task
- **THEN** codebot proceeds directly to OpenSpec archival without attempting to run an e2e suite or treating its absence as a failure

## ADDED Requirements

### Requirement: Fresh verification gate
After implementation, codebot SHALL enter a persisted verification phase that
requires fresh test evidence, strict validation of the active OpenSpec change,
and confirmation that all implementation tasks are complete. Codebot SHALL NOT
advance on a missing, malformed, or failed verification result.

#### Scenario: Verification succeeds
- **WHEN** the agent reports the commands it ran, those commands pass, strict OpenSpec validation passes, and every apply task is complete
- **THEN** codebot records verification success and advances to internal review

#### Scenario: Verification evidence is missing or failed
- **WHEN** the verification response omits the required result contract or reports a failed command, invalid change, or incomplete task
- **THEN** codebot remains in verification and retries or asks the user for help within its configured retry limit

### Requirement: Internal review gate
After verification, codebot SHALL require an internal code review against the
OpenSpec requirements before running the optional e2e gate. Critical and
important findings SHALL be fixed and re-reviewed, and tests covering review
fixes SHALL be rerun before the review can pass.

#### Scenario: Internal review is clean
- **WHEN** the reviewer finds no unresolved critical or important issue and the response satisfies the review result contract
- **THEN** codebot records internal-review success and advances to the applicable e2e path

#### Scenario: Internal review finds a blocking issue
- **WHEN** the reviewer reports a critical or important issue
- **THEN** the agent fixes it, reruns covering tests, and obtains a clean re-review before codebot advances

### Requirement: E2E repairs repeat quality gates
Any tracked code change made to repair a failed e2e run SHALL pass fresh
verification and internal review before codebot reruns the e2e suite or
proceeds toward a pull request. When the repair attempt leaves the commit and
tracked working tree unchanged, codebot MAY rerun E2E directly.

#### Scenario: Agent repairs an E2E failure
- **WHEN** the agent changes tracked files while addressing a failed e2e run
- **THEN** codebot transitions to verification and internal review before running the e2e suite again

#### Scenario: E2E failure needs no tracked repair
- **WHEN** the agent addresses a failed e2e run without changing the branch commit or tracked working tree
- **THEN** codebot may rerun the e2e suite without repeating verification and internal review

### Requirement: Validated OpenSpec archival gate
After all applicable quality gates pass, codebot SHALL sync and archive the
OpenSpec change on the feature branch, validate the resulting main specs and
archive, and commit those planning changes before creating the pull request.
The archive operation SHALL be restart-safe and SHALL NOT be repeated when the
expected archive already exists.

#### Scenario: Archive succeeds
- **WHEN** verification, internal review, and the applicable e2e path have passed
- **THEN** codebot syncs and archives the change, validates the resulting OpenSpec state, commits the archive and spec updates, and advances to pull-request creation

#### Scenario: Coderbot restarts after the archive moved
- **WHEN** persisted state still names the archival phase but the active change is absent and its expected archive exists
- **THEN** codebot validates and commits the existing archive result instead of attempting to archive the change again

#### Scenario: Archive or validation fails
- **WHEN** sync, archive, or post-archive validation fails
- **THEN** codebot does not create a pull request and retries or asks the user for help within its configured retry limit

### Requirement: Native pull-request creation
After validated archival, coderbot SHALL push the existing feature branch and
create the pull request directly. The coding agent SHALL NOT receive authority
to push the branch or create the pull request.

#### Scenario: Archive is committed
- **WHEN** validated OpenSpec archive changes are committed on the feature branch
- **THEN** coderbot pushes that branch, creates a pull request against the configured base branch, and stores the returned URL
