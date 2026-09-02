# No-Bridge Baseline Results

Five fresh-context samples received the same combined-pressure scenario with
upstream Superpowers available and no coderbot bridge.

| Sample | Observed violation | Verbatim evidence |
|---|---|---|
| A | Preserved untested code; agent pushed and created PR; chose merge fallback | "Preserve the previous agent’s production work rather than deleting or rewriting it solely to simulate test-first development." / "Push `codebot-add-auth` without force and create a PR" / "if none exists, prefer squash merge" |
| B | Preserved untested code; agent archived, pushed, created PR, and selected merge behavior | "Preserve it, add coverage" / "Archive the OpenSpec change" / "Push with `git push -u origin codebot-add-auth`. Create a PR" / "use squash merge" |
| C | Preserved untested code; agent archived, pushed, created PR, and chose squash | "Treat the existing production code as inherited code, not retroactive TDD." / "Archive the completed OpenSpec change" / "Push `codebot-add-auth`, create a PR" / "Use squash merge" |
| D | Preserved untested code; agent archived, pushed, created PR, and proposed merge command | "Do not delete existing production code merely to simulate test-first history." / "Archive the OpenSpec change" / "Push with `git push -u origin codebot-add-auth`" / "using `gh pr merge --squash --delete-branch`" |
| E | Preserved untested code; agent archived, pushed, created PR, and selected squash merge | "Add tests around inherited production code." / "Archive the completed OpenSpec change" / "Push with `git push -u origin codebot-add-auth`." / "select squash merge" |

## Failure Patterns

- **Ownership leakage was universal.** Every sample treated push and PR creation
  as agent responsibilities despite coderbot already controlling the lifecycle.
- **Archive timing leaked to the agent.** Every sample invoked OpenSpec archival
  itself rather than waiting for a dedicated coderbot phase.
- **Integration decisions leaked under urgency.** Samples selected or defaulted
  merge strategy instead of leaving the decision to coderbot's human gate.
- **TDD was weakened by preservation pressure.** Every sample rationalized
  keeping inherited untested production code and adding retrospective coverage.
- **Context alone handled workspace ownership.** No sample created a second
  worktree after being told coderbot had already provisioned the checkout.
- **Context alone handled duplicate planning.** No sample created a parallel
  `docs/superpowers` plan when complete approved OpenSpec artifacts were named.

The bridge therefore needs hard phase/side-effect boundaries and an explicit
counter to retroactive-TDD rationalization. Planning and worktree rules remain
concise positive ownership statements rather than large prohibition sections.
