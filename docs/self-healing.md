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
definitions must also be supplied as JSON. Provider configuration that references
`CODEBOT_*`, `GH_TOKEN` or `GITHUB_TOKEN` is refused.

## Incidents

The worker reports failures as typed incidents, never as log text
(`src/selfheal_events.py`). An incident has `version: 2`, `class` (`transient`,
`agent` or `fatal`, from `src/errors.py`), `code`, `errno`, `resource`, a redacted
`message` (500 chars), `retry_after` and up to 16 innermost `src/file:line:function`
frames. A failure before the worker is up (an import error, a SyntaxError, a non-zero
`SystemExit`) is written by a small bootstrap to `data/supervisor/crash.json`, which
the supervisor reads once. The supervisor dispatches on `class` and `code`.

## Runbooks

Operational problems get a deterministic runbook, not an LLM repair
(`scripts/selfheal.py`):

- `git_index_lock`: removes an `index.lock` older than 5 minutes in the target or its
  children when no git process runs.
- `claude_config`: restores a corrupt `~/.claude.json` from its newest valid backup.
- `corrupt_json`: reports state files the worker quarantined (`*.corrupt-*`).
- `retention`: removes `outbox/checks`, `diagnostics`, repair folders and quarantined
  files after 14 days (1 day when disk is under 5% free). It keeps the active
  promotion and every proposed candidate.
- `outage_backoff`: records transient outages.

Runbooks act only in `auto`; `observe` and `propose` log a dry run. Each runs at most
3 times an hour and reports the same finding at most once every 6 hours. They run
every 10 minutes and on every incident that is not a code bug.

## Code repairs

Only `agent`-class incidents of TypeError, AttributeError, KeyError, IndexError,
JSONDecodeError, NameError, UnboundLocalError, UnicodeDecodeError, or a startup
SyntaxError, reach the repair agent. The innermost frame must be in `src/`, outside
the protected files, and the task must not be in IDLE or a wait state. A startup
crash is repaired only on its first restart in the hour.

The worker keeps running during the repair; its task stays parked in WAIT_STUCK. It
is stopped only to drain the repair's tools, briefly around validation, and to
promote.

The repair agent works on a snapshot of the source. It never sees live `data/`,
`.env` or the target repositories:

- Its shell denies `git push`, `git commit`, `git reset`, `git checkout`,
  `git worktree`, `gh`, `docker` and `sudo`, and reads of `/app/.env`,
  `/proc/*/environ` and live `data/`. Claude runs without setting sources and
  without subagents; OpenCode gets the same bash deny patterns.
- It must write `.repair-result.json`: `status` (`fixed`, `not_reproducible`,
  `out_of_scope` or `needs_human`), `root_cause`, `files`, `regression_test`, `risk`,
  `summary`. Any status other than `fixed` ends the repair.
- It has `CODEBOT_REPAIR_AGENT_TIMEOUT` seconds (default 600). A failed agent run is
  not retried.
- After the agent, and again after the tests, the live source is compared with the
  snapshot. Any drift is restored, kept under `<repair>/drift`, and the supervisor
  pauses.

## Validation

- At most 4 files and 400 changed lines; no deletions and no group/world-writable
  modes.
- Only Python source plus one new `tests/test_selfheal_regression_*.py`. Protected
  files (`src/config.py`, `secret_store.py`, `selfheal_events.py`,
  `repair_notifications.py`, `turn_control.py`) and approval-bearing functions
  (`do_approval_reply`, `do_merge_reply`, `_finish_task`, `_abort_and_reset`,
  `_validate_target_state`, `_clear_stuck`, `_acquire_single_instance_lock`) are
  rejected.
- On the old code, the regression test must raise the incident's exception type
  through the incident's frame file. On the candidate it must pass. Each run has
  300 s; new regression tests cannot skip.
- The full suite runs with a fresh HOME/data/XDG and no task credentials (900 s).
  Failures the old snapshot also has are tolerated: that baseline runs once per
  runtime epoch and is cached in `data/supervisor/baseline/`.
- A rejected candidate is retried with the rejection reason, within the budget:
  2 candidates per incident and 3 admissions per UTC day, persisted in `budget.json`.

Candidate and test processes are not an OS sandbox: the container still has host
bind mounts and the Docker socket. Do not enable `auto` for untrusted code.

## Promotion, observation and rollback

There is one supervisor per writable source checkout and data directory. File
preimages, postimages and promotion phases are journaled in `data/supervisor`. The
live source must still match the snapshot before promotion; concurrent edits cancel
it. Interrupted promotions roll back before the worker starts. Rollback changes only
files that still match the repair's version; a conflict pauses the worker until an
operator fixes it and runs `--control resolve`.

After a promotion the supervisor observes the worker for
`max(900, CODEBOT_AGENT_TIMEOUT + 900)` seconds (45 minutes by default). Recovery means
the incident's phase completes, for the same task, into a state other than WAIT_STUCK
(for a startup incident: the worker reports ready). A new incident, a worker exit, a
pause or a rollback request in that window rolls the patch back.

Task state saved by the repaired worker stays authoritative after a rollback.
Runtime fingerprints invalidate agent checkpoints and check caches; PR, message and
backlog receipts are kept. Recovery tickets are bound to the task, target,
state/decision version and the new runtime fingerprint. They resume the incident's
WAIT_STUCK phase only; they never approve, answer or move another task.

Each promotion is announced with the root cause, a per-file diff stat and the exact
rollback command.

## Restarts and health

Liveness/startup restarts are limited to three per hour, `restart_count × 2` seconds
apart. When they are used up, a breaker opens instead of pausing forever: cooldowns
of 5, 10, 20, 40 minutes up to 1 hour, then one probe start; 10 healthy minutes close
it. A `fatal` crash (missing credential, broken configuration) opens the breaker at
once. The worker counts as stalled after `max(180, 3 × CODEBOT_POLL_INTERVAL)` seconds
without a heartbeat.

With self-healing on, `scripts/healthcheck.sh` watches
`data/supervisor/heartbeat.json`: unhealthy after 60 s, PID 1 killed after
`CODEBOT_HEARTBEAT_HARD` (default 600 s). A paused supervisor reports unhealthy and is
never killed.

## Communication

Supervisor events carry opaque task, provider, channel and conversation references.
The delivery bridge uses the existing communication facade and durable receipts.
The supervisor never calls Jira, GitHub or GDoc clients. Delivery failures keep events
queued; delivery runs in a bounded subprocess. Handled events and delivered
notifications are pruned after 7 days.

## Operator controls

```bash
docker compose exec codebot python3 /opt/coderbot-supervisor/supervisor.py --control pause
docker compose exec codebot python3 /opt/coderbot-supervisor/supervisor.py --control resume
docker compose exec codebot python3 /opt/coderbot-supervisor/supervisor.py --control resolve
docker compose exec codebot python3 /opt/coderbot-supervisor/supervisor.py --control rollback <incident-id>
```

`rollback` undoes only the latest committed promotion; any other id is refused.
`resolve` clears a rollback conflict and resumes. `docker compose stop` cancels
repairs and never restarts the worker. Only `/app/src` Python edits apply through a
worker restart; entrypoint, plugin and dependency changes need an image rebuild.
