"""Run coderbot's real tick loop against fake channel/backlog backends.

Launched by tests/test_fsm_walk.py in a subprocess whose environment points coderbot
at a fixture repo and a temp data dir, with fake `claude` and `gh` first on PATH. The
human side is scripted: approve the proposal, then merge the PR. Writes the visited
states, sent messages and outcome to $WALK_DIR/walk.json."""
import faulthandler
import json
import os
import sys
import time
import types
from pathlib import Path

WALK = Path(os.environ["WALK_DIR"])
MAX_TICKS = int(os.environ.get("WALK_MAX_TICKS", "120"))
# A wedged walk dumps every thread's stack and exits instead of idling to the test timeout.
faulthandler.dump_traceback_later(int(os.environ.get("WALK_STALL_SECONDS", "240")), exit=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

record = {"states": [], "sent": [], "done": [], "notes": [], "outcome": None}
# The human answers the decision questions themselves, not progress banners (the
# OPEN_PR banner already reads "PR ready for review" before the PR exists).
REPLIES = json.loads(os.environ.get("WALK_REPLIES") or "null") or [
    ("Do you authorize implementing", "approve"), ("What should I do with this PR?", "merge")]
# WALK_REPLY_EACH=1 answers every message matching a trigger (subject or body), not only the first.
# A list reply is used in turn (the last one repeats), e.g. ["please stop", "approve"].
EACH = os.environ.get("WALK_REPLY_EACH") == "1"
# Opt-in knobs for the workspace walk (tests/test_workspace_walk.py):
# WALK_REPLY_AFTER [[visited, wait, reply]]: answer once, when polled in `wait` after the states
#   `visited` (one, or a list in order) were visited. A restarted process has no record of the
#   messages sent before it, and a reply polled outside a wait is lateral conversation.
# WALK_COMMANDS [[trigger, command, note]]: a HOLD/CONTINUE/... command, once, after a message
#   matching `trigger` was sent. WALK_CLEAN_ON [[trigger, path]]: delete $WALK_DIR/<path> when
#   a message matching `trigger` is sent (the user saving their work).
# WALK_ROUNDS: how many times the backlog item must be completed (a re-picked slug).
AFTER = json.loads(os.environ.get("WALK_REPLY_AFTER") or "[]")
COMMANDS = json.loads(os.environ.get("WALK_COMMANDS") or "[]")
CLEAN_ON = json.loads(os.environ.get("WALK_CLEAN_ON") or "[]")
ROUNDS = int(os.environ.get("WALK_ROUNDS") or "1")
answered = set()
held = set()
(WALK / "driver.pid").write_text(str(os.getpid()))  # fake tools kill -9 it after a side effect


class StopWalk(BaseException):
    pass


# ---- channel backend underneath the real conversation façade (COMM_CHANNEL=email)
channel = types.ModuleType("gmail_client")
channel.__name__ = "gmail_client"


def _send(subject, body, thread_id=None, attachments=None, *, progress=None, delivery_id=None):
    record["sent"].append({"subject": subject, "body": body[:3000], "thread": thread_id or "thread-1",
                           "files": [Path(a).name for a in attachments or []]})
    for trigger, path in CLEAN_ON:
        if trigger in subject + "\n" + body:
            target = WALK / path
            target.unlink(missing_ok=True)
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
        matches = [i for i, m in enumerate(record["sent"])
                   if trigger in (m["subject"] + "\n" + m["body"] if EACH else m["body"])]
        for i in matches if EACH else matches[:1]:
            if f"reply-{i}" not in answered:
                if isinstance(text, list):  # the n-th matching message gets the n-th reply
                    return f"reply-{i}", text[min(matches.index(i), len(text) - 1)]
                return f"reply-{i}", text
    for n, (visited, wait, text) in enumerate(AFTER):
        states = iter(record["states"])
        if (f"after-{n}" not in answered and record["states"][-1] == wait
                and all(state in states for state in ([visited] if isinstance(visited, str) else visited))):
            return f"after-{n}", text
    return None


def _poll_command():
    for n, (trigger, command, note) in enumerate(COMMANDS):
        sent = [m for m in record["sent"] if trigger in m["subject"] + "\n" + m["body"]]
        if sent and f"command-{n}" not in answered:
            return f"command-{n}", sent[0]["thread"], command, True, note
    return None


channel.send = _send
channel.send_envelope = _send_envelope
channel.attachment_limits = lambda: {"file": 20 * 1024 * 1024, "mime": True}
channel.reconcile_delivery = lambda envelope, key, receipt: None
channel.poll_reply = _poll_reply
channel.mark_processed = answered.add
channel.poll_command = _poll_command
for name in ("poll_status", "poll_kick", "poll_verify"):
    setattr(channel, name, lambda *a, **k: None)
channel.drain_thread = lambda thread_id: 0
sys.modules["gmail_client"] = channel

# ---- backlog backend underneath the real task_source façade
ITEM = {"id": "", "text": os.environ["WALK_ITEM"], "detail": "", "images": [],
        "claimed_by_me": False, "priority": None, "url": None}
backlog = types.SimpleNamespace(
    list_pending_items=lambda: [] if len(record["done"]) >= ROUNDS or held else [dict(ITEM)],
    claim_task=lambda text, item_id=None: True, unclaim_task=lambda text, item_id=None: True,
    hold_task=lambda text, item_id=None: held.add(text) or True,
    unhold_task=lambda text, item_id=None: held.discard(text) or True,
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
    try:
        state = _load()
    except Exception as error:  # the loop logs and retries forever; the walk names it
        record["load_errors"] = record.get("load_errors", 0) + 1
        if record["load_errors"] > 20:
            record["outcome"] = f"state cannot be loaded: {error}"
            raise StopWalk() from error
        raise
    record["states"].append(state["state"])
    if len(record["done"]) >= ROUNDS and state["state"] == "IDLE":
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
