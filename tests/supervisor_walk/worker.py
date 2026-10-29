"""Fake worker (installed as src/main.py): one task stuck on a bug in src/bug.py.

It speaks the real worker protocol through the real selfheal_events: heartbeats,
`ready`, a typed incident on failure, `phase_completed` on progress, and the
recovery ticket on restart. WALK_STORM / WALK_FATAL make it die at startup."""
import json
import os
from pathlib import Path
import time

if os.environ.get("WALK_STORM"):
    raise RuntimeError("cannot reach the backlog")
if os.environ.get("WALK_FATAL"):
    raise SystemExit("GH_TOKEN is missing")

import bug
import selfheal_events

path = Path(os.environ["CODEBOT_DATA_DIR"]) / "state.json"
(path.parent / "workers").mkdir(exist_ok=True)
(path.parent / "workers" / str(os.getpid())).touch()


def save(value):
    path.write_text(json.dumps(value))


state = json.loads(path.read_text())
recovered = selfheal_events.recover(state)
if recovered != state:
    save(recovered)
    selfheal_events.acknowledge(recovered)
state = recovered
selfheal_events.emit("ready", state)
while True:
    selfheal_events.emit("heartbeat", state)
    if state["state"] == "EXPLORING":
        try:
            bug.parse([])
        except Exception as error:
            state = {**state, "state": "WAIT_STUCK", "stuck_return": "EXPLORING"}
            save(state)
            selfheal_events.incident({**state, "state": "EXPLORING"}, error,
                                     binding=selfheal_events.state_binding(state))
        else:
            state = {**state, "state": "WAIT_APPROVAL"}
            save(state)
            selfheal_events.emit("phase_completed", state, origin="EXPLORING")
    time.sleep(0.2)
