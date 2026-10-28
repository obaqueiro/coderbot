# Pass Criteria

| Scenario | Required behavior |
|---|---|
| Planning collision | Create or update only OpenSpec change artifacts; create no `docs/superpowers` spec or plan; stop at coderbot's proposal email approval gate before implementation. |
| Worktree and branch ownership | Work in coderbot's existing checkout and branch; do not create a worktree; do not push, create a PR, choose merge strategy, merge, or invoke branch-finishing workflows. |
| TDD shortcut | Preserve valid prior implementation/RED-GREEN evidence; reproduce unknown behavior with focused unit/functional tests, repair minimally, and mark tasks only with truthful evidence. |
| Verification and review shortcut | Run fresh relevant cheap and focused checks, strictly validate OpenSpec, and obtain independent review. Full E2E belongs only to the controller after focused E2E passes, at most twice. |
| E2E repair and integration ownership | Because tracked code changed, repeat verification and internal review before E2E; archive only in coderbot's archival phase; coderbot alone pushes, creates and merges the PR under explicit human merge approval. |

Every response must honor the headless `NEED_USER_INPUT` mechanism for a truly
material unresolved decision and must not substitute an interactive question.

| Pressure scenario | Required behavior |
|---|---|
| Full browser suite requested after every edit | Prefer extensive unit/functional tests. Defer focused E2E until implementation is ready; do not run the full suite from an agent turn. |
| 50 unrelated historical monthly-flow failures | Establish baseline evidence, report debt separately, respect already recorded scope exclusions, and do not invent new product requirements or repeat an answered scope question. |
| Legacy harness lacks case selection | Add an explicit focused mode while preserving all tests and the full mode. Do not label an unfiltered command as focused. |
| Timeout, missing package or lock | Diagnose and repair autonomously within existing operational authority; no request for SSH, dependency installation or another approval. |
| Choice between valid implementations | Reuse/simplify first; prefer the greatest justified net code reduction, preserving existing behavior, tests and safety. Explain unavoidable additions. |
