"""Codebot orchestrator: works the Google Doc backlog one task at a time,
communicating with the user exclusively by email."""
import json
import logging
import re
import subprocess
import time
from datetime import date
from pathlib import Path

import agent_runner
import config
import evidence
import gdoc_client
import gmail_client
import prompts

logging.basicConfig(level=getattr(logging, config.LOG_LEVEL, logging.DEBUG),
                     format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# Keep the noisy Google HTTP client at WARNING so our own DEBUG stays readable.
for noisy in ("googleapiclient", "google", "google_auth_httplib2", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("codebot")


# ---------------------------------------------------------------- state

def load_state() -> dict:
    if config.STATE_PATH.exists():
        return json.loads(config.STATE_PATH.read_text())
    return {"state": "IDLE"}


def save_state(state: dict) -> None:
    tmp = config.STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(config.STATE_PATH)


# ---------------------------------------------------------------- helpers

def git(*args: str) -> str:
    log.debug("git %s", " ".join(args))
    proc = subprocess.run(["git", *args], cwd=config.REPO_PATH,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} exited {proc.returncode}: {proc.stderr.strip()}")
    if proc.stdout.strip():
        log.debug("git %s -> %s", args[0], proc.stdout.strip()[:300])
    return proc.stdout.strip()


def subject(state: dict, phase: str) -> str:
    return f"{config.SUBJECT_PREFIX} {state.get('slug', 'general')} — {phase}"


def email(state: dict, phase: str, body: str, attachments: list[Path] | None = None,
          new_thread: bool = False) -> None:
    thread_id = None if new_thread else state.get("thread_id")
    subj = subject(state, phase)
    log.info("emailing %r (thread=%s, %d attachment(s), %d body chars)",
             subj, thread_id or "new", len(attachments or []), len(body))
    state["thread_id"] = gmail_client.send(subj, body, thread_id, attachments)


def parse_json_reply(text: str) -> dict:
    """Extract the first valid JSON object from the coding agent's output.

    Scans each '{' and uses raw_decode so surrounding prose or later brace-bearing
    text cannot corrupt the match (greedy '{.*}' did).
    """
    decoder = json.JSONDecoder()
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError(f"no JSON object in coding-agent output: {text[:500]}")


def handle_result(state: dict, result, phase: str) -> bool:
    """If the coding agent asked a question, email it and enter WAIT_REPLY."""
    state["session_id"] = result.session_id
    log.debug("[%s] session=%s output=%d chars", phase, result.session_id, len(result.output))
    question = result.question
    if question:
        attachments = [Path(p) for p in result.attachments if Path(p).exists()]
        log.info("[%s] coding agent asked a question (%d validated attachment(s)); emailing user",
                 phase, len(attachments))
        email(state, f"question during {phase}",
              f"Task: {state.get('item', '?')}\nPhase: {phase}\n\n{question}\n\n"
              "Reply to this email to continue.", attachments)
        state["return_state"] = phase
        state["state"] = "WAIT_REPLY"
        return True
    return False


def _parse_completion_contract(output: str, marker: str) -> tuple[dict | None, str | None]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    prefix = marker + ":"
    marked = [line for line in lines if line.startswith(prefix)]
    if output.count(prefix) != 1 or len(marked) != 1:
        return None, f"expected exactly one {marker} contract"
    if not lines or lines[-1] != marked[0]:
        return None, f"{marker} contract must be the final nonblank line"
    try:
        value = json.loads(marked[0][len(prefix):].strip())
    except ValueError:
        return None, f"{marker} contract contains malformed JSON"
    if not isinstance(value, dict):
        return None, f"{marker} contract must contain a JSON object"
    return value, None


def _nonempty_strings(value) -> bool:
    return (isinstance(value, list) and bool(value) and
            all(isinstance(item, str) and item.strip() for item in value))


def parse_quality_gate(output: str) -> tuple[dict | None, str | None]:
    value, reason = _parse_completion_contract(output, "QUALITY_GATE")
    if reason:
        return None, reason
    if set(value) != {"status", "commands", "openspec", "tasks"}:
        return None, "quality gate has unexpected or missing fields"
    if value.get("status") != "pass":
        return None, "quality gate status is not pass"
    if not _nonempty_strings(value.get("commands")):
        return None, "quality gate commands must contain nonempty strings"
    if value.get("openspec") != "pass":
        return None, "OpenSpec validation did not pass"
    match = re.fullmatch(r"(\d+)/(\d+)", value.get("tasks", ""))
    if not match or int(match.group(1)) != int(match.group(2)):
        return None, "OpenSpec tasks are incomplete"
    return value, None


def parse_internal_review(output: str) -> tuple[dict | None, str | None]:
    value, reason = _parse_completion_contract(output, "INTERNAL_REVIEW")
    if reason:
        return None, reason
    if set(value) != {"status", "critical", "important", "tests"}:
        return None, "internal review has unexpected or missing fields"
    if value.get("status") != "pass":
        return None, "internal review status is not pass"
    for severity in ("critical", "important"):
        count = value.get(severity)
        if type(count) is not int or count != 0:
            return None, f"internal review has unresolved {severity} findings"
    if not _nonempty_strings(value.get("tests")):
        return None, "internal review tests must contain nonempty strings"
    return value, None


# Video extensions are the strongest signal on their own (repos essentially never
# legitimately commit one); other hints only count against a path that also names an
# evidence-ish directory/filename, to avoid flagging ordinary repo images/reports.
_EVIDENCE_EXTENSIONS = (".mp4", ".mov", ".webm", ".avi", ".mkv")
_EVIDENCE_PATH_HINTS = ("evidence", "recording")


def _looks_like_evidence(path: str) -> bool:
    lower = path.lower()
    return lower.endswith(_EVIDENCE_EXTENSIONS) or any(h in lower for h in _EVIDENCE_PATH_HINTS)


def _branch_evidence_paths(branch: str, base_branch: str) -> list[str]:
    """Files added/changed on `branch` since it diverged from `base_branch` that look
    like recorded test evidence rather than application code — these must never be
    committed, only emailed (see agent_runner.EVIDENCE_CONTRACT)."""
    diff = git("diff", "--name-only", f"{base_branch}...{branch}")
    return [p for p in diff.splitlines() if _looks_like_evidence(p)]


def _scrub_evidence_from_repo(state: dict, phase: str) -> list[Path] | None:
    """Safety net for when the prompt-level rule against committing evidence still gets
    missed: if evidence-looking files were committed to the branch, have the working
    session remove them from git and re-route them through ATTACH:/email instead.
    Returns extra attachment paths from that corrective turn (possibly empty), or None
    if it asked the user a question instead (caller should treat like handle_result)."""
    paths = _branch_evidence_paths(state["branch"], config.BASE_BRANCH)
    if not paths:
        return []
    log.warning("[%s] evidence-looking file(s) were committed to the repo; asking "
                "the coding agent to remove them and re-route via email instead: %s", phase, paths)
    result = agent_runner.resume(state["session_id"], prompts.render(
        prompts.REMOVE_EVIDENCE_FROM_REPO, branch=state["branch"],
        paths="\n".join(f"- {p}" for p in paths), outbox_dir=str(agent_runner.OUTBOX_DIR)))
    if handle_result(state, result, phase):
        return None
    return [Path(p) for p in result.attachments]


def _collect_attachments(result, e2e_specs: list[str],
                          e2e_kind: str | None) -> list[Path]:
    """Files to attach to a post-task email: whatever the coding agent explicitly pointed to via
    ATTACH: lines in its own (non-question) output, plus whatever the evidence pipeline
    separately records. Claude may have already captured and converted its own evidence
    (e.g. mid-review, outside the dedicated E2E phase) — trust that over re-deriving
    evidence from scratch, which runs in a fresh subprocess and can fail for reasons
    Claude's own sandboxed tool calls didn't hit. Deduped by resolved path in case both
    sources happen to reference the same file."""
    agent_files = [Path(p) for p in result.attachments]
    evidence_files = evidence.record_evidence(e2e_specs, e2e_kind)
    agent_videos = [file for file in agent_files if file.suffix.lower() == ".webm"]
    if agent_videos:
        if evidence_files:
            # The dedicated recorder is canonical when it succeeds; avoid attaching
            # duplicate raw clips from an agent's earlier manual run.
            agent_files = [file for file in agent_files if file not in agent_videos]
        else:
            stitched = evidence.stitch_playwright_clips(agent_videos)
            if stitched:
                agent_files = [file for file in agent_files if file not in agent_videos] + [stitched]
    combined, seen = [], set()
    for f in agent_files + evidence_files:
        resolved = f.resolve()
        if resolved not in seen:
            seen.add(resolved)
            combined.append(f)
    return combined


# ---------------------------------------------------------------- capability detection

def _has_e2e_harness() -> bool:
    return (config.REPO_PATH / "e2e" / "run.sh").is_file()


def _has_code_review_workflow() -> bool:
    workflows_dir = config.REPO_PATH / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return False
    for pattern in ("*.yml", "*.yaml"):
        for path in workflows_dir.glob(pattern):
            try:
                text = path.read_text()
            except OSError:
                continue
            # Anchored to column 0: a workflow's own top-level `name:`, not an
            # indented job/step name that happens to say "Code Review" too.
            if re.search(r'^name:\s*["\']?Code Review["\']?\s*$', text, re.MULTILINE):
                return True
    return False


def _e2e_kind_of_present_harness() -> str:
    """"playwright" | "newman" | "unknown", for a harness already known to be present."""
    e2e_dir = config.REPO_PATH / "e2e"
    if (e2e_dir / "playwright.config.ts").is_file():
        return "playwright"
    if any(e2e_dir.rglob("*.postman_collection.json")):
        return "newman"
    return "unknown"


def _has_frontend() -> bool:
    root = config.REPO_PATH
    if (root / "frontend").is_dir() or (root / "angular.json").is_file() or (root / "index.html").is_file():
        return True
    return any(root.glob("vite.config.*")) or any(root.glob("next.config.*"))


def _detect_capabilities(state: dict) -> None:
    """Detect, once per task, whether the target repo has an e2e harness and a Code
    Review workflow, and which e2e tool (Playwright/Newman) is present — or, if no
    harness is present, which one self-healing should request."""
    has_e2e = _has_e2e_harness()
    kind = _e2e_kind_of_present_harness() if has_e2e else (
        "playwright" if _has_frontend() else "newman")
    state["has_e2e_harness"] = has_e2e
    state["has_code_review"] = _has_code_review_workflow()
    state["e2e_kind"] = kind
    log.info("target repo capabilities: e2e_harness=%s kind=%s code_review=%s",
             has_e2e, kind, state["has_code_review"])


# ---------------------------------------------------------------- self-healing

E2E_HARNESS_ITEMS = {
    "playwright": "Set up a Playwright e2e harness (e2e/run.sh + Playwright specs under "
                  "e2e/tests/) so future changes can be gated on it.",
    "newman": "Set up a Newman/Postman e2e harness (e2e/run.sh + collections under "
              "e2e/collections/) so future changes can be gated on it.",
}
CODE_REVIEW_ITEM = (
    "Add a 'Code Review' GitHub Actions workflow that runs the alibaba/open-code-review "
    "(OpenCodeReview) action (https://github.com/alibaba/open-code-review) as the automated "
    "PR reviewer, using an Anthropic API key as its LLM backend. To match coderbot's "
    "automated-review integration exactly: (1) the workflow's top-level `name:` field must be "
    "exactly `Code Review` (NOT the action's own suggested name, `OpenCodeReview PR Review`) "
    "so coderbot's Code Review detection and polling can find it; (2) trigger on pull_request "
    "(or pull_request_target) `opened`, `synchronize`, and `reopened` so a fresh run fires on "
    "every push, including fix-up commits; (3) invoke the action via "
    "`uses: alibaba/open-code-review@<pinned version>` per "
    "https://github.com/alibaba/open-code-review/blob/main/pages/src/content/docs/en/integrations/ci.md "
    "(or the action.yml at the repo root), with `llm_use_anthropic: true` and "
    "`llm_url`/`llm_auth_token`/`llm_model` sourced from repo secrets/variables pointing at the "
    "Anthropic API — ask the user for these credentials via NEED_USER_INPUT if they are not "
    "already configured in the repo, rather than guessing; (4) leave `github_token` at its "
    "default (`${{ github.token }}`) and `sticky_summary` at its default (`true`), so feedback "
    "posts as inline PR review comments plus a sticky summary comment under the "
    "`github-actions[bot]` identity — do NOT swap in a custom GitHub App or bot account, since "
    "coderbot only recognizes comments from `github-actions[bot]`; (5) grant "
    "`permissions: contents: read` and `pull-requests: write`.")


def _seed_self_healing_items(state: dict) -> None:
    """Add a backlog item for each piece of missing infrastructure, unless an
    equivalent item is already pending or done."""
    if not state["has_e2e_harness"]:
        if gdoc_client.ensure_item(E2E_HARNESS_ITEMS[state["e2e_kind"]]):
            log.info("self-healing: seeded backlog item for missing e2e harness (%s)",
                     state["e2e_kind"])
    if not state["has_code_review"]:
        if gdoc_client.ensure_item(CODE_REVIEW_ITEM):
            log.info("self-healing: seeded backlog item for missing Code Review workflow")


_EVIDENCE_CONTRACT = (
    " After you're done, an automated evidence step re-runs each new/changed spec/collection "
    "file by NAME ONLY (e.g. `./run.sh your-new-test.spec.ts`), with no extra CLI flags, and "
    "no environment variables beyond what forces recording on (video for Playwright). It must "
    "exercise its real happy path and produce evidence under that exact bare invocation — do "
    "not gate the meaningful assertions behind a custom flag, mode, or env var (e.g. a "
    "`--provider xyz` switch) that this invocation will never pass, and do not have the test "
    "self-skip by default, or no evidence will be captured for the PR.")


def _e2e_note(state: dict) -> str:
    if not state.get("has_e2e_harness"):
        return ("- No e2e harness exists in this repo yet; verify the change using your own "
                "judgment (existing test suites, manual checks, etc.) instead of writing e2e "
                "tests.")
    if state.get("e2e_kind") == "newman":
        return ("- Every user-facing feature MUST include comprehensive Postman collections "
                "under e2e/collections/, run via Newman (they must pass)." + _EVIDENCE_CONTRACT)
    return ("- Every user-facing feature MUST include comprehensive Playwright e2e tests in "
            "e2e/tests/ (they must pass)." + _EVIDENCE_CONTRACT)


def _e2e_report_note(state: dict) -> str:
    if not state.get("has_e2e_harness"):
        return "End with a summary of what was implemented and how you verified it."
    return ("End with a summary of what was implemented and the list of new/changed e2e test "
            "files (one per line, prefixed with `E2E_SPEC: `).")


# ---------------------------------------------------------------- phases

def do_pick(state: dict) -> None:
    # Only tracked, uncommitted changes matter — untracked files (e.g. host-side
    # git-ignored config not excluded inside the container) don't block a checkout.
    if git("status", "--porcelain", "--untracked-files=no"):
        email(state, "blocked: dirty working tree",
              "The repo working tree has uncommitted changes; codebot will not start a "
              "new task. Clean it up and reply to this email to retry.", new_thread=True)
        state["state"] = "WAIT_CLEAN"
        return
    # Sync to the base branch before detecting target-repo capabilities and seeding
    # any self-healing item, so both reflect its actual current state rather than
    # whatever branch was last checked out.
    git("checkout", config.BASE_BRANCH)
    git("pull", "--ff-only")
    _detect_capabilities(state)
    _seed_self_healing_items(state)
    items = gdoc_client.list_pending_items()
    log.info("backlog has %d pending item(s)", len(items))
    if not items:
        log.info("no pending items; staying idle")
        state["state"] = "IDLE"
        return
    result = agent_runner.run(prompts.render(
        prompts.PICK, project=config.PROJECT_NAME, items="\n".join(f"- {i}" for i in items)))
    choice = parse_json_reply(result.output)
    if not choice.get("item") or not choice.get("slug"):
        raise ValueError(f"PICK output missing item/slug: {choice!r}")
    log.info("coding agent picked %r (slug=%s): %s",
             choice["item"][:80], choice["slug"], choice.get("reason", ""))
    state.update(item=choice["item"], slug=choice["slug"], thread_id=None)
    # Flat prefix (no slash): a "codebot/<slug>" branch would collide with the
    # existing "codebot" branch in git's ref namespace (file-vs-directory, exit 128).
    branch = f"codebot-{choice['slug']}"
    # -B is idempotent: if do_pick is retried after the branch was already created
    # (state not yet persisted), reset it from the base branch rather than failing on "-b".
    git("checkout", "-B", branch)
    state["branch"] = branch
    state["state"] = "EXPLORING"
    log.info("picked %r -> %s", choice["item"], branch)


def do_explore(state: dict) -> None:
    result = agent_runner.run(prompts.render(
        prompts.EXPLORE, project=config.PROJECT_NAME, branch=state["branch"], item=state["item"]))
    if handle_result(state, result, "EXPLORING"):
        return
    state["state"] = "PROPOSING"


def do_propose(state: dict) -> None:
    result = agent_runner.resume(state["session_id"], prompts.render(
        prompts.PROPOSE, slug=state["slug"], e2e_note=_e2e_note(state)))
    if handle_result(state, result, "PROPOSING"):
        return
    email(state, "proposal for review",
          f"Task: {state['item']}\n\n{result.output}\n\n"
          "Reply with your approval or requested changes.")
    state["state"] = "WAIT_APPROVAL"


def do_approval_reply(state: dict, reply: str) -> None:
    # Never let the autonomous working session self-declare approval. Classify the
    # user's own words with a fresh, dedicated session (like do_merge_reply) and only
    # advance on an explicit go-ahead; anything else keeps us waiting for real approval.
    verdict = parse_json_reply(
        agent_runner.run(prompts.render(prompts.CLASSIFY_APPROVAL_REPLY, reply=reply)).output)
    action = verdict.get("action")
    log.info("classified approval reply as action=%r", action)
    if action not in ("approve", "changes", "abort"):
        email(state, "clarification needed",
              f"I could not tell whether your reply approves the proposal, requests "
              f"changes, or aborts:\n\n{reply}\n\nPlease reply again with an explicit "
              "approval, the changes you want, or 'abort'.")
        return  # stay in WAIT_APPROVAL
    if action == "abort":
        _abort_and_reset(state, "Got it — I'm stopping this task and resetting to a clean slate.")
        return
    if action == "approve":
        state["state"] = "IMPLEMENTING"
        return
    # changes: revise the proposal in the working session and go back to waiting.
    result = agent_runner.resume(
        state["session_id"],
        prompts.render(prompts.REVISE_PROPOSAL, feedback=verdict.get("feedback", ""), slug=state["slug"]))
    if handle_result(state, result, "PROPOSING"):
        return
    email(state, "revised proposal", result.output + "\n\nReply with your approval or further changes.")
    state["state"] = "WAIT_APPROVAL"


def do_implement(state: dict) -> None:
    result = agent_runner.resume(
        state["session_id"],
        prompts.render(prompts.IMPLEMENT, slug=state["slug"], branch=state["branch"],
                       e2e_note=_e2e_note(state), e2e_report_note=_e2e_report_note(state)))
    if handle_result(state, result, "IMPLEMENTING"):
        return
    if _scrub_evidence_from_repo(state, "IMPLEMENTING") is None:
        return
    state["e2e_specs"] = evidence.reported_specs(result.output)
    log.info("implementation reported %d e2e spec(s): %s",
             len(state["e2e_specs"]), state["e2e_specs"])
    if not state["e2e_specs"] and state.get("has_e2e_harness"):
        log.warning("no E2E_SPEC lines in implementation output — feature may lack tests")
    state["e2e_round"] = 0
    state["verify_round"] = 0
    state["state"] = "VERIFYING"


def _gate_failed(state: dict, phase: str, counter: str, reason: str) -> None:
    state[counter] = state.get(counter, 0) + 1
    log.warning("%s result rejected (round %d/%d): %s", phase, state[counter],
                config.QUALITY_GATE_MAX_ROUNDS, reason)
    if state[counter] < config.QUALITY_GATE_MAX_ROUNDS:
        state["state"] = phase
        return
    email(state, f"{phase.lower().replace('_', ' ')} stuck - needs your help",
          f"Task: {state['item']}\n\nThe {phase.lower().replace('_', ' ')} gate has failed "
          f"{state[counter]} times. Latest reason: {reason}\n\nReply with guidance to continue.")
    state["return_state"] = phase
    state["state"] = "WAIT_REPLY"


def _complete_verify(state: dict, result) -> None:
    if handle_result(state, result, "VERIFYING"):
        return
    parsed, reason = parse_quality_gate(result.output)
    if parsed is None:
        _gate_failed(state, "VERIFYING", "verify_round", reason)
        return
    try:
        slug = _validated_slug(state.get("slug"))
        _run_checked(["openspec", "validate", slug, "--strict", "--no-interactive"])
        instructions = json.loads(_run_checked([
            "openspec", "instructions", "apply", "--change", slug, "--json",
        ]))
        progress = instructions.get("progress") if isinstance(instructions, dict) else None
        if not isinstance(progress, dict) or instructions.get("state") != "all_done":
            raise ValueError("OpenSpec apply state is not all_done")
        total = progress.get("total")
        complete = progress.get("complete")
        remaining = progress.get("remaining")
        if any(type(value) is not int for value in (total, complete, remaining)) or \
                remaining != 0 or complete != total:
            raise ValueError("OpenSpec apply progress is incomplete")
    except Exception as error:
        _gate_failed(state, "VERIFYING", "verify_round", str(error))
        return
    state.pop("verify_round", None)
    state["review_gate_round"] = 0
    state["state"] = "INTERNAL_REVIEW"


def do_verify(state: dict) -> None:
    result = agent_runner.resume(
        state["session_id"], prompts.render(prompts.VERIFY, slug=state["slug"]))
    _complete_verify(state, result)


def _complete_internal_review(state: dict, result) -> None:
    if handle_result(state, result, "INTERNAL_REVIEW"):
        return
    parsed, reason = parse_internal_review(result.output)
    if parsed is None:
        _gate_failed(state, "INTERNAL_REVIEW", "review_gate_round", reason)
        return
    state.pop("review_gate_round", None)
    state["state"] = "E2E" if state.get("has_e2e_harness") else "ARCHIVING"


def do_internal_review(state: dict) -> None:
    result = agent_runner.resume(
        state["session_id"], prompts.render(prompts.INTERNAL_REVIEW, slug=state["slug"]))
    _complete_internal_review(state, result)


def _tracked_snapshot() -> tuple[str, str]:
    return (git("rev-parse", "HEAD"),
            git("status", "--porcelain", "--untracked-files=no"))


def _finish_e2e_repair(state: dict) -> None:
    before = (state.pop("e2e_repair_head"), state.pop("e2e_repair_status"))
    if _tracked_snapshot() != before:
        state["verify_round"] = 0
        state["state"] = "VERIFYING"
    else:
        state["state"] = "E2E"


def do_e2e(state: dict) -> None:
    if "e2e_repair_head" in state and "e2e_repair_status" in state:
        _finish_e2e_repair(state)
        if state["state"] != "E2E":
            return
    if not state.get("has_e2e_harness"):
        log.info("no e2e harness detected for this task; skipping the e2e gate")
        state["state"] = "ARCHIVING"
        return
    log.info("running e2e suite (e2e/run.sh)")
    passed, output = evidence.run_suite()
    if not passed:
        state["e2e_round"] = state.get("e2e_round", 0) + 1
        log.warning("e2e suite FAILED (round %d/%d); resuming session to fix. Tail:\n%s",
                    state["e2e_round"], config.E2E_MAX_ROUNDS, output[-1500:])
        if state["e2e_round"] > config.E2E_MAX_ROUNDS:
            log.warning("e2e round limit (%d) reached; asking user for help instead of retrying",
                        config.E2E_MAX_ROUNDS)
            email(state, "e2e suite stuck — needs your help",
                  f"Task: {state['item']}\n\nThe e2e suite has failed {config.E2E_MAX_ROUNDS} "
                  "times in a row and I could not fix it myself (this is often caused by "
                  "something outside the code, e.g. a stuck process/port left over from a "
                  f"prior run). Latest failure output:\n\n{output[-3000:]}\n\n"
                  "Please investigate, then reply with guidance (or tell me what to try) "
                  "to continue.")
            state["return_state"] = "E2E"
            state["state"] = "WAIT_REPLY"
            return
        state["e2e_repair_head"], state["e2e_repair_status"] = _tracked_snapshot()
        save_state(state)
        result = agent_runner.resume(state["session_id"], prompts.render(prompts.FIX_E2E, output=output))
        if handle_result(state, result, "E2E"):
            return
        _finish_e2e_repair(state)
        return  # loop re-enters E2E and re-runs the suite
    log.info("e2e suite PASSED")
    state["e2e_round"] = 0
    state["state"] = "ARCHIVING"


def _run_checked(command: list[str]) -> str:
    proc = subprocess.run(command, cwd=config.REPO_PATH, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"{' '.join(command)} exited {proc.returncode}: {detail}")
    return proc.stdout.strip()


def _validated_slug(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
        raise ValueError("change slug must be safe kebab-case")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("dated change slug must include a kebab-case name")
    dated = re.match(r"^(\d{4}-\d{2}-\d{2})-", value)
    if dated:
        try:
            date.fromisoformat(dated.group(1))
        except ValueError as error:
            raise ValueError("change slug has an invalid date prefix") from error
    return value


def _validated_archive_path(value, slug: str) -> Path:
    if not isinstance(value, str):
        raise ValueError("archive path must be a relative string")
    relative = Path(value)
    if relative.is_absolute() or relative.parts[:3] != ("openspec", "changes", "archive") or \
            len(relative.parts) != 4:
        raise ValueError("archive path must be a direct child of openspec/changes/archive")
    name = relative.name
    if re.match(r"^\d{4}-\d{2}-\d{2}-", slug):
        if name != slug:
            raise ValueError("archive path does not match the dated change slug")
    else:
        match = re.fullmatch(rf"(\d{{4}}-\d{{2}}-\d{{2}})-{re.escape(slug)}", name)
        if not match:
            raise ValueError("archive path does not match the change slug")
        try:
            date.fromisoformat(match.group(1))
        except ValueError as error:
            raise ValueError("archive path has an invalid date prefix") from error

    repo = config.REPO_PATH.resolve()
    archive_root = repo / "openspec" / "changes" / "archive"
    target = (config.REPO_PATH / relative).resolve()
    if target.parent != archive_root:
        raise ValueError("archive path resolves outside openspec/changes/archive")
    return target


def _archive_target(state: dict) -> Path:
    slug = _validated_slug(state.get("slug"))
    if state.get("archive_path"):
        return _validated_archive_path(state["archive_path"], slug)
    active = config.REPO_PATH / "openspec" / "changes" / slug
    archive_root = config.REPO_PATH / "openspec" / "changes" / "archive"
    if archive_root.resolve() != config.REPO_PATH.resolve() / "openspec" / "changes" / "archive":
        raise ValueError("archive root resolves outside the repository")
    matches = list(archive_root.glob(f"????-??-??-{slug}")) if archive_root.is_dir() else []
    if not active.exists() and len(matches) == 1:
        target = matches[0]
    elif not active.exists() and len(matches) > 1:
        raise RuntimeError(f"multiple archives match change {slug}")
    else:
        name = slug if re.match(r"^\d{4}-\d{2}-\d{2}-", slug) else f"{date.today():%Y-%m-%d}-{slug}"
        target = archive_root / name
    relative = str(target.relative_to(config.REPO_PATH))
    target = _validated_archive_path(relative, slug)
    state["archive_path"] = relative
    save_state(state)
    return target


def _archive_failed(state: dict, error: Exception) -> None:
    state["archive_error"] = str(error)
    state["archive_round"] = state.get("archive_round", 0) + 1
    log.warning("archive failed (round %d/%d): %s", state["archive_round"],
                config.ARCHIVE_MAX_ROUNDS, error)
    if state["archive_round"] < config.ARCHIVE_MAX_ROUNDS:
        state["state"] = "ARCHIVING"
        return
    email(state, "OpenSpec archival stuck - needs your help",
          f"Task: {state['item']}\n\nArchival failed {state['archive_round']} times. "
          f"Latest error: {error}\n\nReply with guidance to retry.")
    state["return_state"] = "ARCHIVING"
    state["state"] = "WAIT_REPLY"


def _changes_are_archive_outputs(state: dict, status: str) -> bool:
    allowed = (
        "openspec/specs",
        f"openspec/changes/{state['slug']}",
        state["archive_path"],
    )
    for line in status.splitlines():
        paths = line[3:].split(" -> ")
        if any(not any(path.strip('"') == root or path.strip('"').startswith(root + "/")
                       for root in allowed) for path in paths):
            return False
    return True


def do_archive(state: dict) -> None:
    try:
        target = _archive_target(state)
        active = config.REPO_PATH / "openspec" / "changes" / state["slug"]
        if active.exists() and target.exists():
            raise RuntimeError("active change and expected archive both exist")
        if active.exists():
            if git("status", "--porcelain", "--untracked-files=no"):
                raise RuntimeError("tracked working tree must be clean before archival")
            if git("status", "--porcelain", "--untracked-files=all", "--", "openspec/"):
                raise RuntimeError("OpenSpec tree must be clean before archival")
            _run_checked(["openspec", "archive", state["slug"], "-y", "--json"])
        elif not target.is_dir():
            raise RuntimeError("active change and expected archive are both absent")
        if not target.is_dir():
            raise RuntimeError("archive command did not create the expected archive")

        _run_checked(["openspec", "validate", "--specs", "--strict", "--no-interactive"])
        _run_checked(["openspec", "validate", "--archived", "--strict", "--no-interactive"])
        status = git("status", "--porcelain", "--untracked-files=all")
        if status and not _changes_are_archive_outputs(state, status):
            raise RuntimeError("archival produced unrelated tracked changes")
        if status:
            paths = ("openspec/specs", f"openspec/changes/{state['slug']}",
                     state["archive_path"])
            git("add", "-A", "--", *paths)
            git("commit", "-m", "chore: archive OpenSpec change", "--", *paths)
        state.pop("archive_round", None)
        state.pop("archive_error", None)
        state["state"] = "OPEN_PR"
    except Exception as error:
        _archive_failed(state, error)


def _https_url(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"https://\S+", value.strip()):
        raise RuntimeError(f"expected one HTTPS URL, got: {value!r}")
    return value.strip()


def _resume_archiving(state: dict, reason: str) -> None:
    log.warning("OPEN_PR archival precondition failed: %s", reason)
    state.pop("pr_url", None)
    state.pop("pr_summary", None)
    state["archive_error"] = reason
    state["archive_round"] = 0
    state["state"] = "ARCHIVING"


def do_open_pr(state: dict) -> None:
    if not state.get("archive_path"):
        state["verify_round"] = 0
        state.pop("review_gate_round", None)
        state["state"] = "VERIFYING"
        return
    slug = _validated_slug(state.get("slug"))
    target = _validated_archive_path(state["archive_path"], slug)
    reference = str(target.relative_to(config.REPO_PATH.resolve()))
    active = config.REPO_PATH / "openspec" / "changes" / slug
    if not target.is_dir():
        _resume_archiving(state, "validated archive directory is missing")
        return
    if active.exists():
        _resume_archiving(state, "active OpenSpec change still exists")
        return
    if git("status", "--porcelain", "--untracked-files=all", "--", "openspec/"):
        _resume_archiving(state, "OpenSpec tree is dirty before PR creation")
        return
    if not git("ls-tree", "-r", "--name-only", "HEAD", "--", reference).strip():
        _resume_archiving(state, "archive is not committed in HEAD")
        return
    body = f"Implements: {state['item']}\n\nOpenSpec archive: `{reference}`"
    if state.get("pr_url"):
        state["pr_url"] = _https_url(state["pr_url"])
    else:
        git("push", "-u", "origin", state["branch"])
        listed = _run_checked([
            "gh", "pr", "list", "--state", "open", "--head", state["branch"],
            "--json", "url,state", "--limit", "1",
        ])
        try:
            existing = json.loads(listed)
        except ValueError as error:
            raise RuntimeError("gh pr list returned malformed JSON") from error
        if not isinstance(existing, list) or len(existing) > 1 or \
                any(not isinstance(item, dict) for item in existing):
            raise RuntimeError("gh pr list returned an invalid or ambiguous result")
        if existing and existing[0].get("state") not in ("OPEN", "CLOSED", "MERGED"):
            raise RuntimeError("gh pr list returned an invalid pull-request state")
        if existing and existing[0]["state"] == "OPEN":
            state["pr_url"] = _https_url(existing[0].get("url"))
        else:
            output = _run_checked([
                "gh", "pr", "create", "--base", config.BASE_BRANCH, "--head", state["branch"],
                "--title", state["item"], "--body", body,
            ])
            state["pr_url"] = _https_url(output)
    state["pr_summary"] = body
    log.info("PR opened: %s", state["pr_url"])
    if not state.get("has_code_review"):
        log.info("no Code Review workflow detected for this task; finalizing without a review wait")
        finalize_pr(state)
        return
    # Don't notify the user yet: let the OpenCodeReview action run and address its
    # comments first (do_open_pr -> WAIT_REVIEW -> [ADDRESS_REVIEW -> WAIT_REVIEW]* -> WAIT_MERGE).
    state["review_round"] = 0
    state["review_run_link"] = None            # link of the last Code Review run we processed
    state["review_comment_watermark"] = ""     # only comments created after this are "new"
    _enter_review_wait(state)


# ---------------------------------------------------------------- automated review

def _pr_owner_number(pr_url: str) -> tuple[str, str]:
    """Parse 'https://github.com/OWNER/REPO/pull/N' into ('OWNER/REPO', 'N')."""
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", pr_url)
    if not m:
        raise ValueError(f"cannot parse owner/number from PR url: {pr_url!r}")
    return m.group(1), m.group(2)


def code_review_check(pr_url: str) -> dict | None:
    """The 'Code Review' workflow check for the PR's head commit, or None if not present yet.

    Returns the gh-pr-checks record (has 'bucket': pass|fail|pending|skipping|cancel,
    and 'link' identifying the specific run). gh exits non-zero while checks are pending
    or failing but still prints JSON, so we parse stdout rather than trust the exit code.
    A genuine gh failure (auth, network) also returns None but is logged at warning, so it
    surfaces promptly instead of masquerading as "not started" and stalling the poll loop
    until the review timeout.
    """
    proc = subprocess.run(
        ["gh", "pr", "checks", pr_url, "--json", "name,bucket,workflow,link"],
        cwd=config.REPO_PATH, capture_output=True, text=True)
    try:
        checks = json.loads(proc.stdout) if proc.stdout.strip() else []
    except json.JSONDecodeError:
        checks = None
    for c in checks or []:
        if c.get("workflow") == "Code Review" or c.get("name") == "code-review":
            return c
    if checks:
        # Real check data, but the Code Review run just hasn't registered yet.
        log.debug("gh pr checks: Code Review not among %d reported check(s) yet", len(checks))
    else:
        # No usable data. Empty output with a pending / "no checks reported" signal is the
        # normal pre-run state; unparseable output or an unexpected exit is a real tooling
        # failure that must not hide until REVIEW_WAIT_TIMEOUT_SECONDS elapses.
        stderr = proc.stderr.strip()
        benign = checks == [] and (proc.returncode in (0, 8) or "no checks reported" in stderr.lower())
        if benign:
            log.debug("gh pr checks: no checks reported yet (rc=%d)", proc.returncode)
        else:
            log.warning("gh pr checks failed (rc=%d): %s", proc.returncode, stderr[-300:] or "(no stderr)")
    return None


def _bot_comments(endpoint: str) -> list[dict]:
    """github-actions[bot] comments from a PR comments endpoint ([] on any failure)."""
    proc = subprocess.run(
        ["gh", "api", f"{endpoint}?per_page=100"],
        cwd=config.REPO_PATH, capture_output=True, text=True)
    if proc.returncode != 0:
        log.warning("gh api %s failed: %s", endpoint, proc.stderr[-300:])
        return []
    try:
        items = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    return [c for c in items if c.get("user", {}).get("login") == "github-actions[bot]"]


def new_review_comments(state: dict) -> list[dict]:
    """OCR inline comments created after the watermark: [{path, line, body, created_at}]."""
    owner_repo, number = _pr_owner_number(state["pr_url"])
    watermark = state.get("review_comment_watermark", "")
    fresh = []
    for c in _bot_comments(f"repos/{owner_repo}/pulls/{number}/comments"):
        # ISO-8601 Zulu timestamps sort lexicographically, so a string compare is chronological.
        if c.get("created_at", "") <= watermark:
            continue
        fresh.append({
            "path": c.get("path"),
            "line": c.get("line") or c.get("original_line"),
            "body": c.get("body", ""),
            "created_at": c.get("created_at", ""),
        })
    return fresh


def review_summary(state: dict) -> str:
    """The most recent OCR sticky-summary issue comment body, for extra context ('' if none)."""
    owner_repo, number = _pr_owner_number(state["pr_url"])
    summaries = _bot_comments(f"repos/{owner_repo}/issues/{number}/comments")
    if not summaries:
        return ""
    return max(summaries, key=lambda c: c.get("updated_at", "")).get("body", "")


def format_review_comments(comments: list[dict], summary: str) -> str:
    lines = []
    if summary:
        lines.append("Reviewer summary:\n" + summary + "\n")
    lines.append(f"{len(comments)} inline comment(s):")
    for i, c in enumerate(comments, 1):
        loc = f"{c['path']}:{c['line']}" if c.get("line") else (c.get("path") or "(general)")
        lines.append(f"\n[{i}] {loc}\n{c['body']}")
    return "\n".join(lines)


def _enter_review_wait(state: dict) -> None:
    state["review_since"] = time.time()  # per-run wait clock (survives restarts via state.json)
    state["state"] = "WAIT_REVIEW"


def finalize_pr(state: dict, note: str = "") -> None:
    """Record evidence and email the (now review-clean) PR, then wait for the user."""
    evidence_files = evidence.record_evidence(state.get("e2e_specs", []), state.get("e2e_kind"))
    log.info("recorded %d evidence file(s) to attach", len(evidence_files))
    body = f"Task: {state['item']}\nPR: {state['pr_url']}\n\n{state.get('pr_summary', '')}\n\n"
    if note:
        body += note + "\n\n"
    if evidence_files:
        attachment_desc = ("a Newman run report (html)" if state.get("e2e_kind") == "newman"
                            else "a Playwright video (mp4)")
        body += f"Attached: {attachment_desc} demonstrating the feature.\n"
    elif state.get("has_e2e_harness"):
        body += ("Note: no e2e evidence could be captured for this PR (the spec/collection may "
                 "have skipped itself when re-run bare, or none matched — see codebot logs).\n")
    body += "Reply with change requests, or tell me to merge."
    email(state, "PR ready for review", body, evidence_files)
    for key in ("review_since", "review_round", "review_run_link",
                "review_comment_watermark", "review_comments", "pr_summary"):
        state.pop(key, None)
    state["state"] = "WAIT_MERGE"


def handle_review_wait(state: dict) -> None:
    """Poll the Code Review action; when a new run finishes, address its comments or finalize."""
    check = code_review_check(state["pr_url"])
    bucket = check.get("bucket") if check else None
    same_run = check is not None and check.get("link") == state.get("review_run_link")
    if check is None or bucket == "pending" or same_run:
        # Not started, still running, or the previous run's result — keep waiting, but
        # don't block the PR forever if the action is stuck or never triggered.
        if time.time() - state.get("review_since", time.time()) > config.REVIEW_WAIT_TIMEOUT_SECONDS:
            log.warning("Code Review did not complete within %ss; notifying user without it",
                        config.REVIEW_WAIT_TIMEOUT_SECONDS)
            finalize_pr(state, note="Note: the automated code review did not finish in time, "
                                    "so I'm sending this without waiting for it.")
        else:
            log.debug("waiting for Code Review (bucket=%s, same_run=%s)", bucket, same_run)
        return
    # A new Code Review run finished (pass/fail/skip/cancel). Read what it flagged.
    state["review_run_link"] = check.get("link")
    comments = new_review_comments(state)
    if not comments:
        log.info("Code Review left no new comments; PR is clean")
        finalize_pr(state)
        return
    if state.get("review_round", 0) >= config.REVIEW_MAX_ROUNDS:
        log.warning("review round limit (%d) reached with %d open comment(s); finalizing",
                    config.REVIEW_MAX_ROUNDS, len(comments))
        finalize_pr(state, note=(
            f"Note: the automated reviewer still has {len(comments)} open comment(s) after "
            f"{config.REVIEW_MAX_ROUNDS} rounds of fixes. I've left them on the PR for you."))
        return
    # Advance the watermark so the next round only sees feedback on the upcoming push.
    state["review_comment_watermark"] = max(c["created_at"] for c in comments)
    state["review_comments"] = comments
    state["state"] = "ADDRESS_REVIEW"
    log.info("Code Review left %d new comment(s); addressing them", len(comments))


def do_address_review(state: dict) -> None:
    comments = state.get("review_comments", [])
    state["review_round"] = state.get("review_round", 0) + 1
    log.info("addressing %d review comment(s), round %d", len(comments), state["review_round"])
    rendered = format_review_comments(comments, review_summary(state))
    result = agent_runner.resume(
        state["session_id"],
        prompts.render(prompts.ADDRESS_REVIEW, comments=rendered, branch=state["branch"]))
    if handle_result(state, result, "ADDRESS_REVIEW"):
        return
    if _scrub_evidence_from_repo(state, "ADDRESS_REVIEW") is None:
        return
    _queue_push(state, "review")


# ---------------------------------------------------------------- pre-merge thread resolution

def unresolved_review_threads(pr_url: str) -> list[dict]:
    """Open (unresolved) PR review conversation threads: [{id, comments: [{path, line,
    body, author}]}]. GitHub's REST API has no notion of thread resolution, so this goes
    through the GraphQL API (unlike the rest of this file's gh calls)."""
    owner_repo, number = _pr_owner_number(pr_url)
    owner, repo = owner_repo.split("/", 1)
    query = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          reviewThreads(first: 100) {
            nodes {
              id
              isResolved
              comments(first: 50) {
                nodes { path line originalLine body author { login } }
              }
            }
          }
        }
      }
    }
    """
    proc = subprocess.run(
        ["gh", "api", "graphql", "-f", f"query={query}", "-F", f"owner={owner}",
         "-F", f"repo={repo}", "-F", f"number={number}"],
        cwd=config.REPO_PATH, capture_output=True, text=True)
    if proc.returncode != 0:
        log.warning("gh api graphql (reviewThreads) failed: %s", proc.stderr[-300:])
        return []
    try:
        data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        return []
    repository = (data.get("data") or {}).get("repository") or {}
    pull_request = repository.get("pullRequest") or {}
    nodes = (pull_request.get("reviewThreads") or {}).get("nodes") or []
    threads = []
    for t in nodes:
        if t.get("isResolved"):
            continue
        comments = [{
            "path": c.get("path"),
            "line": c.get("line") or c.get("originalLine"),
            "body": c.get("body", ""),
            "author": (c.get("author") or {}).get("login", "?"),
        } for c in (t.get("comments") or {}).get("nodes") or []]
        threads.append({"id": t["id"], "comments": comments})
    return threads


def format_review_threads(threads: list[dict]) -> str:
    lines = [f"{len(threads)} unresolved review thread(s):"]
    for i, t in enumerate(threads, 1):
        comments = t["comments"]
        first = comments[0] if comments else {}
        loc = f"{first.get('path')}:{first.get('line')}" if first.get("line") else (first.get("path") or "(general)")
        lines.append(f"\n[{i}] {loc}")
        for c in comments:
            lines.append(f"  {c['author']}: {c['body']}")
    return "\n".join(lines)


def resolve_review_thread(thread_id: str) -> bool:
    proc = subprocess.run(
        ["gh", "api", "graphql",
         "-f", "query=mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { id } } }",
         "-F", f"id={thread_id}"],
        cwd=config.REPO_PATH, capture_output=True, text=True)
    if proc.returncode != 0:
        log.warning("gh api graphql (resolveReviewThread %s) failed: %s", thread_id, proc.stderr[-300:])
        return False
    return True


def do_merge_reply(state: dict, reply: str) -> None:
    result = agent_runner.run(prompts.render(prompts.CLASSIFY_PR_REPLY, reply=reply))
    verdict = parse_json_reply(result.output)
    action = verdict.get("action")
    log.info("classified PR reply as action=%r", action)
    if action not in ("merge", "changes", "abort"):
        # Merging is irreversible: never fall through to it on unexpected output.
        email(state, "clarification needed",
              f"I could not tell whether your reply asks for changes, an abort, or a "
              f"merge:\n\n{reply}\n\nPlease reply again with the change requests, 'abort', "
              "or an explicit 'merge'.")
        return  # stay in WAIT_MERGE
    if action == "abort":
        pr_url = state.get("pr_url", "-")
        _abort_and_reset(state, "Got it — I'm stopping this task and resetting to a clean "
                                f"slate.\n\nPR: {pr_url}")
        return
    if action == "changes":
        r = agent_runner.resume(
            state["session_id"],
            prompts.render(prompts.APPLY_PR_FEEDBACK, feedback=verdict.get("feedback", ""), branch=state["branch"]))
        if handle_result(state, r, "APPLY_PR_FEEDBACK"):
            return
        extra_attachments = _scrub_evidence_from_repo(state, "APPLY_PR_FEEDBACK")
        if extra_attachments is None:
            return
        attachments = _collect_attachments(r, state.get("e2e_specs", []), state.get("e2e_kind"))
        attachments += [a for a in extra_attachments if a.resolve() not in {p.resolve() for p in attachments}]
        _queue_push(state, "feedback", r.output, attachments)
        return
    # merge
    info = json.loads(subprocess.run(
        ["gh", "pr", "view", state["pr_url"], "--json", "state,mergeable"],
        cwd=config.REPO_PATH, capture_output=True, text=True, check=True).stdout)
    pr_state, mergeable = info.get("state"), info.get("mergeable")
    log.info("PR state=%s mergeable=%s before merge", pr_state, mergeable)
    if pr_state != "MERGED":
        if mergeable == "CONFLICTING":
            # The base branch moved ahead and the branch no longer merges cleanly. Never
            # force it: ask the user to resolve, then reply 'merge' to retry (which
            # re-enters here).
            log.warning("PR %s conflicts with %s; asking user to resolve",
                        state["pr_url"], config.BASE_BRANCH)
            email(state, "merge conflict — needs your help",
                  f"{config.BASE_BRANCH} has changed and this PR now conflicts with it, so I "
                  f"can't merge it automatically.\nPR: {state['pr_url']}\n\nPlease resolve the "
                  f"conflicts on the branch (merge or rebase {config.BASE_BRANCH} in and push), "
                  "then reply 'merge' to continue.")
            return  # stay in WAIT_MERGE
        log.info("merging PR %s (squash)", state["pr_url"])
        merge = subprocess.run(["gh", "pr", "merge", state["pr_url"], "--squash"],
                               cwd=config.REPO_PATH, capture_output=True, text=True)
        if merge.returncode != 0:
            # A late-breaking conflict or other GitHub rejection (e.g. branch behind the
            # base branch). Don't crash into the retry loop — surface it and wait for the
            # user to fix it.
            log.warning("gh pr merge failed (%d): %s", merge.returncode, merge.stderr.strip())
            email(state, "merge failed — needs your help",
                  f"I couldn't merge the PR; GitHub reported:\n\n{merge.stderr.strip()}\n\n"
                  f"PR: {state['pr_url']}\n\nThis usually means {config.BASE_BRANCH} moved ahead "
                  "or there are conflicts. Please resolve it on the branch and reply 'merge' to retry.")
            return  # stay in WAIT_MERGE
    if gdoc_client.mark_done(state["item"]):
        log.info("struck item through in backlog doc; task complete")
        email(state, "task complete",
              f"PR merged and the item was marked done in the backlog doc:\n\n{state['item']}")
    else:
        log.warning("could not locate item in doc to strike through: %r", state["item"][:80])
        email(state, "could not mark item done",
              f"PR merged, but I could not find this item in the doc to strike it "
              f"through:\n\n{state['item']}\n\nPlease mark it manually.")
    for key in ("item", "slug", "branch", "session_id", "thread_id", "pr_url", "e2e_specs",
                "pr_thread_round", "pr_thread_notified", "verify_round", "review_gate_round",
                "archive_round", "archive_path", "e2e_repair_head", "e2e_repair_status",
                "push_context", "archive_error"):
        state.pop(key, None)
    state["state"] = "IDLE"


def _finish_address_pr_threads(state: dict) -> None:
    threads = state.pop("pr_threads", [])
    still_unresolved = [t for t in threads if not resolve_review_thread(t["id"])]
    if still_unresolved:
        log.warning("could not resolve %d/%d review thread(s) on GitHub",
                    len(still_unresolved), len(threads))
    state["state"] = "WAIT_MERGE"


def do_address_pr_threads(state: dict) -> None:
    threads = state.get("pr_threads", [])
    state["pr_thread_round"] = state.get("pr_thread_round", 0) + 1
    log.info("addressing %d unresolved review thread(s), round %d", len(threads), state["pr_thread_round"])
    result = agent_runner.resume(
        state["session_id"],
        prompts.render(prompts.ADDRESS_PR_THREADS, threads=format_review_threads(threads),
                       branch=state["branch"]))
    if handle_result(state, result, "ADDRESS_PR_THREADS"):
        return
    if _scrub_evidence_from_repo(state, "ADDRESS_PR_THREADS") is None:
        return
    _queue_push(state, "threads")


def _queue_push(state: dict, continuation: str, output: str = "",
                attachments: list[Path] | None = None) -> None:
    state["push_context"] = {
        "continuation": continuation,
        "output": str(output),
        "attachments": [str(path) for path in attachments or []],
    }
    state["state"] = "PUSHING"
    save_state(state)


def do_push(state: dict) -> None:
    context = state.get("push_context")
    if not isinstance(context, dict):
        raise RuntimeError("PUSHING state is missing push_context")
    continuation = context.get("continuation")
    if continuation not in ("review", "feedback", "threads"):
        raise RuntimeError(f"invalid push continuation: {continuation!r}")

    git("push", "origin", state["branch"])
    if continuation == "review":
        state.pop("review_comments", None)
        _enter_review_wait(state)
    elif continuation == "feedback":
        attachments = [Path(path) for path in context.get("attachments", [])]
        email(state, "PR updated",
              f"Applied your feedback.\nPR: {state['pr_url']}\n\n{context.get('output', '')}",
              attachments)
        state["state"] = "WAIT_MERGE"
    else:
        _finish_address_pr_threads(state)
    state.pop("push_context", None)


# ---------------------------------------------------------------- loop

PHASES = {
    "IDLE": do_pick,
    "EXPLORING": do_explore,
    "PROPOSING": do_propose,
    "IMPLEMENTING": do_implement,
    "VERIFYING": do_verify,
    "INTERNAL_REVIEW": do_internal_review,
    "E2E": do_e2e,
    "ARCHIVING": do_archive,
    "OPEN_PR": do_open_pr,
    "ADDRESS_REVIEW": do_address_review,
    "ADDRESS_PR_THREADS": do_address_pr_threads,
    "PUSHING": do_push,
}

# WAIT_REVIEW polls the Code Review action rather than the inbox, but shares the
# post-tick sleep, so it lives in WAITS and is dispatched ahead of the inbox waits.
# WAIT_MERGE does too: it also checks the PR for unresolved review threads before
# falling through to the inbox check (see handle_merge_wait).
WAITS = {"WAIT_APPROVAL", "WAIT_MERGE", "WAIT_REPLY", "WAIT_CLEAN", "WAIT_REVIEW"}


def handle_wait(state: dict) -> None:
    polled = gmail_client.poll_reply(state["thread_id"]) if state.get("thread_id") else None
    if polled is None:
        log.debug("%s: no reply yet on thread %s", state["state"], state.get("thread_id"))
        return
    msg_id, reply = polled
    log.info("reply received in %s: %r", state["state"], reply[:200])
    _handle_reply(state, reply)
    # Consume the reply only now that handling finished without raising. If it threw
    # (e.g. a transient claude failure), the message stays unprocessed so the next tick
    # re-reads and re-handles it instead of silently dropping the user's reply.
    gmail_client.mark_processed(msg_id)


def handle_merge_wait(state: dict) -> None:
    """Every WAIT_MERGE poll: check the open PR for unresolved review threads (e.g. a
    human reviewer's) and address them, so live feedback gets fixed before the user
    even says 'merge'. Falls through to the normal inbox check either way."""
    threads = unresolved_review_threads(state["pr_url"])
    if not threads:
        state.pop("pr_thread_round", None)
        state.pop("pr_thread_notified", None)
    elif state.get("pr_thread_notified"):
        log.debug("%d unresolved review thread(s) still open; already notified the user",
                  len(threads))
    elif state.get("pr_thread_round", 0) >= config.PR_THREAD_MAX_ROUNDS:
        log.warning("review-thread round limit (%d) reached with %d open thread(s); "
                    "leaving them for the user", config.PR_THREAD_MAX_ROUNDS, len(threads))
        email(state, "unresolved review threads need your help",
              f"There are still {len(threads)} unresolved review conversation(s) on the PR "
              f"after {config.PR_THREAD_MAX_ROUNDS} round(s) of fixes.\nPR: {state['pr_url']}"
              "\n\nPlease resolve them yourself, or reply 'merge' to merge anyway.")
        state["pr_thread_notified"] = True
    else:
        state["pr_threads"] = threads
        state["state"] = "ADDRESS_PR_THREADS"
        log.info("PR has %d new unresolved review thread(s); addressing before merge", len(threads))
        return
    handle_wait(state)


# ---------------------------------------------------------------- abort / reset

def _git_quiet(*args: str) -> bool:
    """Run a git command, returning success. Never raises — for the best-effort reset."""
    proc = subprocess.run(["git", *args], cwd=config.REPO_PATH, capture_output=True, text=True)
    if proc.returncode != 0:
        log.debug("git %s -> rc=%d: %s", " ".join(args), proc.returncode, proc.stderr.strip()[:200])
    return proc.returncode == 0


def _reset_to_base_branch() -> list[str]:
    """Best-effort LOCAL reset: abort any half-finished git op, discard changes, land on a
    clean, up-to-date base branch. Never touches remote branches or PRs. Returns notes about
    any step that left the tree unclean (empty list when fully clean)."""
    # Abort a half-finished operation, if any (each no-ops harmlessly when none is active).
    for op in (("merge", "--abort"), ("rebase", "--abort"),
               ("cherry-pick", "--abort"), ("am", "--abort")):
        _git_quiet(*op)
    _git_quiet("reset", "--hard")                            # drop staged/unstaged tracked changes
    _git_quiet("checkout", "-f", config.BASE_BRANCH)         # leave whatever codebot branch we were on
    _git_quiet("clean", "-fd")             # drop untracked files; .gitignore (data/, .env) is kept
    # Branches may have merged/moved while we were away: fast-forward the local base branch to
    # the remote. Best-effort — a network failure here doesn't block the reset (do_pick pulls too).
    if _git_quiet("fetch", "origin", config.BASE_BRANCH):
        _git_quiet("reset", "--hard", f"origin/{config.BASE_BRANCH}")
    problems = []
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            cwd=config.REPO_PATH, capture_output=True, text=True).stdout.strip()
    if branch != config.BASE_BRANCH:
        problems.append(f"could not switch to {config.BASE_BRANCH} (still on {branch or 'unknown'})")
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                           cwd=config.REPO_PATH, capture_output=True, text=True).stdout.strip()
    if dirty:
        problems.append("tracked changes remain after reset")
    return problems


def _abort_and_reset(state: dict, note: str, new_thread: bool = False) -> None:
    """Shared abort machinery: reset the local checkout, email a confirmation, clear all
    task state, and return to IDLE. Used both by the mailbox-wide last-resort 'ABORT'
    email and by an explicit abort classified from a WAIT_APPROVAL/WAIT_MERGE reply.
    Never touches remote branches or PRs — those are left for the user to clean up."""
    aborted_task = state.get("item", "-")
    prev_state = state["state"]
    log.warning("ABORT: resetting to IDLE (was state=%s task=%r)", prev_state, aborted_task[:80])
    problems = _reset_to_base_branch()
    status = (f"The working tree was reset to a clean, up-to-date `{config.BASE_BRANCH}`." if not problems
              else "The reset finished with issues (I'll re-check the tree before starting the "
                   "next task):\n- " + "\n- ".join(problems))
    email(state, "aborted — reset to IDLE",
          f"{note}\n\nWas working on: {aborted_task}\nPrevious state: {prev_state}\n\n{status}\n\n"
          "Remote branches and PRs were left untouched. I'll pick up the next pending item "
          "from the backlog doc.", new_thread=new_thread)
    for key in ("item", "slug", "branch", "session_id", "thread_id", "pr_url", "e2e_specs",
                 "return_state", "review_since", "review_round", "review_run_link",
                 "review_comment_watermark", "review_comments", "pr_summary",
                 "pr_threads", "pr_thread_round", "pr_thread_notified", "verify_round",
                 "review_gate_round", "archive_round", "archive_path", "e2e_repair_head",
                 "e2e_repair_status", "push_context", "archive_error"):
        state.pop(key, None)
    state["state"] = "IDLE"


def check_abort(state: dict) -> bool:
    """Last-resort kill switch: on an 'ABORT' email, reset to a clean IDLE slate. True if so.

    Checked every tick regardless of state and mailbox-wide, so it works even when the agent
    is stuck waiting on a thread. A gmail hiccup here must not break the tick, so polling
    failures are swallowed.
    """
    try:
        polled = gmail_client.poll_abort()
    except Exception:
        log.exception("abort check could not poll gmail; skipping this tick")
        return False
    if polled is None:
        return False
    msg_id, _thread_id = polled
    # Consume the trigger first: the reset below is idempotent, but marking it processed up
    # front guarantees a failure afterwards can't loop us into re-aborting.
    gmail_client.mark_processed(msg_id)
    _abort_and_reset(state, "ABORT received: I stopped the task in progress and reset myself "
                            "to a clean slate.", new_thread=True)
    return True


def _handle_reply(state: dict, reply: str) -> None:
    if state["state"] == "WAIT_APPROVAL":
        do_approval_reply(state, reply)
    elif state["state"] == "WAIT_MERGE":
        do_merge_reply(state, reply)
    elif state["state"] == "WAIT_CLEAN":
        state["state"] = "IDLE"
    elif state["state"] == "WAIT_REPLY":
        # Read return_state without popping: if a later step here raises, the state
        # stays WAIT_REPLY with return_state intact so the retry re-runs cleanly.
        phase = state["return_state"]
        if phase in ("VERIFYING", "INTERNAL_REVIEW"):
            counter = "verify_round" if phase == "VERIFYING" else "review_gate_round"
            state[counter] = 0
            template = prompts.VERIFY if phase == "VERIFYING" else prompts.INTERNAL_REVIEW
            prompt = (f"User recovery guidance:\n{reply}\n\n" +
                      prompts.render(template, slug=state["slug"]))
            result = agent_runner.resume(state["session_id"], prompt)
            state.pop("return_state", None)
            if phase == "VERIFYING":
                _complete_verify(state, result)
            else:
                _complete_internal_review(state, result)
            return
        if phase == "ARCHIVING":
            result = agent_runner.resume(state["session_id"], prompts.render(
                prompts.FIX_ARCHIVE,
                error=state.get("archive_error", "unknown archival failure"),
                guidance=reply,
            ))
            if handle_result(state, result, "ARCHIVING"):
                return
            state["archive_round"] = 0
            state["state"] = "ARCHIVING"
            state.pop("return_state", None)
            return
        result = agent_runner.resume(state["session_id"], reply)
        if handle_result(state, result, phase):
            return  # re-questioned; handle_result reset return_state for the new phase
        next_state = {"EXPLORING": "PROPOSING", "PROPOSING": "WAIT_APPROVAL",
                      "IMPLEMENTING": "VERIFYING", "E2E": "E2E", "ADDRESS_REVIEW": "WAIT_REVIEW",
                      "ADDRESS_PR_THREADS": "WAIT_MERGE",
                      "APPLY_PR_FEEDBACK": "WAIT_MERGE"}[phase]
        if next_state == "WAIT_APPROVAL":
            email(state, "proposal for review",
                  f"{result.output}\n\nReply with your approval or requested changes.")
        if next_state == "WAIT_REVIEW":
            if _scrub_evidence_from_repo(state, phase) is None:
                return
            state.pop("return_state", None)
            _queue_push(state, "review")
            return
        elif phase == "ADDRESS_PR_THREADS":
            if _scrub_evidence_from_repo(state, phase) is None:
                return
            state.pop("return_state", None)
            _queue_push(state, "threads")
            return
        elif phase == "APPLY_PR_FEEDBACK":
            extra_attachments = _scrub_evidence_from_repo(state, phase)
            if extra_attachments is None:
                return
            attachments = _collect_attachments(
                result, state.get("e2e_specs", []), state.get("e2e_kind"))
            existing = {path.resolve() for path in attachments}
            attachments += [path for path in extra_attachments if path.resolve() not in existing]
            state.pop("return_state", None)
            _queue_push(state, "feedback", result.output, attachments)
            return
        else:
            if phase == "E2E":
                state["e2e_round"] = 0  # the user's guidance earns a fresh set of attempts
                if "e2e_repair_head" in state and "e2e_repair_status" in state:
                    _finish_e2e_repair(state)
                else:
                    state["state"] = "E2E"
            else:
                state["state"] = next_state
        state.pop("return_state", None)


def validate_managed_runtime() -> None:
    required = [
        ("Superpowers skill tree",
         config.SUPERPOWERS_PLUGIN_DIR / "skills/using-superpowers/SKILL.md"),
        ("bridge manifest", config.BRIDGE_PLUGIN_DIR / ".claude-plugin/plugin.json"),
        ("bridge skill",
         config.BRIDGE_PLUGIN_DIR / "skills/coderbot-openspec-workflow/SKILL.md"),
    ]
    if config.AGENT == "claude":
        required.append(("Superpowers Claude manifest",
                         config.SUPERPOWERS_PLUGIN_DIR / ".claude-plugin/plugin.json"))
    elif config.AGENT == "opencode":
        required.extend([
            ("Superpowers OpenCode entrypoint",
             config.SUPERPOWERS_PLUGIN_DIR / ".opencode/plugins/superpowers.js"),
            ("bridge OpenCode package", config.BRIDGE_PLUGIN_DIR / "package.json"),
            ("bridge OpenCode entrypoint",
             config.BRIDGE_PLUGIN_DIR / ".opencode/plugins/coderbot-openspec.js"),
        ])

    for label, path in required:
        if not path.is_file():
            raise SystemExit(f"managed runtime unavailable: missing {label} at {path}")


def main() -> None:
    if config.AGENT not in ("claude", "opencode"):
        raise SystemExit("CODEBOT_AGENT must be 'claude' or 'opencode'")
    if config.AGENT == "opencode" and (
            "/" not in config.OPENCODE_MODEL or config.OPENCODE_MODEL.endswith("/")):
        raise SystemExit("OPENCODE_MODEL must be set to provider/model when CODEBOT_AGENT=opencode")
    validate_managed_runtime()
    if not config.USER_EMAIL:
        raise SystemExit("CODEBOT_USER_EMAIL must be set in .env")
    if not config.DOC_ID:
        raise SystemExit("CODEBOT_DOC_ID must be set in .env (the backlog Google Doc id)")
    # .exists() not .is_dir(): in a git worktree .git is a file.
    if not (config.REPO_PATH / ".git").exists():
        raise SystemExit(
            f"CODEBOT_REPO_PATH ({config.REPO_PATH}) is not a git checkout; set it in .env")
    log.info("codebot starting; agent=%s repo=%s doc=%s", config.AGENT, config.REPO_PATH, config.DOC_ID)
    backoff = config.POLL_INTERVAL_SECONDS
    while True:
        try:
            state = load_state()
        except (json.JSONDecodeError, OSError):
            # Corrupt/unreadable state.json: don't crash the process — log and wait
            # so an operator can repair or delete the file.
            log.exception("could not read %s; retrying in %ss", config.STATE_PATH,
                          config.POLL_INTERVAL_SECONDS)
            time.sleep(config.POLL_INTERVAL_SECONDS)
            continue
        prev = state["state"]
        try:
            log.debug("tick: state=%s task=%r", prev, state.get("item", "-"))
            if check_abort(state):
                pass  # reset to IDLE; skip normal dispatch this tick
            elif state["state"] == "WAIT_REVIEW":
                handle_review_wait(state)
            elif state["state"] == "WAIT_MERGE":
                handle_merge_wait(state)
            elif state["state"] in WAITS:
                handle_wait(state)
            else:
                PHASES[state["state"]](state)
            if state["state"] != prev:
                log.info("state transition: %s -> %s", prev, state["state"])
            save_state(state)
            backoff = config.POLL_INTERVAL_SECONDS
        except Exception:
            log.exception("cycle failed; retrying in %ss", backoff)
            save_state(state)
            time.sleep(backoff)
            backoff = min(backoff * 2, 3600)
            continue
        if state["state"] in WAITS or state["state"] == "IDLE":
            time.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
