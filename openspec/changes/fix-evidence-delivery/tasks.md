## 1. Regression coverage and harness contract

- [x] 1.1 Add failing codebot regressions reproducing empty cached recording/upload, zero-attachment investigation replies and delivery-only requests incorrectly entering replanning.
- [x] 1.2 Document generic recording validation and recovery; verify codebot reports harness rejection precisely without bypassing it or encoding target-specific inventories.

## 2. Provenance and media processing

- [x] 2.1 Persist snapshot/run/report provenance for successful recordings and expose structured capture/validation diagnostics while preserving rejected-run source clips.
- [x] 2.2 Unify supplied and recorded artifact validation; reject unknown/stale provenance, select requested demos and eliminate raw/HTML duplicate clips by hash.
- [x] 2.3 Produce content-addressed MP4 files, verify readable video and positive duration using ffprobe, and retain source clips on conversion failure.

## 3. Durable retryable delivery

- [x] 3.1 Add optional checkpoint result validation and explicit retryable/blocked/not-applicable outcomes while keeping unrelated legacy callers compatible.
- [x] 3.2 Reconcile legacy empty and stale completions without deleting history; resume the first incomplete step and revalidate cached files.
- [x] 3.3 Build a shared controller-owned service for record, convert, upload, access, PR sync and notification with independently persisted outcomes.

## 4. Drive access and PR synchronization

- [x] 4.1 Return structured uploaded-file metadata and reconcile existing hash-based uploads before retrying external effects.
- [x] 4.2 Always preserve inherited folder permissions, verify effective ACLs including domain access, and preserve uploaded files when verification fails; never create public or individual grants.
- [x] 4.3 Verify the bot-owned PR video section by read-back, preserve human edits and retry failed PR synchronization without another upload.

## 5. Feedback and actual message delivery

- [x] 5.0 Retain exact merge replies and bare displayed PR choices during repair/push/review; dispatch only for the same actionable decision with existing merge gates, preserving explicit lateral and stale-decision protections.

- [x] 5.1 Extend investigation contracts with structured delivery intent and configured capability context; preserve mixed product requirements and pending merge/approval decisions.
- [x] 5.2 Schedule durable delivery requests at safe points without product replanning solely for publication; keep failed requests pending and ask only concrete access/destination questions.
- [x] 5.3 Validate structured attachments and compatible ATTACH markers through a common allowed-path extractor and pass actual files into investigation message delivery.
- [x] 5.4 Report real links/attachments and exact pending steps; remove unsupported claims that capture, sharing or delivery succeeded.

## 6. Integration and operational recovery

- [x] 6.1 Add end-to-end controller tests covering fresh delivery, existing current demo, failed conversion/upload/access/PR sync, restart reconciliation and duplicate prevention.
- [x] 6.2 Run focused and full codebot tests and strict OpenSpec validation; record results and any remaining blockers without marking partial tasks complete.
- [x] 6.3 Document deployment/rollback, sharing configuration and explicit PR recovery procedure including head/provenance checks and no merge; do not execute deployment or publication as part of this change.
