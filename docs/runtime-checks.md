# Runtime checks against the pinned CLIs

Recorded results of the checks the streamlining plan depends on. Re-run them after
bumping a pin in the `Dockerfile` (`docker run --rm --entrypoint bash <image> -c '…'`).

| Check | Pinned version | Result |
|---|---|---|
| C0 flags: `--json-schema`, `--append-system-prompt`, `--disallowedTools`, `--settings`, `--no-session-persistence`, `--tools`, `--strict-mcp-config`, `--setting-sources`, `--permission-mode` | claude-code 2.1.257 | present (2026-10-28) |
| C0 env: `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`, `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`, `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`, `CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION` | claude-code 2.1.257 | present in the binary (2026-10-28) |
| `--max-turns` | claude-code 2.1.257 | absent: turn caps are controller budgets |
| C1 `--json-schema` on a fresh run with tools and the three plugin dirs | claude-code 2.1.257, Haiku | `structured_output` returned (2026-10-28) |
| C2 `--resume` + `--tools ''` + a different schema | claude-code 2.1.257 | `structured_output` returned |
| C3 unsatisfiable schema | claude-code 2.1.257 | **exit 0, `subtype=success`, `structured_output: null`**: a missing `structured_output` must be treated as a failed turn, never as success |
| C4 `permissions.deny` for `git push` | claude-code 2.1.257 | **leaks**: `Bash(git push:*)` blocks plain, `bash -c` and `cd … &&` forms but not `git -C . push`; `Bash(git push *)` also misses `bash -c`. Deny rules are a first line only; the PreToolUse guard must normalise `git -C`, `bash -c`, `env`, `cd &&` |
| C5 PreToolUse hook | claude-code 2.1.257 | a `permissionDecision: deny` JSON blocks writes outside `openspec/` and allows inside; a crashing hook (exit 1/3) or a timed-out hook **fails open**; **exit code 2 blocks** (so the guard must report denials only through its JSON and exit 0 on every error; note `python3 missing.py` exits 2) |
| C6 `stream-json --verbose` + `--json-schema` + `--disallowedTools Task Agent` | claude-code 2.1.257 | final `result` event carries `structured_output`; assistant events carry `usage`; no subagent call attempted |
| C7 utility classification (Haiku, `--effort low`, isolation flags, `--no-session-persistence`) | claude-code 2.1.257 | 3.0 s, correct `approve` for "sí, apruébalo" |
| C8 `plan.md` inside a change | openspec 1.9.0 | `openspec validate --strict` passes |
| C9 reviewer template / draft PRs | superpowers v6.3.0, gh | `requesting-code-review/code-reviewer.md` uses `[DESCRIPTION]`, `[PLAN_OR_REQUIREMENTS]`, `[BASE_SHA]`, `[HEAD_SHA]`; `gh pr create --draft` available |
| C8 `openspec-update-change` skill present | image built from the current Dockerfile | present, with apply/archive/explore/propose/sync. An image built before the Dockerfile generated `/opt/coderbot/openspec-skills` (e.g. a 2 Sep `coderbot-codebot:latest`) lacks the directory, and coderbot then refuses to start: rebuild before deploying |
| C10 OpenCode `prompt_async` with `format: json_schema` | opencode 1.18.18, 1.18.29, 1.19.5; DeepSeek v4.1 Flash via OpenRouter | **broken**: the prompt is accepted but every later `GET /session/{id}/message` fails with `Expected OutputFormatJsonSchema` (the server cannot re-encode the stored format), which also breaks the bridge. OpenCode uses the text-JSON result plus one repair turn instead |
| C10 bash permission `{permission: bash, pattern: "git push*", action: deny}` after allow-all | opencode 1.18.18 | blocks `git push` and `git -C . push`; plain turns complete with DeepSeek v4.1 Flash |
