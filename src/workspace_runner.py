"""Feature coordinator for a writable, non-Git root containing independent repos.

No permission sandbox: the configured workspace is trusted and wholly writable.
Git publication is still scoped to explicit repositories and never resets user work.
"""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess

import config
import workspace
import workspace_state
from execution_identity import verification_snapshot as snapshot


def target():
    return workspace.discover(config.REPO_PATH, manifest_path=config.WORKSPACE_MANIFEST)


def command(repo, argv):
    process = subprocess.run(argv, cwd=repo, capture_output=True, text=True,
                             timeout=config.SUBPROCESS_TIMEOUT_SECONDS)
    if process.returncode:
        import diagnostics
        raise RuntimeError(diagnostics.redact(f"{argv[0]} failed in {repo}: "
                                               + process.stderr + process.stdout))
    return process.stdout.strip()


def git(repo, *args):
    return command(repo.path, ["git", *args])


def gh(repo, *args):
    if not repo.remote.startswith("github.com/"):
        raise workspace.WorkspaceError(f"PR publication requires a GitHub origin: {repo.relative_path}")
    return command(repo.path, ["gh", *args, "--repo", repo.remote.removeprefix("github.com/")])


def inspect_repositories(context):
    import repo_provenance
    return {repo.relative_path: {**repo_provenance.inspect(repo.path),
            "branch": git(repo, "branch", "--show-current")} for repo in context.repositories}


def preflight(context):
    # Existing work is not permission-protected, but cannot be attributed to the bot.
    # Capture/check before letting the agent write anywhere in the workspace.
    inventory = inspect_repositories(context)
    dirty = [name for name, value in inventory.items() if value["files"] or value["operations"]]
    if dirty:
        raise workspace.WorkspaceError("Existing repository work must be saved before starting a workspace task: "
                                       + ", ".join(dirty))
    return inventory


def bind(state, context):
    validated = workspace_state.bind_and_validate(state, context)
    state.clear()
    state.update(validated)


def record_changes(state, context, before):
    """Autonomously track every changed repository, including late discoveries."""
    after = inspect_repositories(context)
    records = state["workspace"]["repositories"]
    baseline = state["workspace"].get("baseline", {})
    for repo in context.repositories:
        name = repo.relative_path
        old = before.get(name)
        if old is None:
            raise workspace.WorkspaceError(f"New repository appeared during a turn: {name}; inspect before continuing")
        changed = after[name]["fingerprint"] != old["fingerprint"] or after[name]["branch"] != old["branch"]
        if changed and name not in records:
            validated = workspace_state.repository_record(state, context, name, reason="Detected feature edits")
            state["workspace"] = validated["workspace"]
            records = state["workspace"]["repositories"]
            base = workspace.git(repo.path, "rev-parse", "--verify",
                                 f"refs/remotes/origin/{repo.base_branch}^{{commit}}", required=False)
            records[name].update(base_sha=base or baseline.get(name, old)["head"],
                                 branch=after[name]["branch"], state="IMPLEMENTING")
        if name in records:
            records[name]["last_head"] = after[name]["head"]
            records[name]["last_fingerprint"] = after[name]["fingerprint"]
    return after


def instructions(state, context):
    import prompts
    branches = {repo.relative_path: {"base": repo.base_branch,
                "branch": state["workspace"]["repositories"].get(repo.relative_path, {}).get("branch")
                          or state["branch"]} for repo in context.repositories}
    return ("\nWORKSPACE EXECUTION\nThis root is NOT a Git repository. All files and child repositories "
            "within it are writable; there is no repository-selection permission gate. Discover the relevant "
            "subset yourself and leave unrelated work alone. Read workspace and applicable child AGENTS.md. "
            "Never git init this root. Run every Git command in the appropriate child repository. "
            "Before editing a child, checkout/create its feature branch from origin/<base> using the mapping below; preserve "
            "existing work and never reset/clean/force-push. Do not push, create PRs, or merge: the controller "
            "publishes all changed repositories. Shared root files remain local. Do not commit evidence or secrets. "
            "Use the root openspec/ for the shared feature plan; include repository paths in tasks. "
            "Adding another repository does not require selection approval. Honor the current functional phase.\n"
            + json.dumps(branches, sort_keys=True)
            + "\n" + prompts.MINIMAL_CHANGE_POLICY + prompts.TESTING_POLICY
            + "\nFor verification emit CHECK_PLAN with explicit commands and workspace-relative cwd; "
              "include repository checks AND a root integration/E2E check. Never invent passing evidence. "
              "Do not run development-stack cleanup or prune shared Docker resources.\n")


def _shared_files(context):
    """Only eligible local source; do not copy credentials or generated outputs."""
    import fnmatch
    import diagnostics
    result = {}
    repo_roots = {repo.path for repo in context.repositories}
    def walk(directory):
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or path.name in workspace.EXCLUDED_DIRS | {"data", "output", ".DS_Store"}:
                continue
            if path in repo_roots:
                continue
            if path.is_dir():
                walk(path)
                continue
            relative = path.relative_to(context.root).as_posix()
            if not any(fnmatch.fnmatch(relative, pattern) or relative.startswith(pattern.rstrip("/") + "/")
                       for pattern in context.manifest.shared_inputs):
                continue
            if (path.name == ".env" or path.name.startswith(".env.") and not path.name.endswith("example")
                    or path.suffix in {".pem", ".key", ".mp4", ".webm"}):
                continue
            try:
                text = path.read_text()
            except (UnicodeError, OSError):
                continue
            if diagnostics.redact(text) != text:
                continue
            result[relative] = {"text": text, "mode": path.stat().st_mode & 0o777,
                                "hash": hashlib.sha256(text.encode()).hexdigest()}
    walk(context.root)
    return result


def journal(state, context, before, after):
    edits = state["workspace"].setdefault("local_edits", [])
    folder = config.DATA_DIR / "workspace-edits" / state["execution_task_id"]
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = hashlib.sha256((path + json.dumps(after.get(path))).encode()).hexdigest()
        destination = folder / (key + ".json")
        if not destination.exists():
            # Private journals are never attachments or PR content.
            with destination.open("x") as output:
                destination.chmod(0o600)
                json.dump({"path": path, "before": before.get(path), "after": after.get(path)}, output)
        if not any(entry["id"] == key for entry in edits):
            edits.append({"id": key, "path": path, "journal": str(destination)})


def restore_local(context, journal_path):
    """Explicit recovery only, refusing divergent user changes."""
    value = json.loads(Path(journal_path).read_text())
    path = workspace.contained(context.root, value["path"])
    current = _shared_files(context).get(value["path"])
    if current != value["after"]:
        raise workspace.WorkspaceError("Local recovery would overwrite changed user content: " + value["path"])
    original = value["before"]
    if original is None:
        path.unlink()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(original["text"])
        path.chmod(original["mode"])


def turn(state, prompt, *, fresh=False):
    import agent_runner
    import main
    context = target()
    bind(state, context)
    before = inspect_repositories(context)
    initial = state["workspace"].get("baseline", {})
    for name, entry in before.items():
        if name not in state["workspace"]["repositories"] and name in initial:
            if entry["fingerprint"] != initial[name]["fingerprint"]:
                raise workspace.WorkspaceError(f"Unattributed changes in {name}; save user work before continuing")
    shared = _shared_files(context)
    agent_runner.set_task_context(state)
    try:
        prompt += instructions(state, context)
        result = (agent_runner.run(prompt) if fresh or not state.get("session_id")
                  else agent_runner.resume(state["session_id"], prompt))
    finally:
        # Keep partial edits attributed even after interruption; do not discard them.
        record_changes(state, context, before)
        shared_after = _shared_files(context)
        # An existing file newly excluded as sensitive is not a deletion: never
        # retain its former contents as a publicly reportable bot edit.
        shared = {name: value for name, value in shared.items()
                  if name in shared_after or not (context.root / name).exists()}
        journal(state, context, shared, shared_after)
        main.save_state(state)
    state["session_id"] = result.session_id
    state["workspace"]["session_id"] = result.session_id
    state["workspace"]["attachments"] = list(dict.fromkeys([
        *state["workspace"].get("attachments", []), *getattr(result, "attachments", [])]))
    return result


def pick(state):
    import main
    context = target()
    requested = sorted((hold for hold in main._load_holds() if hold.get("requested")),
                       key=lambda hold: hold.get("requested_at", 0))
    if requested:
        main._resume_held_task(state, requested[0])
        return
    baseline = preflight(context)
    items = main.task_source.list_pending_items()
    mine = [item for item in items if item.get("claimed_by_me")]
    items = mine or main.prioritize_items(items)
    if not items:
        return
    if len(items) == 1:
        chosen = items[0]
        slug = re.sub(r"[^a-z0-9]+", "-", chosen["text"].casefold()).strip("-")[:60] or "task"
    else:
        choice = main.parse_json_reply(main.agent_runner.run(main.prompts.render(
            main.prompts.PICK, project=config.PROJECT_NAME, items=main.render_items(items)), contract=False).output)
        chosen = next((item for item in items if main.task_source.normalize(item["text"])
                       == main.task_source.normalize(choice.get("item", ""))), None)
        if chosen is None:
            raise ValueError("PICK chose something that is not a pending backlog item")
        slug = main._validated_slug(choice.get("slug"))
    if not main.task_source.claim_task(chosen["text"], chosen.get("id")):
        return
    state.pop("workspace", None)
    state["state"] = "IDLE"
    bind(state, context)
    state.update(item=chosen["text"], item_detail=chosen.get("detail", ""),
                 item_images=chosen.get("images", []), item_id=chosen.get("id"),
                 item_url=chosen.get("url"), item_key=chosen.get("key"), slug=slug,
                 branch=f"{config.INSTANCE_ID}-{slug}", state="EXPLORING", thread_id=None)
    state["workspace"]["baseline"] = baseline
    main.task_records.migrate(state, main.phase_checkpoint.store(), context.root)
    main._ensure_task_language(state)
    main.save_state(state)
    if config.COMM_CHANNEL == "slack":
        state["thread_nonce"] = secrets.token_hex(12)
        main.save_state(state)
        state["thread_id"] = main.gmail_client.open_thread(state)
        main.save_state(state)
    main.announce_milestone(state, "exploring", "I'm exploring the feature workspace.")


def run_phase(state):
    import main
    phase = state["state"]
    if phase == "IDLE":
        return pick(state)
    if phase == "EXPLORING":
        prompt = main.prompts.render(main.prompts.EXPLORE, project=config.PROJECT_NAME,
            branch=state["branch"], item=state["item"], detail=state.get("item_detail", ""),
            images=main.render_images(state.get("item_images", []))) + "\n" + state.get("pending_feedback", "")
        result = turn(state, prompt, fresh=True)
        if not main.handle_result(state, result, phase):
            state["state"] = "PROPOSING"
        return
    if phase == "PROPOSING":
        result = turn(state, main.prompts.render(main.prompts.PROPOSE, slug=state["slug"],
                      e2e_note="Add checks for affected repos and shared workspace integration/E2E tests.")
                      + "\n" + state.get("pending_feedback", ""))
        if not main.handle_result(state, result, phase):
            main._send_proposal_review(state, result.output)
        return
    if phase == "REPLANNING":
        result = turn(state, main.replanning.prompt(state, config.REPO_PATH))
        if not main.handle_result(state, result, phase):
            main._send_proposal_review(state, result.output, revised=True,
                                       feedback=state.get("replan", {}).get("feedback", ""))
        return
    if phase == "IMPLEMENTING":
        if main._scope_drift(state):
            return
        result = turn(state, main.prompts.render(main.prompts.IMPLEMENT, slug=state["slug"],
            branch=state["branch"], e2e_note="Implement tests in each affected repo plus shared E2E tests.",
            e2e_report_note="Emit CHECK_PLAN for actual repo and integration commands.")
            + "\n" + state.get("implementation_feedback", ""))
        if not main.handle_result(state, result, phase):
            complete_implementation(state, result)
        return
    if phase in ("VERIFYING", "INTERNAL_REVIEW", "E2E", "REPAIR_CHECKS", "EVIDENCE_REVIEW"):
        return verify(state)
    if phase == "ARCHIVING":
        return archive(state)
    if phase in ("OPEN_PR", "PUSHING"):
        return publish(state)
    if phase in ("WAIT_REVIEW", "WAIT_MERGE"):
        return poll_prs(state)
    if phase in ("ADDRESS_REVIEW", "ADDRESS_PR_THREADS", "RESOLVE_CONFLICTS", "APPLY_FEEDBACK", "RECOVERING"):
        result = turn(state, "Address the recorded feedback/recovery below across relevant repositories. "
            "Preserve user changes; resolve Git operations safely without resets or force push. "
            "Emit CHECK_PLAN and RESOLVE lines with truthful fixes.\n" + json.dumps({
                "feedback": state.get("pending_feedback", ""), "review": state.get("workspace_review", {}),
                "recovery": state.get("stuck_error", ""), "phase": phase}))
        if not main.handle_result(state, result, phase):
            remember_checks(state, result.output)
            state["workspace"]["review_output"] = result.output
            state["state"] = "VERIFYING"
        return
    if phase == "WAIT_CLEAN":
        preflight(target())
        state["state"] = "IDLE"
        return
    return main.handle_wait(state)


def remember_checks(state, output):
    import check_plan
    check_plan.remember(state, output)


def complete_implementation(state, result):
    import main
    remember_checks(state, result.output)
    plan = main.task_phases.inspect(config.REPO_PATH, state["slug"])
    state["implementation_summary"] = result.output[:3000]
    if plan["approval"]:
        main._verification_blocked(state, "approval", "\n".join(plan["approval"]))
    elif plan["implementation"]:
        if not verification_attempt(state, "implementation", "Approved implementation tasks remain incomplete"):
            return
        state["implementation_feedback"] = "Complete pending approved tasks:\n" + "\n".join(plan["implementation"])
        state["state"] = "IMPLEMENTING"
    else:
        state["workspace"].get("verification_rounds", {}).pop("implementation", None)
        state.pop("implementation_feedback", None)
        state["state"] = "VERIFYING"


def check_plan_for(state, context):
    import check_plan
    plan = []
    for name in state["workspace"]["repositories"]:
        repo = context.repository(name)
        for check in check_plan.discover(repo.path):
            plan.append(replace(check, id=name + ":" + check.id,
                                cwd=(Path(name) / check.cwd).as_posix(),
                                inputs=[(Path(name) / path).as_posix() for path in check.inputs]))
    plan.extend(check_plan.discover(context.root))
    for entry in json.loads(context.manifest.checks_json):
        plan.append(check_plan.Check(**{k: v for k, v in entry.items() if k != "repositories"}))
    plan.extend(check_plan.Check(**entry) for entry in state.get("reported_check_plan", []))
    unique = {}
    for check in plan:
        workspace.contained(context.root, check.cwd)
        previous = unique.get(check.id)
        if previous:
            check = replace(check, resources=sorted(set(previous.resources) | set(check.resources)),
                            timeout=max(previous.timeout, check.timeout))
        unique[check.id] = check
    return check_plan.normalize(list(unique.values()), context.root)


def verify(state):
    import main
    import checks
    context = target()
    if not state.get("archive_path") and main._verify_preflight(state):
        return
    if state.get("reported_check_plan_error"):
        attempt = state.get("check_plan_repair_round", 0) + 1
        if attempt > config.QUALITY_GATE_MAX_ROUNDS:
            main._enter_stuck(state, "VERIFYING", state["reported_check_plan_error"]
                              + "\nCheck-plan repair exhausted; no malformed plan was executed.")
            return
        result = turn(state, f"Check-plan repair attempt {attempt}/{config.QUALITY_GATE_MAX_ROUNDS}; "
            f"recovery revision {state.get('check_plan_repair_revision', 0)}.\n"
            "Correct only the malformed check contract; do not repeat exploration or implementation. "
            "Emit CHECK_PLAN: followed by a valid JSON object with version 1 and a checks array. "
            "Use id, argv as an array of strings, workspace-relative cwd, and timeout in seconds. "
            'Example: CHECK_PLAN: {"version":1,"checks":[{"id":"actual-check-id","argv":["tool","argument"],"cwd":".","timeout":900}]}\n'
            "Kinds are auto, unit, functional or e2e; scopes are full or focused. Preserve registered checks and "
            "actual commands/selections. Do not run full suites or claim verification passed.\n"
            + main.prompts.fenced("check-plan validation error", state["reported_check_plan_error"])
            + ("\n" + main.prompts.fenced_authoritative("user recovery guidance", state["pending_feedback"])
               if state.get("pending_feedback") else ""))
        if main.handle_result(state, result, "VERIFYING"):
            return
        if state.get("reported_check_plan_error"):
            state["check_plan_repair_round"] = attempt
            main.save_state(state)
            if state["check_plan_repair_round"] >= config.QUALITY_GATE_MAX_ROUNDS:
                main._enter_stuck(state, "VERIFYING", state["reported_check_plan_error"] + "\n"
                    f"The agent could not produce a valid check plan after {attempt} completed repair attempts; no malformed plan was executed.")
            return
        state.pop("check_plan_repair_round", None)
    if (state.get("final_check_report", {}).get("stage") == "full_e2e_budget_exhausted"
            and not state.get("focused_scope_reconciled")):
        attempt = state["workspace"].get("verification_rounds", {}).get("focused_scope", 0) + 1
        if attempt > config.QUALITY_GATE_MAX_ROUNDS:
            main._enter_stuck(state, "VERIFYING", "Focused-plan reconciliation is still missing after bounded recovery; "
                              "preserve the task and historical audit evidence.")
            return
        result = turn(state, main.prompts.TESTING_POLICY + f"\nFocused-plan recovery attempt {attempt}. "
            "Recover this SAME task from the legacy full-audit gate. "
            "Inspect the actual diff across affected repositories and shared/local harness files. "
            "Emit a CHECK_PLAN with genuinely selected E2E cases covering changed behavior AND changed harness paths; "
            "retain required unit/functional checks. Do not repeat the broad-failure investigation, run a full suite, "
            "erase historical registrations/evidence, reset the audit budget, or expand product scope. "
            "Legacy checklist duties to repeat a full audit are superseded by focused verification; reconcile their "
            "verification purpose honestly without claiming the full audit passed or completing missing implementation. "
            "Preserve approvals and user work. No DONE, publication or merge is authorized by this recovery.\n"
            + main.prompts.fenced_authoritative("existing task guidance", state.get("pending_feedback", "")))
        if main.handle_result(state, result, "VERIFYING"):
            return
        remember_checks(state, result.output)
        state["workspace"].setdefault("verification_rounds", {})["focused_scope"] = attempt
        if (state.get("reported_check_plan_error") or not any(
                main.check_plan.is_e2e(c, context.root) and main.check_plan.has_e2e_selection(c)
                for c in main.check_plan.reported(result.output))):
            main.save_state(state)
            return
        state["focused_scope_reconciled"] = True
        main.save_state(state)
        return
    plan = check_plan_for(state, context)
    if not plan or not any(check.cwd in (".", "e2e") for check in plan):
        if not verification_attempt(state, "shared_plan", "No runnable shared integration check was supplied"):
            return
        result = turn(state, "Inspect the whole feature and run cross-repository verification. "
            "Emit CHECK_PLAN for each affected repository and a root/shared integration or E2E check. "
            "Include runnable commands and workspace-relative cwd. Do not claim checks passed without running them. "
            "Fix discovered problems within the approved feature.")
        if main.handle_result(state, result, "VERIFYING"):
            return
        remember_checks(state, result.output)
        plan = check_plan_for(state, context)
    if not plan or not any(check.cwd in (".", "e2e") for check in plan):
        raise workspace.WorkspaceError("Workspace verification requires a shared integration check; emit CHECK_PLAN or configure a manifest")
    from workspace_checks import HarnessBlocked
    try:
        for check in main.check_plan.feature_checks(plan, context.root):
            validate_check(context, check)
    except HarnessBlocked as error:
        state["workspace"]["harness_repair_round"] = state["workspace"].get("harness_repair_round", 0) + 1
        if state["workspace"]["harness_repair_round"] > config.QUALITY_GATE_MAX_ROUNDS:
            main._enter_stuck(state, "VERIFYING", "Workspace harness still needs repair: " + str(error))
            return
        result = turn(state, "Repair the shared local test harness/configuration, not application scope. "
            f"Repair attempt {state['workspace']['harness_repair_round']}: re-evaluate the current blocker; "
            "a previous repair response did not resolve it. "
            "Keep all required repository and integration tests; do not skip them. Use an explicit dedicated "
            "Compose test project with only project-scoped volumes/networks, no external/shared cleanup or prune. "
            "Guard macOS report opening on Linux. Reuse local Docker socket paths; configure host_root only for remote Docker. "
            "Do not execute cleanup until the controller validates it. Emit the corrected CHECK_PLAN.\nBlocker: " + str(error))
        if not main.handle_result(state, result, "VERIFYING"):
            remember_checks(state, result.output)
            state["state"] = "VERIFYING"
        return
    state["workspace"].pop("harness_repair_round", None)
    before = snapshot(context.root)
    import staged_checks
    store = main.phase_checkpoint.store()
    staged = staged_checks.run(state, plan, context.root, store,
        evaluate=lambda c, r: workspace_gate(c, r, state, context, store))
    outcomes = staged["checks"]
    if staged["stage"] in ("needs_focused_e2e", "full_e2e_budget_exhausted"):
        state["final_check_report"] = {"status": "indeterminate", **staged}
        if staged["stage"] == "full_e2e_budget_exhausted":
            main._enter_stuck(state, "VERIFYING", "The two full E2E regression audits have been consumed. "
                "Focused feature evidence is preserved; the broad audit is not green. "
                "Do not repeat an already answered scope decision or expand unrelated product functionality. "
                "Recorded audit failures require attribution/repair before publication, not another identical full run.")
            return
        if not verification_attempt(state, "focused_selection", "A genuinely selected focused E2E check is still missing"):
            return
        result = turn(state, main.prompts.TESTING_POLICY + "\nController stage: " + staged["stage"] +
            "\nProvide explicit focused E2E selections for the approved feature with kind=e2e and scope=focused. "
            "Declare a structured reporter and prove a positive executed-test count; a filename/zero exit alone is insufficient. "
            "Do not run or re-register the full suite to evade its two-run budget. Preserve all full-suite tests. "
            "Repair the named registrations below using the SAME IDs; adding a new focused ID alone does not fix "
            "an old unfiltered entry still labeled focused. Give a broad command scope=full, or add real case selections; "
            "do not execute the full suite yourself. Shell syntax validation is a cheap check, not browser evidence. "
            "Do not repeat already answered scope questions. Report existing regression-audit failures separately; "
            "do not claim a green audit or expand product scope. Emit CHECK_PLAN for cheap/focused diagnostics.\n"
            + main.prompts.fenced("controller selection defects", json.dumps(staged.get("selection_issues", [])))
            + ("\n" + main.prompts.fenced_authoritative("user recovery guidance", state["pending_feedback"])
               if state.get("pending_feedback") else ""))
        main.handle_result(state, result, "VERIFYING")
        return
    state["workspace"].get("verification_rounds", {}).pop("focused_selection", None)
    failed = main.check_repair.unresolved({"checks": outcomes})
    after = snapshot(context.root)
    state["final_check_report"] = main.final_checks.report(staged, context.root)
    state["final_check_report"].update(source_changed=before != after)
    if before != after:
        state["final_check_report"]["status"] = "indeterminate"
    if failed:
        if not verification_attempt(state, "failures", "Verification remains unresolved: " + json.dumps(failed)):
            return
        result = turn(state, main.prompts.TESTING_POLICY + "\nFix these verification failures within the approved feature; do not hide or skip tests.\n"
                        "The controller deduplicates identical checks, locks shared E2E execution, and manages bounded timeouts. "
                        "Do not ask the user to configure the controller or approve a timeout increase. "
                        "Emit an explicit timeout in CHECK_PLAN when more time is justified. "
                        "Diagnose authentication/fixture and endpoint failures before claiming "
                       "they are outside scope. Ask only a concrete product-scope or missing-secret decision, "
                       "with numbered options and consequences.\n"
                       + ("Source also changed during checks; their evidence is stale and verification must run again after repairs.\n"
                          if before != after else "")
                       + json.dumps(failed))
        if not main.handle_result(state, result, "VERIFYING"):
            remember_checks(state, result.output)
            state["state"] = "VERIFYING"
        return
    if before != after:
        raise workspace.WorkspaceError("Source changed during workspace checks; verification must run again")
    if staged["stage"] != "complete":
        # No prefix of successful checks can stand in for the remaining stages.
        verification_attempt(state, "incomplete_stage", "Required stage remains incomplete: " + staged["stage"])
        return
    path = context.root / "openspec/changes" / state["slug"] / "tasks.md"
    if not state.get("archive_path") and path.is_file():
        pending = main.task_phases.inspect(context.root, state["slug"])
        if pending["implementation"] or pending["verification"]:
            if not verification_attempt(state, "tasks", "Approved implementation/evidence tasks remain unresolved"):
                return
            result = turn(state, main.prompts.TESTING_POLICY + "\nComplete/reconcile these approved tasks using real focused evidence. "
                "Legacy full-audit repetition duties are superseded; explain that without claiming a green full suite. "
                "Do not mark missing evidence complete, expand scope, or rerun full E2E.\n" + json.dumps(pending))
            main.handle_result(state, result, "VERIFYING")
            return
    state["workspace"]["verified_snapshot"] = before
    state["workspace"]["verification_repositories"] = [repo.relative_path for repo in context.repositories]
    if state["workspace"].get("reviewed_snapshot") != before:
        result = turn(state, "Independently review this completed cross-repository feature for critical/important "
            "bugs and contract mismatches. Check shared requirements and implementation across affected repos. "
            "Do not edit code, push or merge. End with exactly INTERNAL_REVIEW: "
            '{"status":"pass","critical":[],"important":[],"tests":["actual verification references"]} '
            "only if no critical or important issues remain; otherwise report status fail and real issues.", fresh=True)
        if main.handle_result(state, result, "INTERNAL_REVIEW"):
            return
        review, reason = main.parse_internal_review(result.output)
        if reason:
            state["pending_feedback"] = "Fix internal review findings:\n" + result.output
            state["state"] = "APPLY_FEEDBACK"
            return
        if snapshot(context.root) != before:
            state["state"] = "VERIFYING"
            return
        state["workspace"]["reviewed_snapshot"] = before
    state["state"] = "ARCHIVING" if not state.get("archive_path") else "OPEN_PR"
    state["workspace"].pop("verification_rounds", None)


def verification_attempt(state, kind, detail):
    """Bound successful ticks that make no verified progress, including replayed turns."""
    import main
    rounds = state["workspace"].setdefault("verification_rounds", {})
    rounds[kind] = rounds.get(kind, 0) + 1
    if rounds[kind] > config.QUALITY_GATE_MAX_ROUNDS:
        main._enter_stuck(state, state["state"], detail + "; bounded automatic recovery exhausted. Work and evidence are preserved.")
        return False
    return True


def workspace_gate(check, result, state, context, store):
    """Repository-local attribution; composite checks need immutable shared evidence."""
    import main
    for repo in context.repositories:
        relative = Path(check.cwd)
        if relative == Path(repo.relative_path) or Path(repo.relative_path) in relative.parents:
            record = state["workspace"]["repositories"].get(repo.relative_path, {})
            base = record.get("base_sha") or state["workspace"].get("baseline", {}).get(repo.relative_path, {}).get("head")
            # Default workspace inputs include other repositories, so do not claim
            # a single-repository baseline establishes a composite check's provenance.
            if (check.inputs and all(p == repo.relative_path or p.startswith(repo.relative_path + "/") for p in check.inputs)
                    and (check.dependency_inputs is None or all(p == repo.relative_path or p.startswith(repo.relative_path + "/")
                                                              for p in check.dependency_inputs))):
                local = replace(check, cwd=relative.relative_to(repo.relative_path).as_posix(),
                    inputs=[Path(p).relative_to(repo.relative_path).as_posix() for p in check.inputs],
                    dependency_inputs=[Path(p).relative_to(repo.relative_path).as_posix() for p in check.dependency_inputs]
                                      if check.dependency_inputs is not None else None)
                return main.final_checks.gate(local, result, {**state, "base_sha": base}, repo.path, store)
    value = main.final_checks.gate(check, result, {**state, "base_sha": None}, context.root, store)
    if result["status"] == "fail":
        value["reason"] = "immutable_composite_baseline_missing"
    return value


def validate_check(context, check):
    """Allow cleanup only after validating an explicit dedicated test project."""
    from workspace_checks import HarnessBlocked, validate_cleanup, validate_docker
    cwd = workspace.contained(context.root, check.cwd)
    text = " ".join(check.argv)
    visited = set()
    scripts = []
    def script(path):
        nonlocal text
        if path in visited or not path.is_file() or path.suffix not in (".sh", ".py", ".js", ".mjs"):
            return
        visited.add(path)
        body = path.read_text()
        scripts.append(body)
        text += "\n" + body
        # Shell harnesses commonly delegate cleanup to a sibling/root script.
        for name in re.findall(r"(?:\$ROOT/|\$\{ROOT\}/|\$root/|\$\{root\}/|\./)([A-Za-z0-9_./-]+\.sh)", body):
            for root in (context.root, path.parent):
                candidate = workspace.contained(context.root, root / name)
                script(candidate)
    for arg in check.argv:
        if arg.startswith("-"):
            continue
        candidate = cwd / arg
        if candidate.exists() and candidate.is_relative_to(context.root):
            script(candidate)
    if re.search(r"\b(?:system|volume|container|network)\s+prune\b|\bvolume\s+rm\b", text):
        raise HarnessBlocked(f"Check {check.id} performs global destructive Docker cleanup")
    if re.search(r"\bdown\b[^\n]*(?:--volumes|-v\b)", text):
        validate_cleanup(context, [*scripts, " ".join(check.argv)], command)
    if "docker" in text:
        validate_docker(context, command)
    if re.search(r"^\s*open\s+", text, re.MULTILINE) and not (
            "uname" in text and "Darwin" in text and "command -v open" in text):
        raise HarnessBlocked(f"Check {check.id} calls macOS open without a platform guard")


def archive(state):
    import main
    context = target()
    if state["workspace"].get("verified_snapshot") != snapshot(context.root):
        state["state"] = "VERIFYING"
        return
    active = context.root / "openspec/changes" / main._validated_slug(state["slug"])
    if active.exists():
        # Shared plan files are local: there is no Git clean/commit gate here.
        main.task_phases.complete_final_checks(context.root, state["slug"])
        text = (active / "tasks.md").read_text()
        from task_phases import TASK, VERIFICATION
        (active / "tasks.md").write_text(TASK.sub(lambda task: task[1] + "x" + task[3] + task[4]
            if VERIFICATION in task[4] else task[0], text))
        command(context.root, ["openspec", "archive", state["slug"], "-y", "--json"])
    matches = sorted((context.root / "openspec/changes/archive").glob("*-" + state["slug"]))
    if len(matches) != 1:
        raise workspace.WorkspaceError("Cannot identify the shared local feature archive")
    state["archive_path"] = matches[0].relative_to(context.root).as_posix()
    # Planning is excluded from the implementation identity. Publication still
    # verifies the actual source identity and refuses any concurrent code change.
    state["state"] = "OPEN_PR"


def _validate_pr(repo, record, value):
    expected = "https://github.com/" + repo.remote.removeprefix("github.com/") + "/pull/"
    if (not isinstance(value, dict) or not isinstance(value.get("url"), str)
            or not re.fullmatch(re.escape(expected) + r"\d+", value["url"])
            or value.get("headRefName") != record["branch"] or value.get("baseRefName") != repo.base_branch
            or value.get("isCrossRepository") is not False):
        raise workspace.WorkspaceError("PR does not match the expected repository/branch: " + repo.relative_path)


def status_body(state):
    records = state["workspace"]["repositories"]
    lines = ["Feature: " + state.get("item", "")]
    for name, record in records.items():
        if record.get("pr_url"):
            lines.append(f"- {name}: {record['pr_url']} ({record.get('merge', {}).get('status', 'OPEN')})")
    edits = sorted({entry["path"] for entry in state["workspace"].get("local_edits", [])})
    if edits:
        lines += ["", "Local-only workspace changes (NOT delivered by merging PRs):", *["- " + p for p in edits]]
    lines += ["", "Each repository PR is independent; the feature completes only after all required merges and integration checks."]
    return "\n".join(lines)


def publish(state):
    import main
    context = target()
    if main._scope_drift(state):
        return
    if main._conversation_delivery_blocked(state):
        return
    bind(state, context)
    if state["workspace"].get("verified_snapshot") != snapshot(context.root):
        state["state"] = "VERIFYING"
        return
    records = state["workspace"]["repositories"]
    for name, record in records.items():
        repo = context.repository(name)
        if record.get("merge", {}).get("status") == "MERGED":
            continue
        current = git(repo, "branch", "--show-current")
        branch = record.get("branch") or state["branch"]
        if current == repo.base_branch or current != branch:
            # Move committed feature work to a feature branch, never reset the base.
            if git(repo, "status", "--porcelain", "--untracked-files=no"):
                raise workspace.WorkspaceError(f"{name}: commit feature changes on its feature branch before publication")
            if current == repo.base_branch and not record.get("pr_url"):
                branch = state["branch"]
                git(repo, "checkout", "-b", branch)
            else:
                raise workspace.WorkspaceError(f"{name}: expected feature branch {branch}, found {current}")
        record["branch"] = branch
        if git(repo, "status", "--porcelain", "--untracked-files=no"):
            result = turn(state, f"Commit completed feature changes in {name} on {branch}, excluding evidence/secrets. "
                          "Do not push; leave unrelated changes alone.")
            if main.handle_result(state, result, "OPEN_PR"):
                return
            state["state"] = "VERIFYING"
            return
        if not git(repo, "diff", "--name-only", record["base_sha"], "HEAD"):
            record["state"] = "NO_CHANGES"
            continue
        git(repo, "push", "-u", "origin", "HEAD:refs/heads/" + branch)
        fields = "url,state,headRefName,baseRefName,headRefOid,isCrossRepository"
        if record.get("pr_url"):
            value = json.loads(gh(repo, "pr", "view", record["pr_url"], "--json", fields))
        else:
            listed = json.loads(gh(repo, "pr", "list", "--state", "all", "--head", branch,
                                   "--base", repo.base_branch, "--json", fields, "--limit", "2"))
            if not isinstance(listed, list) or len(listed) > 1:
                raise workspace.WorkspaceError(f"Ambiguous PR publication for {name}")
            if listed:
                value = listed[0]
            else:
                body = (f"Feature: {state['item']}\nSource: {state.get('item_url') or 'backlog'}\n"
                        f"Repository: {name}\nShared local plan: {state.get('archive_path', state['slug'])}\n\n"
                        + state.get("implementation_summary", "") + "\n\n" + status_body(state))
                url = gh(repo, "pr", "create", "--base", repo.base_branch, "--head", branch,
                         "--title", main._pr_title(state), "--body", body)
                # Persist accepted publication before any further call or notification.
                record["pr_url"] = url
                main.save_state(state)
                value = json.loads(gh(repo, "pr", "view", url, "--json", fields))
        _validate_pr(repo, record, value)
        if value["state"] == "CLOSED":
            raise workspace.WorkspaceError(f"{name}: feature PR was closed without merging")
        if value["state"] == "OPEN" and value["headRefOid"] != git(repo, "rev-parse", "HEAD"):
            raise workspace.WorkspaceError(f"{name}: published PR head does not match tested content")
        record.update(pr_url=value["url"], delivered_sha=value["headRefOid"], state="WAIT_MERGE")
        record["merge"] = {"status": value["state"]}
        main.save_state(state)
        if not record.get("announced"):
            reports = [entry["report"] for entry in state.get("final_check_report", {}).get("checks", [])
                       if entry.get("report") and Path(entry["report"]).is_file()]
            attachments = [Path(path) for path in dict.fromkeys([
                *reports, *state["workspace"].get("attachments", [])]) if Path(path).is_file()]
            main.email(state, "PR opened: " + name, status_body(state), attachments)
            if state.get("last_delivery", {}).get("complete") is not False:
                record["announced"] = True
                main.save_state(state)
            try:
                main.task_source.note_pr(state["item"], state.get("item_id"), record["pr_url"])
            except Exception:
                main.log.exception("Could not link workspace PR on backlog")
    review = state.get("workspace_review", {})
    output = state["workspace"].pop("review_output", None)
    if output and review.get("pr_url"):
        reviewed = {"pr_url": review["pr_url"], "pr_threads": review.get("threads", [])}
        main._resolve_review_threads(reviewed, output, key="pr_threads")
        name = review["repository"]
        records[name].setdefault("review", {})["seen"] = reviewed.get("pr_threads_seen", {})
        state.pop("workspace_review", None)
    state["state"] = "WAIT_MERGE"
    if not any(record.get("pr_url") for record in records.values()):
        main.email(state, "local workspace changes verified", status_body(state))
        if state.get("last_delivery", {}).get("complete") is not False:
            main._finish_task(state, "Verified local-only workspace changes.\n" + status_body(state), reset_repo=False)


def poll_prs(state):
    import main
    context = target()
    records = state["workspace"]["repositories"]
    pending = False
    for name, record in records.items():
        if not record.get("pr_url"):
            continue
        repo = context.repository(name)
        value = json.loads(gh(repo, "pr", "view", record["pr_url"], "--json",
                            "url,state,mergeable,headRefName,baseRefName,isCrossRepository,headRefOid"))
        _validate_pr(repo, record, value)
        if value.get("headRefOid") != record.get("delivered_sha"):
            raise workspace.WorkspaceError(f"{name}: PR changed after verified publication; reconcile and reverify before completing")
        record["merge"] = {"status": value["state"], "mergeable": value.get("mergeable")}
        if value["state"] == "CLOSED":
            raise workspace.WorkspaceError(f"{name}: PR closed without merging; feature remains incomplete")
        if value["state"] == "MERGED":
            continue
        pending = True
        if value.get("mergeable") == "CONFLICTING":
            state["workspace_review"] = {"repository": name, "pr_url": record["pr_url"],
                                         "conflict": repo.base_branch}
            state["state"] = "RESOLVE_CONFLICTS"
            return
        threads = main.unresolved_review_threads(record["pr_url"])
        if threads is None:
            continue
        seen = record.setdefault("review", {}).get("seen", {})
        fresh = [thread for thread in threads if main.thread_has_new_comments(thread, seen)]
        if fresh and record["review"].get("rounds", 0) < config.PR_THREAD_MAX_ROUNDS:
            state["workspace_review"] = {"repository": name, "pr_url": record["pr_url"], "threads": fresh}
            record["review"]["rounds"] = record["review"].get("rounds", 0) + 1
            state["state"] = "ADDRESS_PR_THREADS"
            return
    if pending:
        main.handle_wait(state)
        return
    if records and any(record.get("pr_url") for record in records.values()):
        if state["workspace"].get("verified_snapshot") != snapshot(context.root):
            state["state"] = "VERIFYING"
            return
        main._finish_task(state, "All feature PRs merged.\n" + status_body(state), reset_repo=False, merged=True)


def reply(state, reply):
    import main
    phase = state["state"]
    if phase == "WAIT_APPROVAL":
        return main.do_approval_reply(state, reply)
    if phase == "WAIT_STUCK":
        action = ("retry" if re.fullmatch(r"(?i)\s*retry[.!]?\s*", reply) else
                  main.parse_json_reply(main.agent_runner.run(main.prompts.render(
                      main.prompts.CLASSIFY_STUCK_REPLY, reply=reply) + main.handoffs.classification_context(state),
                      contract=False).output).get("action"))
        if action == "abort":
            return main._abort_and_reset(state, "Task stopped; workspace changes preserved.")
        if action == "complete":
            return main._finish_task(state, "Marked complete at your request; local changes preserved.", reset_repo=False)
        if action in ("retry", "instructions"):
            origin = state.get("stuck_return", "VERIFYING")
            if action == "instructions":
                state["pending_feedback"] = reply
            main._clear_stuck(state, origin)
            state["state"] = origin
        return
    if phase == "WAIT_REPLY":
        verdict = main.parse_json_reply(main.agent_runner.run(main.prompts.render(
            main.prompts.CLASSIFY_QUESTION_REPLY, reply=reply,
            question=state.get("pending_question", "")), contract=False).output)
        if verdict.get("action") == "abort":
            return main._abort_and_reset(state, "Task stopped; workspace changes preserved.")
        if verdict.get("action") == "complete":
            return main._finish_task(state, "Marked complete at your request.", reset_repo=False)
        origin = state.get("return_state", "VERIFYING")
        result = turn(state, main.prompts.render(main.prompts.ANSWER_REPLY, reply=reply,
                                               rules=main.prompts.PHASE_RULES.get(origin, "")))
        if main.handle_result(state, result, origin):
            return
        state.pop("pending_question", None)
        state.pop("return_state", None)
        state["state"] = origin
        if origin == "EXPLORING":
            state["state"] = "PROPOSING"
        elif origin in ("PROPOSING", "REPLANNING"):
            main._send_proposal_review(state, result.output, revised=origin == "REPLANNING")
        elif origin == "IMPLEMENTING":
            complete_implementation(state, result)
        return
    if phase == "WAIT_MERGE":
        verdict = main.parse_json_reply(main.agent_runner.run(main.prompts.render(
            main.prompts.CLASSIFY_PR_REPLY, reply=reply) + main.handoffs.classification_context(state),
            contract=False).output)
        action = verdict.get("action")
        if action == "abort":
            return main._abort_and_reset(state, "Task stopped; workspace changes and PRs preserved.")
        if action == "complete":
            return main._finish_task(state, "Marked complete at your request.", reset_repo=False)
        if action == "merge":
            if main._scope_drift(state) or main._conversation_delivery_blocked(state):
                return
            context = target()
            if state["workspace"].get("verified_snapshot") != snapshot(context.root):
                state["state"] = "VERIFYING"
                return
            for name, record in state["workspace"]["repositories"].items():
                if record.get("pr_url") and record.get("merge", {}).get("status") != "MERGED":
                    threads = main.unresolved_review_threads(record["pr_url"])
                    if threads is None or threads and verdict.get("force") is not True:
                        main.email(state, "merge blocked", "Review threads remain or could not be checked.\n" + status_body(state))
                        return
                    gh(context.repository(name), "pr", "merge", record["pr_url"], "--squash",
                       "--match-head-commit", record["delivered_sha"])
                    record["merge"] = {"status": "MERGED"}
                    main.save_state(state)
            return poll_prs(state)
        if action == "wait":
            main.email(state, "merge deferred", "Work and PRs are preserved; I will keep waiting.")
            return
        state["pending_feedback"] = verdict.get("feedback") or reply
        state["state"] = "APPLY_FEEDBACK"
        return
    state["pending_feedback"] = reply
