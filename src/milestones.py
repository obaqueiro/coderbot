"""Static lifecycle progress images and per-task announcement bookkeeping."""
from pathlib import Path

STAGES = ("exploring", "proposing", "approval", "implementing", "verifying",
          "archiving", "pr_review", "merged")
LABELS = ("Exploration", "Proposal", "Awaiting approval", "Implementation",
          "Verification", "Archiving", "PR ready for review", "PR merged")
ASSETS = Path(__file__).resolve().parent / "assets" / "milestones"

# Several FSM states belong to the same visible lifecycle step. Keep the exact
# state in the notice, but send the illustration only when the visible step changes.
STATE_STAGES = {
    "EXPLORING": "exploring", "PROPOSING": "proposing", "REPLANNING": "proposing",
    "WAIT_APPROVAL": "approval", "IMPLEMENTING": "implementing",
    "APPLY_FEEDBACK": "implementing", "VERIFYING": "verifying",
    "INTERNAL_REVIEW": "verifying", "E2E": "verifying", "ARCHIVING": "archiving",
    "REPAIR_CHECKS": "verifying",
    # OPEN_PR is still preparing the PR; "PR ready for review" before it existed made
    # people answer "merge" to a PR that was not there yet.
    "OPEN_PR": "archiving", "WAIT_REVIEW": "pr_review", "WAIT_MERGE": "pr_review",
    "ADDRESS_REVIEW": "pr_review", "ADDRESS_PR_THREADS": "pr_review",
    "RESOLVE_CONFLICTS": "pr_review", "PUSHING": "pr_review",
}


def state_stage(state: dict) -> str:
    phase = state["state"]
    if phase in ("WAIT_REPLY", "WAIT_STUCK", "WAIT_CLEAN"):
        origin = state.get("stuck_return") if phase == "WAIT_STUCK" else state.get("return_state")
        return STATE_STAGES.get(origin or "", state.get("banner_stage") or "exploring")
    return STATE_STAGES.get(phase, "exploring")


def image(stage: str) -> Path:
    if stage not in STAGES:
        raise ValueError(f"unknown milestone: {stage}")
    return ASSETS / f"{stage}.png"


def label(stage: str) -> str:
    return LABELS[STAGES.index(stage)]


def announced(state: dict, stage: str) -> bool:
    return stage in state.get("milestones_announced", [])


def mark(state: dict, stage: str) -> None:
    if not announced(state, stage):
        state.setdefault("milestones_announced", []).append(stage)
