## Context

See proposal.md. Recording scans both raw and HTML copies, finalization caches all return values as completed, Drive returns links even when sharing fails, and feedback investigation sends answer text without attachments. Target harnesses may reject filtered recordings; codebot must not silently waive their exit status.

## Goals / Non-Goals

**Goals:** Share one controller-owned delivery service across finalization, feedback updates and explicit delivery requests; retain durable provenance and step-specific recovery.

**Non-Goals:** Change product behavior, bypass final checks, auto-merge, deploy automatically, publish a live PR during tests, or mutate another repo from this change.

## Decisions

1. **Typed delivery results and validated checkpoints.** Add optional result validation to checkpoints and structured outcomes for recording/upload/access/PR/message steps. Existing unrelated checkpoints retain compatibility. Invalid cached results become retryable with audit history. Prefer this to deleting SQLite rows or retrying every step blindly.
2. **Run manifests are the authority.** Capture snapshot before execution and persist run/report/hash provenance; supplied files need matching provenance. Harvest only current-run raw clips, deduplicate by hash, and prefer the requested evidence test. Never reuse a file based solely on mtime or existence. Retain diagnostic clips on rejected harness runs without treating them as approved.
3. **Content-addressed conversion.** Store outputs under a run/content-specific evidence directory. Validate MP4 with ffprobe (video stream and positive duration); keep source clips on failure. Test media probing with fixtures and mocks. Do not overwrite a process-global evidence.mp4.
4. **Controller-owned delivery intent.** Extend validated feedback classification with a delivery action and structured requested operations. Supply configured capabilities to the classifier. Persist requests, execute at safe points, and preserve all user-side waits and pending decisions. Avoid an unconditional keyword trigger that could misroute mixed product requests.
5. **Structured attachments.** Extend the investigation result with validated attachments, retaining compatible ATTACH parsing through a shared extractor. Resolve only allowed outbox/evidence paths, reject traversal and unavailable files, and pass files to the sender. Do not infer successful delivery from answer prose.
6. **Separate upload from inherited access verification.** Always preserve existing folder ACLs; never create public or individual permissions. Verify the file's parent and effective folder principals, including domain ACLs. Retain legacy configuration keys without enabling permission mutation. Return file id, URL and access outcome; retry read-only access checks without another upload.
7. **PR sync is its own step.** Preserve the bot-owned video section and human content, read back to verify the expected link, then notify. Failed sync remains retryable; upload metadata survives restarts. Required delivery status does not silently authorize merge or falsely mark all delivery complete; ordinary review progress remains independent.
8. **Reconcile before recording.** Register attached MP4s only after controller acceptance of verification/internal review. Bind their hashes to implementation files excluding OpenSpec paths so archive-only changes retain evidence. Require matching attestation, live Drive checksum/size/parent/ACL and the existing PR URL before adoption. A local receipt or arbitrary PR URL alone is insufficient. Map only the documented `/results/test-results/` mount into the current run and reject traversal and symlink escapes.
9. **Defer PR decisions, not authority.** An exact merge reply or bare displayed option during repair/push/review belongs to the controller queue. Bind it to the same PR URL and displayed decision, wait for WAIT_MERGE, and use the normal merge handler. A changed decision requires confirmation. Explicit /btw, mixed conditional messages and different tasks never acquire merge authority.

## Risks / Trade-offs

- Legacy artifacts lack snapshot provenance → regenerate unless trustworthy matching manifest/report metadata exists.
- Sharing policies differ → explicit modes, blocked access outcome and targeted clarification; no automatic public escalation.
- LLM classification can omit mixed requirements → structured validation, preserved original request and regression coverage.
- External side effects cannot be transactional with SQLite → reconcile by artifact hash, file id, PR section and existing message receipt before retry.
- Target harness behavior is outside this change → keep codebot failure reporting strict without encoding application-specific validator contracts.

## Migration Plan

Run regression tests locally, review changes, then deploy only on explicit authorization. Reconcile legacy empty/stale checkpoints lazily using validators, retaining their audit trail. Configure authorized sharing before live delivery. For an existing PR, explicitly authorize recovery, verify the current head, regenerate if provenance is insufficient, upload, verify access and PR section, and notify without merging. Rollback preserves source media and durable upload identities; new structured data must remain readable or ignored safely by legacy callers.
