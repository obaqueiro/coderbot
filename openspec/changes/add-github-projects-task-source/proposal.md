## Why

Codebot's backlog is hard-wired to a Google Doc: `src/main.py` imports
`gdoc_client` directly and calls it wherever it lists, claims, holds, releases or
completes a task. Teams that already track work in GitHub cannot point codebot at
that board; they must mirror every item into a Doc. We already have a GitHub
Projects v2 board (columns Backlog / Ready / In progress /
In review / Done) and want codebot to work it directly, while existing
deployments keep using the Doc unchanged.

## What Changes

- Introduce a **task source** abstraction: a `task_source` façade module that
  dispatches on `CODEBOT_TASK_SOURCE` (`gdoc`, the default, or `github`) exactly
  the way `agent_runner` dispatches on `CODEBOT_AGENT`. `main.py` calls only the
  façade.
- Items keep their dict shape (`text`, `detail`, `images`, `claimed_by_me`,
  `priority`) and gain a stable `id` (and `url`) so a backend can address an item
  by identity rather than by text; the identity is carried in `state["item_id"]`
  and in hold records, with text matching as the fallback.
- Move the backend-neutral text helpers (`normalize`, `priority_of`) into
  `task_text.py`; `gdoc_client` re-exports them.
- Add `github_projects_client.py`: a GitHub Projects v2 backend driven entirely
  by the `gh` CLI (GraphQL for the project, REST for labels, no new
  dependencies). Pickable items are project items whose content is an **issue in
  the target repo** in a pick column (default `Ready`). Ownership is a label on
  the issue (`codebot:<instance>` while implementing, `codebot-hold:<instance>`
  while on hold). Status moves Ready → In progress on claim, → In review when
  the PR opens, → Done on merge/DONE, back to Ready on abort.
- PR write-back (new, GitHub only): the PR body carries `Closes <issue url>`
  and the issue receives a comment with the PR link.
- Self-healing seeding on GitHub creates a real issue in the target repo and
  adds it to the board in the pick column.
- Startup validation checks the project, its Status options and the token's
  `project` scope, and fails with an actionable message.
- **Activity trail** (all sources): at every task milestone the bot posts a note,
  message body included, on the backlog item — an issue comment on GitHub, one
  comment thread per task on the Google Doc (Drive scope widened to full `drive`).
  One façade call, `note_activity`, is the contract a JIRA backend would implement.
- Setup script, `.env.example` and README document the new source.

## Capabilities

### New Capabilities
- `task-source`: selection of the backlog backend, the item contract every
  backend implements, and the GitHub Projects v2 backend's semantics.

### Modified Capabilities
(none)

## Impact

- `src/main.py`: all `gdoc_client.` call sites become `task_source.` calls;
  `do_pick` stores `item_id`/`item_url`; `do_open_pr` links the PR; `main()`
  validation is per source.
- `src/gdoc_client.py`: imports the shared text helpers; write functions accept
  and ignore an `item_id` keyword; gains `note_pr`/`validate`/`describe`.
- New: `src/task_text.py`, `src/task_source.py`, `src/github_projects_client.py`.
- `src/config.py`: `TASK_SOURCE` and `GH_PROJECT_*` settings.
- Tests: FSM tests stub `task_source` instead of `gdoc_client`; new unit tests
  for the façade and the GitHub backend.
- Docs: `.env.example`, `scripts/setup.sh`, `README.md`.
- Operators using GitHub need a `GH_TOKEN` with the `project` scope in addition
  to `repo`.
