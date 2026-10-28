# Coderbot self-healing

Self-healing is opt-in. An independent standard-library supervisor is baked into
the image; it does not import Coderbot's repairable application modules. The
default `CODEBOT_SELF_HEAL=off` preserves direct worker startup.

## Modes and rollout

Run `scripts/setup.sh` (or `scripts/setup.sh --text`) and open **Self-healing**.
Choose **Disabled** to turn it off, **Enabled** for automatic validated repairs,
or **Observe** / **Propose** for a gradual rollout. Save and Close updates `.env`
without starting or restarting the bot. You can also edit the setting directly:

```dotenv
CODEBOT_SELF_HEAL=observe
```

- `off`: existing worker behavior.
- `observe`: supervise startup/liveness and record/report worker incidents; no patches.
- `propose`: generate and test a candidate; retain it for manual review, no promotion.
- `auto`: validate, promote Python repairs, restart the worker, and observe recovery.

Rebuild the image before using this feature: the stable supervisor and launcher
are copied into the image. Start with `observe`, then `propose`; use `auto` only
after confirming the installed coding CLI's credentials and local deployment.
No automatic Git commit/push or Docker image rebuild is performed.
Stop any legacy/manual worker sharing the source before switching to supervision.

Claude repairs use `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`, not the
host's conversation history. OpenCode repairs copy stored provider authentication
and the selected provider's JSON configuration (including
`OPENCODE_CONFIG_CONTENT` and its explicit environment references). They do not
copy global agents, MCP definitions, or task sessions. JSONC-only custom provider
definitions must also be supplied as JSON for this initial adapter. V2 runs use
`--standalone`; the installed legacy CLI's private `run` path remains supported.

## Repair boundaries

Automatic source repairs are limited to reproducible TypeError, AttributeError,
KeyError, IndexError, JSONDecodeError and eligible startup SyntaxError incidents.
User-input waits, credentials/configuration, outages, disk errors, and application
test failures are not automatically reclassified as implementation bugs.

The supervisor stops the worker before snapshotting. Snapshots include current
uncommitted source but exclude live `data/`, `.env`, and target repositories.
The repair agent gets its own provider session/configuration; it cannot certify
its own patch. Promotion accepts only Python source changes plus newly added
`tests/test_selfheal_regression_*.py` tests. Existing tests, approval/recovery
functions, configuration, credential handling, event boundaries, dependencies,
and supervisor files are not automatically editable. Syntax repair of approval-
bearing `main.py` needs manual review.

A new regression must fail on the old implementation and pass on the candidate.
The full candidate suite must pass with a fresh HOME/data/XDG environment and
without task/provider-integration credentials. New regression tests cannot skip;
existing environment-dependent suite skips remain unchanged. Existing full-suite failures require manual review in this first
release rather than automatic waivers.

Candidate/test processes run under the trusted-agent model. Environment/path
separation is **not an OS security sandbox**: the parent container still has
host bind mounts and the Docker socket. Do not enable auto for untrusted code.
The supervisor enforces promotion scope, not a security boundary around shell tools.

## Promotion, recovery and rollback

There is one supervisor per writable source checkout and data directory. File
preimages/postimages and promotion phases are journaled in `data/supervisor`.
The whole live source snapshot must still match before promotion; concurrent
edits cancel it. Workers do not start against partially applied patches.

Interrupted promotions roll back before worker startup. Rollback changes only
files still matching the repair's version; divergent human edits are preserved
and require operator intervention. Failed promotion/observation restores the
original source without rewinding task state or externally accepted effects.
State saved by the repaired worker remains authoritative even after rollback;
the original worker may retry the interrupted phase with its preserved receipts.

Runtime fingerprints invalidate computational agent checkpoints/check caches.
Task identities and externally accepted PR/message/backlog receipts are retained.
Recovery tickets are bound to the task, target, state/decision version and new
runtime fingerprint, and consumed durably before phase execution. They can resume the matching incident's WAIT_STUCK phase,
not approve a proposal, replace a new question, or force a different task forward.

Recovery requires the task to advance legitimately during a 15-minute observation
window, not merely an exit-code-zero restart. Startup repairs require worker
readiness. Two candidates per incident and three daily candidate admissions are
persisted across restarts. Provider execution has a 10-minute deadline; full-suite
validation has a 15-minute deadline. Liveness/startup restarts are limited to
three per hour, persisted across supervisor restarts; exhaustion pauses the worker.

## Communication and provider independence

Supervisor events contain opaque task/provider/channel/conversation references.
The delivery bridge uses the existing communication facade and durable receipts;
Email and Slack are adapters, not dependencies of the supervisor state machine.
Future adapters use the same event contract. Backlog updates remain worker-owned;
the supervisor never calls Jira/GitHub/GDoc clients or changes backlog claims.

Delivery failures keep events queued and cannot invalidate a successful repair.
Channel changes do not automatically redirect private queued messages.
Delivery runs in a bounded subprocess, not the task loop. Its receipts use an
isolated namespace, sending is locked, and interrupted attempts rotate so one
unresponsive event does not starve the rest. The supervisor retries while paused.

Operator controls are currently local, through the stable supervisor CLI:

```bash
docker compose exec codebot python3 /opt/coderbot-supervisor/supervisor.py --control pause
docker compose exec codebot python3 /opt/coderbot-supervisor/supervisor.py --control resume
```

`docker compose stop` cancels repairs and never restarts the worker. Channel-native
repair approval/control commands and broad automatic stall diagnosis are not part
of this initial rollout. Use local controls/manual review for those cases.

## Operational checks

- Supervisor heartbeat remains fresh while the worker is intentionally stopped.
- Heartbeats stop when supervisor operation deadlines expire; hard-stale checks
  trigger container recovery, including interrupted-promotion rollback.
- Worker heartbeat/readiness stalls get bounded restarts, not blind code rewrites.
- Python agent/check subprocess groups and Linux orphaned descendants are stopped
  before promotion. Unowned Docker resources are never automatically removed.
- Private repair logs/journals are not attachments; notifications contain status
  and changed paths, not secrets or full model prompts.
- Source/image drift is explicit: only `/app/src` Python edits apply via worker
  restart. Entrypoint, baked plugin and dependency repairs require rebuilding.

Implementation/tests do not enable self-healing or restart the live bot. This is
an initial guarded rollout: synthetic promotion/rollback/lifecycle tests are not
a substitute for validating real provider credentials and the rebuilt image.
