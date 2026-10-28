"""Run coderbot's real tick loop against fake channel/backlog backends.

Launched by tests/test_fsm_walk.py in a subprocess whose environment points coderbot
at a fixture repo and a temp data dir, with fake `claude` and `gh` first on PATH. The
human side is scripted: approve the proposal, then merge the PR. Writes the visited
states, sent messages and outcome to $WALK_DIR/walk.json."""
import json
import os
import sys
import time
import types
from pathlib import Path

WALK = Path(os.environ["WALK_DIR"])
MAX_TICKS = int(os.environ.get("WALK_MAX_TICKS", "120"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

record = {"states": [], "sent": [], "done": [], "notes": [], "outcome": None}
# The human answers the decision questions themselves, not progress banners (the
# OPEN_PR banner already reads "PR ready for review" before the PR exists).
REPLIES = [("Do you authorize implementing", "approve"), ("What should I do with this PR?", "merge")]
answered = set()


class StopWalk(BaseException):
    pass


# ---- channel backend underneath the real conversation façade (COMM_CHANNEL=email)
channel = types.ModuleType("gmail_client")
channel.__name__ = "gmail_client"


def _send(subject, body, thread_id=None, attachments=None, *, progress=None, delivery_id=None):
    record["sent"].append({"subject": subject, "body": body[:3000], "thread": thread_id or "thread-1",
                           "files": [Path(a).name for a in attachments or []]})
    return thread_id or "thread-1"


def _send_envelope(envelope, receipts, confirm):
    thread = _send(envelope["subject"], envelope["body"], envelope.get("thread_id"),
                   [a["path"] for a in envelope["attachments"]])
    for key in ["body", *[a["id"] for a in envelope["attachments"]]]:
        confirm(key, {"status": "confirmed", "thread_id": thread})
    return thread


def _poll_reply(thread_id):
    """The human answers each decision once: the first message naming it."""
    for trigger, text in REPLIES:
        first = next((i for i, m in enumerate(record["sent"]) if trigger in m["body"]), None)
        if first is not None and f"reply-{first}" not in answered:
            return f"reply-{first}", text
    return None


channel.send = _send
channel.send_envelope = _send_envelope
channel.attachment_limits = lambda: {"file": 20 * 1024 * 1024, "mime": True}
channel.reconcile_delivery = lambda envelope, key, receipt: None
channel.poll_reply = _poll_reply
channel.mark_processed = answered.add
for name in ("poll_command", "poll_status", "poll_kick", "poll_verify"):
    setattr(channel, name, lambda *a, **k: None)
channel.drain_thread = lambda thread_id: 0
sys.modules["gmail_client"] = channel

# ---- backlog backend underneath the real task_source façade
ITEM = {"id": "", "text": os.environ["WALK_ITEM"], "detail": "", "images": [],
        "claimed_by_me": False, "priority": None, "url": None}
backlog = types.SimpleNamespace(
    list_pending_items=lambda: [] if record["done"] else [dict(ITEM)],
    claim_task=lambda text, item_id=None: True, unclaim_task=lambda text, item_id=None: True,
    hold_task=lambda text, item_id=None: True, unhold_task=lambda text, item_id=None: True,
    mark_done=lambda text, item_id=None: record["done"].append(text) or True,
    ensure_item=lambda text: True, note_pr=lambda text, item_id, url: record["notes"].append(url),
    note_activity=lambda *a, **k: None, validate=lambda: None, describe=lambda: "walk")
import task_source  # noqa: E402
task_source._impl = lambda: backlog

import main  # noqa: E402

_load = main.load_state


def load_state():
    # The lateral conversation worker is a thread in production; drain it in step
    # with the loop so the walk is deterministic.
    import agent_runner
    import parallel_conversation
    import phase_checkpoint
    for _ in range(10):
        if not parallel_conversation.process_one(
                phase_checkpoint.store(), main.config.REPO_PATH, agent_runner.converse,
                main._deliver_conversation, notify=main._deliver_conversation_status):
            break
    state = _load()
    record["states"].append(state["state"])
    if record["done"] and state["state"] == "IDLE":
        record["outcome"] = "merged"
        raise StopWalk()
    if len(record["states"]) > MAX_TICKS:
        record["outcome"] = f"no progress after {MAX_TICKS} ticks in {state['state']}"
        raise StopWalk()
    return state


main.load_state = load_state
main.time.sleep = lambda seconds: None
import conversation  # noqa: E402
conversation.wait = lambda seconds: None
started = time.monotonic()
try:
    main._run_loop()
except StopWalk:
    pass
finally:
    record["seconds"] = round(time.monotonic() - started, 2)
    states = []
    for name in record["states"]:  # collapse consecutive repeats for readability
        if not states or states[-1] != name:
            states.append(name)
    record["states"] = states
    (WALK / "walk.json").write_text(json.dumps(record, indent=1))
