"""Durable execution outcomes; independent of the optional progress telemetry."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid


ENTITIES = ("task", "phase_attempt", "operation", "check_run", "finding",
            "checkpoint", "artifact", "delivery_step", "feedback", "recovery",
            "incident", "delivery_receipt", "contact", "provenance", "check_environment",
            "conversation", "conversation_input")


class ExecutionStore:
    def reusable_check(self, task_id, identity):
        rows = self.list("check_run", task_id, identity=identity, status="complete")
        return rows[0] if rows else None

    def record_check(self, task_id, identity, *, result, status="complete", tdd=None):
        if tdd not in (None, "RED", "GREEN"):
            raise ValueError("TDD evidence must be RED or GREEN")
        return self.put("check_run", task_id=task_id, identity=identity, status=status,
                        data={"result": result, "tdd": tdd})

    def interrupt_running(self, task_id):
        with self.connection() as db:
            for entity in ("operation", "check_run", "phase_attempt"):
                db.execute(f"UPDATE {entity} SET status='interrupted',updated=? "
                           "WHERE task_id=? AND status='running'", (time.time(), task_id))

    def task_identity(self, state, repo):
        """Derivable for legacy state even if its new cursor was never saved."""
        if state.get("execution_task_id"):
            return state["execution_task_id"]
        if state.get("workspace"):
            raw = json.dumps(["workspace-feature", state["workspace"]["identity"],
                              state.get("item_id") or state.get("item"), state.get("slug")])
            return hashlib.sha256(raw.encode()).hexdigest()
        raw = json.dumps([str(Path(repo).resolve()), state.get("branch"),
                          state.get("item_id") or state.get("item"), state.get("base_sha")])
        return hashlib.sha256(raw.encode()).hexdigest()

    def repository_identity(self, state, repo, repository):
        """Independent operation namespace beneath a stable workspace feature."""
        raw = json.dumps([self.task_identity(state, repo), repository.identity])
        return hashlib.sha256(raw.encode()).hexdigest()

    def record_repository(self, entity, state, workspace, repository, identity, data,
                          *, status="running"):
        task_id = self.repository_identity(state, workspace.root, repository)
        return self.put(entity, task_id=task_id, identity=identity, status=status,
                        parent_id=self.task_identity(state, workspace.root), data=data)

    def reconcile(self, state, repo):
        task_id = self.task_identity(state, repo)
        state["execution_task_id"] = task_id
        if not self.get("task", task_id):
            self.put("task", task_id=task_id, id=task_id, data={"branch": state.get("branch")},
                     status="active")
        checkpoints = self.list("checkpoint", task_id, status="complete")
        if checkpoints:
            checkpoint = checkpoints[0]
            state["execution_checkpoint_id"] = checkpoint["id"]
            state.update(checkpoint["data"].get("state_patch", {}))
        return task_id

    def begin_attempt(self, state, repo):
        task_id = self.reconcile(state, repo)
        phase = state.get("state", "IDLE")
        active = self.list("phase_attempt", task_id, identity=phase, status="running")
        if active:
            id = active[0]["id"]
        else:
            id = self.put("phase_attempt", task_id=task_id, identity=phase,
                          data={"phase": phase})
        state["execution_attempt_id"] = id
        return id

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise ValueError(f"Execution store schema {version} is newer than supported")
            for entity in ENTITIES:
                db.execute(f"CREATE TABLE IF NOT EXISTS {entity} ("
                    "id TEXT PRIMARY KEY, task_id TEXT NOT NULL, parent_id TEXT, "
                    "status TEXT NOT NULL, identity TEXT, created REAL NOT NULL, "
                    "updated REAL NOT NULL, data TEXT NOT NULL)")
                db.execute(f"CREATE INDEX IF NOT EXISTS {entity}_task_identity "
                           f"ON {entity}(task_id,identity,status)")
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=5000")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _entity(entity):
        if entity not in ENTITIES:
            raise ValueError(f"Unknown execution entity: {entity}")
        return entity

    def put(self, entity, *, task_id, data, status="running", identity=None,
            parent_id=None, id=None):
        entity = self._entity(entity)
        id = id or uuid.uuid4().hex
        now = time.time()
        encoded = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self.connection() as db:
            db.execute(f"INSERT INTO {entity} VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
                "identity=excluded.identity,updated=excluded.updated,data=excluded.data",
                (id, task_id, parent_id, status, identity, now, now, encoded))
        return id

    def get(self, entity, id):
        with self.connection() as db:
            row = db.execute(f"SELECT * FROM {self._entity(entity)} WHERE id=?", (id,)).fetchone()
        return self._decode(row) if row else None

    @staticmethod
    def _decode(row):
        result = dict(row)
        result["data"] = json.loads(result["data"])
        return result

    def list(self, entity, task_id, *, identity=None, status=None):
        conditions, args = ["task_id=?"], [task_id]
        for name, value in (("identity", identity), ("status", status)):
            if value is not None:
                conditions.append(f"{name}=?")
                args.append(value)
        with self.connection() as db:
            rows = db.execute(f"SELECT * FROM {self._entity(entity)} WHERE "
                              + " AND ".join(conditions) + " ORDER BY updated DESC", args).fetchall()
        return [self._decode(row) for row in rows]

    def record(self, entity, state, repo, identity, data, *, status="pending"):
        """Idempotent versioned event; additive tables keep v1 readers compatible."""
        task_id = self.task_identity(state, repo)
        id = hashlib.sha256(json.dumps([entity, task_id, identity]).encode()).hexdigest()
        entity = self._entity(entity)
        now = time.time()
        with self.connection() as db:
            db.execute(f"INSERT OR IGNORE INTO {entity} VALUES(?,?,?,?,?,?,?,?)",
                (id, task_id, None, status, identity, now, now,
                 json.dumps({"version": 1, **data}, ensure_ascii=False, sort_keys=True)))
        return self.get(entity, id)

    def update(self, entity, row, *, status=None, **data):
        self.put(entity, task_id=row["task_id"], id=row["id"], identity=row["identity"],
                 status=status or row["status"], data={**row["data"], **data})
        return self.get(entity, row["id"])
