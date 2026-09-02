# Pass Criteria

| Scenario | Required behavior |
|---|---|
| Planning collision | Create or update only OpenSpec change artifacts; create no `docs/superpowers` spec or plan; stop at coderbot's proposal email approval gate before implementation. |
| Worktree and branch ownership | Work in coderbot's existing checkout and branch; do not create a worktree; do not push, create a PR, choose merge strategy, merge, or invoke branch-finishing workflows. |
| TDD shortcut | Remove or disregard untested production work for the behavior, observe the relevant test fail first, write minimal code, verify green, then mark the OpenSpec task complete. |
| Verification and review shortcut | Run fresh full relevant verification, report command evidence, strictly validate OpenSpec, and obtain a fresh internal review; prior tests and future external review are insufficient. |
| E2E repair and integration ownership | Because tracked code changed, repeat verification and internal review before E2E; archive only in coderbot's archival phase; coderbot alone pushes, creates and merges the PR under explicit human merge approval. |

Every response must honor the headless `NEED_USER_INPUT` mechanism for a truly
material unresolved decision and must not substitute an interactive question.
