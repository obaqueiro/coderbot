"""Feature coordinator for a writable, non-Git root containing independent repos.

The agent edits and commits inside child repositories; the controller owns branches,
fetches, pushes, PRs, verification, review and merges (lifecycle.py). The workspace is
trusted and wholly writable; nothing here resets, cleans or force-pushes user work.
"""
from dataclasses import replace
import hashlib
import json
import logging
from pathlib import Path
import re
import secrets
import sys

import config
import errors
import lifecycle
import workspace
import workspace_state
from execution_identity import verification_snapshot as snapshot

log = logging.getLogger("coderbot")
LEGACY = {"E2E": "VERIFYING", "EVIDENCE_REVIEW": "VERIFYING", "RECOVERING": "VERIFYING",
          "APPLY_PR_FEEDBACK": "APPLY_FEEDBACK", "ADDRESS_REVIEW": "ADDRESS_PR_THREADS"}


def target():
    return workspace.discover(config.REPO_PATH, manifest_path=config.WORKSPACE_MANIFEST)


def command(repo, argv):
    return lifecycle.run(argv, repo)


def inspect_repositories(context):
    import repo_provenance
    return {repo.relative_path: {**repo_provenance.inspect(repo.path),
            "branch": lifecycle.RepoOps(repo.path, repo.relative_path, repo.remote, repo.base_branch).branch()}
            for repo in context.repositories}


def observe(context):
    """One observation of the workspace: child repositories plus shared root files."""
    return {"repos": inspect_repositories(context), "shared": _shared_files(context)}


def preflight(context):
    # Existing work is not permission-protected, but cannot be attributed to the bot.
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


def record_changes(state, context, before, after=None):
    """Enrol every repository the agent changed, on this attempt's own branch."""
    after = after or inspect_repositories(context)
    records = state["workspace"]["repositories"]
    ops = lifecycle.Target.of(context)
    for repo in context.repositories:
        name = repo.relative_path
        old = before.get(name)
        if old is None:
            log.warning("repository %s appeared during the turn; it is baselined, not adopted", name)
            continue
        changed = after[name]["fingerprint"] != old["fingerprint"] or after[name]["branch"] != old["branch"]
        if changed and name not in records:
            validated = workspace_state.repository_record(state, context, name, reason="Detected feature edits")
            state["workspace"] = validated["workspace"]
            records = state["workspace"]["repositories"]
            records[name].update(base_sha=ops.repo(name).base_sha(old["head"]), branch=state["branch"],
                                 state="IMPLEMENTING")
        if name in records:
            lifecycle.adopt(ops.repo(name), records[name], old)
            records[name]["last_head"] = after[name]["head"]
            records[name]["last_fingerprint"] = after[name]["fingerprint"]
    return after


def instructions(state, context):
    import prompts
    records = state["workspace"]["repositories"]
    branches = {repo.relative_path: {"base": repo.base_branch, "branch": records.get(repo.relative_path, {}).get("branch")}
                for repo in context.repositories}
    return ("\nWORKSPACE EXECUTION\nThis root is NOT a Git repository. All files and child repositories "
            "within it are writable; there is no repository-selection permission gate. Discover the relevant "
            "subset yourself and leave unrelated work alone. Read workspace and applicable child AGENTS.md. "
            "Never git init this root. Run every Git command in the appropriate child repository. "
            "Coderbot owns branches: it creates each repository's feature branch, fetches, pushes and opens the PRs; "
            "you have no push credentials. Stay on the checked-out branch and commit there; never switch branches, "
            "reset, clean or force-push. Shared root files remain local. Do not commit evidence or secrets. "
            "Use the root openspec/ for the shared feature plan; include repository paths in tasks. "
            "Honor the current functional phase.\n"
            + json.dumps(branches, sort_keys=True)
            + "\n" + prompts.MINIMAL_CHANGE_POLICY + prompts.TESTING_POLICY
            + "\nIn checks, give explicit commands with a workspace-relative cwd: each affected repository's checks "
              "AND a root integration/E2E check (cwd \".\" or \"e2e\"). Do not run development-stack cleanup or "
              "prune shared Docker resources.\n")


def _shared_files(context):
    """Only eligible local source; do not copy credentials or generated outputs."""
    import fnmatch
    import diagnostics
    result = {}
    repo_roots = {repo.path for repo in context.repositories}
    ignore = workspace.ignore_patterns(context.root)

    def walk(directory):
        for path in workspace.children(directory):
            relative = path.relative_to(context.root).as_posix()
            try:
                if (path.is_symlink() or path.name in workspace.EXCLUDED_DIRS or path in repo_roots
                        or workspace.ignored(relative, ignore)):
                    continue
                if path.is_dir():
                    walk(path)
                    continue
            except OSError:
                continue
            if not any(fnmatch.fnmatch(relative, pattern) or relative.startswith(pattern.rstrip("/") + "/")
                       for pattern in context.manifest.shared_inputs):
                continue
            if (path.name == ".env" or path.name.startswith(".env.") and not path.name.endswith("example")
                    or path.suffix in {".pem", ".key", ".mp4", ".webm"}):
                continue
            try:
                text = path.read_text()
                mode = path.stat().st_mode & 0o777
            except (UnicodeError, OSError):
                continue
            if diagnostics.redact(text) != text:
                continue
            result[relative] = {"text": text, "mode": mode, "hash": hashlib.sha256(text.encode()).hexdigest()}
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
    ops = lifecycle.Target.of(context)
    for name, record in state["workspace"]["repositories"].items():
        lifecycle.on_branch(ops.repo(name), record)
    before = observe(context)
    initial = state["workspace"].get("baseline", {})
    for name, entry in before["repos"].items():
        if name not in state["workspace"]["repositories"] and name in initial:
            if entry["fingerprint"] != initial[name]["fingerprint"]:
                raise workspace.WorkspaceError(f"Unattributed changes in {name}; save user work before continuing")
    agent_runner.set_task_context(state)
    try:
        prompt += instructions(state, context)
        result = (agent_runner.run(prompt) if fresh or not state.get("session_id")
                  else agent_runner.resume(state["session_id"], prompt))
    finally:
        # Keep partial edits attributed even after interruption; do not discard them.
        after = observe(context)
        record_changes(state, context, before["repos"], after["repos"])
        # An existing file newly excluded as sensitive is not a deletion: never
        # retain its former contents as a publicly reportable bot edit.
        shared = {name: value for name, value in before["shared"].items()
                  if name in after["shared"] or not (context.root / name).exists()}
        journal(state, context, shared, after["shared"])
        main.save_state(state)
    if fresh:
        # A fresh reviewer never replaces the implementer's session: later fix turns
        # must resume the agent that knows the code.
        state["workspace"]["review_session_id"] = result.session_id
    else:
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
    # Children left on a finished, aborted or held attempt's branch go back to base.
    ops = lifecycle.Target.of(context)
    lifecycle.release(ops, {r.name: {} for r in ops.repos if r.branch().startswith(config.INSTANCE_ID + "-")})
    try:
        baseline = preflight(context)
    except workspace.WorkspaceError as error:
        # Dirty child repositories (e.g. after HOLD or ABORT): say so once and recheck
        # quietly from WAIT_CLEAN until they are saved.
        if not state.get("dirty_notified"):
            main.email(state, "blocked: dirty working tree", f"{error}. I will recheck automatically and "
                       "start the next task once every repository is clean.", new_thread=True)
            state["dirty_notified"] = True
        state["state"] = "WAIT_CLEAN"
        return
    state.pop("dirty_notified", None)
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
    # A unique branch per attempt: re-picking a slug never adopts an old (merged) PR.
    state.update(item=chosen["text"], item_detail=chosen.get("detail", ""),
                 item_images=chosen.get("images", []), item_id=chosen.get("id"),
                 item_url=chosen.get("url"), item_key=chosen.get("key"), slug=slug,
                 branch=f"{config.INSTANCE_ID}-{slug}-{secrets.token_hex(3)}", state="EXPLORING", thread_id=None)
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
    state["state"] = LEGACY.get(state["state"], state["state"])
    handler = PHASES.get(state["state"])
    if handler:
        return handler(state)
    if state["state"] in main.WAITS:
        return main.handle_wait(state)
    state["state"] = "VERIFYING" if state.get("approved_scope_fingerprint") else "IDLE" if not state.get("item") else "EXPLORING"


def explore(state):
    import main
    prompt = main.prompts.render(main.prompts.EXPLORE, project=config.PROJECT_NAME,
        branch=state["branch"], item=state["item"], detail=state.get("item_detail", ""),
        images=main.render_images(state.get("item_images", []))) + "\n" + state.get("pending_feedback", "")
    result = turn(state, prompt, fresh=True)
    if not main.handle_result(state, result, "EXPLORING"):
        state["state"] = "PROPOSING"


def propose(state):
    import main
    result = turn(state, main.prompts.render(main.prompts.PROPOSE, slug=state["slug"],
                  e2e_note="Add checks for affected repos and shared workspace integration/E2E tests.")
                  + "\n" + state.get("pending_feedback", ""))
    if not main.handle_result(state, result, "PROPOSING"):
        main._send_proposal_review(state, result.output)


def replan(state):
    import main
    result = turn(state, main.replanning.prompt(state, config.REPO_PATH))
    if not main.handle_result(state, result, "REPLANNING"):
        main._send_proposal_review(state, result.output, revised=True,
                                   feedback=state.get("replan", {}).get("feedback", ""))


def implement(state):
    import main
    if main._scope_drift(state):
        return
    result = turn(state, main.prompts.render(main.prompts.IMPLEMENT, slug=state["slug"],
        branch=state["branch"], e2e_note="Implement tests in each affected repo plus shared E2E tests.",
        e2e_report_note="In checks, list the actual repository and integration commands.")
        + "\n" + state.get("implementation_feedback", ""))
    lifecycle.rails(_self, state)
    if not main.handle_result(state, result, "IMPLEMENTING"):
        complete_implementation(state, result)


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
        if workspace.inside(context.root, check.cwd) is None:
            log.warning("ignoring check %s: its cwd leaves the workspace", check.id)
            continue
        previous = unique.get(check.id)
        if previous:
            check = replace(check, resources=sorted(set(previous.resources) | set(check.resources)),
                            timeout=max(previous.timeout, check.timeout))
        unique[check.id] = check
    return check_plan.normalize(list(unique.values()), context.root)


def _problem(check, reason, status="not_run"):
    return {"check": check, "status": status, "reason": reason, "gate": {"status": "indeterminate"}}


def validate_plan(state, context):
    """`openspec validate --strict` on the shared change; a missing CLI fails open."""
    if state.get("archive_path"):
        return None
    try:
        command(context.root, ["openspec", "validate", state["slug"], "--strict", "--no-interactive"])
    except errors.Fatal as error:
        log.warning("openspec validation skipped: %s", error)
    except errors.CoderbotError as error:
        return _problem("openspec validate", str(error)[-2000:], "fail")
    return None


def verify(state):
    """Controller-only verification: checklist, OpenSpec, declared and discovered checks."""
    import main
    import check_plan
    import staged_checks
    context = target()
    if not state.get("archive_path") and main._verify_preflight(state):
        return
    ops = lifecycle.Target.of(context)
    for name, record in state["workspace"]["repositories"].items():
        lifecycle.on_branch(ops.repo(name), record)  # a held task's children were returned to base
    tasks = context.root / "openspec/changes" / state["slug"] / "tasks.md"
    if not state.get("archive_path") and tasks.is_file():
        pending = main.task_phases.inspect(context.root, state["slug"])
        if pending["implementation"]:
            if verification_attempt(state, "implementation", "Approved implementation tasks remain incomplete"):
                state["implementation_feedback"] = ("Complete these approved tasks before verification:\n"
                                                    + "\n".join(pending["implementation"]))
                state["state"] = "IMPLEMENTING"
            return
    plan = check_plan_for(state, context)
    problems = [p for p in [validate_plan(state, context)] if p]
    if state.get("reported_check_plan_error"):
        problems.append(_problem("check_plan", state["reported_check_plan_error"]))
    if not any(check.cwd in (".", "e2e") for check in plan):
        problems.append(_problem("shared_integration", "No root integration/E2E check (cwd \".\" or \"e2e\") is "
                                 "declared; add one to checks or the manifest"))
    from workspace_checks import HarnessBlocked
    for check in check_plan.feature_checks(plan, context.root):
        try:
            validate_check(context, check)
        except HarnessBlocked as error:
            problems.append(_problem(check.id, "Harness blocked (repair the shared harness/configuration, not "
                                     "application scope): " + str(error), "blocked"))
    if problems:
        return repair(state, {"status": "indeterminate", "checks": problems})
    before = snapshot(context.root)
    store = main.phase_checkpoint.store()
    staged = staged_checks.run(state, plan, context.root, store,
                               evaluate=lambda c, r: workspace_gate(c, r, state, context, store))
    report = main.final_checks.report(staged, context.root)
    report["source_changed"] = snapshot(context.root) != before
    if report["source_changed"]:
        report["status"] = "indeterminate"
    state["final_check_report"] = report
    if report["status"] != "pass":
        if report["source_changed"] and not main.check_repair.unresolved(report):
            # Files changed under passing checks: run them again, a bounded number of times.
            verification_attempt(state, "source_changed", "Files keep changing while the checks run")
            return
        return repair(state, report)
    rounds = state["workspace"].setdefault("verification_rounds", {})
    for kind in ("repair", "implementation", "source_changed"):
        rounds.pop(kind, None)
    state.pop("check_repair", None)
    state["workspace"]["verified_snapshot"] = before
    state["workspace"]["verification_repositories"] = [repo.relative_path for repo in context.repositories]
    state["state"] = ("INTERNAL_REVIEW" if state["workspace"].get("reviewed_snapshot") != before
                      else "OPEN_PR" if state.get("archive_path") else "ARCHIVING")


def repair(state, report):
    """One FIX turn (REPAIR_CHECKS) with the controller's report, bounded per task."""
    import main
    if not verification_attempt(state, "repair", main.check_repair.summary(report)):
        return
    previous = state.get("check_repair", {})
    state["check_repair"] = {"attempt": state["workspace"]["verification_rounds"]["repair"], "resume": "VERIFYING",
                             "checks": main.check_repair.unresolved(report), "previous": previous.get("previous", ""),
                             "guidance": state.pop("pending_feedback", "") or previous.get("guidance", "")}
    state["state"] = "REPAIR_CHECKS"


def repair_checks(state):
    import main
    context = state.get("check_repair")
    if not isinstance(context, dict):
        state["state"] = "VERIFYING"
        return
    result = turn(state, main.check_repair.prompt(state, context))
    if main.handle_result(state, result, "REPAIR_CHECKS"):
        return
    state["check_repair"]["previous"] = result.output.strip()[-5000:]  # turn() rebinds state
    state["state"] = "VERIFYING"


def review(state):
    """One fresh, read-only feature review over every repository's diff."""
    import main
    context = target()
    ops = lifecycle.Target.of(context)
    records = state["workspace"]["repositories"]
    rounds = state["workspace"].setdefault("verification_rounds", {})
    rounds["review"] = rounds.get("review", 0) + 1
    before = snapshot(context.root)
    result = turn(state, main.prompts.render(main.prompts.FEATURE_REVIEW, slug=state["slug"],
        plan=state.get("archive_path") or f"openspec/changes/{state['slug']}/", review_guide=main._review_guide(state),
        repositories=lifecycle.diff_summary(ops, records, config.REVIEW_CONTEXT_CHARS)), fresh=True)
    if main.handle_result(state, result, "INTERNAL_REVIEW"):
        return
    if snapshot(context.root) != before:
        log.warning("the read-only reviewer changed files; discarding its review")
        state["state"] = "VERIFYING"
        return
    value = (getattr(result, "data", None) or {}).get("review") or {}
    critical, important = int(value.get("critical") or 0), int(value.get("important") or 0)
    findings = [str(f) for f in value.get("findings") or []]
    if critical or important:
        if rounds["review"] <= config.REVIEW_MAX_ROUNDS:
            # The reviewer is coderbot's own agent: its findings are data for the fix wave.
            state["workspace"]["findings"] = findings or [result.output[-3000:]]
            state["state"] = "APPLY_FEEDBACK"
            return
        if critical:
            main._enter_stuck(state, "INTERNAL_REVIEW", f"The feature review still reports {critical} critical "
                              "finding(s) after the bounded fix waves:\n" + "\n".join(findings))
            return
    state["workspace"]["concerns"] = findings if critical or important else [f for f in findings if f][:20]
    state["workspace"]["reviewed_snapshot"] = before
    state["state"] = "OPEN_PR" if state.get("archive_path") else "ARCHIVING"


CARDS = {
    "APPLY_FEEDBACK": "Task: apply the feedback below across the relevant repositories. Internal review findings "
                      "come from coderbot's reviewer: use receiving-code-review and answer each one fixed or declined "
                      "with evidence in the summary.",
    "ADDRESS_PR_THREADS": "Task: address the unresolved PR review threads below (grouped by repository). For each "
                          "thread fill threads: {id, action fixed|answered|declined, reply}.",
    "RESOLVE_CONFLICTS": "Task: in each repository below, merge origin/<base> (already fetched by coderbot; merge, "
                         "never rebase or force) and resolve every conflict preserving both sides' intent; commit.",
}


def address(state):
    """One implementer turn for owner feedback, review findings, PR threads or conflicts."""
    import main
    phase = state["state"]
    review_data = state.get("workspace_review", {})
    if phase == "RESOLVE_CONFLICTS":
        ops = lifecycle.Target.of(target())
        for name in review_data.get("conflicts", [review_data.get("repository")]):
            if name:
                ops.repo(name).fetch()
    data = {"findings": state["workspace"].get("findings"), "threads": lifecycle.threads_by_repo(review_data),
            "conflicts": review_data.get("conflicts") or ([review_data["repository"]] if review_data.get("conflict") else None)}
    prompt = (CARDS.get(phase, CARDS["APPLY_FEEDBACK"]) + " Preserve user changes; commit in each repository.\n"
              + main.prompts.fenced("controller data", json.dumps({k: v for k, v in data.items() if v}, ensure_ascii=False))
              + ("\n" + main.prompts.fenced_authoritative("owner feedback", state["pending_feedback"])
                 if state.get("pending_feedback") else ""))
    result = turn(state, prompt)
    if main.handle_result(state, result, phase):
        return
    remember_checks(state, result.output)
    if phase == "RESOLVE_CONFLICTS":
        ops = lifecycle.Target.of(target())
        for name in data["conflicts"] or []:
            record = state["workspace"]["repositories"].get(name)
            if record:  # the merged base is the PR's new base for review and diffs
                record["base_sha"] = ops.repo(name).base_sha()
    state.pop("pending_feedback", None)
    state["workspace"].pop("findings", None)
    state["workspace"]["review_output"] = result.output
    state["state"] = "VERIFYING"


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
    cwd = workspace.inside(context.root, check.cwd)
    if cwd is None:
        raise HarnessBlocked(f"Check {check.id} runs outside the workspace")
    text = " ".join(check.argv)
    visited = set()
    scripts = []

    def script(path):
        nonlocal text
        if path is None or path in visited or not path.is_file() or path.suffix not in (".sh", ".py", ".js", ".mjs"):
            return
        visited.add(path)
        try:
            body = path.read_text()
        except (OSError, UnicodeError):
            return
        scripts.append(body)
        text += "\n" + body
        # Shell harnesses commonly delegate cleanup to a sibling/root script.
        for name in re.findall(r"(?:\$ROOT/|\$\{ROOT\}/|\$root/|\$\{root\}/|\./)([A-Za-z0-9_./-]+\.sh)", body):
            for root in (context.root, path.parent):
                script(workspace.inside(context.root, root / name))
    for arg in check.argv:
        if not arg.startswith("-"):
            script(workspace.inside(context.root, cwd / arg))
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
            origin = LEGACY.get(state.get("stuck_return", "VERIFYING"), state.get("stuck_return", "VERIFYING"))
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
        origin = LEGACY.get(state.get("return_state", "VERIFYING"), state.get("return_state", "VERIFYING"))
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
            return lifecycle.merge_all(_self, state, force=verdict.get("force") is True)
        if action == "wait":
            main.email(state, "merge deferred", "Work and PRs are preserved; I will keep waiting.")
            return
        state["pending_feedback"] = verdict.get("feedback") or reply
        state["state"] = "APPLY_FEEDBACK"
        return
    state["pending_feedback"] = reply


def wait_clean(state):
    try:
        preflight(target())
    except workspace.WorkspaceError:
        return  # still dirty: keep waiting without failing the tick
    state["state"] = "IDLE"


_self = sys.modules[__name__]
PHASES = {"IDLE": pick, "EXPLORING": explore, "PROPOSING": propose, "REPLANNING": replan,
          "IMPLEMENTING": implement, "VERIFYING": verify, "REPAIR_CHECKS": repair_checks,
          "INTERNAL_REVIEW": review, "ARCHIVING": archive, "OPEN_PR": lambda s: lifecycle.publish(_self, s),
          "PUSHING": lambda s: lifecycle.publish(_self, s),
          "WAIT_REVIEW": lambda s: lifecycle.poll_prs(_self, s),
          "WAIT_MERGE": lambda s: lifecycle.poll_prs(_self, s), "APPLY_FEEDBACK": address,
          "ADDRESS_PR_THREADS": address, "RESOLVE_CONFLICTS": address, "WAIT_CLEAN": wait_clean}
