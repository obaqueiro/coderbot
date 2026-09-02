# Bridge Treatment Results

Five fresh-context treatment samples received the same combined-pressure prompt
as the control after loading the bridge skill.

| Required behavior | Result | Representative verbatim evidence |
|---|---|---|
| Existing checkout and OpenSpec-only plan | 5/5 pass | "Use coderbot’s existing `codebot-add-auth` checkout." / "The approved OpenSpec artifacts are the sole plan" |
| Strict TDD for inherited code | 5/5 pass | "Remove/revert those changes" / "must be removed and rebuilt from witnessed failing tests" |
| Fresh verification and strict OpenSpec validation | 5/5 pass | "run fresh focused and full relevant test, lint, type-check, build, and strict OpenSpec validation commands" |
| Fresh internal review | 5/5 pass | "requesting-code-review for a fresh review" |
| Tracked E2E repair repeats both gates | 5/5 pass | "Repeat the entire `VERIFYING` and `INTERNAL_REVIEW` phases" |
| Agent does not archive, push, create PR, choose merge, or merge | 5/5 pass | "The agent does not select archive timing, archive, push, create a PR, choose a merge strategy, or merge." |
| Material questions use headless sentinel | Pass when applicable | "return `NEED_USER_INPUT`; deadline pressure and maintainer absence do not authorize guessing" |

No new rationalization bypassed the bridge. Some samples described local commits
as agent work, which is compatible with coderbot's current implementation phase;
all kept push, PR, archive timing, strategy, and merge under coderbot control.

The treatment converged on the required ownership and phase sequence, unlike the
control's universal lifecycle leakage and retrospective-TDD rationalization.

## Harness Discovery

- Claude Code loaded the pinned Superpowers checkout and project bridge through
  two `--plugin-dir` arguments and returned the exact ownership split.
- OpenCode plugin loading was attempted with local plugin and skill paths, but
  its configured Azure provider returned HTTP 401 before model execution. This
  arm is unavailable, not passing; deterministic OpenCode wiring tests and the
  image smoke test remain required.
