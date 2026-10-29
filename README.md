# coderbot

Autonomous coding agent that works the improvements backlog — a Google Doc
(`CODEBOT_DOC_ID`), a GitHub Projects v2 board (`CODEBOT_TASK_SOURCE=github`),
or a Jira Cloud project (`CODEBOT_TASK_SOURCE=jira`) —
of whatever target repo you point it at (`CODEBOT_REPO_PATH`). It plans with the
managed OpenSpec workflow, drives a headless coding agent (Claude Code or OpenCode),
and talks to the team by email (default) or Slack (`CODEBOT_COMM_CHANNEL=slack`).
Several instances can share a backlog; Slack instances each use their own app.

Coderbot is repository- and organization-agnostic. Target-specific checks, provider
groups, preparation and artifacts belong in the target's declarative contracts,
not hardcoded application adapters. See [harness contracts](docs/harness-contract.md).

Target mode is automatic: a root `.git` means single repository; otherwise the
directory is a workspace. See [feature workspaces](docs/workspaces.md).

Optional independent supervision and validated Python repairs are documented in
[self-healing](docs/self-healing.md); they are off by default.

## Repository layout

- `src/` — the bot: `main.py` (state machine and single-repo handlers),
  `lifecycle.py` (the lifecycle over a list of repositories, used by workspace mode),
  `config.py`, `prompts.py` (system prompt and phase cards), `turns.py` (structured
  turn results), `tool_policy.py` (runtime guardrails), `replies.py` (reply
  classifier), `errors.py` (typed errors), `message_templates.py` (EN/ES catalog), the
  agent runners and the backlog/channel clients. Flat modules, imported by name.
- `tests/` — unit tests and the fake-agent walks (`tests/test_fsm_walk.py`,
  `test_workspace_walk.py`, `test_supervisor_walk.py`).
- `scripts/` — `setup.sh` (guided host setup), `setup_oauth.py` (Google consent),
  `entrypoint.sh` and `healthcheck.sh` (container runtime), `supervisor.py` and
  `selfheal.py` (self-healing), `test.sh` (the suite on the image's Python 3.12).
- `agent-plugin/` — the Claude/OpenCode plugin injected into agent sessions,
  including the guard hook (`runtime/guard.py`).
- `openspec/` — this repo's own OpenSpec specs and changes.
- `data/` — durable runtime state (git-ignored, bind-mounted into the container).

More detail: [conversation and diagnostics](docs/conversation-recovery.md),
[parallel conversation](docs/parallel-conversation.md),
[evidence delivery](docs/evidence-delivery-recovery.md),
[delivery recovery](docs/delivery-recovery.md),
[runtime checks](docs/runtime-checks.md),
[performance results](docs/performance-results.md).

## Target repo prerequisites

Coderbot lives in its own repo and is pointed at a target checkout via
`CODEBOT_REPO_PATH`. Two pieces of target-repo infrastructure make the lifecycle
stricter, but neither is required. Coderbot detects each at the start of every task:

- An **`e2e/` test harness**, run via `e2e/run.sh` by the final checks. Either tool
  is supported, detected by what's under `e2e/`:
  - **Playwright** (web UI) — `e2e/playwright.config.ts`; specs under `e2e/tests/`;
    evidence is a stitched video from `test-results/`.
  - **Newman/Postman** (API-only repos) — collections under
    `e2e/collections/*.postman_collection.json`; evidence is the run report.
  - **Missing**: the E2E gate is skipped for the task, and coderbot adds a backlog
    item requesting the harness (Playwright with a frontend, Newman otherwise).
- A **`Code Review` GitHub Actions workflow** — matched by its top-level `name:`
  being exactly `Code Review` — drives the automated review loop. Coderbot's
  self-healing item asks for it to be built on
  [alibaba/open-code-review](https://github.com/alibaba/open-code-review), posting
  as `github-actions[bot]`, and to run on `opened, ready_for_review, synchronize,
  reopened` only while the PR is not a draft.
  - **Missing**: the review wait is skipped and coderbot adds a backlog item for the
    workflow (`CODEBOT_SELF_HEAL_CODE_REVIEW=off` stops that).

OpenSpec owns requirements, design and tasks; Superpowers owns engineering
discipline; coderbot owns state, archive timing, push, PR and merge. The image
supplies pinned OpenSpec 1.9.0 and Superpowers v6.3.0 for both Claude Code and
OpenCode: the `openspec-*` skills are generated at build time by the pinned
`openspec init` into `/opt/coderbot/openspec-skills`, so target repos need neither an
`openspec init` of their own nor a Superpowers installation.

## Backlog sources

`CODEBOT_TASK_SOURCE` selects where tasks come from: `gdoc` (default), `github`,
or `jira`. `CODEBOT_COMM_CHANNEL` independently selects `email` (default) or
`slack`: GDoc+Slack and Jira+email both work. `main.py` only talks to the
`task_source` façade; each backend implements the same operations (list pending,
claim, hold, unclaim, mark done, seed an item, link the PR).

### Google Doc (`CODEBOT_TASK_SOURCE=gdoc`)

The backlog is a Google Doc (`CODEBOT_DOC_ID`). Each **top-level bullet** is one task.
Indented sub-bullets are the user's clarifications of that task and are shown to the
agent with it, never picked on their own. Inline images under a bullet are downloaded
to `data/doc_images/` and handed to the exploration session. Struck-through bullets
are done; coderbot strikes a task through when its PR merges or on `DONE`.

To force an order, write `Codebot[1]`, `Codebot[2]`, ... anywhere in a task's text:
tagged tasks are picked before untagged ones, lowest number first.

Set `CODEBOT_DOC_SECTION` to a heading text (e.g. `New:`) to make only the bullets
under that heading pickable. Picking a task appends
`[implementing: <instance>@<fingerprint>]` to its bullet so other instances skip it;
the marker is removed when the task completes or is aborted. A task parked with HOLD
carries `[on hold: <instance>@<fingerprint>]` instead.

When idle, coderbot picks, in this order: a held task whose CONTINUE was requested;
a task it already claimed but lost track of; the lowest `Codebot[n]` tag; otherwise
the coding agent chooses among all pending items (prerequisites first). That PICK
call also reports the task's language, so no separate detection call is needed.

### GitHub Projects v2 (`CODEBOT_TASK_SOURCE=github`)

The backlog is a Projects v2 board (`CODEBOT_GH_PROJECT_URL`, or
`CODEBOT_GH_PROJECT_OWNER` + `CODEBOT_GH_PROJECT_NUMBER`). A task is a board card
backed by an **issue of the target repo** (the checkout's `origin`, or
`CODEBOT_GH_ISSUE_REPO`): the issue title is the task, its body the clarifications,
images in the body go to `data/gh_images/`. Draft cards, PR cards, cards from other
repos and closed issues are ignored.

Cards move through the board's **Status** field (names configurable, case-insensitive):

| Event | Status | Labels on the issue |
|---|---|---|
| pickable | one of `CODEBOT_GH_PROJECT_PICK_STATUSES` (default `Ready`) | none |
| picked | `In progress` | `codebot:<instance>@<fingerprint>` |
| PR opened | `In review` (PR linked in a comment; PR body says `Closes #n`) | unchanged |
| HOLD | unchanged | `codebot-hold:<instance>@<fingerprint>` |
| ABORT | first pick status | removed |
| merged / DONE | `Done` | removed |

`Codebot[n]` in an issue title orders tasks; without a tag, a single-select
**Priority** field orders them by option position. `GH_TOKEN` needs the `project`
scope in addition to `repo` (a classic PAT's `read:project` cannot move cards).

### Jira Cloud (`CODEBOT_TASK_SOURCE=jira`)

Configure `CODEBOT_JIRA_URL=https://<site>.atlassian.net`, `CODEBOT_JIRA_PROJECT_KEY`
(the `ENG` in `ENG-42`), `CODEBOT_JIRA_EMAIL` and `CODEBOT_JIRA_API_TOKEN` (create one
at [Atlassian account security](https://id.atlassian.com/manage-profile/security/api-tokens)).
Map the **workflow status** names (`CODEBOT_JIRA_PICK_STATUS`,
`CODEBOT_JIRA_ACTIVE_STATUS`, `CODEBOT_JIRA_REVIEW_STATUS`, `CODEBOT_JIRA_DONE_STATUS`);
these are not board columns. `scripts/setup.sh` lists the project's statuses for you.
A new task needs **both** the pick status and the opt-in label
`CODEBOT_JIRA_PICK_LABEL` (e.g. `codebot-ready`). The issue summary is the task;
descriptions and attached images go to the agent.

Claims use `codebot-claim-<instance>-<fingerprint>` labels; HOLD uses
`codebot-hold-<instance>-<fingerprint>`. When the PR opens, the issue moves to the
review status and gets a comment with the PR URL; the PR body contains
`Closes <JIRA-KEY>` and the Jira link. Coderbot transitions the issue to done after
merging or on DONE. ABORT restores the pick status. Self-healing items are created as
`CODEBOT_JIRA_ISSUE_TYPE` (default `Task`).

### Activity trail

Whatever the source, coderbot leaves a log on the item itself (`CODEBOT_ACTIVITY_TRAIL`,
default on): a note at every milestone (picked, proposal, approval, questions and
answers, verified, reviewed, archived, PR opened, review rounds, merged, hold,
resume, abort, stuck) with the full message body. On GitHub and Jira each note is an
issue comment. On the Google Doc the first note creates a doc-level comment and later
notes reply in that thread; this needs the full `drive` OAuth scope. A tracker failure
is logged and never affects the task.

## Conversation channels

Email is the default (`CODEBOT_COMM_CHANNEL=email`): `CODEBOT_USER_EMAIL` receives
task threads and its listed senders can reply or issue commands. In Slack mode set
`CODEBOT_SLACK_CHANNEL_ID` (public `C...` channel), `CODEBOT_SLACK_BOT_TOKEN`
(`xoxb-...`) and `CODEBOT_SLACK_APP_TOKEN` (`xapp-...`). Create **one Slack app per
coderbot instance**, with Socket Mode, an app-level token with `connections:write`,
the bot scopes `chat:write`, `channels:history`, `channels:read`, `files:write`, and
the `message.channels` event. Invite each app to the channel.

Coderbot posts one root message when it claims a task. Approvals, questions, PR
review and commands stay in that task's replies; any human member of the channel can
act. Top-level messages are ignored except `status?`/`STATUS` and an explicitly
targeted `KICK <instance>`. If a Socket Mode event is missed, the waiting thread is
reconciled from Slack history at most once a minute. Slack edits the root message with
the task's live progress (phase, `:loading:` while working, `:question:` when input
is needed, the last three safe log lines), at most once every 30 seconds.

Each task's language is taken from its backlog text and used for agent output,
OpenSpec artifacts and outgoing messages. Coderbot's own fixed messages come from an
English/Spanish catalog (`message_templates.PHRASES`), so they cost no model call in
either language. A model translates only text the catalog does not cover, or messages
in other languages.

## Lifecycle

```
IDLE → pick → claim → branch <instance>-<slug>
  → EXPLORING → PROPOSING (⇄ REPLANNING) → WAIT_APPROVAL
  → approval: commit the approved change, push, open a draft PR
  → IMPLEMENTING (one turn per tasks.md section; push after every turn)
  → VERIFYING (controller) → INTERNAL_REVIEW (read-only reviewer)
       ⇄ APPLY_FEEDBACK (one fix wave) → VERIFYING → one re-review
  → E2E (controller final checks; evidence recorded) → ARCHIVING
  → OPEN_PR (mark ready) → WAIT_REVIEW ⇄ ADDRESS_REVIEW → PUSHING
  → WAIT_MERGE ⇄ ADDRESS_PR_THREADS / APPLY_FEEDBACK / RESOLVE_CONFLICTS → PUSHING
  → merge → mark the item done → IDLE
Failing checks at any gate → REPAIR_CHECKS → back to that gate.
```

### Planning

EXPLORING and PROPOSING share one planning session. EXPLORE records a
`classification` (`bounded`, `spike` or `architectural`) and a PR title. PROPOSE writes
`openspec/changes/<slug>/`. Coderbot sends the proposal as one offline HTML file
(proposal, design, tasks and every spec, with an index), stored under
`data/review_packages/`. Revisions regenerate it and summarize what changed.

Planning turns run read-only or write only under `openspec/`, and cannot commit
(see "Guardrails"). As a backstop, when a planning phase ends coderbot reverts any
overreach: premature commits are undone, tracked changes outside `openspec/` are
discarded, and the proposal message says what was cleaned up.

### Approval and the draft PR

On approval (`CODEBOT_DRAFT_PR=on`, the default), coderbot commits the approved
change, pushes the branch, reuses or opens a **draft PR** and posts its link. A failure
is retried once; after that the PR is opened at the end instead. `off` keeps the PR
for the end. The task inventory and an approval fingerprint of the change are frozen
at approval. Later changes to requirements, design or task text are scope drift: you
are offered "1 accept" (re-baseline) or "2 restore".

### Implementation

The implementer starts a **fresh session** at approval, seeded with the approved change
and a short handoff. Every later implementer turn (repairs, the review fix wave,
review threads, conflicts, feedback) resumes it. When the change is architectural, the
first turn writes `plan.md` in the change with Superpowers' `writing-plans`.

Each IMPLEMENTING turn covers one `tasks.md` section. After every turn the controller:

- runs the checks the agent declared (each distinct command once per plan; an
  unchanged command reuses its result), except browser E2E, which waits for the final
  checks;
- moves committed evidence files out of the repo (`git rm` plus a commit) and into
  the conversation;
- checks the checklist through `openspec instructions apply`;
- pushes to the draft PR.

**Budget:** implementation gets `tasks.md` sections + 3 turns, at most 12, frozen at
approval. When the turns run out, or after `CODEBOT_QUALITY_GATE_MAX_ROUNDS` (3) turns
with no progress, coderbot retries **once in a fresh session** (with
`CODEBOT_ESCALATION_MODEL` if set), which gets 3 more turns. If that also stalls, it
asks you.

### Verification

VERIFYING normally runs no agent turn. The controller runs
`openspec validate --strict`, checks the checklist counts, and uses the focused check
results from the last turn. Pending implementation tasks send the task back to
IMPLEMENTING; failing checks go to REPAIR_CHECKS. An agent VERIFY turn runs only when
the controller cannot decide: approved `[codebot:verification]` tasks to reconcile, a
change that does not validate, or a `VERIFY` request with guidance.

### Feature review

INTERNAL_REVIEW is one review of the whole feature. It runs in a **fresh, read-only
session** (`CODEBOT_REVIEW_MODEL` if set) with Superpowers' `code-reviewer.md` template
filled with the task, the change, the base SHA and HEAD; a built-in checklist is used if
the template is missing. If the reviewer changes the tree anyway, its review is
discarded (its edits are stashed). Above `CODEBOT_REVIEW_SPLIT_LINES` (2000) changed
lines, the first review is split into two parts by changed files.

Critical or Important findings get **one fix wave** in the implementer session
(APPLY_FEEDBACK, with `receiving-code-review`), then VERIFYING, then **one re-review**
of the fix commits only. After that, open Critical findings go back through the gate;
Important and Minor leftovers do not block and are listed on the PR body and in the
"PR ready" message. A passing review is reused only for the exact content it reviewed.

### Final checks, evidence and archive

The E2E state runs the controller's final checks (`src/final_checks.py`), with no
agent turn:

- unit/functional checks first, then the selected E2E specs. When the plan has no
  focused E2E check, the specs this branch changed are selected (`e2e:changed`); if it
  changed none, E2E is not required.
- A structured reporter must prove that tests ran; zero tests is not a pass.
  `CODEBOT_EMPTY_SUITE_FAILS=on` also fails declared non-E2E checks that ran zero tests.
- Failures are compared with the same check on `base_sha`; failures that already exist
  there do not block. New failures, unknown attribution and missing focused evidence
  block.
- Failures go to REPAIR_CHECKS: one FIX turn with the controller's report. A repair
  that changed tracked files returns to VERIFYING (and to review if the content
  changed); an environment-only repair returns to the gate it came from. After
  `CODEBOT_QUALITY_GATE_MAX_ROUNDS` repairs, you get the diagnosis and choices.

The **evidence** video is recorded during that same E2E run (`PW_VIDEO=on`); see
"Evidence". Then ARCHIVING archives the OpenSpec change on the feature branch, so it
lands with the implementation. If the archive fails, one FIX_ARCHIVE turn may repair
OpenSpec files only (`CODEBOT_ARCHIVE_MAX_ROUNDS`).

OPEN_PR edits the draft's title and body (concerns included) and runs `gh pr ready`;
without a draft it opens the PR. The backlog item moves to review.

### Review threads and merge

With a Code Review workflow, WAIT_REVIEW waits for a run to finish
(`CODEBOT_REVIEW_WAIT_TIMEOUT`, default 45 min). The work queue is the PR's
**unresolved** bot threads (GraphQL, paginated). ADDRESS_REVIEW hands them to the
implementer; its result lists each thread as fixed, answered or declined with a
reply. Coderbot pushes (PUSHING), then posts the replies and resolves those threads,
then waits for the next run. If nothing was pushed, it does not wait for a new run.
A bot finding that repeats one already declined is answered and resolved without an
agent turn. Rounds are capped by `CODEBOT_REVIEW_MAX_ROUNDS` (3); leftovers are named
in the "PR ready" message.

In WAIT_MERGE coderbot keeps polling for unresolved threads from anyone
(ADDRESS_PR_THREADS, capped by `CODEBOT_PR_THREAD_MAX_ROUNDS`). For human threads the
agent sees the whole thread and the reviewer's word is final; a thread that only asked
a question gets an answer and stays open. New commits go through VERIFYING before
PUSHING, so any head you can merge has passed the checks.

On `merge`, coderbot re-checks the PR: unresolved threads (or a failed query) block the
merge with a list; reply `merge anyway` to force. It merges with
`gh pr merge --squash --match-head-commit <reviewed head>`, so a head that moved since
you saw it is not merged. A PR merged on GitHub by someone else finishes the task the
same way; a PR closed without merging stops and asks.

### Base-branch conflicts

A `CONFLICTING` PR goes to RESOLVE_CONFLICTS. **Coderbot** fetches the base and starts
`git merge origin/$CODEBOT_BASE_BRANCH` itself (merge, never rebase). A clean merge
needs no agent turn. Otherwise the agent resolves the listed paths and commits;
coderbot pushes and the review runs again. Attempts are capped by
`CODEBOT_CONFLICT_MAX_ROUNDS` (3), then WAIT_STUCK.

## How coderbot drives the agent

- **One system prompt, short phase cards.** Working turns get a fixed system prompt of
  about 2.4k characters (role, working rules, result protocol) on every call; it is not
  repeated in the conversation. Each phase sends a short card naming the one
  Superpowers/OpenSpec skill it uses, numbered steps, "Done when" and the result fields.
  The bridge skill `coderbot-openspec-workflow` answers those skills' interactive steps
  (questions, approvals, file locations, "which approach?" → inline, worktrees,
  finishing) with coderbot's mechanisms.
- **Turn facts.** Every working turn starts with a short YAML block built from state:
  `phase`, `task`, `language`, `branch`, `base_sha`, `change`, `skill`,
  `classification`, `e2e_harness`, and in IMPLEMENTING `pending_tasks` and `turn`
  ("N of budget"). It is deterministic, so checkpoint replays still match.
- **Structured results** (`src/turns.py`). Every working turn ends with one JSON object:
  `status` (`done`, `done_with_concerns`, `needs_input`, `needs_approval`, `blocked`),
  `summary`, and optional `question` (≤4 options), `concerns`, `attachments`,
  `checks`, `e2e_specs`, `credential`, `classification`, `language`, `pr_title`,
  `verification`, `review` and `threads`. Claude Code enforces it with
  `--json-schema`; OpenCode is asked for a JSON block (its native json_schema mode is
  broken, see [runtime checks](docs/runtime-checks.md)). When the object is missing in
  VERIFYING, INTERNAL_REVIEW, ADDRESS_REVIEW or ADDRESS_PR_THREADS, coderbot asks once
  more in the same session with tools off; otherwise the text is used. Internally the
  object is rendered back to the controller's line markers for the existing parsers.
- **Three sessions per task:** planning, implementer (fresh at approval), reviewer
  (fresh, read-only). A session above `CODEBOT_CONTEXT_TOKEN_BUDGET` tokens is rotated
  with a handoff.
- **Replies without a model where possible.** Commands, option numbers, plain
  answers to a displayed decision (`yes`, `sí`, `no`, `ok`, `lgtm`, `dale`,
  `adelante`, `go ahead`) and `retry`, `abort`, `complete`, `merge` are resolved
  deterministically. Other replies cost one utility call: one classifier for
  every wait (`src/replies.py`), on `CODEBOT_UTILITY_MODEL` if set (e.g. `haiku`).
- Untrusted text (check output, review comments, email bodies) is fenced as data
  inside prompts. Add project facts (how to run the tests, what the container lacks)
  in `data/environment.md` or `CODEBOT_ENVIRONMENT_NOTES`.

### Guardrails

Lifecycle rules live in the runtime, not in prompt prose (`CODEBOT_TOOL_POLICY`,
default on; `src/tool_policy.py`):

- The agent cannot `git push`, create/merge/close/ready/edit PRs, `openspec archive`
  or `git worktree add`. Claude Code gets deny rules plus a PreToolUse guard hook
  (`agent-plugin/runtime/guard.py`) that also catches wrapped forms (`git -C . push`,
  `bash -c "…"`, `env …`, `cd … &&`). OpenCode gets bash deny patterns.
- Planning phases write only under `openspec/` and cannot commit, reset, checkout,
  merge, rebase or stash. EXPLORING and the reviewer are read-only (no Edit/Write).
- Working phases after approval cannot spawn subagents (`CODEBOT_EXECUTION=subagent`
  re-allows them for an A/B test).
- **Agents have no push credentials.** Their git and gh run with `GH_TOKEN` empty, an
  empty `credential.helper` and no SSH identity, so a push the guard misses still
  fails. Coderbot does every fetch, push and base merge itself.
- The guard fails open: a broken hook never blocks a turn.

### Budgets

Every counter is per task. Only `_grant_budget` resets one, on a human decision
(`retry` or instructions in WAIT_STUCK, an answer to a cap question, `VERIFY`); it logs
the reset.

| Counter | Default | At the cap |
|---|---|---|
| implementation turns | `tasks.md` sections + 3, at most 12 | one fresh session (3 turns), then ask |
| no-progress turns, gate rounds, check repairs | `CODEBOT_QUALITY_GATE_MAX_ROUNDS` = 3 | ask with a report |
| review fix waves / re-reviews | 1 / 1 | leftovers listed on the PR |
| bot review rounds, PR thread rounds | 3 each | finalize with a note / help message |
| conflict rounds | 3 | WAIT_STUCK |
| archive rounds | 3 | ask |
| consecutive questions | 8 (`CODEBOT_QUESTION_MAX_ROUNDS`) | WAIT_STUCK |
| failures per state | 5 (`CODEBOT_MAX_STATE_FAILURES`), agent faults only | WAIT_STUCK |
| same transition on an unchanged repo | 4 | WAIT_STUCK (loop guard) |

## Failure handling

Errors are typed by kind, not by message text (`src/errors.py`):

- **Transient** (network, outage, rate limit, disk full): retried with backoff
  (1 s doubling to 1 h, or the service's `retry_after`). It never burns the failure
  budget. After `CODEBOT_MAX_STATE_FAILURES` attempts you get one "service unavailable"
  message and the task waits in WAIT_STUCK.
- **Agent fault** (bad output, failed gate): burns the per-state budget; at the cap
  the task goes to WAIT_STUCK.
- **Fatal** (missing credential, broken configuration, wrong target): straight to
  WAIT_STUCK with the diagnosis.

In WAIT_STUCK reply with **retry**, **abort**, **complete**, or free-form
**instructions** (applied in the working session, then the step resumes). Failure
notices name the cause, impact and evidence, with a redacted diagnostic report.
Every git/gh call has a timeout (`CODEBOT_SUBPROCESS_TIMEOUT`, default 120 s).

A daily **retention sweep** removes `data/outbox/checks`, `data/diagnostics` and
`data/workspaces` entries older than 14 days (keeping the current and held tasks'
files and checkpoints) and prunes git worktrees. At startup a **runtime probe** checks
the installed claude/opencode/openspec versions and required flags against the pins and
notifies you once per mismatch; it never blocks startup.

## Decisions and reminders

Every human decision is presented in the conversation itself: Yes/No for one
authorization, or numbered alternatives with consequences. Reply with the number (and
any details) or in your own words; full replies such as `3. Dejemos el registro
asíncrono` are interpreted in full. A selection applies only to the current task and
wait, including after a restart. "No" keeps waiting; it does not abort.

An unchanged pending decision gets at most one short reminder after 24 hours without
contact (`CODEBOT_PING_SCHEDULE=24h`; `off` disables it). Working phases send no
check-ins.

`status?` (or `STATUS`) gets a brief answer in the task thread at any time, without
waiting for the agent: task, phase, elapsed time, and for OpenCode the running
subagents and the last event time (read-only from `opencode.db`). It never includes
reasoning, prompts or tool output. For an older running container,
`python3 /app/scripts/status_watch.py` can answer STATUS alone until the next restart.

Reply `VERIFY` in the task thread (or `VERIFY codebot-<id>` by email) to move an
existing, unarchived change to verification; optional guidance follows a colon. It
interrupts a running turn without discarding changes and implies no approval, push or
merge. `VERIFY --restore-plan <commit SHA>: guidance` restores the active change's
proposal, design, tasks and specs from an ancestor commit with a complete checklist,
after backing up the current ones under `data/verification-recovery/`.

## Mailbox commands: ABORT / STATUS / KICK / DONE / HOLD / CONTINUE

Send a body of exactly `ABORT`, `STATUS`, `KICK`, `DONE`, `HOLD` (or `PAUSE`), or
`CONTINUE` (or `RESUME`, optionally with a note) — case-insensitive — optionally with
an instance name (`ABORT codebot-x7k2`). Commands are read first in every tick. **Only
mail from `CODEBOT_USER_EMAIL` is honored**; other senders in a task thread are logged
and ignored.

- **ABORT** discards working-tree changes, returns to a clean, up-to-date base branch,
  clears task state and goes back to IDLE. Remote branches and PRs are left as they are.
- **STATUS** replies with a snapshot (state, task, branch, PR, pending question,
  counters, heartbeat age, holds, last message) without changing anything.
- **KICK** ends the running agent turn and retries its phase from the last checkpoint,
  keeping task, branch, approvals, session and files. It does not use the failure
  budget. Bare `KICK` is scoped to the task's thread; elsewhere use `KICK <instance>`.
- **HOLD** parks the task: pending work is committed on the task branch (evidence
  excluded), state is saved in `data/holds.json`, the item gets an `[on hold: …]`
  marker, and coderbot takes the next item. **CONTINUE** on the held task's thread
  makes it the next task picked, restored on its branch and session; a note becomes the
  reply to whatever it was waiting on.
- **DONE** marks the task complete in the backlog, resets the checkout and returns to
  IDLE. A bare `DONE` on the active task's thread is not this command.

## One-time setup

**Recommended**: run the guided setup on a macOS or Linux host. `scripts/setup.sh`
works from bash or zsh, bootstraps a pinned Textual UI into `.venv/`, and falls back to
a text menu (`--text`). Sections: Repository, Backlog, Conversation, Agent, Evidence,
Self-healing and Advanced. `Test` checks the current section; `Save and Close` tests
and saves it to `.env`. Advanced searches every runtime setting. Setup preserves
unknown keys, comments and untouched secrets in `.env`, and backs up the original.
**Sync to VM** (`v` in text mode) copies the code to an SSH host.

```bash
scripts/setup.sh
scripts/setup.sh --text
```

All editable keys and their defaults are in [`.env.example`](.env.example) and
`scripts/setup_catalog.py`; a test keeps the two in step with the code.

Slack: create the `xoxb-` token under [OAuth & Permissions](https://api.slack.com/apps)
with `channels:read`, `channels:history`, `chat:write` and `files:write`; reinstall
after changing scopes. Enable Socket Mode and create the `xapp-` token with
`connections:write`. Invite **that app's bot** to the public channel. Setup's `Test`
checks both tokens, membership and history access.

<details>
<summary>Manual setup (what <code>scripts/setup.sh</code> automates)</summary>

1. **Google OAuth client (email, GDoc or Drive upload)**: in Google Cloud Console
   enable the needed Gmail, Docs and/or Drive APIs, create a *Desktop app* OAuth client
   and download its JSON to `data/credentials.json`. Jira+Slack with evidence upload off
   needs no Google credentials.
2. **Consent flow** (on the host, from the repo root):
   ```bash
   pip install -r requirements.txt
   python3 scripts/setup_oauth.py     # opens a browser; writes data/token.json
   ```
3. **Configuration**: copy `.env.example` to `.env` and fill in at least
   `CODEBOT_REPO_PATH` (absolute host path; mounted at the same path in the
   container), the backlog settings, the channel settings, `GH_TOKEN` (a PAT with
   `repo`, plus `project` for GitHub Projects), and `CLAUDE_CODE_OAUTH_TOKEN`
   (`claude setup-token` on the host) or the OpenCode provider and model. macOS keeps
   Claude credentials in the Keychain, which the Linux container can't read — hence
   the explicit token.
4. **Coding-agent authentication**: Claude Code uses the host's `~/.claude` and
   `~/.claude.json`. For OpenCode, `scripts/setup.sh` runs the provider's login in the
   image and stores credentials under `data/opencode/`. Coderbot pushes over HTTPS with
   `GH_TOKEN`; the agent itself gets no credentials.

</details>

### One-time copy to an SSH VM

After configuring `.env`, stop any running bot on both machines and run:

```bash
bash scripts/sync-to-vm.sh azureuser@74.235.122.91
```

This copies this checkout to `~/codebot/app/` and the target checkout to
`~/codebot/target/` with `rsync` over SSH, including `.git`, durable `data/`, `.env`
and lockfiles, but not `node_modules/`, `.venv/`, `venv/`, `ms-playwright/` or
`data/opencode/cache/` (Mac binaries do not run on an amd64 VM). It also copies
`~/.npmrc` (mode `0600`). On the VM, `.env.local` keeps the original `.env`, and `.env`
gets VM paths; the script refuses to overwrite an existing `.env.local`. It never
deletes VM files. Do not run both copies against the same backlog: `data/` holds this
installation's identity. It uses passwordless `sudo` to give uid/gid `501` ownership of
`~/codebot/app/data` and `~/codebot/target`; without sudo run
`sudo chown -R 501:501 ~/codebot/app/data ~/codebot/target` on the VM.

For later code updates:
`bash scripts/sync-to-vm.sh azureuser@74.235.122.91 --code-only`, then restart the VM's
bot container.

### Start with no previous task

If the copied `data/state.json` refers to an earlier backlog or thread:

```bash
cd ~/codebot/app
docker compose stop codebot
sudo python3 -m scripts.reset_bot_state
docker compose up -d codebot
```

The script moves the active task, holds, Slack inbox and heartbeat into a timestamped
directory under `data/`, keeping the instance identity, credentials and email markers.

## Run

```bash
docker compose up -d --build
docker compose logs -f
```

State lives in `data/state.json` (one schema, atomic writes; a corrupt file is moved
aside and you are notified) and `data/execution.sqlite`; the container restarts safely
from any state. Saved tasks from older builds load: removed states are mapped forward
(RECOVERING and EVIDENCE_REVIEW → VERIFYING). Both compose files bind-mount `data/`
from the host, so it survives container recreation and image rebuilds; back it up
before replacing a host.

Only one coderbot may run against a `data/` dir (`data/state.lock`). The tick loop
touches `data/heartbeat` while the current tick is younger than
`CODEBOT_HEARTBEAT_MAX_TICK` (agent timeout + 15 min). `healthcheck.sh` reports
unhealthy after `CODEBOT_HEARTBEAT_STALE` (360 s) of silence and kills PID 1 after
`CODEBOT_HEARTBEAT_HARD` (600 s), so `restart: unless-stopped` restarts it.

### On a Linux server (EC2)

`docker-compose.ec2.yml` is the same stack with fixed host paths: this repo at
`/opt/coderbot`, the env file at `/opt/codebot/codebot.env`, the bot's `~/.claude` at
`/opt/codebot/claude`, a read-only `~/.claude.json` seed at `/opt/codebot/claude.json`,
and the Google OAuth files in `/opt/coderbot/data/`. Provision those files,
`chown -R 501` both trees, then:

```bash
cd /opt/coderbot && docker compose -f docker-compose.ec2.yml up -d --build
```

For a fleet on interruptible instances with a persistent disk per agent, see
[`deploy/aws/`](deploy/aws/README.md) (CloudFormation, Spot, EBS) and
[`deploy/azure/`](deploy/azure/README.md) (Bicep, Spot VMs, managed disks).

## Multiple instances

Any number of coderbots can share a backlog. Each installation generates a readable
name in `data/instance_id` (e.g. `codebot-warty-warthog`) and an ownership fingerprint
in `data/instance_fingerprint`; `CODEBOT_INSTANCE` may override the name. **Back up
both files.** Instances coordinate only through the backlog's claim markers/labels: in
the Doc the claim write carries the revision it read, and on a board both instances
back off when two labels land.

Email-mode instances can share a Gmail account; each reads only its own threads.
Address commands to one instance (`ABORT codebot-x7k2`) or reply on one of its threads.
A bare `ABORT` or `STATUS` elsewhere is honored by **every** instance; a bare `DONE`,
`HOLD` or `CONTINUE` outside an instance's threads is ignored. In Slack mode commands
work only as replies in that app's own task threads.

## Secrets for live checks (optional)

With `CODEBOT_CREDENTIAL_REQUESTS=on` the agent can ask for a credential it is missing
(the `credential` field of its result: `env`, `service`, `purpose`). Coderbot pauses
and offers: **1** send it through a secure one-time link, **2** continue without it,
**3** pause.

On **1**, coderbot starts a temporary [PrivateBin](https://privatebin.info) container
(`CODEBOT_PRIVATEBIN_IMAGE`, burn-after-reading) behind a
[localhost.run](https://localhost.run) SSH tunnel and sends you the address. Paste the
value, press Create, and reply with the link. Coderbot reads the paste once, decrypts
it locally (the key is in the link's `#fragment`) and resumes the agent. On **2**,
checks that need it are reported as **skipped**, never as passed.

The value is stored in `data/secrets/<task>/<NAME>.json` (`0700`/`0600`) for
`CODEBOT_SECRET_TTL_SECONDS` (4 h), deleted when the task ends, injected as `NAME` into
the agent and controller-run checks, and registered with the log redactor. It is never
written to `state.json`, prompts, PR bodies or Git. Names that would shadow the bot's
settings (`GH_TOKEN`, `PATH`, `CODEBOT_*`, `CLAUDE_*`, `SLACK_*`…) are refused. A check
that calls a live API lists its credentials in `env_keys` and sets `"optional": true`.
`CODEBOT_PRIVATEBIN_INSTANCES` accepts links from your own PrivateBin instances.

## Optional Caveman mode

In `scripts/setup.sh`, open **Agent** and select **Usar Caveman: YES/NO**. Only the
exact value `USE_CAVEMAN=true` enables it. The image includes the
`JuliusBrussee/caveman` plugin, pinned to v3.2.0 commit
`e20f07e8152a0c0360f58c09e79d30ac94329991`; the coding harness starts in Caveman mode,
utility sessions do not. Output contracts and permissions are unchanged. Recreate the
container after changing `.env`.

## Toolchain versions

The image pins exact versions: the base image (`python:3.12.13-slim`), the claude-code,
opencode and openspec CLIs (`@<version>` in the `Dockerfile`), Superpowers
(`config.SUPERPOWERS_VERSION`), Node 22 and the Python deps (`requirements.txt`). The
startup probe reports a mismatch. To bump: check the running version
(`docker compose exec codebot claude --version`), edit the pin, re-run the runtime
checks in [docs/runtime-checks.md](docs/runtime-checks.md), and rebuild. The image also
installs Docker CLI, Compose and Buildx.

## Smoke tests

Inside the container (`docker compose exec codebot bash`):

```bash
claude -p 'say ok' --model "$CLAUDE_MODEL" --dangerously-skip-permissions  # CODEBOT_AGENT=claude
opencode run --auto --model "$OPENCODE_MODEL" 'say ok' # when CODEBOT_AGENT=opencode
gh auth status
docker buildx version
git -C "$CODEBOT_REPO_PATH" fetch
cd src && python3 -c 'import task_source; task_source.validate(); print(task_source.list_pending_items())'
cd src && python3 -c 'import gmail_client; print(gmail_client.send("[codebot] test", "hello"))'  # email mode
cd src && python3 -c 'import slack_client; slack_client.validate(); print("Slack ok")' # Slack mode
```

Unit tests, on the host from the repo root, and on the image's Python 3.12:

```bash
python3 -m unittest discover -s tests -t .
scripts/test.sh                      # rebuilds the image; extra args go to unittest
```

### Manual integration smoke (using a disposable task)

1. Save a Jira+Slack configuration with `scripts/setup.sh`; repeat with `--text`,
   change one Advanced value and check the `.env` backup kept every untouched line.
2. Create a disposable Jira issue in the pick status with the pick label, and one
   without it. Start coderbot: only the labeled issue is claimed, with one root Slack
   message. Reply `STATUS`, then `HOLD`, then `CONTINUE`. Restart the container while a
   reply is pending; the reply is handled once.
3. Approve the proposal; check the draft PR appears right away and receives pushes.
   When it is marked ready, check `Closes <JIRA-KEY>` in its body, the Jira comment and
   review status. Reply `merge`; Jira moves to done. Repeat `DONE` and `ABORT` on
   disposable issues.

## Troubleshooting

**`managed runtime unavailable: missing ...`**: the image's managed Superpowers,
bridge or OpenSpec skill files are absent. Rebuild the image and check that no volume
replaces `/opt/coderbot/plugins`, `/opt/coderbot/agent-plugin` or
`/opt/coderbot/openspec-skills`.

**`Claude Code X does not support this model`**: the claude-code pin in the
`Dockerfile` predates `CLAUDE_MODEL`. Bump the pin (see "Toolchain versions") and
rebuild; also check the model id spelling.

**OpenCode returns `401 Unauthorized`**: rerun `scripts/setup.sh` and complete the
provider's authentication. A provider 401 is a failed smoke test.

**`claude exited 1: apiKeyHelper failed`**: the target repo's own
`.claude/settings.json` defines an `apiKeyHelper`. Claude Code tries it before
`CLAUDE_CODE_OAUTH_TOKEN`, with no fallback. Set the variable it reads with a real key
via `CLAUDE_API_KEY` (billed per token).

**The agent's `git push` fails**: expected. Agents have no push credentials; coderbot
pushes after the turn.

## Logs

All activity goes to stdout (`docker compose logs -f`). At the default `DEBUG` level you
see every state transition, each agent call (model, effort, prompt size, session),
every message sent, git/gh commands, check results, review-thread activity and the
evidence harvest. Failures are logged with their per-state count
(`cycle failed in VERIFYING (2/5)`). `CODEBOT_LOG_LEVEL=INFO` is quieter.

## Evidence

If the target has an e2e harness, the implementation adds coverage for it, and one
Playwright test per feature is the demo: its title contains `@evidence`.

- **Playwright**: the final-check E2E run exports `PW_VIDEO=on` (the harness can use it
  to enable `video: "on"`), and coderbot keeps the clips of the `@evidence` tests from
  that run, so nothing is re-run. Only if that harvest is incomplete does it record
  separately (`--grep @evidence`, then the full selected specs). Clips are stitched into
  one H.264 `data/evidence.mp4` with ffmpeg (each scaled/padded to 1280x720); without
  ffmpeg the raw `.webm` clips are attached.
- **Newman**: the run's newest report from `e2e/test-results/` is attached.

With `CODEBOT_EVIDENCE_UPLOAD=on` (default) the mp4 is uploaded to Google Drive (a
`Codebot evidence` folder, or `CODEBOT_DRIVE_FOLDER_ID`) with the folder's own
permissions, and linked from the PR, the "PR ready" message and the activity trail. A
failed upload attaches the file instead, up to `CODEBOT_MAX_ATTACH_BYTES`. See
[evidence delivery](docs/evidence-delivery-recovery.md).

Evidence never belongs in the target repo: committed videos and evidence-named images
or HTML are moved to the outbox and removed by a controller commit.

A `run.sh` that exceeds `CODEBOT_E2E_TIMEOUT` (900 s) is killed; coderbot then tears
down the harness's compose stack (`docker compose -f $CODEBOT_E2E_COMPOSE_FILE down -v`,
default `docker-compose.e2e.yaml`).

## Explicit verification scope

Targets can define `.codebot/checks.json` (version 1) to replace automatic check
discovery with an explicit list of suites. `inputs` selects source inputs;
`dependency_inputs` selects dependency manifests per check (e.g.
`backend/requirements*.txt`). Document excluded trees in the target's OpenSpec so
exploration and review keep the same scope; exclusion is a scope decision, not a pass.

Baseline comparisons reuse installed dependencies only when the relevant manifests
match. Otherwise the controller prepares the baseline's own lockfile/requirements in a
disposable worktree; installs are never mixed with the feature checkout.
