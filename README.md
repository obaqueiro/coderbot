# coderbot

Autonomous coding agent that works the improvements backlog (a Google Doc,
`CODEBOT_DOC_ID`) of whatever target repo you point it at (`CODEBOT_REPO_PATH`),
plans with that repo's OpenSpec workflow via a selectable headless coding agent
(Claude Code or OpenCode), and talks
to the maintainer exclusively by email (`CODEBOT_USER_EMAIL`).

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
  required for codebot's detection and comment-polling to see it.
  - **Missing**: the post-PR review wait is skipped and the PR is emailed to
    the user immediately, and codebot adds a backlog item requesting the
    workflow.

OpenSpec owns requirements, design, and tasks; Superpowers owns engineering
discipline; coderbot owns state, archive timing, push, PR, and merge. The image
supplies managed, pinned OpenSpec 1.9.0 and Superpowers v6.3.0 integrations for
both Claude Code and OpenCode. Target repos and host profiles do not need their
own Superpowers installation.

## Lifecycle

```
IDLE → pick item (non-struck ¶ in the Doc, coding agent chooses) → branch codebot-<slug>
      → EXPLORING → PROPOSING → email proposal → WAIT_APPROVAL
     → IMPLEMENTING → VERIFYING → INTERNAL_REVIEW → E2E (when present)
     → ARCHIVING → OPEN_PR → WAIT_REVIEW ⇄ ADDRESS_REVIEW → PUSHING (if review fixes exist)
     → email PR + evidence → WAIT_MERGE
     → merge → strike item through in the Doc → IDLE
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

## Abort / reset (last resort)

Email codebot with a body of exactly `ABORT` (case-insensitive) to force a reset.
Checked at the start of every tick and mailbox-wide (any thread, or a brand-new
email), so it works even when the agent is stuck waiting on a thread. On receipt it
discards the working-tree changes, returns to a clean, up-to-date base branch
(`git reset --hard` + `checkout -f $CODEBOT_BASE_BRANCH` + `clean -fd` + fast-forward
to `origin/$CODEBOT_BASE_BRANCH`), clears all task state, and goes back to IDLE —
then emails a confirmation. It only touches the **local** checkout: remote branches
and PRs are left as-is (clean those up on GitHub yourself if needed). The check runs
between phases, so an abort sent mid-phase takes effect once the current Claude call
returns.

## Automated code review (WAIT_REVIEW ⇄ ADDRESS_REVIEW)

Opening the PR triggers the `Code Review` GitHub Action (OpenCodeReview, see
`.github/workflows/code-review.yml`), which leaves inline comments. Codebot does
**not** email the user yet: it enters `WAIT_REVIEW` and polls that action's check on
the PR head. When a run finishes, it fetches the `github-actions[bot]` inline comments
posted since the last round (plus the sticky summary for context) and, if any are new,
hands them to the same coding-agent session (`ADDRESS_REVIEW`) to fix genuine issues
and commit. Coderbot pushes the commit, which re-triggers the action. The loop repeats
until a run leaves no new
comments (then it records evidence and emails the PR), or until `CODEBOT_REVIEW_MAX_ROUNDS`
(default 3) or `CODEBOT_REVIEW_WAIT_TIMEOUT` (default 45 min per run) is hit, in which
case it emails anyway with a note about the unresolved review.

## One-time setup

**Recommended**: from the coderbot repo root, run the guided setup script. It
walks through every value below, explains what it is and where to get it,
pre-fills defaults where discoverable (git config, an authenticated `gh` CLI,
an existing `.env`), validates what it can, and writes `.env` for you. Re-run
it whenever you need to reconfigure codebot, such as switching from Claude Code
to OpenCode: it shows the current non-secret settings and lets you keep or
change each one:

```bash
./setup.sh
```

It also detects `data/credentials.json` (see step 1 below) and, if present,
offers to run the consent flow in step 2 for you.

<details>
<summary>Manual setup (what <code>setup.sh</code> automates)</summary>

1. **Google OAuth client**: in Google Cloud Console create a project, enable the
   Gmail, Google Docs and Google Drive APIs, create an OAuth client of type
   *Desktop app*, and download its JSON to `data/credentials.json`.
2. **Consent flow** (on the host, from the coderbot repo root):
   ```bash
   pip install -r requirements.txt
   python3 setup_oauth.py     # opens a browser; writes data/token.json
   ```
3. **Configuration**: create `.env` at the repo root (see `.env.example`):
   ```
   # Required: absolute host path of the target git checkout. It is mounted into
   # the container at the SAME path (Claude's per-project state and the e2e
   # docker stack both depend on host==container paths).
   CODEBOT_REPO_PATH=/absolute/path/to/target-repo
   # Required: Google Doc id of the improvements backlog
   CODEBOT_DOC_ID=<the id from the doc's URL>
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
   # Optional; defaults to claude-opus-4-8
   CLAUDE_MODEL=claude-opus-4-8
   # Required when CODEBOT_AGENT=opencode. Run ./setup.sh to complete the
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
   ```
   (macOS keeps Claude credentials in the Keychain, which the Linux container
   can't read — hence the explicit token.)

</details>

4. **Coding-agent authentication**: Claude Code uses the host's `~/.claude` and
   `~/.claude.json`. OpenCode is installed in the Codebot Docker image; `setup.sh`
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
To abort the current task: stop the container, delete `data/state.json`, clean the
git branch, restart.

## Smoke tests

Inside the container (`docker compose exec codebot bash`):

```bash
claude -p 'say ok' --dangerously-skip-permissions   # when CODEBOT_AGENT=claude
opencode run --auto --model "$OPENCODE_MODEL" 'say ok' # when CODEBOT_AGENT=opencode
gh auth status                                       # GH token works
git -C "$CODEBOT_REPO_PATH" fetch                    # HTTPS auth via GH_TOKEN works
python3 -c 'import gdoc_client; print(gdoc_client.list_pending_items())'
python3 -c 'import gmail_client; print(gmail_client.send("[codebot] test", "hello"))'
```

## Troubleshooting

**`managed runtime unavailable: missing ...`**: coderbot fails closed at startup if
the image's managed Superpowers or coderbot/OpenSpec bridge files are absent. Rebuild
the image from this repository and check that no volume mount replaces
`/opt/coderbot/plugins` or `/opt/coderbot/agent-plugin`; do not install Superpowers in
the target repo or host profile as a workaround.

**OpenCode provider authentication fails or returns `401 Unauthorized`**: rerun
`./setup.sh` and complete authentication for `OPENCODE_PROVIDER`, then retry the
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
default `DEBUG` level you see every state transition, each Claude invocation
(model, prompt size, returned session/output size, whether it hit the
`NEED_USER_INPUT` sentinel), every email sent (subject, thread, attachment count
and byte totals, any size-skipped files) and reply received, git commands, the
e2e suite result, and the evidence-video harvest (which `.webm` files were found
and their sizes — with explicit warnings if none were produced or a feature
shipped without e2e specs). Set `CODEBOT_LOG_LEVEL=INFO` for a quieter feed.

## Evidence

If the target repo has an e2e harness, the implementation phase adds coverage for
it and, once the suite passes, codebot re-runs the feature's own tests to capture
evidence for the PR email:

- **Playwright**: re-runs the feature's specs with `PICA_E2E_VIDEO=on` (forces
  `video: "on"` in `e2e/playwright.config.ts`), then stitches the resulting
  `.webm` clips from `e2e/test-results/` into a single H.264 `evidence.mp4` with
  ffmpeg (each clip scaled/padded to 1280x720 so mixed viewport sizes concatenate
  cleanly). If ffmpeg is unavailable or stitching fails, it falls back to
  attaching the raw `.webm` clips.
- **Newman**: re-runs the feature's collection(s) and attaches the newest
  generated report file from `e2e/test-results/`.

If no e2e harness is present for the task, no evidence is produced and the PR
email is sent without an attachment.
