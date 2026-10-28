---
name: coderbot-openspec-workflow
description: Use when a headless coderbot agent is working on an OpenSpec-managed change
---

# Coderbot OpenSpec Workflow

OpenSpec owns requirements, design, and tasks; Superpowers owns engineering discipline; coderbot owns branch, state, email approvals, archive timing, push, PR, and merge.

You run headless: coderbot is your human partner. Follow the skill each card names and answer that skill's interactive steps as below. Your turn ends with the result object described in your instructions.

## Bindings

| When a skill says | In coderbot |
|---|---|
| ask the user, wait for guidance, discuss with your human partner | Return `needs_input` with one question and 2-4 options, then stop. |
| brainstorming: ask clarifying questions | One question per turn, as above; decide minor points yourself. |
| brainstorming: the request's scale | Report it as `classification` (spike, bounded, architectural). |
| brainstorming: present the design, get approval, write and commit the design doc, user reviews the spec | Write the design into `openspec/changes/<change>/design.md` (the user's preferred location) without pausing or committing. Coderbot's single proposal approval covers all of these. |
| openspec-propose: confirm before creating the change | Already confirmed: create the change the card names. |
| writing-plans: save the plan | Save it as `openspec/changes/<change>/plan.md` (the user's preferred location). "Which approach?" is answered: Inline Execution. |
| executing-plans / openspec-apply-change | Work in this session, in order, one `tasks.md` section per turn. Stop at blockers with `needs_input` or `blocked`. Ignore archive suggestions. |
| test-driven-development | RED, then GREEN, then refactor, for each behaviour. Code already on the branch is earlier work: add its missing test; do not delete it. |
| systematic-debugging: three fixes failed, discuss with your partner | Return `blocked` with your hypotheses and what you tried. |
| verification-before-completion: run the full suite | Run the commands the card names plus focused checks, and list them in `checks`. Coderbot runs the final suites; never run an unfiltered end-to-end suite. |
| requesting-code-review | Use the reviewer only where the card says so; report findings in `review`. |
| receiving-code-review | Answer every finding or thread in `threads`: fixed, or declined with technical evidence. Implement human reviewers' requests; if one is unclear, ask. |
| using-git-worktrees, finishing-a-development-branch | Skip: the checkout coderbot gave you is the isolated workspace, and coderbot pushes, opens and merges the PR. |

## Hard Boundaries

The agent must never push, create a PR, merge, choose archive timing, finish the branch, or choose a merge strategy.

The runtime enforces these. Report the work in your result and stop; coderbot performs every lifecycle step.

## OpenSpec in other languages

Write the human-readable text in the task language, but keep the OpenSpec structure literal: `## ADDED Requirements`, `### Requirement:`, `#### Scenario:`, SHALL/WHEN/THEN, and `- [ ]` checkboxes.
