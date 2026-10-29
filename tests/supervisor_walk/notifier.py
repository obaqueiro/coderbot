"""File notifier (installed as src/repair_notifications.py): delivers each supervisor
event by appending it to data/notified.jsonl."""
import json
import os
from pathlib import Path

data = Path(os.environ["CODEBOT_DATA_DIR"])
for path in sorted((data / "supervisor/notifications").glob("*.json")):
    with (data / "notified.jsonl").open("a") as log:
        log.write(path.read_text() + "\n")
    path.rename(path.with_suffix(".delivered"))
