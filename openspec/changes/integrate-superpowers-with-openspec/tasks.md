## 1. Skill behavior baselines

- [x] 1.1 Create isolated, non-production pressure scenarios covering test-after shortcuts, duplicate Superpowers planning artifacts, nested worktrees, skipped verification, and agent-owned PR or merge decisions.
- [x] 1.2 Run the scenarios without the coderbot bridge for Claude Code and OpenCode where credentials are available, and record the exact baseline violations or rationalizations without modifying project code.
- [x] 1.3 Define the expected pass criteria for each scenario, including OpenSpec-only planning artifacts, work on coderbot's existing branch, test-first implementation, fresh verification evidence, and no autonomous integration decision.

## 2. Pinned agent runtime

- [x] 2.1 Add failing runner tests proving new and resumed Claude Code invocations include deterministic Superpowers and bridge plugin directories.
- [x] 2.2 Add failing runner tests proving new, resumed, and recovery OpenCode invocations receive local Superpowers plugin and bridge skill configuration without relying on target-repo or user configuration.
- [x] 2.3 Add failing startup-validation tests for missing Superpowers and bridge manifests and for a valid managed runtime.
- [x] 2.4 Pin `@fission-ai/openspec` to 1.9.0 and install Superpowers v6.3.0 at a deterministic image path in `Dockerfile`, including build-time plugin validation.
- [x] 2.5 Add shared runtime path/version constants and startup validation that fails before backlog selection when the selected agent cannot access required managed components.
- [x] 2.6 Update `claude_runner.py` so every run and resume loads both managed plugin directories, then pass the Claude runner tests.
- [x] 2.7 Update `agent_runner.py` so every OpenCode subprocess receives merged inline configuration for the selected model, disabled sharing/autoupdate, local Superpowers plugin, and bridge skill path, then pass OpenCode runner tests.
- [x] 2.8 Remove the now-duplicated OpenCode inline configuration construction from `entrypoint.sh` while preserving its private XDG directories and authentication setup.

## 3. Coderbot/OpenSpec bridge skill

- [x] 3.1 Create the coderbot agent-plugin manifest and the minimum bridge skill frontmatter with a unique name and a trigger limited to headless coderbot OpenSpec tasks.
- [x] 3.2 Implement the bridge's authority contract: OpenSpec owns artifacts/tasks, coderbot owns lifecycle and branch integration, and Superpowers supplies phase-specific engineering discipline.
- [x] 3.3 Add the planning adaptation that uses OpenSpec artifacts instead of `docs/superpowers` specs/plans and treats coderbot's proposal email approval as the implementation gate.
- [x] 3.4 Add implementation boundaries that forbid nested worktrees, independent branch-finishing choices, agent pushes, agent PR creation, merge decisions, and archival outside coderbot's assigned phase.
- [x] 3.5 Add phase guidance for brainstorming, TDD, systematic debugging, fresh verification, and internal review without duplicating the full upstream skills.
- [x] 3.6 Run the pressure scenarios with the bridge for both available agents, identify new loopholes, tighten only the failing guidance, and rerun until every scenario meets its pass criteria.
- [x] 3.7 Add deterministic bridge contract tests for required ownership statements, phase mappings, and forbidden duplicate workflow actions.

## 4. Phase prompt contracts

- [x] 4.1 Add failing prompt tests proving exploration explicitly invokes the bridge, OpenSpec exploration, and brainstorming while routing material decisions through `NEED_USER_INPUT`.
- [x] 4.2 Add failing prompt tests proving proposal generation creates only OpenSpec artifacts and does not start implementation.
- [x] 4.3 Add failing prompt tests proving implementation invokes OpenSpec apply plus TDD and systematic-debugging guidance while preserving the no-push and evidence contracts.
- [x] 4.4 Add failing prompt tests for exact standalone `QUALITY_GATE` and `INTERNAL_REVIEW` JSON contracts containing fresh command evidence and clean verdicts.
- [x] 4.5 Add failing prompt tests proving E2E repair uses systematic debugging, adds a regression test where applicable, and does not claim the task is ready for PR.
- [x] 4.6 Update `prompts.py` with the bridge-aware exploration, proposal, implementation, verification, internal-review, and E2E-repair prompts, remove agent-owned PR creation, then pass all prompt tests.

## 5. Persisted verification and review phases

- [x] 5.1 Add failing state-machine tests for `IMPLEMENTING -> VERIFYING -> INTERNAL_REVIEW`, followed by E2E when a harness exists or direct archival when it does not.
- [x] 5.2 Add failing tests proving missing, malformed, or failed verification results cannot advance and exhaust a bounded retry counter into the existing email user-input path.
- [x] 5.3 Add failing tests proving internal review uses a fresh reviewer subagent, unresolved critical or important findings cannot advance, and review fixes include fresh covering-test evidence.
- [x] 5.4 Add failing tests proving tracked E2E repair changes return to `VERIFYING` and `INTERNAL_REVIEW`, while unchanged repair attempts may rerun E2E directly.
- [x] 5.5 Add bounded verification/review retry configuration and persist the counters in `state.json` without breaking older state files.
- [x] 5.6 Implement verification and internal-review result parsers that accept only exact standalone completion contracts and retain useful failure context for retries.
- [x] 5.7 Implement `do_verify` and `do_internal_review`, register both phases, and update implementation/E2E transitions to match the specified gate order.
- [x] 5.8 Update `WAIT_REPLY`, abort/reset cleanup, and successful task cleanup for the new states and counters, then pass state-machine tests.
- [x] 5.9 Add a separate bounded archive retry setting and map exhausted archival recovery through `WAIT_REPLY` with `return_state` set to `ARCHIVING`.

## 6. Restart-safe OpenSpec archival

- [x] 6.1 Add failing tests proving pull-request creation is unreachable until archival and strict post-archive validation succeed.
- [x] 6.2 Add failing tests for normal archive completion, command failure, validation failure, an archive already moved before restart, and an archive committed before state persistence.
- [x] 6.3 Implement archive-state discovery using active OpenSpec status, the persisted expected target, and existing dated archive directories without selecting an ambiguous archive.
- [x] 6.4 Implement the `ARCHIVING` phase to run the bridge-aware non-interactive OpenSpec archive workflow, independently validate all resulting specs and archived tasks strictly, and reject unrelated working-tree changes.
- [x] 6.5 Commit only the OpenSpec main-spec and archive changes after validation and store the archive path for PR-body generation.
- [x] 6.6 Transition both successful E2E and the no-harness path to `ARCHIVING`, then transition successful archival to `OPEN_PR`.
- [x] 6.7 Replace the agent PR prompt with native coderbot branch push and `gh pr create`, generate a deterministic body linking the archived change, and pass PR creation tests.

## 7. Documentation and final verification

- [x] 7.1 Update `.env.example` and `README.md` with the combined ownership model, lifecycle diagram, pinned versions, bounded quality retries, archive placement, and plugin troubleshooting.
- [x] 7.2 Run the complete Python test suite and confirm zero failures.
- [x] 7.3 Run Python compilation checks for all changed modules and confirm zero errors.
- [x] 7.4 Run `openspec validate integrate-superpowers-with-openspec --strict --no-interactive` before archival and resolve every validation issue.
- [x] 7.5 Build the Docker image and verify the pinned OpenSpec CLI, Superpowers plugin, and bridge plugin are present and valid.
- [x] 7.6 Run one headless new-session and resumed-session discovery smoke test for Claude Code and OpenCode when credentials are available, documenting any unavailable live test without claiming it passed.
- [x] 7.7 Review the final branch diff against every requirement and scenario in both delta specs, fixing any uncovered gap before completion.
