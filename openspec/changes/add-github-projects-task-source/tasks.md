## 1. Shared text helpers

- [x] 1.1 Create `src/task_text.py` with `normalize`, `PRIORITY_RE`, `priority_of`
      moved out of `gdoc_client`; `gdoc_client` re-imports them. Existing tests pass.

## 2. Configuration

- [x] 2.1 Add `TASK_SOURCE`, `GH_PROJECT_URL/OWNER/NUMBER`,
      `GH_PROJECT_PICK_STATUSES`, `GH_PROJECT_ACTIVE_STATUS`,
      `GH_PROJECT_REVIEW_STATUS`, `GH_PROJECT_DONE_STATUS`, `GH_LABEL_PREFIX`,
      `GH_ISSUE_REPO` to `src/config.py` (URL parsed into owner/number).

## 3. Façade and FSM wiring

- [x] 3.1 Create `src/task_source.py` dispatching every backlog function on
      `config.TASK_SOURCE` with lazy imports; `normalize` from `task_text`.
- [x] 3.2 `gdoc_client`: accept `item_id=None` on write functions; add `note_pr`
      (no-op), `validate`, `describe`.
- [x] 3.3 `main.py`: import `task_source`; replace all `gdoc_client.` calls; store
      `item_id`/`item_url` in `do_pick`; add them to `RESET_KEYS` and hold records;
      call `note_pr` and add `Closes <url>` in `do_open_pr`; per-source validation
      and startup log in `main()`; neutral "backlog" wording.
- [x] 3.4 `prompts.PICK`: "backlog Google Doc" → "backlog".
- [x] 3.5 Update FSM tests to stub/patch `task_source`; add tests for `item_id`
      plumbing through hold/resume and PR linking.

## 4. GitHub Projects v2 backend

- [x] 4.1 `src/github_projects_client.py`: `_gh`/`_graphql` helpers with scope hint,
      project/field resolution and caching, target repo from `origin`.
- [x] 4.2 Item listing (paginated), parsing, `_pending` filtering, priority, image
      download.
- [x] 4.3 Mutations: status updates, label add/remove; `claim_task` with
      add-then-verify; `unclaim_task`, `hold_task`, `unhold_task`, `mark_done`.
- [x] 4.4 `ensure_item` (issue creation + board add + pick status), `note_pr`,
      `validate` (statuses, labels), `describe`.
- [x] 4.5 `tests/test_github_projects_client.py` with canned `gh` responses.
- [x] 4.6 Wire `github` into the façade; `tests/test_task_source.py`.

## 5. Docs and setup

- [x] 5.1 `.env.example`: task source block; `GH_TOKEN` scope note.
- [x] 5.2 `scripts/setup.sh`: source prompt, conditional Doc/GitHub prompts, scope
      note, new vars written and listed.
- [x] 5.3 `README.md`: backlog sources section, multiple instances, setup and
      smoke snippets.

## 7. Activity trail

- [x] 7.1 `task_source.note_activity` contract; `config.ACTIVITY_TRAIL`; Drive scope.
- [x] 7.2 GitHub backend: issue comment; `note_pr` no longer comments.
- [x] 7.3 Doc backend: Drive v3 comment thread per task with reply/404/403 handling.
- [x] 7.4 `main.trail()` + hooks in `email()` and the silent transitions; `trail_ref`
      in `RESET_KEYS`.
- [x] 7.5 Tests for both backends, the façade and the FSM helper; docs.
- [ ] 7.6 Live: notes on a test GitHub issue; Doc thread after re-consent.

## 6. Verification

- [x] 6.1 `python3 -m unittest discover -s tests -t .` green.
- [x] 6.2 `openspec validate --strict` on this change.
- [x] 6.3 Smoke against a configured project: validate, list, and a
      claim → hold → unhold → note_pr → mark_done round trip on a throwaway issue.
