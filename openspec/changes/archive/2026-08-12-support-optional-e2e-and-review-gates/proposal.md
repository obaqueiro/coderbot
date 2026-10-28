## Why

Codebot's lifecycle hard-requires two things from the target repo: an `e2e/run.sh`
Playwright harness (`do_e2e`/`evidence.py`) and a "Code Review" GitHub Action
(`code_review_check`/`do_open_pr`'s wait loop). A target repository may have neither
— no `e2e/` directory
and no code-review workflow in `.github/workflows/`. As written, `do_e2e` calls
`subprocess.run(["./run.sh"], cwd=E2E_DIR, ...)` unconditionally; against this repo
that raises `FileNotFoundError` (no such directory) and crashes the orchestrator
instead of failing a test run. The E2E-mandatory language baked into the `PROPOSE`
and `IMPLEMENT` prompts also assumes an existing harness Claude can add tests to,
which does not hold here.

Beyond this one repo, codebot is meant to be pointed at arbitrary target repos, so
silently and permanently skipping a gate whenever infrastructure is missing would
leave every such repo without e2e coverage or automated review forever. Codebot
should instead treat a missing dependency as something to fix, the same way it
fixes any other backlog item — and since not every target repo has a frontend to
drive with Playwright (an API-only backend is a first-class case), "fix it" needs
to mean the right tool for the repo, not just Playwright.

## What Changes

- Detect, once per task (in `do_pick`), whether the target repo has an `e2e/run.sh`
  harness and whether a "Code Review" GitHub Actions workflow file exists under
  `.github/workflows/`. Record both booleans on `state`.
- Make the E2E phase conditional: when no harness is present, `do_e2e` skips
  straight to `OPEN_PR` (logging that the gate was skipped) instead of invoking
  `run.sh`. When a harness is present, behavior is unchanged.
- Make the post-PR review wait conditional: when no Code Review workflow file is
  present, `do_open_pr` skips `WAIT_REVIEW`/`ADDRESS_REVIEW` entirely and finalizes
  the PR directly, instead of polling `gh pr checks` for a check that can never
  appear.
- Adjust the `PROPOSE`/`IMPLEMENT`/`FIX_E2E` prompt templates so the Playwright
  e2e-test mandate and the `E2E_SPEC:` reporting convention are only asserted when
  `state["has_e2e_harness"]` is true; when false, tell Claude to rely on its own
  judgment for verifying the change (existing test suites, manual verification) and
  drop the `E2E_SPEC:` convention.
- `evidence.record_videos` and `finalize_pr` already accept an empty spec list and
  degrade to "no video" — no change needed there beyond not calling `run_suite` when
  the harness is absent.
- **Self-healing**: when a required piece of infrastructure is detected missing (no
  e2e harness, no Code Review workflow), codebot appends a new pending item to the
  backlog Google Doc describing that gap, deduplicated against existing pending or
  done items so it is seeded once, not every cycle. `do_pick`'s existing
  "prerequisites first" guidance means a later cycle can pick that item and
  implement the missing infrastructure itself; once present, detection flips and the
  corresponding gate re-engages normally. This generalizes: missing infrastructure
  codebot depends on becomes a self-assigned backlog item, not a permanent silent
  skip.
- **Support two e2e harness kinds**: Playwright (web UI, existing behavior) and
  Newman/Postman (API-only projects with no frontend), both invoked identically via
  `e2e/run.sh`. Detect which kind a present harness is, and — for self-healing
  seeding when no harness exists yet — which kind is appropriate for the target repo
  (based on whether it has a frontend). Adapt prompt guidance, the self-healing item
  text, and evidence collection (video vs. Newman run report) to the detected kind.
- Document in the README that the e2e harness and Code Review action are each
  optional and auto-detected, not hard prerequisites, and that the e2e harness may
  be Playwright- or Newman-based depending on the target repo.

## Capabilities

### New Capabilities
- `quality-gates`: Codebot's per-task E2E-test and automated-code-review gates,
  including detection of whether a target repo provides each one, the conditional
  lifecycle behavior when it does not, self-healing backlog seeding for missing
  infrastructure, and support for both Playwright- and Newman-based e2e harnesses.

### Modified Capabilities
(none — no existing specs/ capabilities yet)

## Impact

- `main.py`: `do_pick` (detection of harness/workflow presence, harness kind,
  frontend presence, and self-healing backlog seeding), `do_e2e` (conditional
  skip), `do_open_pr` (conditional review wait entry).
- `prompts.py`: `PROPOSE`, `IMPLEMENT`, `FIX_E2E` templates gain a harness-present
  branch and a kind-aware (Playwright vs. Newman) branch.
- `evidence.py`: `run_suite`/`record_videos` are no longer called at all when the
  harness is absent; evidence collection branches by harness kind (stitched video
  for Playwright, generated HTML/JSON run report for Newman); the branch-spec
  fallback glob becomes kind-dependent.
- `gdoc_client.py`: new capability to check whether an equivalent backlog item
  already exists (pending or done) and to append a new pending item.
- `README.md`: "Target repo prerequisites" section updated to describe
  auto-detection, self-healing, and both supported e2e harness kinds instead of a
  single hard Playwright requirement.
- No changes needed to `claude_runner.py`, `config.py`, `gmail_client.py`,
  `google_auth.py`, or Docker/compose files.
