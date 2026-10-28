"""Deterministic checks with content-bound results and per-run reports."""
from pathlib import Path
import subprocess
import time
import uuid
import logging
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import operations
import preparation
import check_plan
from dataclasses import replace
from selfheal_events import runtime_epoch
from check_results import parse_output
from execution_identity import digest, environment_identity, verification_snapshot as snapshot
log = logging.getLogger(__name__)


def environment_revision(store, repo, check_id):
    rows = store.list("check_environment", "environment:" + str(Path(repo).resolve()), identity=check_id)
    return rows[0]["data"]["revision"] if rows else 0


def invalidate_environment(store, repo, check_ids):
    """A runtime repair invalidates only affected results, including baseline cache."""
    task_id = "environment:" + str(Path(repo).resolve())
    for check_id in set(check_ids):
        rows = store.list("check_environment", task_id, identity=check_id)
        store.put("check_environment", task_id=task_id, identity=check_id,
                  id=rows[0]["id"] if rows else None,
                  data={"revision": environment_revision(store, repo, check_id) + 1})


def execute_plan(plan, repo, store, task_id, *, workers=3, audit_task=None):
    plan = check_plan.normalize(plan, repo)
    # Each process takes sorted exclusive resources before launch. Preserve result
    # order for deterministic reporting, independently of completion order.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        kwargs = {"audit_task": audit_task} if audit_task else {}
        futures = [pool.submit(copy_context().run, execute, check, repo, store, task_id, **kwargs) for check in plan]
        return [future.result() for future in futures]


def check_environment():
    """The environment checks run with: the process env plus this task's credentials."""
    import secret_store
    return secret_store.environment()


def execute(check, repo, store, task_id, *, reuse=True, audit_task=None):
    repo = Path(repo).resolve()
    check = check_plan.normalize([check], repo)[0]
    if check_plan.is_harness(check, repo):
        history = store.list("check_run", task_id)
        key = check_plan.execution_key(check, repo)
        for row in history:
            old = row["data"].get("check")
            result = row["data"].get("result", {})
            if (isinstance(old, dict) and check_plan.execution_key(check_plan.Check(**old), repo) == key
                    and result.get("reason") == "execution_timeout"):
                check = replace(check, timeout=min(3600, max(check.timeout, old.get("timeout", 900) * 2)))
                break
    env = check_environment()
    missing = check.missing_credentials(env) if check.optional else []
    if missing:
        detail = "missing credential " + ", ".join(missing)
        log.info("check skipped: %s (%s)", check.id, detail,
                 extra={"public_progress": f"check skipped: {check.id} (missing required credential)"})
        return {"status": "skipped", "skipped": detail, "failures": {}, "exit_code": None,
                "check": check.id, "reused": False}
    preparation_error = None
    if check.preparation:
        try:
            preparation.ensure(check.preparation, repo, store, task_id)
        except (RuntimeError, OSError, TimeoutError, subprocess.TimeoutExpired) as error:
            preparation_error = error
    cwd = (repo / check.cwd).resolve()
    content = snapshot(repo, check.inputs)
    contract = json.loads(check_plan.execution_key(check, repo))
    versions = tool_versions(check.argv, cwd)
    if contract["argv"][0] == "posix-script":
        versions = {**versions, "executable": "posix-script"}
    environment = environment_identity(contract["argv"], cwd, env_keys=check.env_keys, environ=env,
        tools={**versions, "repair_revision": environment_revision(store, repo, check.id),
               "runtime_epoch": runtime_epoch()},
        key_path=store.path.parent / "identity.key")
    from check_baseline import dependency_snapshot, dependency_inputs
    dependencies = dependency_snapshot(repo, dependency_inputs(check, repo))
    identity = digest({"content": content, "environment": environment, "check": contract, "timeout": check.timeout,
                       "dependencies": dependencies, "version": 3})
    previous = store.reusable_check(task_id, identity) if reuse and not preparation_error else None
    if previous and previous["data"]["result"]["status"] not in ("infrastructure", "unknown", "skipped"):
        log.info("check reused: %s status=%s report=%s", check.id,
                 previous["data"]["result"]["status"], previous["data"]["result"].get("report"),
                 extra={"public_progress": f"check reused: {check.id} status={previous['data']['result']['status']}"})
        return {**previous["data"]["result"], "check": check.id, "reused": True}
    if (previous and previous["data"]["result"].get("reason") == "execution_timeout"
            and check_plan.is_harness(check, repo) and check.timeout >= 3600):
        # A timed-out attempt at the ceiling is not a pass and not permission to
        # run the same unchanged suite forever. Changed source/environment can retry.
        log.warning("check timeout budget exhausted: %s; repair before retrying", check.id,
                    extra={"public_progress": f"check blocked: {check.id} (timeout ceiling reached; repair required)"})
        return {**previous["data"]["result"], "reused": True, "timeout_budget_exhausted": True}
    # Reserve each actual full-suite launch, not a batch. Cache hits cost nothing;
    # crashes conservatively consume a reservation. Baselines share the feature budget.
    if audit_task and not preparation_error and check.scope == "full" and check_plan.is_e2e(check, repo):
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM operation WHERE task_id=? AND identity=?",
                               (audit_task, "full-e2e-launch")).fetchone()[0]
            if count >= 2:
                return {"check": check.id, "status": "unknown", "failures": {},
                        "reason": "full_e2e_budget_exhausted", "reused": False}
            now = time.time()
            db.execute("INSERT INTO operation VALUES(?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, audit_task,
                None, "reserved", "full-e2e-launch", now, now, json.dumps({"check": check.to_dict(), "identity": identity})))
    candidates = store.list("check_run", task_id)
    previous_check = next((r for r in candidates if isinstance(r["data"].get("check"), dict)
                           and r["data"]["check"].get("id") == check.id), None)
    reason = "no_previous_result"
    if previous_check:
        old = previous_check["data"].get("result", {})
        reason = ("content_changed" if old.get("content") != content else
                  "environment_or_command_changed" if old.get("environment") != environment else
                  "incomplete_or_unusable_result")
    run_id = uuid.uuid4().hex
    report_dir = store.path.parent / "outbox/checks" / run_id
    report_dir.mkdir(parents=True)
    started = time.time()
    store.put("check_run", task_id=task_id, id=run_id, identity=identity, data={"check": check.id})
    log.info("check started: %s cwd=%s timeout=%ss reason=%s", check.id, cwd, check.timeout, reason,
             extra={"public_progress": f"check started: {check.id}"})
    try:
        if preparation_error:
            raise OSError("Check preparation failed: " + str(preparation_error))
        process = operations.run(check.argv, cwd=cwd, timeout=check.timeout,
                                 exclusive=check.resources, env=env)
        output = process.stdout + "\n" + process.stderr
        result = parse_output(output, process.returncode, check.reporter)
    except (subprocess.TimeoutExpired, TimeoutError, OSError) as error:
        output = f"{type(error).__name__}: {error}"
        for field in ("stdout", "stderr"):
            value = getattr(error, field, None)
            if value:
                output += "\n" + (value.decode(errors="replace") if isinstance(value, bytes) else value)
        result = {"status": "infrastructure", "failures": {}, "exit_code": None}
        if isinstance(error, subprocess.TimeoutExpired):
            result["reason"] = "execution_timeout"
    except BaseException:
        store.put("check_run", task_id=task_id, id=run_id, identity=identity,
                  status="interrupted", data={"check": check.id})
        raise
    report = report_dir / "output.log"
    report.write_text(output)
    result.update(check=check.id, identity=identity, content=content, environment=environment,
                  report=str(report), started=started, finished=time.time(), reused=False,
                  repetition_reason=reason)
    store.put("check_run", task_id=task_id, id=run_id, identity=identity, status="complete",
              data={"check": check.to_dict(), "result": result})
    log.info("check finished: %s status=%s exit=%s duration=%.1fs report=%s", check.id,
              result["status"], result["exit_code"], result["finished"] - started, report,
              extra={"public_progress": f"check finished: {check.id} status={result['status']} exit={result['exit_code']}"})
    return result


def tool_versions(argv, cwd):
    """Version probes are part of identity; unknown means conservative no reuse."""
    executable = argv[0]
    versions = {"executable": executable}
    if Path(executable).name in ("node", "npm", "python", "python3", "pytest", "openspec"):
        result = operations.run([executable, "--version"], cwd=cwd, timeout=15, kind="probe")
        versions["version"] = (result.stdout + result.stderr).strip()[:300]
    elif (Path(cwd) / executable).is_file():
        import hashlib
        versions["executable_hash"] = hashlib.sha256((Path(cwd) / executable).read_bytes()).hexdigest()
    for name in ("node", "python3"):
        if name == executable:
            continue
        try:
            probe = operations.run([name, "--version"], cwd=cwd, timeout=15, kind="probe")
            versions[name] = (probe.stdout + probe.stderr).strip()[:100]
        except OSError:
            versions[name] = "unavailable"
    return versions
