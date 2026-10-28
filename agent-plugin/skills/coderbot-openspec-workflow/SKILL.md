---
name: coderbot-openspec-workflow
description: Use when a headless coderbot agent is working on an OpenSpec-managed change
---

# Coderbot OpenSpec Workflow

## Overview

Honor the orchestrator boundary while applying OpenSpec and Superpowers exactly where they own the work. OpenSpec owns requirements, design, and tasks; coderbot owns branch, state, email approvals, archive timing, push, PR, and merge; Superpowers owns engineering discipline.

Use the checkout coderbot supplied. Treat approved OpenSpec artifacts as the only plan.

## Quick Reference

| Phase | Contract |
|---|---|
| EXPLORING | The agent uses `openspec-explore`; OpenSpec holds discoveries and coderbot controls transition. |
| PROPOSING | The agent uses `openspec-propose`; OpenSpec holds proposal, design, specs, and tasks; coderbot obtains approval. |
| IMPLEMENTING | The agent uses `openspec-apply-change` and required Superpowers skills, including `test-driven-development`; coderbot owns state. |
| VERIFYING | The agent runs focused checks and validates OpenSpec; coderbot owns the complete final suites and baseline comparison after independent review. |
| INTERNAL_REVIEW | The agent obtains a fresh internal review using `requesting-code-review`. Prior test evidence and future external review are not substitutes; coderbot owns remediation state. |
| E2E repair | Repair only confirmed new regressions using focused checks, obtain independent re-review of the fix, and let coderbot invalidate affected checks before the final gate. |
| ARCHIVING / OPEN_PR | Coderbot alone chooses archive timing, archives, pushes, creates the PR, and requests merge approval. |

## Engineering priority

Top engineering priority: add as little code as possible. Preserve requested and
existing behavior, tests and safety; among valid solutions prefer reuse,
simplification and the largest justified net code reduction. Inspect existing
abstractions before adding helpers or subsystems. Do not introduce speculative
architecture, minify code, or remove functionality/assertions to improve line counts.
Use small readable patches and explain unavoidable additions.

## Hard Boundaries

The agent must never push, create a PR, merge, choose archive timing, finish the branch, or choose a merge strategy.

Stop after reporting phase output and evidence; coderbot performs lifecycle side effects.

## Headless Input

For a truly material unresolved decision, return `NEED_USER_INPUT` with the decision, options, and impact. Do not ask an interactive question. Otherwise continue autonomously within the current phase.

## Parallelism

Wall-clock time is the cost that matters in a headless run. Whenever work splits into independent parts, fan it out to multiple subagents launched together in one message and integrate the results; batch independent tool calls into one message. Typical fan-outs per phase:

| Phase | Fan out |
|---|---|
| EXPLORING | One subagent per affected area (data model, API, UI, tests, conventions). |
| IMPLEMENTING | One subagent per independent `tasks.md` task, each doing strict TDD; serialise only dependent tasks. |
| VERIFYING | Request independent focused checks with CHECK_PLAN; do not rerun full suites or baseline work already owned by coderbot. |
| INTERNAL_REVIEW / review threads | Unrelated findings or threads fixed by separate subagents. |

Serialise only steps that genuinely depend on an earlier result. Parallel subagents inherit every hard boundary above.

## Strict TDD

Use test-driven-development for new behavior. On recovery inspect durable RED/GREEN evidence, completed tasks and the actual diff first. Preserve valid work from previous attempts; do not delete or rebuild implementation solely because the current session did not witness its tests. For unknown or incomplete work, diagnose and run focused coverage, then repair minimally. Report missing TDD evidence honestly rather than inventing it.

## Test pyramid and controller authority

This section overrides generic OpenSpec/Superpowers advice to rerun all verification
at every task or handoff. Their discipline remains; their generic sequence does not
override the controller's cost and ownership policy.

1. Use extensive unit tests and functional/integration tests throughout development:
   successful paths, boundary cases, errors, contracts and regressions. Use TDD where
   appropriate; isolate expensive dependencies with representative fixtures.
2. Defer browser/E2E execution until the approved implementation and cheap tests are
   ready. Then select only files/cases testing the changed behavior. Make harness
   selection explicit if unsupported; retain all original tests and the full mode.
3. Emit focused CHECK_PLAN entries with `kind: "e2e"`, `scope: "focused"` and real
   file/case arguments and a structured reporter (`playwright`, `junit` or JSON with
   `ran`). Prove a positive executed-test count; zero exit alone is not E2E evidence.
   A whole-suite command relabeled focused is not focused evidence.
4. After focused E2E passes, coderbot runs the unchanged full suite once or twice for
   regression assurance. Agents/subagents never launch it themselves, duplicate it,
   reset its budget, or remove tests to obtain a green result.

### Apply the Superpowers skills pragmatically

- `test-driven-development`: RED/GREEN for the behavior being changed, not a full
  browser suite after each edit. Preserve durable RED/GREEN evidence across sessions.
- `systematic-debugging`: diagnose exact reports; use minimal reproductions and
  functional tests. Do not repeat a broad suite when the root cause is unchanged.
- `verification-before-completion`: fresh, relevant evidence for the approved feature
  plus the controller's separate regression audit. Never say all tests pass when
  inherited failures remain; never rebuild finished work just because a session restarted.
- `requesting-code-review`: review scope and relevant evidence; don't demand another
  full E2E run as ceremony when the controller has valid evidence already.
- Planning/apply/archive skills: approved artifacts are the plan. Do not introduce a
  second plan or invent approvals, housekeeping tasks, or new product scope as blockers.

### Existing failures are not new product requirements

Record new regressions separately from confirmed baseline failures and environment
defects. Being outside the edited subsystem or present on main is not baseline proof.
Fix approved behavior and operational prerequisites autonomously. Do not force unrelated
historical product functionality into this task to make the entire suite green. Respect
scope decisions already recorded; carry forward unrelated debt without repeatedly asking
the same question. An unresolved audit remains explicitly unresolved, not silently waived.
Only a genuinely new product decision, destructive action or unobtainable credential
requires user input. Ask one concrete question with numbered options and consequences;
timeouts, dependencies, locks, focused-test selection and controller settings do not.
Confirmed baseline-only failures do not block the regression gate; raw suite status
still reports failure. New/worsened failures, unknown attribution and missing required
feature evidence remain blocking. Never substitute a green regression gate for a green suite.

## Rationalization Counters

| Excuse | Reality |
|---|---|
| "Restart all work after KICK." | Preserve valid checkpoints and RED/GREEN evidence; repair only incomplete work. |
| "The work is verified, so I can open the PR." | Verification produces evidence; coderbot owns every lifecycle side effect. |
| "Archiving is the obvious next step." | Only coderbot selects archive timing and enters `ARCHIVING / OPEN_PR`. |

## Red Flags

- Inventing RED/GREEN evidence or discarding valid work merely because a session restarted
- Any agent-run archive, push, PR, merge, or branch-finishing command
- Any merge-strategy recommendation

Stop and return control to coderbot when any red flag appears.

## Example

In `IMPLEMENTING`, recover partial code: inspect checkpoints, run focused checks for incomplete behavior, repair minimally and hand control back to coderbot.

## Common Mistakes

- Treating phase completion as permission to advance the workflow
- Rebuilding previous-agent work without examining its checkpoint evidence
