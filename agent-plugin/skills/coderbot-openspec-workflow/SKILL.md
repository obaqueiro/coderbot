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
| VERIFYING | The agent runs fresh full relevant verification, reports command evidence, and strictly validates OpenSpec using `verification-before-completion`; coderbot decides transition. |
| INTERNAL_REVIEW | The agent obtains a fresh internal review using `requesting-code-review`. Prior test evidence and future external review are not substitutes; coderbot owns remediation state. |
| E2E repair | If repair changes tracked code, the agent uses strict TDD, then repeats VERIFYING and INTERNAL_REVIEW before coderbot retries E2E. |
| ARCHIVING / OPEN_PR | Coderbot alone chooses archive timing, archives, pushes, creates the PR, and requests merge approval. |

## Hard Boundaries

The agent must never push, create a PR, merge, choose archive timing, finish the branch, or choose a merge strategy.

Stop after reporting phase output and evidence; coderbot performs lifecycle side effects.

## Headless Input

For a truly material unresolved decision, return `NEED_USER_INPUT` with the decision, options, and impact. Do not ask an interactive question. Otherwise continue autonomously within the current phase.

## Strict TDD

Strict TDD applies to inherited production code. If production implementation exists without an observed failing test, including work from a previous agent, delete or revert it and restart test-first. Tests added afterward are not TDD. Use `test-driven-development`; do not replace its RED-GREEN-REFACTOR procedure here.

## Rationalization Counters

| Excuse | Reality |
|---|---|
| "Preserve inherited code and add coverage." | Untested inherited implementation must be removed and rebuilt from a witnessed RED. |
| "The work is verified, so I can open the PR." | Verification produces evidence; coderbot owns every lifecycle side effect. |
| "Archiving is the obvious next step." | Only coderbot selects archive timing and enters `ARCHIVING / OPEN_PR`. |

## Red Flags

- Retrospective tests for inherited implementation
- Any agent-run archive, push, PR, merge, or branch-finishing command
- Any merge-strategy recommendation

Stop and return control to coderbot when any red flag appears.

## Example

In `IMPLEMENTING`, discover inherited untested code: remove it, witness the focused test fail, implement minimally, report the phase result, and hand control back to coderbot.

## Common Mistakes

- Treating phase completion as permission to advance the workflow
- Keeping previous-agent code because deleting it feels wasteful
