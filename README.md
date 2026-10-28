# coderbot

Autonomous coding agent that works the improvements backlog — a Google Doc
(`CODEBOT_DOC_ID`), a GitHub Projects v2 board (`CODEBOT_TASK_SOURCE=github`),
or a Jira Cloud project (`CODEBOT_TASK_SOURCE=jira`) —
of whatever target repo you point it at (`CODEBOT_REPO_PATH`), plans with the
managed OpenSpec workflow via a selectable headless coding agent (Claude Code or
OpenCode), and talks to the team by email (default) or Slack (`CODEBOT_COMM_CHANNEL=slack`).
Several instances can share a backlog; Slack instances each use their own app.

Coderbot is repository- and organization-agnostic. Target-specific checks, provider
groups, preparation and artifacts belong in the target's declarative contracts,
not hardcoded application adapters. See [harness contracts](docs/harness-contract.md).

Target mode is automatic: a root `.git` means single repository; otherwise the
directory is a workspace. See [feature workspaces](docs/workspaces.md).

Optional independent supervision and validated Python repairs are documented in
[self-healing](docs/self-healing.md); automatic repair is disabled by default.

The communication façade supports optional live task progress. Slack edits the
initial feature message with its phase, `:loading:` while working, `:question:`
when user input is required, and the last three safe operational log lines.
Enable the animated custom `:loading:` emoji in your workspace. Updates are
deduplicated, normally at most once every 30 seconds, and respect Slack's
rate-limit retries; pause/completion updates can bypass the normal interval.
The original message and task marker are preserved across restarts. Log tails
are bounded in memory per task, redact known credentials, and exclude reasoning,
prompts and tool output. Email does not send extra progress emails. Future
editable channels can implement `update_progress(thread_id, snapshot)` without
changing the task supervisor. Questions and approvals still arrive in the thread.

## Repository layout

Conversation and recovery contracts, diagnostic attachments and the rollout procedure
are documented in [docs/conversation-recovery.md](docs/conversation-recovery.md).

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
    workflow (set `CODEBOT_SELF_HEAL_CODE_REVIEW=off` to not add it).

OpenSpec owns requirements, design, and tasks; Superpowers owns engineering
discipline; coderbot owns state, archive timing, push, PR, and merge. The image
supplies managed, pinned OpenSpec 1.9.0 and Superpowers v6.3.0 integrations for
both Claude Code and OpenCode: the `openspec-*` skills (`openspec-explore`,
`openspec-propose`, `openspec-apply-change`, …) are generated at image build time by
the pinned `openspec init` into `/opt/coderbot/openspec-skills` and loaded as a Claude
plugin dir / an OpenCode `skills.paths` entry, so target repos need neither an
`openspec init` of their own nor a Superpowers installation.

## Backlog sources

`CODEBOT_TASK_SOURCE` selects where tasks come from: `gdoc` (default), `github`,
or `jira`. `CODEBOT_COMM_CHANNEL` independently selects `email` (default) or
`slack`: GDoc+Slack and Jira+email both work.
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
appends `[implementing: <instance>@<fingerprint>]` to its bullet so other instances sharing the doc
skip it; the marker is removed when the task completes or is aborted. A task parked
with the HOLD command carries `[on hold: <instance>@<fingerprint>]` instead and is skipped by everyone
until CONTINUE (see "Mailbox commands").

When codebot is idle it picks, in this order: a held task whose CONTINUE was requested;
a task it already claimed but lost track of (e.g. `data/` was rebuilt); the
`Codebot[n]` tagged task(s) with the lowest number; otherwise the coding agent chooses
among all pending items (prerequisites and enablers first).

### GitHub Projects v2 (`CODEBOT_TASK_SOURCE=github`)

The backlog is a Projects v2 board (`CODEBOT_GH_PROJECT_URL`, e.g.
`https://github.com/orgs/example/projects/3`, or `CODEBOT_GH_PROJECT_OWNER` +
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
| picked | `In progress` | `codebot:<instance>@<fingerprint>` |
| PR opened | `In review` (PR linked in a comment; PR body says `Closes #n`) | unchanged |
| HOLD | unchanged | `codebot-hold:<instance>@<fingerprint>` |
| ABORT | first pick status | removed |
| merged / DONE | `Done` | removed |

`Codebot[n]` in an issue title orders tasks as in the Doc; without a tag, a
single-select **Priority** field on the board orders them by option position.
Self-healing items (missing e2e harness / Code Review workflow) are created as real
issues and placed in the pick column. The labels are created on first start;
`GH_TOKEN` needs the `project` scope in addition to `repo` (a classic PAT's
`read:project` cannot move cards; an SSO-protected org needs the token authorized).

### Jira Cloud (`CODEBOT_TASK_SOURCE=jira`)

Configure `CODEBOT_JIRA_URL=https://<site>.atlassian.net`, `CODEBOT_JIRA_PROJECT_KEY`,
`CODEBOT_JIRA_EMAIL` and an account `CODEBOT_JIRA_API_TOKEN` (create one at
[Atlassian account security](https://id.atlassian.com/manage-profile/security/api-tokens)). Set the **issue workflow**
status names (`CODEBOT_JIRA_PICK_STATUS`, `CODEBOT_JIRA_ACTIVE_STATUS`,
`CODEBOT_JIRA_REVIEW_STATUS`, `CODEBOT_JIRA_DONE_STATUS`) to statuses that exist in
your project; these are not board columns. Only issues in the pick status are offered.
Set `CODEBOT_JIRA_PICK_LABEL` to a Jira label such as `codebot-ready` and add
that label to each issue Coderbot may take. **Both** the eligible status and the
label are required for a new task. A task this instance already claimed remains
recoverable if someone removes the opt-in label later; the ownership label is
separate. The opt-in label is retained on hold, abort and completion. Self-healing
issues created by Coderbot receive it automatically.
After you enter the Jira site, project key, account email and API token,
`scripts/setup.sh` queries that project's workflow statuses and lets you map
all four using dropdowns (numbered choices in `--text` mode). If access fails,
the wizard asks you to correct the connection instead of guessing status names.
Individual issue workflows can still restrict transitions; Coderbot checks
each transition when it acts on an issue.
The project key is the short prefix in issue IDs: `ENG` in `ENG-42`, visible in
the issue URL (`.../browse/ENG-42`) or Jira's Project settings. It is not the
full project name or a UUID, and it need not be exactly three or four letters.
The issue summary is the task; rich descriptions and attached images are delivered
to the coding agent. `Codebot[n]` in the summary takes priority.

Claims use `codebot-claim-<instance>-<fingerprint>` labels; HOLD uses
`codebot-hold-<instance>-<fingerprint>`. After a PR opens, the issue moves to the
configured review status, remains open, and records the PR URL in a comment. The
PR body contains `Closes <JIRA-KEY>` and a clickable Jira link for context; Coderbot
explicitly transitions Jira to done after merging or on DONE (GitHub does not
automatically close Jira issues). ABORT restores the pick status. Self-healing
items are created as issues of `CODEBOT_JIRA_ISSUE_TYPE` (default `Task`).

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
affects the task. On Jira, notes become issue comments.

## Conversation channels

Email is the default (`CODEBOT_COMM_CHANNEL=email`): `CODEBOT_USER_EMAIL` receives
task threads and its listed senders can reply or issue commands. In Slack mode set
`CODEBOT_COMM_CHANNEL=slack`, `CODEBOT_SLACK_CHANNEL_ID` (public `C...` channel),
`CODEBOT_SLACK_BOT_TOKEN` (`xoxb-...`) and `CODEBOT_SLACK_APP_TOKEN` (`xapp-...`).
Create and install **one Slack app per Coderbot instance**. Enable Socket Mode and
an app-level token with `connections:write`; grant the bot `chat:write`,
`channels:history`, `channels:read`, `files:write`, and subscribe to
`message.channels` events.
Invite each app to the public channel. Coderbot posts one root message when it
claims a task. Approvals, questions, PR review, STATUS/KICK/ABORT/DONE/HOLD/CONTINUE and
check-ins stay in that task's replies; any human member of the channel can act.
Top-level messages are ignored except `status?`/`STATUS` and an explicitly targeted
`KICK <instance>`. Replies appear under the command message; a bare top-level
`STATUS` is visible to every codebot app in the channel. Replies to other bots'
task threads are ignored. Socket Mode records incoming replies immediately; if an
event is missed, the waiting
thread is reconciled from Slack history at most once a minute. If replies only
arrive through reconciliation, check that Event Subscriptions is enabled and
`message.channels` is subscribed for this app. An in-flight coding-agent turn
finishes before Coderbot acts on task-changing replies; brief `STATUS` replies are
served independently without interrupting that turn. A dirty checkout before a
task starts is announced once in the channel and automatically rechecked; replies to that
operational notice are ignored. Small screenshots and reports are shared in
the task thread; large evidence videos are linked from Google Drive.
For each task, the language of its title and details in Jira, GitHub Projects or
Google Docs is recorded in task state and used for agent output, OpenSpec
artifacts and outgoing task messages. A later reply in another language does
not change that task language; code, identifiers and control commands retain
their original spelling.

## Lifecycle

### Trusted autonomous runtime and performance

Coderbot's runtime is fully trusted: the principal and every subagent have all
operational permissions, including external directories. Managed configuration
overrides inherited permission restrictions; residual requests are automatically
answered for child sessions. Product/proposal/merge decisions remain in the task
conversation. Runtime configuration is loaded on process startup; restart the
service when changing plugins/configuration.

OpenCode defaults to the managed HTTP event bridge (`CODEBOT_OPENCODE_TRANSPORT=http`)
so tool starts, child activity and provider retries are observable. Defaults:
aggregate agent 1800 s, E2E 900 s, local tools other than bash/task 200 s, subagent 600 s,
provider retry 180 s and utilities 30 s. Each is environment-overridable in the
setup catalog. KICK also cancels controller-run tests and evidence operations.

Tool execution watchdogs start only on `running`, never `pending`; argument
generation and queueing still consume the aggregate agent budget. Repeated running
updates do not extend local tool deadlines. Completed/error events retire the
watchdog, including errors before execution; child metadata updates retain subagent
progress tracking.

Only primary sessions may launch subagents; child sessions (including resumed ones)
are rejected before entering the concurrency queue. `CODEBOT_MAX_SUBAGENTS` defaults
to 3. Queued delegations wait at most `CODEBOT_LOCAL_TOOL_TIMEOUT` (200 s by default).
Completed or failed tasks release their slot even when the runtime skips the
after-tool hook. On delegation rejection or queue timeout, the agent must finish
that unit directly rather than retrying delegation or increasing runtime limits.

There is no separate AI requirement-coverage audit before final checks or delivery.
OpenSpec verification, independent internal review and controller-run final checks
remain the verification gates.

Reply `VERIFY` in the active task thread (or `VERIFY codebot-<id>` in email) to
move an existing, unarchived change directly to verification. Optional guidance,
such as `VERIFY: commit abc123 contains the fixes; verify that work`, is passed to
the verifier. A running agent turn is interrupted first without discarding Git
changes. Planning and implementation are not restarted by the command, but
verification can still identify unfinished tasks or failures. No approval, pass,
push or merge is implied. Held tasks must be resumed first with `CONTINUE`.
Existing planning/repair continuations are superseded in the execution ledger
(their history is retained), so reloading cannot undo the verification request.

The controller freezes the task inventory on approval and implementation completion.
Every human decision is presented in the conversation itself, in both email and
Slack: Yes/No for a single authorization, or 3-4 numbered alternatives with their
consequences and a recommendation when justified. Reply with the number (and any
requested details), or write your own answer. Full-text replies such as
`3. Dejemos el registro asíncrono y la BDD opcional`, `1: <details>` or
`Sí, pero cambia el diseño` are interpreted in full with the displayed choices;
no particular separator is required. Qualifications never become an automatic
approval, merge or wait just because the reply starts with an option number.
During exploration/proposal preparation, clarifications stay in that initial
planning phase rather than attempting to revise a nonexistent approved change.
A selection applies only to the
current task and waiting phase, including after a restart. No means keep waiting
and preserve the work, not abort or complete the task. Approval and merge gates
still apply; Yes alone never authorizes a merge in a multi-option PR decision.
Supporting reports do not replace the visible question and options. Reminders
repeat the same active decision instead of truncating its choices.
Each input handoff shows one question with each numbered alternative on its own
line. Inline agent options are normalized without adding a second generic menu;
open questions accept full-text answers. The agent asks one decision and waits
for its answer before continuing.

Changing checkmarks is allowed; adding, removing or changing tasks is a scope
decision, not automatic permission for another implementation cycle. Tasks tagged
`[codebot:approval]` block for an explicit decision. Existing `[codebot:verification]`
tasks proceed to verification, where current results and evidence are reconciled
without restarting implementation. They remain unchecked if required evidence is
missing; the verifier asks for the specific missing result or artifact, not another
spec approval. Historical results do not prove the integrated version passed.
Unclassified pending tasks on an explicit `VERIFY` request pause
for a decision rather than restarting work. Replying with explicit authorization
to complete the pending implementation tasks of the already approved spec resumes
`IMPLEMENTING` without replanning, but only if the approved task inventory is
unchanged and no approval tasks remain. A bare "continue" or another `VERIFY`
does not grant this authorization. Controller-run unknown/interrupted
checks cannot be overridden by an agent's pass report. A clean internal review is
reused only on the exact content snapshot, including after task-state recovery.
Successful verification is also reused only on an exact snapshot; reported task
counts must match the actual checklist and OpenSpec CLI counts.

To recover a completed plan polluted by an unwanted replanning, explicitly send
`VERIFY --restore-plan <commit SHA>: optional guidance`. The commit must be an
ancestor with a complete checklist and the same artifact inventory. This replaces
only the active change's proposal, design, tasks and specs; it does not touch
application code, HEAD or the Git index, and does not imply approval or passing
tests. The current documents and staged diff are backed up outside the repository
under `data/verification-recovery/` before any replacement. Review legitimate spec
changes first; omit this option when they must be kept and reconcile them manually.

Top engineering priority: implement the requested behavior with as little code as
possible. Prefer reuse, simplification and justified net code removal; preserve
existing functionality, tests, readability and safety. Line-count tricks do not count.

With `CODEBOT_DETERMINISTIC_CHECKS=true` (default), the feature gate runs extensive
unit/functional checks, then genuinely selected E2E covering the actual changes,
including changed harness paths. E2E plans declare `kind: "e2e"`, focused plans declare
`scope: "focused"`, and a structured reporter must prove positive test execution
(`playwright`, `junit`, or `json` with `ran`). Missing/empty focused evidence blocks
the feature gate. Old unfiltered full-suite registrations remain saved, but do not
run or block feature verification, retries, publication or archival. Explicitly
selected commands with legacy `scope: "full"` are treated as focused checks; an
unfiltered command cannot masquerade as focused. Historical broad results appear
separately as non-required `audit_checks`, without being passed, waived or reused
as current feature evidence. `suite_status` retains raw outcomes (including those
audits); gate `status` depends on required checks only. A separate explicit controller
audit call (`staged_checks.run(..., audit=True)`) retains the full regression path.
At most two actual full E2E launches per task are admitted, including
baseline executions; reservations survive crashes and cache hits cost nothing.
Planning completion/archive alone does not invalidate code checks. Changed approved
requirements, design or task text still require reconciliation; approval fingerprints
ignore checkboxes, not intent. Agents must not launch full or baseline suites themselves.

The deterministic runner compares identifiable failures against immutable `base_sha`.
Results are reused only with matching content, command, tools and declared
environment. Unknown failures and incomparable baseline environments do not pass.
The default response to every failed/unknown/infrastructure check is diagnose and
repair approved behavior or operational prerequisites, not expand scope to fix
unrelated product debt or repeat the same command blindly. Confirmed baseline-only
failures do not block the regression gate: `status` can be `pass` while raw
`suite_status` remains `fail`. New/worsened failures, unknown attribution and missing
required feature evidence still block required-check evaluation; historical broad
audit failures are informational in the focused feature gate. Workspace-local checks can use isolated
repository baselines only with explicitly repository-local inputs; shared/composite
failures remain unresolved without an immutable composite baseline. Agent prose or
being "outside scope" is not baseline evidence. `REPAIR_CHECKS` reads actual feature/baseline logs, repairs
declared dependencies in the check's interpreter and fixes approved code/test/runner
behavior without requiring SSH or manual bot edits. The controller then reruns only
affected invalidated checks; unaffected valid results remain reusable. Agent prose
or "ignore it" never overrides the gate. Tracked repairs re-enter verification and
independent review; environment-only repairs return to the originating check phase.
Repair context/attempts survive restarts. After bounded failed repairs, the bot gives
the exact diagnosis and 2-4 actionable choices in Slack/email; choosing to continue
starts a fresh repair attempt, not another blind run. Product/scope changes or
unobtainable external prerequisites still require a concrete decision. Legacy final
check waits recover through the same path when the user replies; `VERIFY` is not
needed to restart planning or reset state.

In an existing workspace `WAIT_STUCK` caused by the old full-audit budget gate,
plain `retry` deterministically resumes the same task, preserving prior guidance,
approvals, registrations and audit reservations. The bot first reconciles a selected
plan against the actual repository/harness diff, then runs required checks and
continues normal checklist reconciliation and independent review. Interrupted
plan-recovery turns do not spend completed-repair attempts. This does not authorize
missing-evidence completion, publication, merge or DONE.

Check-plan metadata accepts `timeout_seconds` as an alias for `timeout`; conflicting
values, unknown fields and string-valued `argv` remain invalid. Validation names
the check and offending field. Plan repairs use durable attempt/recovery revisions
so rejected outputs are not replayed as new attempts; explicit `retry` resets the
repair counter, not passing evidence or the full-E2E budget.

Responses to user questions and their attachments are sent in the same thread in
every phase before the continuation advances. Failed/partial delivery preserves the
waiting phase and question rather than dropping the answer behind a progress banner.
The completed reply and its continuation are saved before delivery. Pending Slack/
email receipts retry transport only, without running the agent again or consuming
the phase failure budget; confirmed delivery resumes the saved continuation
automatically, including after a restart. Empty Slack attachments are sent as an
explicit zero-byte-source notice, never as invented passing-check evidence.
`.codebot/checks.json` can declare project-specific full/focused check plans.
See [harness contract](docs/harness-contract.md) for preparation, resource locks,
provider grouping and artifact manifests.

`data/execution.sqlite` stores additive durable outcomes/checkpoints and delivery
steps alongside the compatible `state.json` cursor. Keep both during migration;
do not delete execution state while a task is active. See
[delivery recovery](docs/delivery-recovery.md) and
[performance results](docs/performance-results.md).

```sh
python scripts/performance_report.py data/execution.sqlite <task-branch>
python scripts/benchmark_runtime.py
python scripts/test_trusted_runtime.py
```

The benchmark uses controlled local fixtures; its modeled permission-wait savings
are distinct from measurements of real Azure end-to-end throughput.

```
IDLE → pick item (see "Backlog sources") → claim it → branch <instance>-<slug>
      → EXPLORING → PROPOSING → send proposal → WAIT_APPROVAL
     → IMPLEMENTING → VERIFYING → INTERNAL_REVIEW → E2E (when present)
     → ARCHIVING → OPEN_PR → WAIT_REVIEW ⇄ ADDRESS_REVIEW → PUSHING (if review fixes exist)
     → send PR + evidence → WAIT_MERGE ⇄ ADDRESS_PR_THREADS → PUSHING
     → merge → mark item done (Doc / GitHub board / Jira workflow) → IDLE
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

### Review-friendly task messages

Task conversations announce important results, blockers and decisions in concise text.
Routine phase images and working check-ins are suppressed. STATUS reports live activity,
the last effective progress and the next action without starting a coding turn.
Completing or aborting without a confirmed merge never announces a merge.

Proposal-review messages attach one **offline, self-contained HTML file** containing
the actual change's `proposal.md`, `design.md`, `tasks.md` and all nested
`specs/**/spec.md` files. Its left index links to files and headings and can be
filtered; it uses no hosted assets. Each revision regenerates this package and
summarizes changed sections against the last version sent. Generated files and
snapshots are stored under git-ignored `data/review_packages/`, rather than in the
target repo. If required artifacts are missing or the full attachment exceeds the
configured mail limit, codebot asks for help instead of requesting approval of an
incomplete package. Attachment failures have independent durable receipts and retries.
Material feedback reopens planning and requires approval of the revised version; valid
implementation and the existing PR are preserved, and archived work gets a complementary
OpenSpec change. Localized corrections within approved intent proceed through verification.

Messages awaiting input start with the decision or question. Before asking for PR
review, codebot presents recorded verification outcomes (including skipped checks),
what changed, the PR and task links, and an index of evidence that was actually
available. After feedback is pushed, it relates the request to the changes and
new evidence. Upload failures are called out rather than passing for delivered files.

**At PR creation**, before waiting for an optional automated Code Review run, codebot
also sends a separate, informational architecture report. It compares the archived
OpenSpec plan with the committed PR diff, calls out high-impact implemented decisions
and unverified assumptions (for example, a separate managed database or elevated
container permissions), and links each item to a changed PR file. If review or later
feedback materially changes one of these decisions, only the delta is sent. An
unavailable analysis does not claim that no decisions exist. This report never asks
for another approval or delays automated review, the PR-ready handoff or merge.

Every tick starts by checking the mailbox for commands (ABORT / STATUS / DONE / HOLD /
CONTINUE, see below). Any state that keeps failing detours through WAIT_STUCK (see
"Failure handling"), and HOLD parks a task from any state.
Any phase can detour through WAIT_REPLY: if the coding agent needs the user, it ends its
output with `NEED_USER_INPUT: <question>`; codebot sends the question in the task's
configured email or Slack thread (optionally with `ATTACH: <path>` screenshots/videos)
and resumes the same session with the reply. When an exploration reply completes the
phase without another question, its explanation is sent to that thread before proposing.

Task-thread messages are acknowledged as durably received before interpretation, without
claiming that their instructions were applied. Routing gets only the current decision and
bounded input references; detailed verification/history remains available to the separate
answering session. Routing lowers configured reasoning effort to `low` (override via the
existing `CODEBOT_OPENCODE_ROLE_EFFORTS` entry `routing`) and uses a 60-second budget, or
the configured utility budget if longer. Coding model/effort and approval gates are unchanged.
Classification, retry and terminal failure notices use independent, deduplicated delivery
receipts. STATUS also shows message-processing progress. Timeouts report the effective
inherited deadline and elapsed time, not a larger nominal coding-turn timeout.

Evidence (screenshots, recordings, reports) is always routed to the task thread via that same
`ATTACH: <path>` convention (saved under the outbox dir), in every phase — never
committed to the target repo. Every resumed turn restates this rule. As a safety
net, designated handoffs after implementation and review or feedback repairs scan
the branch diff for evidence-looking files (video extensions, or paths naming
"evidence"/"recording") and have the coding agent remove and re-route them via
the configured channel if any are found.

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

Failure notices identify the concrete cause, impact, recorded recovery and verification
evidence, followed by one decision menu. Diagnostics retain redacted rejected check plans
and recorded results; repair counters alone are not proof of fresh provider calls.
Provider-filter notices quote the user's message and available submitted request, or
identify an internal bot request rather than asking the user to rephrase text they never
wrote. Long requests have visible excerpts and complete redacted attachments. Withheld
response text and unknown filter triggers are not invented; unchanged attempts are not
automatically retried. Follow-up conversation receives these facts, not just a file path.

Every external `git`/`gh` call has a timeout (`CODEBOT_SUBPROCESS_TIMEOUT`, default 120s)
so a hung command surfaces as a failure (feeding the budget) instead of wedging the loop.

Every working prompt starts with an "execution environment" preamble of runtime facts.
Add project-specific ones (how to run the tests, what is NOT available in the
container) in `data/environment.md` or `CODEBOT_ENVIRONMENT_NOTES`. Untrusted text
(e2e output, review comments, email bodies) is fenced as data inside prompts so an
embedded instruction cannot hijack the flow.

## Decision reminders

Working phases and automated review do not generate periodic reminders. An unchanged
pending decision gets at most one short reminder after 24 hours of no contact, stating
the decision and valid response without quoting prior messages or uploading the same
diagnostics again. `CODEBOT_PING_SCHEDULE=24h` enables this policy; `off` disables it.
STATUS/KICK contacts refresh the quiet clock without resetting the once-per-decision rule.
STATUS reports the last real email on the thread and how many check-ins followed it.

During an in-flight phase, `status?` (or `STATUS`) gets an independent, **brief**
answer in the task thread without waiting for the coding agent to finish. In Gmail
the supervisor checks for status mail during long turns; in Slack it reads the
durable Socket Mode inbox and, if an event was missed, reconciles the active thread
through Slack's API at most once a minute. The first reconciliation scans the full
thread; later scans cover only the last five minutes, so long conversations are not
downloaded repeatedly. Slack SDK HTTP DEBUG dumps (including message bodies) are
suppressed; its warnings and codebot's own DEBUG messages remain visible. The bot
also accepts a top-level status request.
Other task-changing commands remain queued for the main state machine except `KICK`,
which can interrupt the current agent turn. The status reply names the task,
phase and elapsed time. Codebot writes a small metadata-only turn snapshot to
`data/agent_activity.sqlite`; when using OpenCode it also reads the running session
and its subagents, tasks and latest event timestamps directly from OpenCode's own
`opencode.db` in read-only mode (including its WAL). This works from the main bot or
a separate status watcher, without sharing Python memory or starting another agent.
The message includes the active task, task counts, up to two recent subagents and
time since the last observable event. Neither reasoning content, prompts, shell
commands nor tool output is included. The answer states when the last event was
observed, without inferring whether the model is making progress from a quiet period;
un-emitted LLM thoughts cannot be inspected. Claude's buffered runner may provide
only turn-level signals until it completes. The supervisor never modifies task
state; between turns regular `STATUS` still returns the full state snapshot.

If a bot was already executing an agent turn when updated code was copied to its
bind-mounted `/app`, restarting that container would interrupt the turn. For a
Slack task, `python3 /app/scripts/status_watch.py` may instead run as a separate
container process until the task changes phase. It uses the same bounded Slack
history reconciliation and read-only OpenCode progress reader, answers only
`STATUS`, and never starts another agent or writes the bot's task state. The
built-in supervisor takes over at the next normal container restart.

## Mailbox commands: ABORT / STATUS / KICK / DONE / HOLD / CONTINUE

Email codebot with a body of exactly `ABORT`, `STATUS`, `KICK`, `DONE`, `HOLD` (or `PAUSE`),
or `CONTINUE` (or `RESUME`, which may be followed by a note) — case-insensitive,
trailing punctuation tolerated — optionally with an instance name (`ABORT codebot-x7k2`)
when several instances share the mailbox — see "Multiple instances" below for how bare
commands are scoped.
Commands are checked at the start of every tick; `KICK` also has an independent
supervisor so it works **during** a blocked coding turn. Bare `KICK` is scoped to
the task's thread (or its tagged email subject); outside it use `KICK <instance>`
to avoid interrupting unrelated bots. The other commands work even when the agent
is stuck waiting on a thread — or in
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
- **KICK** ends the currently running Claude/OpenCode CLI turn and retries its phase
  from the last saved FSM checkpoint. It keeps the claimed task, branch, approvals,
  session and files; the resumed agent is instructed to inspect partial work and
  completed tasks before continuing. It does not consume the phase failure budget,
  merge anything or reset the checkout. If no coding turn is active, it answers that
  nothing changed. A bare `KICK` in a Slack task thread works; a top-level Slack
  command must name this installation (`KICK <instance>`). The standalone
  `status_watch.py` used for older running containers reports STATUS only; the
  in-process supervisor is needed for KICK.
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
unresolved threads from anyone (a human reviewer included) and addresses them
(ADDRESS_PR_THREADS, capped by `CODEBOT_PR_THREAD_MAX_ROUNDS`). Human threads are
handled differently from the bot reviewer's: the agent sees the **whole thread** (every
comment, its own earlier replies labelled, the latest comment marked as the one to
answer) and is told the reviewer's word is final — a change request is implemented, not
debated, and a repeated request after an earlier explanation is implemented without
further argument. A thread that only asked a question gets an `answered: …` reply and is
**left open** for the reviewer to close; codebot does not touch it again until they post
something new. On `merge` it re-checks: unresolved threads (or a failed query) **block
the merge** with an email listing them; reply `merge anyway` to force. Merging is
irreversible, so the classifier must return an explicit `force: true` for that.

## Base-branch conflicts (RESOLVE_CONFLICTS)

While a PR is open (WAIT_REVIEW / WAIT_MERGE), every reply-less tick checks the PR's
state. A PR **merged on GitHub by someone else** finishes the task exactly as codebot's
own merge would (item marked done, "task complete" email); one **closed without
merging** stops with a stuck email asking whether to `complete`, `abort`, or reopen it
and `retry`. A `CONFLICTING` PR (the base branch moved — another PR merged) detours
through RESOLVE_CONFLICTS: the working session merges `origin/$CODEBOT_BASE_BRANCH`
into the branch (**merge, never rebase** — the branch is pushed), re-runs the tests and
commits; codebot pushes and the automated review re-runs before the PR can merge. A
conflict with genuinely different reasonable resolutions is emailed as a question
instead of guessed at. Attempts are capped by `CODEBOT_CONFLICT_MAX_ROUNDS` (default 3),
then the task escalates to WAIT_STUCK. Replies the user sent against the pre-conflict
PR content are set aside and the "PR ready" email says so.

## One-time setup

**Recommended**: run the guided terminal setup on a macOS or Linux host.
`scripts/setup.sh` works from bash or zsh, bootstraps a pinned Textual UI into
`.venv/`, and falls back to a menu-driven text mode if that UI is unavailable.
The first screen lists Repository, Backlog, Conversation, Agent, Evidence,
Self-healing and Advanced. Open sections in any order; `Test` checks only the current section
and its saved prerequisites, while `Save and Close` repeats that test and
immediately saves that section to `.env` on success. Failures leave the section
open with its draft intact and explain what to fix. A checkmark appears beside
saved sections; changing a dependency clears affected checkmarks. The menu's
`Test full setup` checks the whole saved configuration; `Exit` needs no final
save. Text mode offers the same menu with `t`/`s`/`e`/`d` in each section.
The main menu also has **Sync to VM** (`v` in text mode): enter `user@host`,
choose code-only (default) or confirm an initial copy, and optionally recreate
the remote bot container and wait for its health check. This runs against the
saved configuration; the SSH host is not stored in `.env`.
The preview masks secrets; Advanced searches every runtime setting, including
inactive integrations and timeouts. On repeat runs setup preserves unknown
keys, comments, untouched credentials and inactive settings in `.env`, backs
up the original, and writes successful sections atomically:

```bash
scripts/setup.sh
scripts/setup.sh --text    # also works: bash scripts/setup.sh / zsh scripts/setup.sh
```

Open **Self-healing** to select **Disabled** (`off`, default) or **Enabled**
(`auto`, automatic validated Python repairs). **Observe** and **Propose** are
available for a gradual rollout. Saving changes `.env` only; rebuild the image
and recreate the container separately to apply the mode. See
[self-healing](docs/self-healing.md) for repair boundaries and rollout guidance.

### One-time copy to an SSH VM

After configuring `.env`, stop any running bot on both machines and run:

```bash
bash scripts/sync-to-vm.sh azureuser@74.235.122.91
```

This uses `rsync` over SSH to copy this checkout to `~/codebot/app/`
and the local checkout named by `CODEBOT_REPO_PATH` to `~/codebot/target/`.
It includes `.git`, durable files in `data/`, `.env`, source and lockfiles, but excludes
`node_modules/`, `.venv/`, `venv/` and `ms-playwright/`: binaries installed
on an M1 Mac cannot run on an amd64 Linux VM. The target repo must install its
dependencies and Chromium for Linux/amd64 in the environment where its tests
run (the Coderbot image already installs Chromium's Linux system libraries;
the target's `e2e/run.sh` installs its pinned browser at runtime). If you
previously copied Mac-built dependencies to the VM, remove those stale copies
before installing Linux versions; rsync deliberately does not delete VM files.
The transient `data/heartbeat` is not copied; the container resets its
heartbeat at each start so an old timestamp cannot trigger a restart loop.
The script also copies your local `~/.npmrc` to the VM user's home with mode
`0600` so `docker-compose.override.yml` can mount it. At container startup,
the root entrypoint copies it from a read-only seed into the bot user's home
with mode `0600`. Short-lived registry tokens will need refreshing on the VM;
restart the container afterward so it picks up the replacement.
The copy does not delete files already on the VM, but does replace files
with the same names. Do not run both copies as separate bots against the same
backlog: the copied `data/` includes this installation's ownership identity.
On the VM, `~/codebot/app/.env.local` preserves the original local `.env`,
and `.env` gets
VM-specific paths (`CODEBOT_REPO_PATH` points to the target on that VM;
absolute paths into the app become container paths under `/app`). The local
`.env` is untouched. The script refuses to overwrite an existing `.env.local`;
move that backup aside before running a new one-time copy. Other configured
absolute paths outside these two checkouts must be configured separately;
the script stops before copying if it cannot translate them. Check VM-specific
mounts in the copied
`docker-compose.override.yml` and the host's `~/.claude.json`/`~/.claude` as
needed before starting Compose on the VM.
OpenCode's `data/opencode/cache/` is excluded from the initial copy: the VM
rebuilds its own platform-specific tools (including `rg`), while its credentials,
sessions and state in `data/opencode/data/` remain part of the migration.
For later code updates, use `bash scripts/sync-to-vm.sh azureuser@74.235.122.91 --code-only`.
This leaves the VM's `data/`, `.env`, `.env.local`, `~/.npmrc` and target checkout
untouched; restart the VM's bot container to load the new Python code.
The script uses passwordless `sudo` on the VM to give uid/gid `501` ownership
of `~/codebot/app/data` and `~/codebot/target`: the container's non-root bot
must be able to write its instance fingerprint, state and Git working tree.
If sudo is unavailable during the copy, run this on the VM before starting
the container:

```bash
sudo chown -R 501:501 ~/codebot/app/data ~/codebot/target
```

### Start with no previous task

If the copied `data/state.json` refers to an earlier backlog or email thread,
stop the bot and reset **only** its task/conversation state on the VM:

```bash
cd ~/codebot/app
docker compose stop codebot
sudo python3 -m scripts.reset_bot_state
docker compose up -d codebot
```

The script moves the old active task, holds, Slack inbox and heartbeat into a
private timestamped directory under `data/`. It retains the installation's
identity, OpenCode/Google credentials and email deduplication markers, and does
not modify the target repo or the original backlog item. You can inspect the
backup path printed by the command if the old task needs manual reconciliation.

Slack: create the bot `xoxb-` token under [OAuth & Permissions](https://api.slack.com/apps)
with `channels:read`, `channels:history`, `chat:write` and `files:write`; install
or reinstall the app after changing scopes. Enable Socket Mode and create the
`xapp-` app-level token with `connections:write` under Basic Information. Invite
**that app's bot** to an active public channel using its Add people/apps menu.
Conversation lists only public channels joined by that bot; `Test` checks both
tokens, membership **and** `channels:history` read access. If it reports a
missing scope, add the named bot scope and reinstall the app; if it reports
non-membership, invite the bot and reload channels. Neither error saves `.env`.
Backlog lists accessible Jira projects and workflow statuses or GitHub Projects
boards after their credentials are available. Setup offers Google consent and
OpenCode provider login actions in relevant sections, and requests only the
Google APIs needed by the selected backlog, channel and Drive upload setting.
All editable keys/defaults are in [`.env.example`](.env.example); a coverage
test catches newly added runtime options missing from setup.

<details>
<summary>Manual setup (what <code>scripts/setup.sh</code> automates)</summary>

1. **Google OAuth client (when using email, GDoc or Drive upload)**: in Google
   Cloud Console create a project, enable the needed Gmail, Docs and/or Drive
   APIs, create an OAuth client of type
   *Desktop app*, and download its JSON to `data/credentials.json`. The consent
   asks for full Drive access when the activity trail posts comments on a Doc or
   evidence videos are uploaded to Drive. Jira+Slack with evidence upload off
   needs no Google credentials.
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
   # Backlog: "gdoc" (default), "github" or "jira"
   CODEBOT_TASK_SOURCE=gdoc
   # Required when gdoc: Google Doc id of the improvements backlog
   CODEBOT_DOC_ID=<the id from the doc's URL>
   # Required when github: the Projects v2 board (GH_TOKEN then also needs "project")
   CODEBOT_GH_PROJECT_URL=https://github.com/orgs/<owner>/projects/<n>
   # Required for Jira: site, project, account and API token; map real statuses
   CODEBOT_JIRA_URL=https://<site>.atlassian.net
    # Example: ENG is the project key in issue ENG-42
    CODEBOT_JIRA_PROJECT_KEY=ENG
   CODEBOT_JIRA_EMAIL=<account@example.com>
    CODEBOT_JIRA_API_TOKEN=<Atlassian API token>
    CODEBOT_JIRA_PICK_LABEL=codebot-ready
   CODEBOT_JIRA_PICK_STATUS=Ready
   CODEBOT_JIRA_ACTIVE_STATUS=In progress
   CODEBOT_JIRA_REVIEW_STATUS=In review
   CODEBOT_JIRA_DONE_STATUS=Done
   # Independent channel: "email" (default) or "slack"
   CODEBOT_COMM_CHANNEL=email
   # Required when email: address codebot sends to and reads replies from
   CODEBOT_USER_EMAIL=you@example.com
   # Required when slack: dedicated app/bot credentials for this instance
   CODEBOT_SLACK_CHANNEL_ID=C123456
   CODEBOT_SLACK_BOT_TOKEN=<xoxb-token>
   CODEBOT_SLACK_APP_TOKEN=<xapp-token>
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
   # Optional GPT reasoning effort; low favors speed, medium balances speed/depth.
   # Choices: none, minimal, low, medium, high, xhigh (model/provider dependent).
   # Empty preserves OpenCode defaults. Also editable in setup's Agent section.
   OPENCODE_EFFORT=
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
   thresholds, project-specific environment notes) is optional, editable in Advanced
   and documented with its default in `.env.example`.
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

Everything codebot must remember across restarts lives in `data/` — Google
OAuth when needed, `state.json`, `holds.json`, `instance_id`,
`instance_fingerprint`, the processed-mail list and `slack_inbox.sqlite` when
using Slack — and both compose files bind-mount that directory from the host (never a
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

Any number of codebots can share a backlog. New installations generate a stable
readable name in `data/instance_id` (e.g. `codebot-warty-warthog`) and an opaque
ownership fingerprint in `data/instance_fingerprint`. Existing `codebot-x7k2`
names are retained. The readable name tags email subjects and git branches;
`CODEBOT_INSTANCE` may override it. **Back up both files** with the rest of `data/`.

Instances coordinate only through the backlog. In the Doc, picking a task atomically
appends `[implementing: <instance>@<fingerprint>]` to its bullet (the write carries the doc revision
it was read at, so two instances can't both win). On a GitHub board, picking adds the
`codebot:<instance>@<fingerprint>` label to the issue and re-reads it; if another instance's label
landed too, both back off and repick next tick. Claimed tasks are invisible to the
others' PICK until the marker/label is removed on completion or abort. Legacy
name-only markers are adopted only when locally saved task state identifies them;
otherwise they need manual reconciliation. If an
installation is retired mid-task, send it `ABORT <name>` first (which unclaims) or
remove the marker/label by hand.

Email-mode instances can share a Gmail account; each one only reads replies on its own
threads (subjects carry its prefix). Address mailbox commands to one instance —
`ABORT codebot-x7k2`, `STATUS codebot-x7k2`, `DONE codebot-x7k2`, `HOLD codebot-x7k2`
— or reply with the bare command on one of its threads. A bare `ABORT` or `STATUS`
sent anywhere else is honored by **every** instance (fleet-wide stop / fleet status —
their last-resort role); a bare `DONE`, `HOLD` or `CONTINUE` outside an instance's
threads is ignored, since they act on one instance's task. Only the instance that put
a task on hold can resume it; delete its `[on hold: …]` marker by hand to free the task
for anyone. In Slack mode each instance uses separate app credentials; commands
only work as replies in that app's own task threads, never as fleet-wide top-level
messages.

## Secrets for live checks (optional)

Some changes can only be proven against the real third-party API (payments, email
providers, LLM APIs). With `CODEBOT_CREDENTIAL_REQUESTS=on` the agent can ask for a
credential it is missing, and you never paste the value into Slack or email.

How it works:

1. The agent ends a turn with
   `CREDENTIAL_REQUEST: {"env": "STRIPE_KEY", "service": "Stripe", "purpose": "live refund test"}`.
   Codebot pauses the phase (`WAIT_REPLY`) and offers three options:
   **1** send it through a secure one-time link, **2** continue without it, **3** pause.
   Numbers, short phrases ("sigue sin la clave", "send me the link") and free text are
   accepted; anything it cannot classify is handled as a normal answer.
2. On **1**, codebot starts a temporary [PrivateBin](https://privatebin.info) container
   (`CODEBOT_PRIVATEBIN_IMAGE`, burn-after-reading on, no discussions or attachments) and
   exposes it through a [localhost.run](https://localhost.run) SSH tunnel. It sends you
   the `https://….lhr.life` address. Paste only the value, press Create, and reply with
   the link PrivateBin gives you.
3. Codebot reads the paste once through the local container address, decrypts it
   locally (the key is in the link's `#fragment`, which PrivateBin's server never sees),
   closes the page and resumes the agent. The paste is destroyed by that first read.
4. On **2**, the agent continues; checks that need the credential are reported as
   **skipped**, never as passed. On **3**, nothing happens until you reply 1 or 2.

Where the value goes: `data/secrets/<task>/<NAME>.json` (directory `0700`, file `0600`)
for `CODEBOT_SECRET_TTL_SECONDS` (default 4 h), deleted when the task ends. It is injected
as environment variable `NAME` into the agent (Claude Code and OpenCode, from the next
turn) and into controller-run checks, and registered with the log redactor. It is never
written to `state.json`, transcripts, prompts, PR bodies or Git. Names that would shadow
the bot's own settings (`GH_TOKEN`, `PATH`, `CODEBOT_*`, `CLAUDE_*`, `SLACK_*`…) are refused.

Optional checks: a check in the agent's `CHECK_PLAN` (or `.codebot/checks.json`) that
calls a live API lists its credentials in `"env_keys"` and sets `"optional": true`. Without
those values it is reported as `skipped (missing credential NAME)`; with them it runs
normally.

Requirements and limits:

- The bot needs the Docker CLI with access to the daemon and outbound SSH (port 22) to
  `localhost.run`; the image already has both. Inside Docker the page joins the bot's
  network namespace and listens on `127.0.0.1:48080`. If the page cannot start, codebot
  says why and options 2 and 3 still work.
- `CODEBOT_PRIVATEBIN_INSTANCES` (comma-separated HTTPS base URLs) accepts links from
  PrivateBin instances you already run, without the temporary page.
- The full link you reply with stays in Slack or the mailbox. It is useless after codebot
  reads it (burn after reading), but prefer short-lived, least-privilege credentials.
- Codebot replaces any PrivateBin-shaped link with `[secure-link]` before logging or
  showing a reply to the agent, and scrubs its local Slack inbox copy once handled.

## Optional Caveman mode

In `scripts/setup.sh`, open **Agent** and select **Usar Caveman: YES/NO**.
YES saves `USE_CAVEMAN=true`; NO saves `USE_CAVEMAN=false`.
Only the exact lowercase string `true` enables the mode; every other value
(including unset, empty, `TRUE`, `yes`, `1` or surrounding spaces) disables it.
Recreate the container after changing `.env`.

The image includes the official `JuliusBrussee/caveman` plugin, pinned to v3.2.0
commit `e20f07e8152a0c0360f58c09e79d30ac94329991`. Rebuild the image once to install it.
When enabled, the selected Claude Code or OpenCode coding harness starts in
Caveman mode and supports `/caveman`; text-only utility/conversation sessions
retain their plugin isolation. No proxy, global installer or target-repo rules
are installed. Machine-readable output contracts and permission settings stay unchanged.

## Toolchain versions

The image pins exact versions for reproducible rebuilds: the base image
(`python:3.12.13-slim`), the claude-code, opencode and openspec CLIs (`@<version>` in
the `Dockerfile`), the Superpowers plugin (`config.SUPERPOWERS_VERSION`), Node (major
`22` via nodesource), and the Python deps (`==` in `requirements.txt`). To bump: read
the version currently working in the running container (`docker compose exec codebot
claude --version`, `… openspec --version`), edit the pin, and rebuild
(`docker compose up -d --build`). Don't switch these to floating/`latest` — a silent
CLI behavior change between rebuilds is exactly what the pins prevent.

The image also installs Docker CLI, Compose and Buildx from Docker's apt
repository. Buildx is required for target harnesses that prepare images using
BuildKit secrets; it is checked during the image build, including as user `bot`.
Rebuild the image to add these tools to an existing deployment; restarting alone
does not update installed packages. Private registry credentials remain external
to the image and are required only when the target harness needs preparation.

## Smoke tests

Inside the container (`docker compose exec codebot bash`):

```bash
claude -p 'say ok' --model "$CLAUDE_MODEL" --dangerously-skip-permissions  # CODEBOT_AGENT=claude
opencode run --auto --model "$OPENCODE_MODEL" 'say ok' # when CODEBOT_AGENT=opencode
gh auth status                                       # GH token works
docker buildx version                                # BuildKit secret support is installed
git -C "$CODEBOT_REPO_PATH" fetch                    # HTTPS auth via GH_TOKEN works
cd src && python3 -c 'import task_source; task_source.validate(); print(task_source.list_pending_items())'
cd src && python3 -c 'import gmail_client; print(gmail_client.send("[codebot] test", "hello"))'  # email mode
cd src && python3 -c 'import slack_client; slack_client.validate(); print("Slack app and public channel accessible")' # Slack mode
```

Unit tests, on the host from the repo root:

```bash
python3 -m unittest discover -s tests -t .
```

### Manual integration smoke (using a disposable task)

1. Run `scripts/setup.sh` on macOS or Linux and review/save a configuration
   with Jira+Slack. Repeat with `scripts/setup.sh --text`, change one Advanced
   value, and verify the old `.env` backup still has every untouched line,
   secret and comment. Verify GDoc+Slack and Jira+email selections separately.
2. In the selected Jira project create a disposable issue in the configured pick
   **status** with `CODEBOT_JIRA_PICK_LABEL`, and another issue in the same status
   without that label. Start Coderbot; the unlabeled issue must stay untouched.
   Confirm one fingerprinted claim label on the labeled issue and one
   root Slack message bearing the issue link and instance name. With a second
   bot app in the same public channel, check that only the owning app responds
   to replies in that thread. Reply `STATUS`, then `HOLD`; the issue should be
   held and the bot can claim another issue. Reply `CONTINUE` on the original
   thread to recover it. Restart the container while a reply is pending and
   confirm that the reply is handled once.
3. Approve the proposal in the same thread. When the PR opens, verify
   `Closes <JIRA-KEY>` plus a Jira URL in its body, a PR link in a Jira comment,
   and Jira's review status (not done). Review the PR and reply `merge`; Jira
   should transition to done and lose only this bot's ownership label. Repeat
   with `DONE` and `ABORT` on disposable issues to verify each transition.
4. In Slack mode with a dirty checkout before any issue is claimed, confirm one
   operational announcement, ignored replies to it, and automatic retry once
   the checkout is cleaned. If evidence is available, inspect the Drive video
   link and an uploaded report/image in the task thread. Test long proposal
   messages and a post-conflict stale merge reply on a disposable PR.

## Troubleshooting

**`managed runtime unavailable: missing ...`**: coderbot fails closed at startup if
the image's managed Superpowers, coderbot/OpenSpec bridge or generated OpenSpec skill
files are absent. Rebuild the image from this repository and check that no volume
mount replaces `/opt/coderbot/plugins`, `/opt/coderbot/agent-plugin` or
`/opt/coderbot/openspec-skills`; do not install Superpowers or run `openspec init` in
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
evidence for the PR handoff in the selected conversation channel:

- **Playwright**: the implementation must include one demo test tagged `@evidence`
  that walks through the feature visibly. Codebot first re-runs only that test
  (`./run.sh <spec> --grep @evidence`), falling back to the feature's full specs when
  no clip appears, with `PW_VIDEO=on` exported (the target harness can use it to enable
  `video: "on"` in `e2e/playwright.config.ts`). The resulting `.webm` clips from
  `e2e/test-results/` are stitched into a single H.264 `data/evidence.mp4` with ffmpeg
  (each clip scaled/padded to 1280x720 so mixed viewport sizes concatenate cleanly).
  If ffmpeg is unavailable or stitching fails, it falls back to attaching the raw
  `.webm` clips.
- **Newman**: re-runs the feature's collection(s) and attaches the newest
  generated report file from `e2e/test-results/`.

The stitched mp4 is not attached when Drive upload is enabled: it is uploaded to Google Drive
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
## Explicit verification scope

Questions, decisions and explanatory context stay visible in the conversation
body in both Slack and email, even when long. Slack splits long text within the
same thread. Raw JSON diagnostics, tracebacks and logs belong in supporting
`.txt` reports; opening an attachment is not required to discover the question.

Targets can define `.codebot/checks.json` (version 1) to replace automatic check
discovery with an explicit list of active suites. `inputs` selects source inputs;
`dependency_inputs` optionally selects dependency manifests relevant to each
check (for example `backend/requirements*.txt`). Historical trees omitted from
those lists do not affect these checks. Document their exclusion in the target's
OpenSpec so exploration, review and recovery preserve the same scope. This is a
scope decision, not a passing result for excluded tests.

Baseline comparisons reuse installed dependencies only when the relevant
manifests match. If they differ, the controller prepares the immutable baseline's
own npm lockfile/Python requirements in its disposable worktree; installs are never
mixed with the feature checkout. Preparation failures include their actual log and
enter diagnosis/repair rather than being reported as preexisting failures. Controller
final-task completion commits only its exact checkbox delta, with provenance,
before archival; unrelated edits remain protected.
