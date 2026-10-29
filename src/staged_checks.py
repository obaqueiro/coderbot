"""Cheap checks and selected E2E gate features; full audits require a separate call."""
from dataclasses import replace
from pathlib import Path

import check_plan
import checks
import config


def _audit_history(full, repo, store, task):
    """Report prior audits as historical facts, never as current focused evidence."""
    history = []
    for row in store.list("check_run", task):
        data = row["data"]
        if not isinstance(data.get("check"), dict) or not isinstance(data.get("result"), dict):
            continue
        try:
            check = check_plan.Check(**data["check"])
        except (TypeError, ValueError):
            continue
        if check.scope == "full" and check_plan.is_e2e(check, repo) and not check_plan.has_e2e_selection(check):
            result = {k: v for k, v in data["result"].items() if k != "gate"}
            history.append({**result, "run_id": row["id"], "required": False,
                            "historical": True})
    return history or [{"check": c.id, "status": "not_run", "required": False,
                        "historical": False, "reason": "full_suite_not_required_for_feature"} for c in full]


def _changed_specs(full, repo):
    """The harness run selected to the spec files this branch changed; [] when it
    changed none (then no E2E is needed), None when the diff cannot tell."""
    if Path(repo).resolve() != config.REPO_PATH.resolve() or len(full) != 1:
        return None
    import evidence
    specs = sorted({Path(spec).name for spec in evidence.detect_branch_specs(None)})
    return [replace(full[0], id="e2e:changed", argv=[*full[0].argv, *specs], scope="focused", kind="e2e")] if specs else []


def run(state, plan, repo, store, *, evaluate=None):
    plan = check_plan.normalize(plan, repo)
    task = store.task_identity(state, repo)
    required = check_plan.feature_checks(plan, repo)
    full = [c for c in plan if check_plan.is_e2e(c, repo) and c.scope == "full"
            and not check_plan.has_e2e_selection(c)]
    audit_checks = _audit_history(full, repo, store, task)
    outcomes = []
    def report(stage, **details):
        return {"stage": stage, "checks": outcomes, "audit_checks": audit_checks,
                "verification_scope": "focused_e2e", **details}

    def execute(selected, *, focused=False):
        result = checks.execute_plan(selected, repo, store, task) if selected else []
        if len(result) != len(selected):
            result = [{"check": c.id, "status": "unknown", "reason": "incomplete_check_results"} for c in selected]
        result = [{**r, "check": c.id, **({"gate": evaluate(c, r)} if evaluate else {})}
                  for c, r in zip(selected, result)]
        outcomes.extend(result)
        if focused:
            return all(not r.get("zero_tests") and r.get("status") == "pass" and
                       type(r.get("test_count")) is int and r["test_count"] > 0 for r in result)
        if evaluate:
            return all(r["gate"]["status"] == "pass" for r in result)
        return all(r.get("status") in ("pass", "skipped") for r in result)

    cheap = [c for c in required if not check_plan.is_e2e(c, repo)]
    focused = [c for c in required if check_plan.is_e2e(c, repo) and c.scope == "focused"]
    if not execute(cheap):
        return report("unit_functional")
    invalid = [c for c in focused if not check_plan.has_e2e_selection(c)]
    if full and not focused and not invalid:
        # No repair lap just to name the specs the diff already names.
        derived = _changed_specs(full, repo)
        if derived == []:
            full = []  # the branch changed no specs: E2E is not required
        elif derived:
            focused = derived
    if (full and not focused) or invalid:
        issues = [{"check": c.id, "argv": c.argv, "cwd": c.cwd,
                   "reason": "unfiltered_e2e_registered_as_focused"} for c in invalid]
        return report("needs_focused_e2e", selection_issues=issues or [
            {"reason": "missing_focused_e2e", "full_checks": [c.id for c in full]}])
    if not execute(focused, focused=True):
        return report("focused_e2e")
    return report("complete")
