"""One final verification policy for all suites and all lifecycle paths."""
from dataclasses import replace
from pathlib import Path

import check_plan
import checks
import config
from check_baseline import baseline_result, compare
from execution_identity import verification_snapshot as snapshot


def run(state, repo, store):
    before = snapshot(repo)
    if state.get("reported_check_plan_error"):
        return {"snapshot": before, "status": "indeterminate", "stage": "invalid_check_plan", "checks": [
            {"check": "check_plan", "status": "not_run", "gate": {"status": "indeterminate"},
             "reason": state["reported_check_plan_error"]}]}
    plan = check_plan.discover(repo)
    waived = any(w.get("scope") == "e2e:general" for w in state.get("check_waivers", []))
    if waived:
        plan = [c for c in plan if not c.id.startswith("e2e")]
    # Same-ID corrections replace execution contracts, including full -> selected.
    known = {c.id for c in plan}
    for entry in state.get("reported_check_plan", []):
        check = check_plan.Check(**entry)
        if check.id in known:
            plan = [replace(check, timeout=max(existing.timeout, check.timeout),
                            resources=sorted(set(existing.resources) | set(check.resources)))
                    if existing.id == check.id else existing for existing in plan]
        if check.id not in known:
            if waived and check.id.startswith("e2e"):
                continue
            plan.append(check)
            known.add(check.id)
    plan = check_plan.relax(plan, [name for name, decision in (state.get("credentials") or {}).items()
                                    if isinstance(decision, dict) and decision.get("decision") == "skip"])
    plan = check_plan.normalize(plan, repo)
    plan.sort(key=lambda c: 0 if not check_plan.is_e2e(c, repo) else 1 if c.scope == "focused" else 2)
    import staged_checks
    staged = staged_checks.run(state, plan, repo, store, evaluate=lambda c, r: gate(c, r, state, repo, store))
    value = report(staged, repo)
    value["source_changed"] = before != value["snapshot"]
    if value["source_changed"]:
        value["status"] = "indeterminate"
    return value


def gate(check, result, state, repo, store):
    if result.get("zero_tests") or result["status"] == "not_run":
        required = (result["status"] == "not_run" or check_plan.is_e2e(check, repo) or config.EMPTY_SUITE_FAILS and
                    any(c.get("id") == check.id for c in state.get("reported_check_plan", [])))
        return {"status": "fail" if required else "pass", "preexisting": [], "regressions": [],
                "reason": "0 tests executed: repair test selection/discovery"}
    if result["status"] in ("pass", "skipped"):
        if result["status"] == "pass" and check_plan.is_e2e(check, repo) and (type(result.get("test_count")) is not int or result["test_count"] <= 0):
            return {"status": "indeterminate", "preexisting": [], "regressions": [],
                    "reason": "E2E execution count is unproven; declare a structured test reporter and repair selection/discovery"}
        return {"status": "pass", "preexisting": [], "regressions": []}
    # Focused evidence must actually pass; baseline debt cannot stand in for it.
    if result["status"] == "fail" and state.get("base_sha") and not (check.scope == "focused" and check_plan.is_e2e(check, repo)):
        baseline = baseline_result(check, repo, state["base_sha"], store,
                                   audit_task=store.task_identity(state, repo))
        value = compare(result, baseline, roots=(Path(repo).resolve(),))
        value["baseline"] = {"sha": state["base_sha"], **{k: baseline.get(k) for k in ("status", "report", "detail", "reason")}}
        if baseline.get("reason"):
            value["reason"] = baseline["reason"]
        return value
    return {"status": "indeterminate", "preexisting": [], "regressions": []}


def report(staged, repo):
    outcomes = [{**r, **({"status": "not_run"} if r.get("zero_tests") and r["gate"]["status"] != "pass" else {})}
                for r in staged["checks"]]
    if staged["stage"] != "complete" and all(r["gate"]["status"] == "pass" for r in outcomes):
        outcomes.append({"check": staged["stage"], "status": "not_run", "gate": {"status": "indeterminate"},
                         "reason": "Required verification stage is incomplete; repair focused evidence/diagnosis, not another identical full run",
                         "selection_issues": staged.get("selection_issues", [])})
    audits = staged.get("audit_checks", [])
    raw = outcomes + audits
    suite_status = ("fail" if any(r["status"] == "fail" for r in raw) else
                    "indeterminate" if audits or not raw or any(r["status"] not in ("pass", "skipped") for r in raw) else
                    "skipped" if any(r["status"] == "skipped" for r in raw) else "pass")
    return {"snapshot": snapshot(repo), "stage": staged["stage"],
            "verification_scope": staged.get("verification_scope", "focused_e2e"), "audit_checks": audits,
            "suite_status": suite_status,
            "status": "pass" if staged["stage"] == "complete" and outcomes and all(r["gate"]["status"] == "pass" for r in outcomes)
            else "indeterminate" if not outcomes or any(r["gate"]["status"] == "indeterminate" for r in outcomes)
            else "fail", "checks": outcomes}
