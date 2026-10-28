## Why

A rejected filtered recording can cause codebot to cache an empty delivery as complete. Later requests may be diverted to product replanning, and messages claiming to share videos can contain zero attachments. Evidence delivery must be independently verifiable and recoverable.

## What Changes

- Validate filtered demo inventories separately from full-suite verification, without accepting failed or omitted cases.
- Unify recording and supplied-artifact handling with snapshot/run provenance, clip deduplication, unique MP4 outputs and media validation.
- Retry empty, failed or stale delivery checkpoints without repeating successful recording or duplicating uploads.
- Route evidence delivery requests through controller-owned delivery at safe points, independently of product replanning and merge authorization.
- Deliver real attachments or verified links; distinguish upload, reviewer access, PR synchronization and notification outcomes.
- Provide an explicit, nonautomatic recovery procedure for existing PRs after deployment.
- Retain explicit PR decisions received during review repairs until the same decision is actionable; never treat them as lateral chat or bypass existing merge gates.

## Capabilities

### New Capabilities
- `verified-evidence-delivery`: Provenance-bound, retryable demo delivery through recording, conversion, Drive, PR synchronization and conversation notification.

### Modified Capabilities

None. Existing review gates and milestone communication remain applicable.

## Impact

Codebot: `src/evidence.py`, `src/artifact_manifest.py`, `src/delivery_checkpoint.py`, `src/drive_client.py`, `src/feedback.py`, `src/main.py`, configuration, tests and operational documentation. No deployment, external upload, merge or live-state mutation is performed by implementation tests.

Target harness validators belong to their own repositories. This change validates Coderbot's handling of rejected recordings without encoding application-specific inventories or making unauthorized target edits.
