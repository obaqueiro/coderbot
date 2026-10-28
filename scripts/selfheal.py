"""Stable, stdlib-only repair machinery. Never import the application here."""
from contextlib import contextmanager
import ast
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
import uuid


VERSION = 1
SNAPSHOT_ROOTS = ("src", "tests", "scripts", "agent-plugin", "docs", "README.md",
                  "Dockerfile", "requirements.txt", ".env.example")
PROTECTED_FILES = {"src/config.py", "src/secret_store.py", "src/selfheal_events.py",
                   "src/repair_notifications.py", "src/turn_control.py"}
PROTECTED_FUNCTIONS = {"do_approval_reply", "do_merge_reply", "_finish_task", "_abort_and_reset",
                       "_validate_target_state", "_clear_stuck", "_acquire_single_instance_lock"}
IMPLEMENTATION_ERRORS = {"TypeError", "AttributeError", "KeyError", "IndexError", "JSONDecodeError", "SyntaxError"}


class RepairError(RuntimeError):
    pass


class RepairCancelled(RepairError):
    pass


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x") as stream:
            temporary.chmod(0o600)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def file_value(path):
    path = Path(path)
    if path.is_symlink():
        raise RepairError("Symlinks are not eligible repair files")
    if not path.exists():
        return None
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mode": path.stat().st_mode & 0o777}


def inventory(root):
    root = Path(root)
    if root.is_symlink():
        raise RepairError("Snapshot root is a symlink")
    result = {}
    for entry in SNAPSHOT_ROOTS:
        path = root / entry
        if path.is_symlink():
            raise RepairError("Source snapshot contains a symlink: " + entry)
        if not path.exists():
            continue
        files = path.rglob("*") if path.is_dir() else (path,)
        for file in files:
            relative = file.relative_to(root)
            if any(p in {"__pycache__", "node_modules", ".venv", ".git", "data"} for p in relative.parts):
                continue
            if file.is_symlink():
                raise RepairError("Source snapshot contains a symlink: " + str(relative))
            if file.is_file():
                result[relative.as_posix()] = file_value(file)
    return result


def epoch(root):
    values = {name: value for name, value in inventory(root).items() if name.startswith("src/")}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def state_binding(state):
    keys = ("execution_task_id", "item_id", "item", "state", "return_state", "stuck_return",
            "pending_question", "pending_decision", "thread_id", "workspace", "branch", "base_sha",
            "conversation_version", "pending_feedback", "replan")
    value = {key: state.get(key) for key in keys}
    # Workspace bookkeeping can change without authorizing a different decision.
    if value.get("workspace"):
        value["workspace"] = {key: value["workspace"].get(key) for key in ("identity", "root", "mode")}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def redact(text, environ=None):
    for key, value in (environ or os.environ).items():
        if re.search(r"TOKEN|PASSWORD|SECRET|API_KEY|CREDENTIAL", key, re.I) and len(value) >= 6:
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)[\w./+=-]+", r"\1[REDACTED]", text)
    return text[-32000:]


def eligible(incident):
    return (incident.get("category") == "implementation" and
            incident.get("error_type") in IMPLEMENTATION_ERRORS and
            incident.get("phase") not in {"IDLE", "WAIT_REPLY", "WAIT_APPROVAL", "WAIT_MERGE", "WAIT_STUCK"})


@contextmanager
def exclusive(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RepairError("Another supervisor or worker owns " + str(path)) from error
        yield


def snapshot(source, destination):
    before = inventory(source)
    destination = Path(destination)
    destination.mkdir(parents=True)
    for relative in before:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(source) / relative, target)
    if inventory(source) != before or inventory(destination) != before:
        raise RepairError("Source changed during snapshot; retry with a stable checkout")
    return before


def definitions(path):
    tree = ast.parse(Path(path).read_text())
    return {node.name: ast.dump(node, include_attributes=False) for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in PROTECTED_FUNCTIONS}


def validate_patch(before, candidate):
    after = inventory(candidate)
    changed = [name for name in sorted(set(before) | set(after)) if before.get(name) != after.get(name)]
    source_changes, regressions = [], []
    for name in changed:
        if name not in after:
            raise RepairError("Automatic repairs cannot delete files: " + name)
        if after[name]["mode"] & 0o022:
            raise RepairError("Repair files cannot be group/world writable")
        if name in PROTECTED_FILES:
            raise RepairError("Protected repair boundary: " + name)
        if name.startswith("src/") and name.endswith(".py"):
            source_changes.append(name)
        elif (Path(name).parent == Path("tests") and name.startswith("tests/test_selfheal_regression_")
              and name.endswith(".py") and name not in before):
            regressions.append(name)
        else:
            raise RepairError("Repair may only modify Python source and add new regression tests: " + name)
    if not source_changes or not regressions:
        raise RepairError("Repair requires implementation changes and a new regression test")
    # The complete source/test inventory is checked, not the agent's claimed diff.
    return changed, regressions


def safe_test_environment(directory):
    directory = Path(directory)
    home = directory / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL", "TZ"}}
    env.update(HOME=str(home), CODEBOT_DATA_DIR=str(directory / "data"), PYTHONDONTWRITEBYTECODE="1")
    for kind in ("DATA", "CONFIG", "CACHE", "STATE"):
        env["XDG_" + kind + "_HOME"] = str(directory / kind.lower())
    return env


def run_process(argv, cwd, env, timeout, log, *, cancelled=None):
    # File-backed bounded-time output avoids pipe deadlocks during worker/agent crashes.
    with Path(log).open("w") as output:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if cancelled and cancelled():
                    raise RepairCancelled("Operator paused the repair")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                try:
                    process.wait(timeout=min(1, remaining))
                except subprocess.TimeoutExpired:
                    pass
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
        finally:
            # Even a successful CLI/test can leave background tools in its group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    with Path(log).open("rb") as stream:
        stream.seek(max(0, stream.seek(0, os.SEEK_END) - 64000))
        text = stream.read().decode("utf-8", errors="replace")
    return process.returncode, text


def tests_executed(output):
    matches = re.findall(r"Ran (\d+) tests?", output)
    return bool(matches and int(matches[-1]) > 0)


class Repairs:
    def __init__(self, source, data, *, attempts=2, daily_limit=3):
        self.source, self.data = Path(source).resolve(), Path(data).resolve()
        self.home = self.data / "supervisor"
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.attempts, self.daily_limit = attempts, daily_limit

    def event(self, kind, incident, **fields):
        id = uuid.uuid4().hex
        event = {"version": VERSION, "id": id, "kind": kind, "incident": incident.get("id"),
                 "created": time.time(), "task_id": incident.get("task_id"),
                 "provider": incident.get("provider"), "channel": incident.get("channel"),
                 "conversation": incident.get("conversation"), **fields}
        atomic_json(self.home / "notifications" / (id + ".json"), event)

    def admit(self, incident):
        if not eligible(incident):
            raise RepairError("Incident is not eligible for automatic source repair")
        fingerprint = hashlib.sha256(json.dumps([incident.get("epoch"), incident.get("task_id"),
            incident.get("phase"), incident.get("error_type"), incident.get("location")]).encode()).hexdigest()
        path = self.home / "incidents" / (fingerprint + ".json")
        previous = read_json(path, {})
        if previous.get("status") in {"recovered", "promoting", "observing", "human_required"}:
            raise RepairError("Incident already handled or awaiting recovery")
        used = previous.get("attempts", 0)
        today = time.strftime("%Y-%m-%d", time.gmtime())
        budget_path = self.home / "budget.json"
        budget = read_json(budget_path, {"day": today, "used": 0})
        if budget["day"] != today:
            budget = {"day": today, "used": 0}
        if used >= self.attempts or budget["used"] >= self.daily_limit:
            raise RepairError("Persistent repair budget exhausted")
        record = {**incident, "id": fingerprint, "attempts": used + 1, "status": "snapshotted"}
        atomic_json(budget_path, {"day": today, "used": budget["used"] + 1})
        atomic_json(path, record)  # A crash may overcount, never admit an unbudgeted candidate.
        folder = self.home / "repairs" / fingerprint / str(used + 1)
        folder.mkdir(parents=True, mode=0o700)
        record["snapshot"] = snapshot(self.source, folder / "before")
        if incident.get("epoch") != epoch(folder / "before"):
            raise RepairError("Incident refers to a different runtime epoch")
        snapshot(folder / "before", folder / "candidate")
        atomic_json(folder / "record.json", record)
        self.event("repair_started", record)
        return folder, path, record

    def validate(self, folder, *, cancelled=None):
        folder = Path(folder)
        before, candidate = folder / "before", folder / "candidate"
        before_values = read_json(folder / "record.json")["snapshot"]
        if inventory(before) != before_values:
            raise RepairError("Repair baseline changed after snapshot")
        changed, regression = validate_patch(before_values, candidate)
        candidate_values = inventory(candidate)
        for name in changed:
            if name.startswith("src/"):
                try:
                    ast.parse((candidate / name).read_text())
                except SyntaxError as error:
                    raise RepairError("Candidate Python syntax is invalid: " + name) from error
                try:
                    old_definitions = definitions(before / name) if (before / name).exists() else {}
                except SyntaxError:
                    if name == "src/main.py":
                        raise RepairError("Syntax repair of approval-bearing main.py requires manual review")
                    old_definitions = {}
                if definitions(candidate / name) != old_definitions:
                    raise RepairError("Repair changes protected approval/recovery functions")
        # Add new regression tests to old source: they must reproduce a failure.
        reproduction = folder / "reproduction"
        snapshot(before, reproduction)
        for name in regression:
            shutil.copy2(candidate / name, reproduction / name)
        for root, pattern, expected in ((reproduction, "test_selfheal_regression_*.py", False),
                                        (candidate, "test_selfheal_regression_*.py", True)):
            env = safe_test_environment(folder / "validation" / root.name)
            code, output = run_process([os.sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-p", pattern],
                root, env, 300, folder / (root.name + ".log"), cancelled=cancelled)
            if not tests_executed(output) or (code == 0) != expected or re.search(r"skipped=\d+", output):
                raise RepairError("Regression must fail on old code and pass on repaired code")
        env = safe_test_environment(folder / "validation" / "suite")
        code, output = run_process([os.sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                                  candidate, env, 900, folder / "suite.log", cancelled=cancelled)
        if code or not tests_executed(output):
            raise RepairError("Full candidate regression suite must pass without live credentials/data")
        if inventory(candidate) != candidate_values or inventory(before) != before_values:
            raise RepairError("Tests modified candidate source; refusing unvalidated promotion")
        atomic_json(folder / "validated.json", {"inventory": candidate_values, "before": before_values, "files": changed})
        return changed

    def promote(self, folder, changed):
        folder = Path(folder)
        before, candidate = folder / "before", folder / "candidate"
        validated = read_json(folder / "validated.json")
        if not validated or validated["files"] != changed or validated["inventory"] != inventory(candidate):
            raise RepairError("Candidate is unvalidated or changed after validation")
        # Validate the entire snapshot, including untouched dirty user files.
        if inventory(before) != validated["before"] or inventory(self.source) != validated["before"]:
            raise RepairError("Live source changed while repair ran; refusing stale promotion")
        images = {name: {"before": validated["before"].get(name), "after": validated["inventory"][name]}
                  for name in changed}
        transaction = {"version": VERSION, "folder": str(folder), "files": changed,
                       "images": images, "status": "promoting"}
        atomic_json(self.home / "promotion.json", transaction)
        for name in changed:
            if file_value(self.source / name) != images[name]["before"]:
                raise RepairError("Concurrent source edit during promotion: " + name)
            if file_value(candidate / name) != images[name]["after"]:
                raise RepairError("Candidate changed during promotion: " + name)
            self._replace(candidate / name, self.source / name, expected=images[name]["after"])
        if inventory(self.source) != validated["inventory"]:
            raise RepairError("Source changed during promotion")
        atomic_json(self.home / "promotion.json", {**transaction, "status": "observing"})
        return epoch(self.source)

    @staticmethod
    def _replace(source, destination, *, expected=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            shutil.copy2(source, temporary)
            if expected is not None and file_value(temporary) != expected:
                raise RepairError("Replacement bytes differ from validated image")
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            sync_directory(destination.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def rollback(self):
        transaction = read_json(self.home / "promotion.json")
        if not transaction or transaction["status"] not in {"promoting", "observing"}:
            return
        folder = Path(transaction["folder"])
        if not folder.resolve().is_relative_to((self.home / "repairs").resolve()):
            raise RepairError("Invalid promotion recovery path")
        for name in transaction["files"]:
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts or not (
                    name.startswith("src/") and name.endswith(".py") or
                    name.startswith("tests/test_selfheal_regression_") and name.endswith(".py")):
                raise RepairError("Invalid promotion recovery file")
            for root in (self.source, folder / "before", folder / "candidate"):
                if root.is_symlink() or any((root / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)):
                    raise RepairError("Symlink in promotion recovery path")
        images = transaction["images"]
        # Verify backups before restoring any file; never trust mutable postimages.
        for name in transaction["files"]:
            if file_value(folder / "before" / name) != images[name]["before"]:
                raise RepairError("Promotion backup changed; manual recovery required")
        conflicts = []
        for name in transaction["files"]:
            current = file_value(self.source / name)
            old, new = images[name]["before"], images[name]["after"]
            if current == old:
                continue  # Crash occurred before applying this file, or already restored.
            if current != new:
                conflicts.append(name)
                continue
            if old is None:
                (self.source / name).unlink()
                sync_directory((self.source / name).parent)
            else:
                self._replace(folder / "before" / name, self.source / name, expected=old)
        atomic_json(self.home / "promotion.json", {**transaction,
                    "status": "conflict" if conflicts else "rolled_back", "conflicts": conflicts})
        if conflicts:
            raise RepairError("Rollback preserved concurrent user edits: " + ", ".join(conflicts))
        (self.home / "recovery.json").unlink(missing_ok=True)
