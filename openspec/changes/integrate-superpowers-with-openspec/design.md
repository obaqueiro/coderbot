## Context

Coderbot already controls a durable lifecycle around a single resumed coding
session: it creates a feature branch, runs OpenSpec explore/propose/apply,
requires email approval, optionally runs E2E and external review gates, and
merges only after another explicit email decision. OpenSpec artifacts are
available in arbitrary target repositories, while coderbot itself and its
agent runtime live in the container. See proposal.md for motivation.

Stock Superpowers workflows assume an interactive partner and may create
`docs/superpowers` plans, worktrees, commits, or branch-finishing choices. Those
assumptions overlap with OpenSpec and coderbot ownership and therefore need an
explicit adapter rather than unqualified plugin installation.

## Goals / Non-Goals

**Goals:**
- Give Claude Code and OpenCode the same pinned Superpowers behavior without
  modifying target repositories.
- Preserve OpenSpec as the only requirements, design, and task authority.
- Make verification, internal review, and archival observable persisted phases
  rather than optional prose inside one implementation prompt.
- Preserve the existing email approval, E2E evidence, external review, PR, and
  merge behavior around the new gates.
- Make every new phase safe to retry after process or session loss.

**Non-Goals:**
- Replacing OpenSpec tasks with `docs/superpowers/plans` or a Superpowers SDD
  ledger.
- Letting Superpowers create a nested worktree, open a PR, merge, or choose how
  to finish coderbot's branch.
- Rewriting coderbot to dispatch one coding session per OpenSpec task.
- Requiring target repositories or host user profiles to install Superpowers.
- Replacing the existing optional E2E and GitHub review systems.

## Decisions

- **Use a hybrid control plane.** OpenSpec defines what and tracks completion;
  Superpowers supplies brainstorming, TDD, debugging, verification, and review
  discipline; coderbot owns lifecycle transitions and side effects. A
  prompt-only overlay was rejected because an autonomous agent can silently
  omit it. Per-task orchestration was rejected because it duplicates
  Superpowers coordination and substantially expands recovery state.

- **Ship one runtime-neutral bridge skill in a coderbot-managed plugin.** The
  plugin contains Claude metadata and a normal `skills/` directory. Claude
  receives it through `--plugin-dir`; OpenCode loads the package as a local
  plugin whose config hook appends the same skills directory. The hook avoids
  replacing target-repository `skills.paths`, which inline OpenCode arrays
  would otherwise do. A unique bridge name avoids collisions with target-repo
  skills. The phase prompts invoke it explicitly rather than depending only on
  automatic skill discovery.

- **Pin both sides of the integration.** The image installs OpenSpec 1.9.0 and
  Superpowers v6.3.0 at deterministic paths. Claude loads the Superpowers and
  bridge plugin directories on every run and resume. OpenCode's subprocess
  environment supplies the local Superpowers package and bridge path alongside
  the selected model and sharing restrictions. Runtime validation fails before
  backlog selection if required manifests are missing. Tracking `latest` was
  rejected because either framework can change behavior without a coderbot
  code change.

- **Adapt planning instead of duplicating it.** Exploration invokes OpenSpec
  explore and Superpowers brainstorming. OpenSpec proposal, specs, design, and
  tasks collectively replace Superpowers design and plan documents. The
  proposal email is the design-review artifact, and `WAIT_APPROVAL` remains the
  hard implementation gate. The bridge explicitly suppresses
  `writing-plans`, worktree creation, and branch-finishing workflows in this
  managed lifecycle.

- **Keep implementation task ownership in OpenSpec apply.** The apply workflow
  reads CLI-provided context files and owns task checkboxes. Each behavior
  change follows Superpowers TDD; failures trigger systematic debugging. The
  agent commits its implementation as today but does not push or open a PR.

- **Add `VERIFYING` and `INTERNAL_REVIEW` phases.** Verification requires a
  strict standalone line in the form
  `QUALITY_GATE: {"status":"pass","commands":[...],"openspec":"pass","tasks":"N/N"}`.
  Internal review dispatches a fresh reviewer subagent and requires
  `INTERNAL_REVIEW: {"status":"pass","critical":0,"important":0,"tests":[...]}`.
  The parsers require the named fields and reject additional status values,
  unresolved blocking counts, empty evidence arrays, and malformed JSON.
  Missing or malformed contracts do not advance. Each phase has a persisted
  bounded retry counter and uses the existing user-input email path when
  autonomous recovery is exhausted.

- **Route tracked E2E repairs back through both gates.** A failed E2E run records
  the branch commit and tracked working-tree state before resuming the coding
  session under systematic-debugging guidance. If the repair changes either,
  state returns to `VERIFYING`, then `INTERNAL_REVIEW`, then E2E. If no tracked
  file changed, coderbot may rerun E2E directly because the reviewed tree is
  unchanged.

- **Archive after quality gates but before PR creation.** Archival updates
  tracked OpenSpec files, so running it only after merge would require a direct
  base-branch push or a second PR. The archival phase runs on the feature branch
  after E2E, validates the resulting main specs and archive, and commits those
  changes. They therefore land atomically when the implementation PR merges.
  Recovery records or derives the expected archive path and treats an existing
  valid archive as completed work.

- **Make push and PR creation native coderbot operations.** After archival,
  coderbot pushes the existing feature branch and runs `gh pr create` itself,
  using deterministic title/body content that links the archived change. The
  agent no longer receives authority to push or create a PR in any phase. This
  resolves the ownership conflict in the previous `PR_BODY` prompt and keeps
  all external branch side effects in the lifecycle controller.

- **Bound archival recovery separately.** `CODEBOT_ARCHIVE_MAX_ROUNDS` defaults
  to three and is independent of verification and review retry limits. An
  exhausted archival loop enters the existing email user-input path with
  `return_state` set to `ARCHIVING`; a reply earns another bounded set of
  attempts after the agent or user has supplied recovery guidance.

- **Retain existing external review as an independent defense.** Internal
  Superpowers review catches issues before PR creation. The optional GitHub
  Code Review workflow and human review continue unchanged after the PR opens;
  neither substitutes for the other.

## Risks / Trade-offs

- [Superpowers or OpenSpec releases become incompatible with a supported agent]
  → Pin tested versions, validate plugin structure at build/startup, and update
  versions only with runner and smoke tests.
- [A target-repo instruction conflicts with the bridge] → Give phase prompts a
  unique explicit bridge contract and treat OpenSpec CLI state plus coderbot's
  lifecycle ownership as controlling inputs; report rather than silently
  reinterpret conflicts.
- [An agent fabricates a pass marker] → Require fresh command details, verify
  strict OpenSpec state independently where possible, retain the executable E2E
  gate, and require an independent review result. The marker controls parsing;
  it is not the only evidence.
- [New phases lengthen task completion and consume more model calls] → Keep one
  call per gate in the success path, bound retries, and avoid the much heavier
  per-task subagent controller.
- [Archive partially succeeds before a crash] → Persist archive context, detect
  active-versus-archived state on retry, validate before commit, and never run a
  second archive over an existing target.
- [Behavioral skill tests require live model credentials] → Keep deterministic
  unit tests for wiring/contracts and run isolated pressure/smoke tests when the
  corresponding agent credentials are available; never claim the live arm
  passed when it was not run.

## Migration Plan

1. Add tests and the bridge plugin while the old lifecycle remains active.
2. Pin and wire both runtimes, then verify plugin discovery in the image.
3. Add phase prompts and state transitions behind the required managed runtime.
4. Exercise restart and failure paths with mocked agents/OpenSpec commands.
5. Build the image and smoke-test one new session and one resumed session per
   configured agent before deploying.
6. Roll back by reverting the image/plugin/state-machine change; existing
   `state.json` loading must tolerate removal of new optional counters, while a
   task already in a new phase should be completed or reset before downgrade.
