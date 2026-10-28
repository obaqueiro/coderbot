# Evidence delivery and recovery

Evidence is not delivered merely because a WEBM exists. Codebot validates the run,
binds artifacts to the implementation snapshot, probes playable MP4 media, verifies
Drive permissions, reads back the PR video section and sends the actual link/files.
No evidence belongs in Git.

## Configuration

- `CODEBOT_EVIDENCE_UPLOAD=on` enables upload.
- `CODEBOT_DRIVE_FOLDER_ID` selects an authorized destination; empty uses the existing
  `Codebot evidence` folder convention.
- Delivery ALWAYS inherits the folder permissions. Codebot verifies the parent and
  effective ACL, including domain permissions, without creating or modifying permissions.
- `CODEBOT_DRIVE_SHARE_MODE` and `CODEBOT_DRIVE_REVIEWERS` remain compatibility settings;
  neither enables public sharing nor creates individual grants.

The Google token needs Drive write scope. Do not paste tokens or secrets into Slack.
Upload failure is retryable; unverifiable inherited access is blocked, not successful.
An uploaded file is reused by SHA-256 if access needs checking again.

## Reconcile before recording

After a controller-approved verification or internal review, attached MP4 files are
registered with hashes and an implementation-content identity (excluding OpenSpec
planning/archive paths). Finalization first rechecks an attested published file against
Drive size/checksum, parent ACL and the current PR link. It reuses that link without
recapturing or uploading. Local attested media can also be reused before publication.
An arbitrary PR link or agent prose alone is never proof of current evidence.

Legacy deliveries without this attestation need a controller-accepted verification
of the existing MP4; keep the MP4 and delivery receipt attached to that result. A failed
remote reconciliation does not start duplicate capture or upload. `/results/` paths in
Playwright reports are mapped only into the current run's `test-results/` directory;
traversal, unrelated paths and escaping symlinks are rejected.

## Harness validation and recording recovery

PR-ready and feedback delivery require a playable MP4 (or the validated Newman
HTML report). Missing evidence blocks the ready cover and merge wait: it is not a
successful delivery with zero attachments. If the filtered `@evidence` recording
fails validation or produces no complete videos, codebot records the complete
selected specs, validates the fresh run, and only then converts its demo clips.
Rejected clips remain diagnostic artifacts, never delivery evidence.

If that full recording still fails, the controller enters bounded automatic
diagnosis/implementation repair. The agent must implement missing approved
functionality, demo tests, runner/video support or conversion prerequisites, not
merely label evidence invalid. Changed code receives internal review and controller
final checks before pushing; recording and delivery are retried afterward. Archived
OpenSpec plans are not restored or rearchived. Exhausted repairs remain blocked and
request concrete recovery guidance; they never waive evidence or invite merge.

Target harnesses own their selection and validation semantics. Coderbot does not
encode a target's spec inventory or alter its validator. Zero matches, missing
cases, skips, failures, retries, report errors and missing/duplicate videos are not
successful evidence. Codebot does not waive a rejected harness exit. Its diagnostics
distinguish capture from post-capture validation and retain clips.

## Deployment and PR recovery

Exact `merge`, `merge anyway` and bare displayed PR options received during review
repairs or pushing are retained for the same PR decision and processed at WAIT_MERGE.
They do not interrupt a running repair, authorize a changed decision or bypass the
normal merge handler. `/btw merge anyway` remains informational, never authorization.
If the reviewed implementation changed, normal content checks can require renewed
review before merge. Sending a decision is not confirmation that GitHub merged it.

1. Review and test the codebot change. Target changes require separate authorization.
2. Obtain explicit deployment authorization and configure authorized Drive access.
3. Stop the controller safely. Check separately spawned E2E containers: compose down
   of codebot does not necessarily terminate those projects. Do not delete unrelated
   projects or worktree changes.
4. Deploy, retaining SQLite, manifests, source media and OAuth data. Invalid legacy
   empty checkpoints are revalidated lazily without deleting audit history.
5. Request delivery in the existing task thread, without authorizing merge. Confirm
   the current PR head and reconcile working changes. Existing media without trusted
   matching provenance must be regenerated, not assumed current.
6. Confirm playable MP4, remote file and reviewer access, PR section read-back and
   actual conversation link. Keep review/merge gates unchanged.

Rollback: stop the controller and restore the prior code only; retain data and media.
Prior code may not understand new delivery metadata and does not enforce verified
sharing. Keep publication disabled until a compatible version is deployed. Never
restore an old state.json over new work or delete delivery history just to force retry.
