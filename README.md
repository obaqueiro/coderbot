# coderbot

Autonomous coding agent that works the improvements backlog — a Google Doc
(`CODEBOT_DOC_ID`) or a GitHub Projects v2 board (`CODEBOT_TASK_SOURCE=github`) —
of whatever target repo you point it at (`CODEBOT_REPO_PATH`), plans with the
managed OpenSpec workflow via a selectable headless coding agent (Claude Code or
OpenCode), and talks to the maintainer exclusively by email (`CODEBOT_USER_EMAIL`).
Several instances can share one mailbox and backlog.

## Repository layout

- `src/` — the bot: `main.py` (state machine), `config.py`, `prompts.py`, the
  agent runners and the Gmail/Docs clients. Flat modules, imported by name.
- `tests/` — unit tests (`python3 -m unittest discover -s tests -t .`).
- `scripts/` — `setup.sh` (guided host setup), `setup_oauth.py` (Google consent
  flow), `entrypoint.sh` and `healthcheck.sh` (container runtime).
- `agent-plugin/` — the Claude/OpenCode plugin injected into agent sessions.
- `openspec/` — this repo's own OpenSpec specs and changes.
- `data/` — durable runtime state (git-ignored, bind-mounted into the container).

## Target repo prerequisites

Coderbot lives in its own repo and is pointed at a target checkout via
`CODEBOT_REPO_PATH`. Two pieces of target-repo infrastructure make codebot's
lifecycle stricter, but neither is a hard requirement — codebot auto-detects
each at the start of every task and adapts, self-healing the gap if it's
missing:

- An **`e2e/` test harness**, run via `e2e/run.sh` as the pre-PR gate. Either
  tool is supported, detected by what's under `e2e/`:
  - **Playwright** (web UI) — `e2e/playwright.config.ts`; specs live under
    `e2e/tests/`; evidence is a stitched video from `test-results/`.
  - **Newman/Postman** (API-only repos with no frontend) — collections under
    `e2e/collections/*.postman_collection.json`; evidence is the generated
    run report from `test-results/`.
  - **Missing**: the e2e gate is skipped for the current task, and codebot
    adds a backlog item requesting the harness (Playwright if the repo has a
    frontend, Newman otherwise) so a later task can build it.
- A **`Code Review` GitHub Actions workflow** — matched by its declared
  top-level workflow `name:` being exactly `Code Review` — drives the
  automated review loop. Codebot's self-healing item asks for this to be
  built on [alibaba/open-code-review](https://github.com/alibaba/open-code-review)
  (OpenCodeReview), an Anthropic-backed automated PR reviewer, with the
  workflow named `Code Review` (not OpenCodeReview's own suggested workflow
  name) and posting under the default `github-actions[bot]` identity — both
  required for codebot's detection and review-thread polling to see it.
  - **Missing**: the post-PR review wait is skipped and the PR is emailed to
    the user immediately, and codebot adds a backlog item requesting the
    workflow.

OpenSpec owns requirements, design, and tasks; Superpowers owns engineering
discipline; coderbot owns state, archive timing, push, PR, and merge. The image
supplies managed, pinned OpenSpec 1.9.0 and Superpowers v6.3.0 integrations for
both Claude Code and OpenCode. Target repos and host profiles do not need their
own Superpowers installation.

## Backlog sources

`CODEBOT_TASK_SOURCE` selects where tasks come from: `gdoc` (default) or `github`.
`main.py` only talks to the `task_source` façade; each backend implements the same
operations (list pending, claim, hold, unclaim, mark done, seed an item, link the
PR). Items keep their text as the identity the agent sees, plus a backend id.

### Google Doc (`CODEBOT_TASK_SOURCE=gdoc`)

The backlog is a Google Doc (`CODEBOT_DOC_ID`). Each **top-level bullet** is one task.
Indented sub-bullets under it are the user's clarifications and sub-requirements of
that task (any nesting level) and are shown to the agent with it — never picked on
their own. Inline images pasted under a bullet (as their own paragraph or inside a
sub-bullet) are downloaded to `data/doc_images/` and handed to the exploration
session as screenshots. Struck-through bullets are done; codebot strikes a task
through (sub-bullets included) when its PR merges or on `DONE`.

To force an order, write `Codebot[1]`, `Codebot[2]`, ... anywhere in a task's text:
tagged tasks are picked before untagged ones, lowest number first (untagged tasks wait
until no tagged task is left). The tag stays part of the task text.

Set `CODEBOT_DOC_SECTION` to a heading text (e.g. `New:`) to make only the bullets
under that heading pickable — items under other headings (say, "Under review:") are
left alone. Unset, every top-level bullet in the doc is a candidate. Picking a task
appends `[implementing: <instance>]` to its bullet so other instances sharing the doc
skip it; the marker is removed when the task completes or is aborted. A task parked
with the HOLD command carries `[on hold: <instance>]` instead and is skipped by everyone
until CONTINUE (see "Mailbox commands").

When codebot is idle it picks, in this order: a held task whose CONTINUE was requested;
a task it already claimed but lost track of (e.g. `data/` was rebuilt); the
`Codebot[n]` tagged task(s) with the lowest number; otherwise the coding agent chooses
among all pending items (prerequisites and enablers first).

### GitHub Projects v2 (`CODEBOT_TASK_SOURCE=github`)

The backlog is a Projects v2 board (`CODEBOT_GH_PROJECT_URL`, e.g.
`https://github.com/orgs/getriverly/projects/3`, or `CODEBOT_GH_PROJECT_OWNER` +
`CODEBOT_GH_PROJECT_NUMBER`). A task is a board card backed by an **issue of the
target repo** (the checkout's `origin`, or `CODEBOT_GH_ISSUE_REPO`): the issue title
is the task text, its body the clarifications, and images in the body are downloaded
to `data/gh_images/`. Draft cards, pull-request cards, cards from other repos and
closed issues are ignored (the log says how many).

Cards move through the board's **Status** field (names configurable, matched
case-insensitively):

| Event | Status | Labels on the issue |
|---|---|---|
| pickable | one of `CODEBOT_GH_PROJECT_PICK_STATUSES` (default `Ready`) | none |
| picked | `In progress` | `codebot:<instance>` |
| PR opened | `In review` (PR linked in a comment; PR body says `Closes #n`) | unchanged |
| HOLD | unchanged | `codebot-hold:<instance>` |
| ABORT | first pick status | removed |
| merged / DONE | `Done` | removed |

`Codebot[n]` in an issue title orders tasks as in the Doc; without a tag, a
single-select **Priority** field on the board orders them by option position.
Self-healing items (missing e2e harness / Code Review workflow) are created as real
issues and placed in the pick column. The labels are created on first start;
`GH_TOKEN` needs the `project` scope in addition to `repo` (a classic PAT's
`read:project` cannot move cards; an SSO-protected org needs the token authorized).

### Activity trail

Whatever the source, codebot leaves a log on the item itself (`CODEBOT_ACTIVITY_TRAIL`,
default on): a note at every milestone — picked and branch, proposal sent, approval,
each question and the user's answer, implemented, verified, reviewed, e2e result,
archived, PR opened, review rounds, PR ready, conflicts resolved, merged/done, hold,
resume, abort, stuck — with the full message body, so the ticket reads as the task's
history. On GitHub each note is a comment on the issue. On the Google Doc the first
note creates a doc-level comment quoting the item and later notes are replies in that
thread (the Drive API cannot anchor comments to a bullet), which needs the full
`drive` OAuth scope: installations set up before this option must re-run
`python3 scripts/setup_oauth.py` once. A tracker failure is logged and never
affects the task. The same one-call contract (`task_source.note_activity`) is what a
JIRA or other tracker backend would implement.

## Lifecycle

```
IDLE → pick item (see "Backlog sources") → claim it → branch <instance>-<slug>
      → EXPLORING → PROPOSING → email proposal → WAIT_APPROVAL
     → IMPLEMENTING → VERIFYING → INTERNAL_REVIEW → E2E (when present)
     → ARCHIVING → OPEN_PR → WAIT_REVIEW ⇄ ADDRESS_REVIEW → PUSHING (if review fixes exist)
     → email PR + evidence → WAIT_MERGE ⇄ ADDRESS_PR_THREADS → PUSHING
     → merge → mark item done (strike through in the Doc / "Done" on the board) → IDLE
WAIT_REVIEW / WAIT_MERGE ⇄ RESOLVE_CONFLICTS → PUSHING   (when the base branch moves)
```

If an E2E repair changes tracked files, coderbot repeats verification and internal
review before retrying E2E. OpenSpec archive/spec sync occurs on the feature branch
before PR creation, so it lands atomically with the implementation.
Coderbot independently validates the active change and confirms OpenSpec apply progress
is complete after the agent's verification report. Post-PR agent fixes enter a persisted
`PUSHING` phase, so a failed or interrupted push retries without rerunning agent work.
Archive retries include untracked OpenSpec files in cleanliness checks and stage only
the validated spec, active-change, and archive paths. If archive recovery needs human
guidance, the existing agent session may repair and commit only OpenSpec planning/spec
files before coderbot retries archival.

Every tick starts by checking the mailbox for commands (ABORT / STATUS / DONE / HOLD /
CONTINUE, see below). Any state that keeps failing detours through WAIT_STUCK (see
"Failure handling"), and HOLD parks a task from any state.
Any phase can detour through WAIT_REPLY: if the coding agent needs the user, it ends its
output with `NEED_USER_INPUT: <question>`; codebot emails the question (optionally
with `ATTACH: <path>` screenshots/videos) and resumes the same session with the reply.

Evidence (screenshots, recordings, reports) is always routed to email via that same
`ATTACH: <path>` convention (saved under the outbox dir), in every phase — never
committed to the target repo. Every resumed turn restates this rule. As a safety
net, designated handoffs after implementation and review or feedback repairs scan
the branch diff for evidence-looking files (video extensions, or paths naming
"evidence"/"recording") and have the coding agent remove and re-route them via
email if any are found.

## Phase discipline (planning never implements)

EXPLORING and PROPOSING are planning phases: their prompts forbid implementing,
committing, and pushing, and every WAIT_REPLY resume restates the active phase's
rules. As a backstop, when either phase completes codebot **mechanically reverts
overreach**: premature commits are undone (`git reset --soft` to the branch base),
tracked changes outside `openspec/` are discarded, and a note about what was cleaned
up is included in the proposal email. Openspec change artifacts are the phases'
legitimate output and are always kept.

Replies to a question (WAIT_REPLY) are classified first: an answer resumes the working
session with the phase rules restated; "mark it complete" / "abort" act on the
lifecycle directly instead of being forwarded to a session that cannot act on them.

## Failure handling (WAIT_STUCK)

No state retries forever in silence. Each state has a **consecutive-failure budget**
(`CODEBOT_MAX_STATE_FAILURES`, default 5), tracked in `state.json` and reset on any
successful tick. When a state exhausts its budget — or a phase asks more than
`CODEBOT_QUESTION_MAX_ROUNDS` (default 8) questions in a row — codebot emails you
(`stuck in <STATE>`, with the last error) and enters `WAIT_STUCK`. Reply on that
thread with:

- **retry** — try the failed step again (clears the counter),
- **abort** — reset to a clean slate (same as the ABORT command below),
- **complete** — mark the task done in the backlog and move on, or
- **instructions** — free-form guidance; codebot applies it in the working session, then
  resumes the failed step.

Every external `git`/`gh` call has a timeout (`CODEBOT_SUBPROCESS_TIMEOUT`, default 120s)
so a hung command surfaces as a failure (feeding the budget) instead of wedging the loop.

Every working prompt starts with an "execution environment" preamble of runtime facts.
Add project-specific ones (how to run the tests, what is NOT available in the
container) in `data/environment.md` or `CODEBOT_ENVIRONMENT_NOTES`. Untrusted text
(e2e output, review comments, email bodies) is fenced as data inside prompts so an
embedded instruction cannot hijack the flow.

## Silence check-ins (is it stuck?)

A task can go quiet for hours — an implementation session, a slow `Code Review`
run, or simply a question you haven't answered yet — and the thread gives no hint
whether codebot is working or wedged. So while a task is in flight, codebot emails a
short **check-in** on the task thread whenever the thread has been quiet (no email
sent *or* received on it) for the next interval of a decaying back-off:
`CODEBOT_PING_SCHEDULE`, default `30m,1h,2h,3h,5h,8h` — the first check-in 30 min
after the last real email, the next 1 h after that check-in, then 2 h, 3 h, 5 h, and
every 8 h from there (the last interval repeats). Any real email in either direction
restarts the schedule; check-ins themselves don't count. Set it to `off` to disable.

Every check-in says whose move it is, and is self-contained:

- **Ball on codebot's side** (a working phase, or WAIT_REVIEW): what it is doing —
  the step, when it started, and for WAIT_REVIEW the PR, how long it has waited, what
  GitHub currently reports for the run and when it will give up — plus whether recent
  attempts at the step have been failing.
- **Ball on your side** (WAIT_APPROVAL / WAIT_MERGE / WAIT_REPLY / WAIT_STUCK /
  WAIT_CLEAN): exactly what it needs from you, spelled out in full (the pending
  question, the PR link and the accepted replies, the stuck step and its error), with
  its last email quoted for reference — never "see above".

Check-ins go out from the tick loop, so one can lag while a single long agent call is
running (bounded by `CODEBOT_AGENT_TIMEOUT`); it is sent as soon as that call returns.
STATUS reports the last real email on the thread and how many check-ins followed it.

## Mailbox commands: ABORT / STATUS / DONE / HOLD / CONTINUE

Email codebot with a body of exactly `ABORT`, `STATUS`, `DONE`, `HOLD` (or `PAUSE`),
or `CONTINUE` (or `RESUME`, which may be followed by a note) — case-insensitive,
trailing punctuation tolerated — optionally with an instance name (`ABORT codebot-x7k2`)
when several instances share the mailbox — see "Multiple instances" below for how bare
commands are scoped.
All are checked at the start of every tick and mailbox-wide (any thread, or a brand-new
email), so they work even when the agent is stuck waiting on a thread — or in
WAIT_REVIEW, which never polls the inbox. **Only mail from `CODEBOT_USER_EMAIL` is
honored** (it may be a comma-separated list) — a stranger emailing "ABORT" is ignored.
Thread replies are held to the same rule: mail from any other sender in a task thread
is logged and ignored.

- **ABORT** discards the working-tree changes, returns to a clean, up-to-date base branch
  (`git reset --hard` + `checkout -f $CODEBOT_BASE_BRANCH` + `clean -fd` + fast-forward
  to `origin/$CODEBOT_BASE_BRANCH`), clears all task state, and goes back to IDLE —
  then emails a confirmation. It only touches the **local** checkout: remote branches
  and PRs are left as-is (clean those up on GitHub yourself if needed). The check runs
  between phases, so an abort sent mid-phase takes effect once the current agent call
  returns.
- **STATUS** replies with a snapshot — instance, current state, task/slug/branch, PR
  URL, pending question, stuck state and failure counters, last transition time, round
  counters, heartbeat age, tasks on hold, the time of the last real email on the task
  thread and the check-ins sent since, and the full text of the last email codebot
  sent — without changing anything.
- **HOLD** (or **PAUSE**) parks the current task: any half-finished git operation is
  aborted, all pending work is committed on the task branch (evidence files excluded),
  the task's state is saved in `data/holds.json`, its bullet in the doc gets an
  `[on hold: <instance>]` marker (no instance picks it), and codebot returns to IDLE
  to take the next item. Reply **CONTINUE** (or **RESUME**, optionally followed by
  instructions) on the held task's thread to bring it back: it becomes the very next
  task codebot picks (after the one in progress, or immediately when idle), restored
  on its branch and session; the instructions are handed to the task as the reply to
  whatever it was waiting on, and without them codebot re-sends its last message so
  you know what it is still waiting for. Held tasks are listed in `data/holds.json`
  and by STATUS. A bare "continue" on the **active** task's thread is ordinary
  conversation, not this command.
- **DONE** marks the current task complete: strikes the item through in the backlog
  doc, resets the local checkout to a clean base branch, and returns to IDLE to pick
  the next item. Use it when the work turned out to already be done (e.g. an earlier
  PR covered it) and the bot is waiting on a thread you'd rather not continue. A bare
  reply on the **active task thread** is never treated as this command (so answering
  "Done" to a question doesn't complete the task) — send it on any other thread or a
  fresh email, or target the instance explicitly (`DONE codebot-x7k2`).

## Automated code review (WAIT_REVIEW ⇄ ADDRESS_REVIEW)

After opening the PR, codebot waits for the target repo's **`Code Review`** GitHub
Action (OpenCodeReview) to finish (`CODEBOT_REVIEW_WAIT_TIMEOUT`, default 45 min per
run). The work queue is the PR's **unresolved review threads** (queried through
GraphQL, so a thread the agent skipped or one posted after a run stays in the queue
instead of silently accumulating until merge time). For each thread opened by the
reviewer bot the working session either fixes the issue (commit; codebot pushes via
PUSHING, which re-triggers the action) or declares it not worth fixing — and either way
emits `RESOLVE: <thread_id> <reason>`; codebot posts the reason as a reply on the
thread and marks it resolved. Only thread ids codebot itself fetched are honored. The
loop is capped by `CODEBOT_REVIEW_MAX_ROUNDS` (default 3); leftovers are mentioned in
the "PR ready" email. A failed thread query is never read as "no threads".

While waiting for your merge decision (WAIT_MERGE) codebot keeps polling the PR for
unresolved threads from anyone (a human reviewer included) and addresses them the same
way (ADDRESS_PR_THREADS, capped by `CODEBOT_PR_THREAD_MAX_ROUNDS`). On `merge` it
re-checks: unresolved threads (or a failed query) **block the merge** with an email
listing them; reply `merge anyway` to force. Merging is irreversible, so the classifier
must return an explicit `force: true` for that.

## Base-branch conflicts (RESOLVE_CONFLICTS)

While a PR is open (WAIT_REVIEW / WAIT_MERGE), every reply-less tick checks its
mergeability. A `CONFLICTING` PR (the base branch moved — another PR merged) detours
through RESOLVE_CONFLICTS: the working session merges `origin/$CODEBOT_BASE_BRANCH`
into the branch (**merge, never rebase** — the branch is pushed), re-runs the tests and
commits; codebot pushes and the automated review re-runs before the PR can merge. A
conflict with genuinely different reasonable resolutions is emailed as a question
instead of guessed at. Attempts are capped by `CODEBOT_CONFLICT_MAX_ROUNDS` (default 3),
then the task escalates to WAIT_STUCK. Replies the user sent against the pre-conflict
PR content are set aside and the "PR ready" email says so.

## One-time setup

**Recommended**: from the coderbot repo root, run the guided setup script. It
walks through every value below, explains what it is and where to get it,
pre-fills defaults where discoverable (git config, an authenticated `gh` CLI,
an existing `.env`), validates what it can, and writes `.env` for you. Re-run
it whenever you need to reconfigure codebot, such as switching from Claude Code
to OpenCode: it shows the current non-secret settings and lets you keep or
change each one:

```bash
scripts/setup.sh
```

It also detects `data/credentials.json` (see step 1 below) and, if present,
offers to run the consent flow in step 2 for you.

<details>
<summary>Manual setup (what <code>scripts/setup.sh</code> automates)</summary>

1. **Google OAuth client**: in Google Cloud Console create a project, enable the
   Gmail, Google Docs and Google Drive APIs, create an OAuth client of type
   *Desktop app*, and download its JSON to `data/credentials.json`. The consent
   asks for full Drive access (the activity trail posts comments on the doc and
   evidence videos are uploaded to Drive).
2. **Consent flow** (on the host, from the coderbot repo root):
   ```bash
   pip install -r requirements.txt
   python3 scripts/setup_oauth.py     # opens a browser; writes data/token.json
   ```
3. **Configuration**: create `.env` at the repo root (see `.env.example`):
   ```
   # Required: absolute host path of the target git checkout. It is mounted into
   # the container at the SAME path (Claude's per-project state and the e2e
   # docker stack both depend on host==container paths).
   CODEBOT_REPO_PATH=/absolute/path/to/target-repo
   # Backlog: "gdoc" (default) or "github"
   CODEBOT_TASK_SOURCE=gdoc
   # Required when gdoc: Google Doc id of the improvements backlog
   CODEBOT_DOC_ID=<the id from the doc's URL>
   # Required when github: the Projects v2 board (GH_TOKEN then also needs "project")
   CODEBOT_GH_PROJECT_URL=https://github.com/orgs/<owner>/projects/<n>
   # Required: the address codebot sends to and reads replies from
   CODEBOT_USER_EMAIL=you@example.com
   GH_TOKEN=<a PAT with repo scope, e.g. from `gh auth token`>
   # Select "claude" (default) or "opencode".
   CODEBOT_AGENT=claude
   CLAUDE_CODE_OAUTH_TOKEN=<output of `claude setup-token` on the host>
   GIT_AUTHOR_NAME=codebot
   GIT_AUTHOR_EMAIL=codebot@example.com
   # Optional: project name used in prompts; defaults to the repo dir name
   CODEBOT_PROJECT_NAME=
   # Optional; defaults to claude-fable-5 / medium (the claude CLI pin in the
   # Dockerfile must support the model you choose — see "Troubleshooting")
   CLAUDE_MODEL=claude-fable-5
   CLAUDE_EFFORT=medium
   # Optional; model to switch to when CLAUDE_MODEL runs out of usage credits
   # (defaults to claude-opus-5; empty disables the fallback), and how long to stay
   # on it before probing the primary model again (defaults to 3600 seconds)
   CLAUDE_FALLBACK_MODEL=claude-opus-5
   CLAUDE_FALLBACK_COOLDOWN_SECONDS=3600
   # Required when CODEBOT_AGENT=opencode. Run scripts/setup.sh to complete the
   # provider's browser/device-code/API-key flow; credentials stay in data/opencode/.
   OPENCODE_PROVIDER=
   OPENCODE_MODEL=<provider/model>
   # Optional; DEBUG (default) or INFO — DEBUG traces every email, video, and git call
   CODEBOT_LOG_LEVEL=DEBUG
   # Optional; maximum verification/internal-review repair rounds (default 3)
   CODEBOT_QUALITY_GATE_MAX_ROUNDS=3
   # Optional; maximum OpenSpec archive repair rounds (default 3)
   CODEBOT_ARCHIVE_MAX_ROUNDS=3
   # Optional; defaults to "main" — the trunk branch codebot syncs from, branches off
   # of, opens PRs against, and resets to on abort
   CODEBOT_BASE_BRANCH=main
   # Optional; only needed if the target repo's own .claude/settings.json defines an
   # "apiKeyHelper" that reads this variable — see "Troubleshooting" below
   CLAUDE_API_KEY=
   # Optional (gdoc); only bullets under this heading of the backlog doc are picked
   CODEBOT_DOC_SECTION=
   # Optional; hand-picked instance name (see "Multiple instances")
   CODEBOT_INSTANCE=
   ```
   Every other knob (failure budgets, timeouts, review/conflict round caps, heartbeat
   thresholds, project-specific environment notes) is optional and documented with its
   default in `.env.example`.
   (macOS keeps Claude credentials in the Keychain, which the Linux container
   can't read — hence the explicit token.)

</details>

4. **Coding-agent authentication**: Claude Code uses the host's `~/.claude` and
   `~/.claude.json`. OpenCode is installed in the Codebot Docker image; `scripts/setup.sh`
   runs its authentication flow there and stores credentials privately under
   `data/opencode/`. This includes browser and device-code provider flows. Git pushes
   use HTTPS with `GH_TOKEN` (no SSH needed).

## Run

From the coderbot repo root:

```bash
docker compose up -d --build
docker compose logs -f
```

State lives in `data/state.json`; the container restarts safely from any state.
To abort the current task, email `ABORT` (see "Mailbox commands") — or stop the
container, delete `data/state.json`, remove the task's `[implementing: …]` marker in
the doc, clean the git branch, restart.

Everything codebot must remember across restarts lives in `data/` — the Google
token, `state.json`, `holds.json` (tasks on hold), `instance_id`, the processed-mail
list — and both compose files bind-mount that directory from the host (never a
named volume), so it survives container recreation, `docker compose down -v`, and
image rebuilds. On a disposable server, back up `data/` (or restore it from your
provisioning) before replacing the host.

Only one codebot may run against a `data/` dir: startup takes a lock on
`data/state.lock` and a second process exits immediately. The container reports
Docker health from a heartbeat the tick loop maintains (`docker inspect --format
'{{.State.Health.Status}}' coderbot-codebot-1`); a loop wedged beyond any plausible
operation (past `CODEBOT_HEARTBEAT_HARD`, ~2.5h) is force-restarted via the
`restart: unless-stopped` policy.

### On a Linux server (EC2)

`docker-compose.ec2.yml` is the same stack with fixed host paths: this repo at
`/opt/coderbot`, the `.env` at `/opt/codebot/codebot.env`, the bot's `~/.claude` at
`/opt/codebot/claude` (with `.credentials.json`), a read-only `~/.claude.json` seed at
`/opt/codebot/claude.json`, and the Google OAuth files in `/opt/coderbot/data/`. The
target repo path comes from `CODEBOT_REPO_PATH` in the env file. Provision those files
(e.g. from a secrets manager in the instance's user-data), `chown -R 501` both trees,
then:

```bash
cd /opt/coderbot && docker compose -f docker-compose.ec2.yml up -d --build
```

For a fleet of agents on cheap interruptible instances, each with a persistent disk so
a reclaimed agent resumes its task, there are two self-contained deployments:
[`deploy/aws/`](deploy/aws/README.md) (CloudFormation, Spot instances and an EBS volume
per agent) and [`deploy/azure/`](deploy/azure/README.md) (Bicep, Spot VMs and a managed
disk per agent). Each is driven entirely by its cloud's CLI.

## Multiple instances

Any number of codebots can share one mailbox and backlog. Each installation
generates a stable identity on first start and persists it in `data/instance_id`
(e.g. `codebot-x7k2`). That id tags the instance's email subjects (`[codebot-x7k2]`)
and its git branches (`codebot-x7k2-<slug>`). `CODEBOT_INSTANCE` in `.env` overrides
the generated name — but then it must be **unique per installation**.

Instances coordinate only through the backlog. In the Doc, picking a task atomically
appends `[implementing: <instance>]` to its bullet (the write carries the doc revision
it was read at, so two instances can't both win). On a GitHub board, picking adds the
`codebot:<instance>` label to the issue and re-reads it; if another instance's label
landed too, both back off and repick next tick. Claimed tasks are invisible to the
others' PICK until the marker/label is removed on completion or abort. If an
installation is retired mid-task, send it `ABORT <name>` first (which unclaims) or
remove the marker/label by hand.

All instances share the Gmail account; each one only reads replies on its own
threads (subjects carry its prefix). Address mailbox commands to one instance —
`ABORT codebot-x7k2`, `STATUS codebot-x7k2`, `DONE codebot-x7k2`, `HOLD codebot-x7k2`
— or reply with the bare command on one of its threads. A bare `ABORT` or `STATUS`
sent anywhere else is honored by **every** instance (fleet-wide stop / fleet status —
their last-resort role); a bare `DONE`, `HOLD` or `CONTINUE` outside an instance's
threads is ignored, since they act on one instance's task. Only the instance that put
a task on hold can resume it; delete its `[on hold: …]` marker by hand to free the task
for anyone.

## Toolchain versions

The image pins exact versions for reproducible rebuilds: the base image
(`python:3.12.13-slim`), the claude-code, opencode and openspec CLIs (`@<version>` in
the `Dockerfile`), the Superpowers plugin (`config.SUPERPOWERS_VERSION`), Node (major
`22` via nodesource), and the Python deps (`==` in `requirements.txt`). To bump: read
the version currently working in the running container (`docker compose exec codebot
claude --version`, `… openspec --version`), edit the pin, and rebuild
(`docker compose up -d --build`). Don't switch these to floating/`latest` — a silent
CLI behavior change between rebuilds is exactly what the pins prevent.

## Smoke tests

Inside the container (`docker compose exec codebot bash`):

```bash
claude -p 'say ok' --model "$CLAUDE_MODEL" --dangerously-skip-permissions  # CODEBOT_AGENT=claude
opencode run --auto --model "$OPENCODE_MODEL" 'say ok' # when CODEBOT_AGENT=opencode
gh auth status                                       # GH token works
git -C "$CODEBOT_REPO_PATH" fetch                    # HTTPS auth via GH_TOKEN works
cd src && python3 -c 'import task_source; task_source.validate(); print(task_source.list_pending_items())'
cd src && python3 -c 'import gmail_client; print(gmail_client.send("[codebot] test", "hello"))'
```

Unit tests, on the host from the repo root:

```bash
python3 -m unittest discover -s tests -t .
```

## Troubleshooting

**`managed runtime unavailable: missing ...`**: coderbot fails closed at startup if
the image's managed Superpowers or coderbot/OpenSpec bridge files are absent. Rebuild
the image from this repository and check that no volume mount replaces
`/opt/coderbot/plugins` or `/opt/coderbot/agent-plugin`; do not install Superpowers in
the target repo or host profile as a workaround.

**`Claude Code X does not support this model; version Y or newer is required`** (or
`There's an issue with the selected model`): the `@anthropic-ai/claude-code@<version>`
pin in the `Dockerfile` predates `CLAUDE_MODEL`. Bump the pin to a version that knows
the model (see "Toolchain versions") and rebuild; also double-check the model id
spelling (`claude-fable-5-1`, dashes only).

**OpenCode provider authentication fails or returns `401 Unauthorized`**: rerun
`scripts/setup.sh` and complete authentication for `OPENCODE_PROVIDER`, then retry the
OpenCode smoke command above. A provider 401 is a failed smoke test; never treat the
CLI starting or returning structured output as a pass when the provider rejected the
request.

**`claude exited 1: apiKeyHelper failed: did not return a value`**: the target repo
has its own `.claude/settings.json` (project-level, applies to any `claude` invocation
with cwd in that repo) that defines an `apiKeyHelper` script codebot doesn't satisfy.
Claude Code's authentication precedence always tries `apiKeyHelper` *before*
`CLAUDE_CODE_OAUTH_TOKEN`, with **no fallback** if the helper fails or returns
nothing — so this silently breaks codebot's OAuth auth for that repo regardless of a
valid `CLAUDE_CODE_OAUTH_TOKEN`. Check what env var the helper reads (e.g.
`cat "$CODEBOT_REPO_PATH/.claude/settings.json"`) and set it to a real key from
[console.anthropic.com](https://console.anthropic.com/) via `CLAUDE_API_KEY` in
`.env` (see `.env.example`) — codebot's work in that repo will then bill via that
API key instead of your OAuth/subscription plan. Note some apps regenerate
`.claude/settings.json` at their own startup (check whether it's gitignored); if so,
the setting will keep reappearing and `CLAUDE_API_KEY` is the durable fix rather than
hand-editing the file.

## Logs

All activity is logged to stdout (visible via `docker compose logs -f`). At the
default `DEBUG` level you see every state transition, each coding-agent invocation
(model, effort, prompt size, returned session/output size, whether it hit the
`NEED_USER_INPUT` sentinel), every email sent (subject, thread, attachment count
and byte totals, any size-skipped files), reply and command received, git/gh
commands, the e2e suite result, review-thread activity, and the evidence-video
harvest (which `.webm` files were found and their sizes — with explicit warnings if
none were produced or a feature shipped without e2e specs). Failures are logged with
their per-state count (`cycle failed in E2E (2/5)`). Set `CODEBOT_LOG_LEVEL=INFO` for
a quieter feed; STATUS by email gives the same picture without the logs.

## Evidence

If the target repo has an e2e harness, the implementation phase adds coverage for
it and, once the suite passes, codebot re-runs the feature's own tests to capture
evidence for the PR email:

- **Playwright**: the implementation must include one demo test tagged `@evidence`
  that walks through the feature visibly. Codebot first re-runs only that test
  (`./run.sh <spec> --grep @evidence`), falling back to the feature's full specs when
  no clip appears, with `PICA_E2E_VIDEO=on` and `PW_VIDEO=on` exported (either forces
  `video: "on"` in `e2e/playwright.config.ts`). The resulting `.webm` clips from
  `e2e/test-results/` are stitched into a single H.264 `data/evidence.mp4` with ffmpeg
  (each clip scaled/padded to 1280x720 so mixed viewport sizes concatenate cleanly).
  If ffmpeg is unavailable or stitching fails, it falls back to attaching the raw
  `.webm` clips.
- **Newman**: re-runs the feature's collection(s) and attaches the newest
  generated report file from `e2e/test-results/`.

The stitched mp4 is not attached: it is uploaded to Google Drive
(`CODEBOT_EVIDENCE_UPLOAD`, default on) and linked from the PR email — and, because
the activity trail mirrors email bodies, from the issue comment or doc thread too,
so reviewers reading the ticket can watch it. Videos land in a `Codebot evidence`
folder found or created at the root of the bot account's My Drive, or in the folder
given by `CODEBOT_DRIVE_FOLDER_ID` (shared drives work). Each file is named
`<branch>-<timestamp>.mp4` and shared as "anyone with the link" (reader); if a
Workspace policy forbids link sharing the upload still succeeds and the link is
sent, but only the bot account can open it. Any upload failure — including a
`data/token.json` issued before the full `drive` scope was requested (re-run
`python3 scripts/setup_oauth.py`) — is logged and the mp4 is attached instead,
subject to `CODEBOT_MAX_ATTACH_BYTES` as before. Newman reports and agent-supplied
screenshots are always attached, never uploaded.

If no e2e harness is present for the task, no evidence is produced and the PR
email is sent without an attachment.

A `run.sh` that exceeds `CODEBOT_E2E_TIMEOUT` is killed, which skips its own cleanup
trap; codebot then tears down the harness's compose stack itself
(`docker compose -f $CODEBOT_E2E_COMPOSE_FILE down -v`, default
`docker-compose.e2e.yaml`, skipped when the file doesn't exist) so leaked containers
don't collide with the next run.
