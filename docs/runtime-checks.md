# Runtime checks against the pinned CLIs

Recorded results of the checks the streamlining plan depends on. Re-run them after
bumping a pin in the `Dockerfile` (`docker run --rm --entrypoint bash <image> -c '…'`).

| Check | Pinned version | Result |
|---|---|---|
| C0 flags: `--json-schema`, `--append-system-prompt`, `--disallowedTools`, `--settings`, `--no-session-persistence`, `--tools`, `--strict-mcp-config`, `--setting-sources`, `--permission-mode` | claude-code 2.1.257 | present (2026-10-28) |
| C0 env: `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`, `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`, `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`, `CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION` | claude-code 2.1.257 | present in the binary (2026-10-28) |
| `--max-turns` | claude-code 2.1.257 | absent: turn caps are controller budgets |
| C1–C10 live behaviour (structured output on run/resume/stream-json, deny rules under `--dangerously-skip-permissions`, guard hook, Haiku utility latency, OpenSpec `update-change` + `plan.md`, reviewer template, OpenCode `format: json_schema`) | claude-code 2.1.257, opencode 1.18.18, openspec 1.9.0 | pending: needs model credentials |
