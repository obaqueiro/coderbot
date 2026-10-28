"""Bounded final-check diagnosis; agent prose never overrides controller evidence."""
import json

import prompts


def unresolved(report):
    return [item for item in report.get("checks", [])
            if item.get("gate", {}).get("status") != "pass"]


def prompt(state, context):
    evidence = ("\nEvidence delivery is blocked. Read the recording logs and JSON reports and compare "
                "the selected cases with the actual cases and current-run videos. Implement missing "
                "approved functionality, @evidence demo tests, video opt-in, mounts or conversion "
                "prerequisites, so the next recording produces complete evidence. Do not weaken inventory "
                "validation, remove tests, accept failed runs or reuse old clips. Do not restore or "
                "re-archive an archived OpenSpec change.\n" if context.get("evidence") else "")
    return (f"Task: final checks failed (repair attempt {context['attempt']}). Diagnose before retrying, "
            "with systematic-debugging, and fix the cause.\n"
            "Steps:\n"
            "1. Read each unresolved check's report below and find the exact cause.\n"
            "2. Repair it: code or tests within the approved scope, or execution prerequisites "
            "(declared dependencies installed in the SAME interpreter the check uses, services, "
            "permissions, runner configuration). This runtime is trusted; do not ask anyone to SSH, "
            "install packages or approve tools.\n"
            "3. Prove the repair with focused probes. Do not rerun full or baseline suites; coderbot "
            "re-runs the affected checks after your turn.\n"
            "4. Commit the intended tracked changes.\n"
            f"The comparison base is the immutable commit {state.get('base_sha', 'unavailable')}, not "
            "current main. A failure inherited from an upstream commit still needs repair; never call an "
            "infrastructure failure, unknown output or a prose comparison passed or pre-existing.\n"
            "If the previous attempt changed nothing, take a different fix; if only a human can decide, "
            "return needs_input with 2-4 options.\n"
            + evidence
            + prompts.fenced("unresolved controller check results", json.dumps(context["checks"], ensure_ascii=False))
            + "\n" + prompts.fenced("previous repair findings", context.get("previous", ""))
            + ("\n" + prompts.fenced_authoritative("user recovery guidance", context["guidance"])
               if context.get("guidance") else "")
            + "\nDone when: the cause is repaired and committed. Return status done with the exact "
            "causes, repairs and diagnostic commands/results, and anything still unresolved; only "
            "coderbot's checks can pass the gate.\n")


def summary(report):
    lines = ["Final checks need diagnosis/repair (not a new implementation plan):"]
    for item in unresolved(report):
        gate = item.get("gate", {})
        lines.append(f"- {item.get('check', 'check')}: {item.get('status', 'unknown')}; "
                     f"gate={gate.get('status', 'indeterminate')}"
                     + (f"; reason={gate['reason']}" if gate.get("reason") else ""))
    return "\n".join(lines)
