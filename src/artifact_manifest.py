"""Run-bound artifact provenance for Playwright, Newman and test diagnostics."""
import hashlib
import json
from pathlib import Path

import persistence


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, *, run_id, snapshot, status, artifacts):
    entries = []
    for entry in artifacts:
        file = Path(entry["path"]).resolve()
        entries.append({**entry, "path": str(file), "hash": file_hash(file), "size": file.stat().st_size})
    value = {"version": 1, "run_id": run_id, "snapshot": snapshot, "status": status, "artifacts": entries}
    persistence.write_json(path, value, ensure_ascii=False)
    return value


def load(path, *, snapshot=None, approved=True):
    value = json.loads(Path(path).read_text())
    if value.get("version") != 1 or not value.get("run_id"):
        raise ValueError("Invalid artifact manifest")
    if snapshot is not None and value.get("snapshot") != snapshot:
        raise ValueError("Artifact content does not match delivered snapshot")
    if approved and value.get("status") != "pass":
        return []
    valid = []
    for entry in value.get("artifacts", []):
        file = Path(entry["path"])
        if file.is_file() and file.stat().st_size and file_hash(file) == entry.get("hash"):
            valid.append(entry)
    return valid
