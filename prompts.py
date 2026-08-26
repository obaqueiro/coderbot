"""Phase prompt templates for the codebot lifecycle.

Templates use string.Template ($name) placeholders, rendered via render() below.
This is deliberate: call sites interpolate UNTRUSTED text (email reply bodies,
raw e2e output) that routinely contains literal '{' and '}' (JSON, stack traces,
code). str.format() would raise KeyError/ValueError on those; Template does not,
and safe_substitute never rescans the substituted values.
"""
from string import Template


def render(template: str, **kwargs) -> str:
    return Template(template).safe_substitute(**kwargs)


PICK = """Here is the list of pending improvements for the $project project (from the
backlog Google Doc). Some items may depend on others; pick the single item that
makes most sense to implement NEXT (prerequisites first, easier enablers first).

Pending items:
$items

Respond with ONLY a JSON object, no other text:
{"item": "<exact text of the chosen item>", "slug": "<short-kebab-case-slug>", "reason": "<one sentence>"}
"""

EXPLORE = """You are working autonomously on the $project repo, on branch $branch,
implementing this improvement from the backlog:

    $item

Use the OpenSpec explore workflow: investigate the codebase, clarify the requirements,
identify integration points, risks, and the simplest solid design. Use the installed
`openspec` CLI and its local instructions when creating or reading change artifacts. Work everything
out yourself; only use the NEED_USER_INPUT mechanism for decisions that genuinely
require the user. End with a concise summary of your conclusions.
"""

PROPOSE = """Now formalize the plan: use the OpenSpec proposal workflow (openspec CLI) to
create a change named $slug with proposal.md, design.md, specs, and tasks.md,
based on your exploration. Requirements:
$e2e_note
When done, output the full text of proposal.md and a summary of the tasks so it can
be emailed to the user for review.
"""

CLASSIFY_APPROVAL_REPLY = """The user replied to the proposal-review email with:

    $reply

Classify their intent. Respond with ONLY a JSON object, no other text:
{"action": "approve" | "changes" | "abort", "feedback": "<the requested changes, empty otherwise>"}

Only choose "approve" when the reply is a clear, explicit go-ahead to implement the
proposal as-is. Only choose "abort" when the reply clearly asks to stop, cancel, or
abandon this task entirely, rather than change the proposal. If the reply is ambiguous,
asks a question, or requests any change short of abandoning the task, choose "changes"
and put the substance in "feedback".
"""

REVISE_PROPOSAL = """The user reviewed the proposal and did NOT approve it yet. They
replied with:

    $feedback

Revise the change artifacts (openspec change $slug) accordingly and output the updated
proposal for another review round. Do not treat this as approval — the user must
explicitly approve before implementation begins.
"""

IMPLEMENT = """The user approved the proposal. Implement the openspec change $slug
fully using the OpenSpec apply workflow: work through every task in tasks.md, marking them
complete. Mandatory:
$e2e_note
- Commit your work on branch $branch with clear messages. Do NOT push yet. Do not
  commit any evidence file (screenshot, recording, report) — those are emailed, never
  committed to the repo (see the evidence contract above).
$e2e_report_note
"""

FIX_E2E = """The e2e suite failed. Fix the issues and re-commit. Failure output:

$output
"""

PR_BODY = """Create a pull request for the current branch $branch against $base_branch using
`gh pr create` (push the branch first). Title it after the improvement; write a clear
body describing the change, the e2e coverage added, and link the openspec change.
End your response with the PR URL on its own line prefixed with `PR_URL: `.
"""

ADDRESS_REVIEW = """An automated code reviewer (OpenCodeReview) reviewed your pull
request and left the comments below. Evaluate each one on its merits — the reviewer
is helpful but pattern-based and not always right.

$comments

For each comment: if it points to a genuine problem, fix it properly. If it is a
false positive or not worth acting on, do NOT change code just to silence it — briefly
note why you're leaving it. Keep the Playwright e2e tests passing and updated. Commit
your changes on branch $branch with clear messages and push (the reviewer re-runs on
the new commit). Do not commit any evidence file (screenshot, recording, report) —
those are emailed, never committed to the repo (see the evidence contract above). End
with a short summary of what you changed and what you left as-is and why.
"""

CLASSIFY_PR_REPLY = """The user replied to the pull-request review email with:

    $reply

Classify their intent. Respond with ONLY a JSON object:
{"action": "merge" | "changes" | "abort", "feedback": "<the change requests, empty otherwise>"}

Only choose "abort" when the reply clearly asks to stop, cancel, or abandon this task
entirely (e.g. "abort", "cancel this", "never mind, stop working on this") rather than
requesting changes to the current PR. If the reply asks for changes to the PR, choose
"changes" even if it also asks to close/withdraw the PR as part of those changes — only
choose "abort" when the user wants codebot itself to stop working on the task.
"""

ADDRESS_PR_THREADS = """The pull request still has unresolved review conversation(s) that
must be resolved before merging:

$threads

For each: if it points to a genuine problem, fix it properly. If it is not worth acting
on, leave a brief reply explaining why (e.g. via `gh pr comment` or a reply on the
thread) rather than silently ignoring it. Keep e2e tests passing and updated. Commit
your changes on branch $branch with clear messages and push. Do not commit any
evidence file (screenshot, recording, report) — those are emailed, never committed to
the repo (see the evidence contract above). End with a short summary of what you
changed and how each thread was addressed.
"""

REMOVE_EVIDENCE_FROM_REPO = """You committed evidence file(s) directly into the repo on
branch $branch — that must never happen; evidence belongs in an email, not the git
history:

$paths

For each one: remove it from git (`git rm` it, or `git rm --cached` if you want to keep
the local file), commit the removal, and push. Then save the file(s) under
$outbox_dir/ instead and list each one's ABSOLUTE path on its own line starting with
`ATTACH: ` in your response, so it can be attached to the email. End with a short
confirmation of what was removed and re-attached.
"""

APPLY_PR_FEEDBACK = """The user reviewed the PR and requested changes:

    $feedback

Apply the requested changes on branch $branch, keep e2e tests passing and updated,
commit and push. If the feedback asks you to "attach" or "provide" Playwright or
Newman evidence, run the requested verification but do NOT create or list `ATTACH:`
evidence files: codebot's dedicated evidence collector will re-run the feature tests
and attach its single canonical artifact. Do not add evidence to the branch/PR. End
with a summary of what changed.
"""
