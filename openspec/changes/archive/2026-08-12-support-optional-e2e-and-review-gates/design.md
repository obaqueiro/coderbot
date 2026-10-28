## Context

`do_pick` (main.py) already runs before any e2e- or review-specific code, on
the freshly checked-out `main`. `do_e2e` and `do_open_pr` are the two places
that currently assume harness/workflow presence unconditionally. See
proposal.md - Why for the failure mode against a target without a harness,
and for why gating should degrade to
"fix it via the backlog" rather than "skip forever" and why not every target
repo has a frontend to drive with Playwright.

## Goals / Non-Goals

**Goals:**
- Detect target-repo capabilities once, cheaply, with plain filesystem/text
  checks — no `gh`/network calls needed for detection itself.
- Make the two gates degrade independently (a repo could have an e2e harness
  but no Code Review action, or vice versa).
- Keep behavior for repos that DO have both (e.g. codebot's own target repos
  used so far) byte-for-byte unchanged.
- When infrastructure is missing, turn that fact into a backlog item codebot
  can later act on itself, instead of a permanent silent skip.
- Support Newman/Postman as an e2e tool for target repos with no frontend,
  using the same `e2e/run.sh` entry point and gate mechanics as Playwright.

**Non-Goals:**
- Synchronously scaffolding an e2e harness or a Code Review workflow as a side
  effect of unrelated task work — codebot seeds a backlog item and lets the
  normal pick cycle (which already prefers prerequisites/enablers) handle the
  actual implementation as its own task, with its own proposal/review/PR.
- Changing how the review wait's timeout/round-limit fallback behaves when a
  workflow IS detected but never completes (already handled).
- Validating that a detected or requested harness actually works beyond
  presence checks (e.g. codebot does not run a smoke test before trusting
  `e2e/run.sh`).

## Decisions

- **Detection lives in `do_pick`, stored on `state`.** `state["has_e2e_harness"]`
  and `state["has_code_review"]` are set once per task and read by `do_e2e` and
  `do_open_pr`. Alternative considered: detect fresh in each phase — rejected
  because it re-reads the filesystem/repo for no benefit and spreads the same
  logic across two functions; `do_pick` already runs once per task and is a
  natural home.
- **E2E detection: `(config.REPO_PATH / "e2e" / "run.sh").is_file()`.** Matches
  exactly what `evidence.run_suite` invokes (`./run.sh` in `E2E_DIR`), so
  detection and execution can never disagree.
- **Code Review detection: static repo scan, not a live `gh` check.** Search
  `.github/workflows/*.yml`/`*.yaml` for a `name:` field equal to "Code
  Review" (same string `code_review_check` already matches on via the `gh pr
  checks` `workflow` field). Alternative considered: call `gh pr checks` speculatively
  before a PR exists — rejected, there is no PR yet at detection time (`do_pick`
  runs before `do_open_pr`), and `gh` calls are network round-trips we can avoid
  in the common "absent" case by reading a file instead.
- **Skipping, not soft-failing.** `do_e2e` with no harness transitions straight
  to `OPEN_PR` rather than calling `evidence.run_suite` and letting it fail —
  avoids depending on `subprocess.run` raising a distinguishable error for
  "script doesn't exist" vs. "script failed."  `do_open_pr` with no workflow
  calls `finalize_pr` directly instead of `_enter_review_wait`, skipping
  `WAIT_REVIEW` altogether rather than entering it and relying on the existing
  timeout fallback — the timeout path is designed for "workflow exists but is
  slow/stuck," not "workflow will never exist," and would otherwise waste up to
  `REVIEW_WAIT_TIMEOUT_SECONDS` per task.
- **Prompt templates take a boolean and branch inline.** `prompts.render`
  already does `Template.safe_substitute`; `PROPOSE`/`IMPLEMENT`/`FIX_E2E` gain
  an `e2e_note` (or similar) kwarg computed in `main.py` from
  `state["has_e2e_harness"]` and interpolated where the current mandatory-e2e
  sentences live, rather than maintaining two near-duplicate template strings
  per phase. The kind-aware wording (Playwright vs. Newman) is a second such
  kwarg, computed from `state["e2e_kind"]`.
- **Self-healing item text is a fixed string per infra key, not templated per
  task.** `"Set up a Playwright e2e harness (e2e/run.sh + Playwright specs
  under e2e/tests/) so future changes can be gated on it."` /
  `"Set up a Newman/Postman e2e harness (e2e/run.sh + collections under
  e2e/collections/) so future changes can be gated on it."` /
  `"Add a 'Code Review' GitHub Actions workflow so pull requests get automated
  review."` are constants, one per (infra key, kind) pair. Alternative
  considered: generate item text dynamically (e.g. include the task that
  surfaced the gap) — rejected, because dynamic text defeats exact-match
  dedup and would let the same gap get re-added under slightly different
  wording every cycle.
- **Dedup checks ALL paragraphs, not just pending ones.** A new helper mirrors
  `gdoc_client._iter_paragraphs`/`_normalize` to check whether the fixed item
  text already exists in the doc regardless of strikethrough state, before
  appending. This reuses the exact-match semantics `mark_done` already relies
  on. Alternative considered: only check pending items — rejected, because a
  completed (struck) item would then get re-added forever if detection ever
  produced a false "still missing" read (e.g. the implementing task didn't
  land in the same branch main was re-checked against yet).
- **Harness kind detection is presence-only, mirroring E2E detection.**
  Playwright kind ⇐ `e2e/playwright.config.ts` exists (the convention the
  README already documents). Newman kind ⇐ `e2e/` contains at least one
  `*.postman_collection.json` (any depth) and no `playwright.config.ts`. If
  `e2e/run.sh` exists but neither marker is found, kind is `"unknown"`: the
  pass/fail gate still works (it's exit-code based and tool-agnostic), but
  kind-aware prompt guidance, self-healing text, and evidence collection all
  no-op rather than guess.
- **Frontend-presence heuristic decides the kind to request when no harness
  exists.** `has_frontend` ⇐ a `frontend/` directory at the target repo root,
  OR any of `vite.config.*`, `next.config.*`, `angular.json`, `index.html` at
  the root. This is intentionally shallow — it only decides which single
  backlog item text to seed, and a wrong guess is corrected the same way any
  backlog item's scope gets refined: during that item's own `/opsx:explore`
  and `/opsx:propose` before implementation starts, not silently.
- **New Newman convention, defined here since none existed before.**
  Collections live at `e2e/collections/*.postman_collection.json` (+ optional
  `*.postman_environment.json`); `e2e/run.sh` is expected to invoke `newman
  run` per collection, mirroring how it already wraps `playwright test` for
  the Playwright kind — codebot does not care how `run.sh` is implemented
  internally, only that it exits nonzero on failure, consistent with the
  existing tool-agnostic gate design.
- **Newman evidence: newest HTML report file, not a stitched video.**
  Newman has no UI to record. `e2e/run.sh`'s Newman invocation is expected to
  write a report (e.g. via `newman-reporter-htmlextra`) to
  `e2e/test-results/*.html`; the Newman evidence path picks the newest such
  file after a forced re-run, mirroring `record_videos`'s
  before/after-diff pattern but without ffmpeg stitching. `detect_branch_specs`
  gains a matching per-kind fallback glob:
  `e2e/collections/*.postman_collection.json` for Newman vs. the existing
  `e2e/tests/*.spec.ts` for Playwright.

## Risks / Trade-offs

- [A target repo renames its workflow's `name:` away from "Code Review" but
  keeps the same file] → Detection would report "absent" and codebot would
  skip the review gate even though the workflow still runs. This mirrors the
  existing exact-match behavior in `code_review_check`/`main.py`'s "Code
  Review" comparison, so it's a pre-existing constraint, not a new one — no
  additional mitigation in this change.
- [A repo has `e2e/run.sh` but it isn't wired to the docker-compose stack the
  README describes] → Codebot will still attempt to run it and treat a nonzero
  exit as a normal test failure, which is correct; presence-detection is
  intentionally shallow and does not validate the harness works.
- [The frontend-presence heuristic misses a non-standard frontend layout] →
  Self-healing seeds a "set up Newman" item for a repo that actually has a UI.
  Low-cost: it's a backlog item like any other, and the mismatch surfaces (and
  gets corrected) during that item's own explore/propose phase, before any
  code is written for it.
- [A target repo's e2e setup uses both Playwright and Newman] → Kind detection
  picks Playwright first (checked first, and the more capable superset for
  guidance purposes); Newman collections would still run if `run.sh` invokes
  both, but prompt guidance and evidence collection would only mention/use the
  Playwright side. Acceptable for now — no target repo needs mixed kinds yet;
  revisit if one does.

## Open Questions
(none — behavior for both "present" and "absent" cases, and for both harness
kinds, is fully specified above)
