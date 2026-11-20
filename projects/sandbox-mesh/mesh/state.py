from __future__ import annotations

import contextlib
import json
import os
import pathlib
import sqlite3
import threading
import time
import uuid


ACTIVE_STATES = {"queued", "cloning", "detecting", "building", "starting", "running", "stopping"}
EXECUTION_STATES = {"cloning", "detecting", "building", "starting", "running", "stopping"}
TRANSITIONS = {
    "queued": {"cloning", "failed", "stopped", "destroyed"},
    "cloning": {"detecting", "failed", "destroyed"},
    "detecting": {"building", "starting", "failed", "destroyed"},
    "building": {"starting", "failed", "destroyed"},
    "starting": {"running", "failed", "destroyed"},
    "running": {"stopping", "stopped", "failed", "destroyed"},
    "stopping": {"stopped", "failed", "destroyed"},
    "stopped": {"queued", "destroyed"},
    "failed": {"queued", "destroyed"},
    "destroyed": set(),
}


class StateError(RuntimeError):
    pass


class CapacityError(StateError):
    pass


class StateStore:
    def __init__(self, path: pathlib.Path, concurrency_cap: int = 1):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.concurrency_cap = concurrency_cap
        self.lock = threading.RLock()
        self._initialize()

    @contextlib.contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS sandboxes (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                commit_sha TEXT,
                status TEXT NOT NULL,
                queued_at REAL,
                queue_reason TEXT,
                agent_status TEXT NOT NULL DEFAULT 'waiting',
                agent_started_at REAL,
                agent_last_heartbeat REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                expires_at REAL,
                container_id TEXT,
                container_name TEXT,
                image TEXT,
                repo_path TEXT,
                build_kind TEXT,
                run_command TEXT,
                app_port INTEGER,
                host_port INTEGER,
                network_name TEXT,
                egress_id TEXT,
                proxy_ticket_hash TEXT,
                proxy_url TEXT,
                error TEXT,
                metadata TEXT NOT NULL DEFAULT '{}'
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(sandboxes)")}
            additions = {
                "container_name": "TEXT",
                "expires_at": "REAL",
                "image": "TEXT",
                "repo_path": "TEXT",
                "build_kind": "TEXT",
                "run_command": "TEXT",
                "app_port": "INTEGER",
                "host_port": "INTEGER",
                "network_name": "TEXT",
                "egress_id": "TEXT",
                "proxy_ticket_hash": "TEXT",
                "proxy_url": "TEXT",
                "queued_at": "REAL",
                "queue_reason": "TEXT",
                "agent_status": "TEXT NOT NULL DEFAULT 'legacy'",
                "agent_started_at": "REAL",
                "agent_last_heartbeat": "REAL",
            }
            for name, kind in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE sandboxes ADD COLUMN {name} {kind}")
            db.execute("UPDATE sandboxes SET queued_at=created_at WHERE queued_at IS NULL")
            db.execute("UPDATE sandboxes SET agent_status='waiting' WHERE status='queued'")
            db.execute("UPDATE sandboxes SET agent_status='legacy' WHERE status='running' AND agent_started_at IS NULL AND agent_last_heartbeat IS NULL")
            db.execute("UPDATE sandboxes SET agent_status='waiting' WHERE status='queued' AND agent_status='starting'")
            db.execute("CREATE INDEX IF NOT EXISTS sandboxes_status_updated ON sandboxes(status, updated_at)")
            db.execute("CREATE INDEX IF NOT EXISTS sandboxes_queue_order ON sandboxes(status, queued_at, id)")
            db.execute("CREATE INDEX IF NOT EXISTS sandboxes_expires ON sandboxes(expires_at)")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def create(self, url: str, metadata: dict | None = None) -> dict:
        now = time.time()
        identifier = uuid.uuid4().hex[:12]
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO sandboxes(id,url,status,queued_at,agent_status,created_at,updated_at,metadata) VALUES(?,?,?,?,?,?,?,?)",
                (identifier, url, "queued", now, "waiting", now, now, json.dumps(metadata or {}, separators=(",", ":"))),
            )
            db.execute("COMMIT")
        return self.get(identifier)

    def requeue(self, identifier: str) -> dict:
        now = time.time()
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM sandboxes WHERE id=?", (identifier,)).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise StateError("sandbox record does not exist")
            if row["status"] not in {"failed", "stopped"}:
                db.execute("ROLLBACK")
                raise StateError("only a failed or stopped sandbox can be restarted")
            db.execute("UPDATE sandboxes SET status='queued',queued_at=?,agent_status='waiting',agent_started_at=NULL,agent_last_heartbeat=NULL,error=NULL,queue_reason=NULL,expires_at=NULL,updated_at=? WHERE id=?", (now, now, identifier))
            db.execute("COMMIT")
        return self.get(identifier)

    def active_count(self) -> int:
        keys = tuple(sorted(EXECUTION_STATES))
        with self._connect() as db:
            return int(db.execute(
                "SELECT COUNT(*) FROM sandboxes WHERE status IN (" + ",".join("?" for _ in keys) + ") OR (status='failed' AND (container_id IS NOT NULL OR network_name IS NOT NULL OR egress_id IS NOT NULL))",
                keys,
            ).fetchone()[0])

    def claim_next(self) -> dict | None:
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            keys = tuple(sorted(EXECUTION_STATES))
            active_count = int(db.execute(
                "SELECT COUNT(*) FROM sandboxes WHERE status IN (" + ",".join("?" for _ in keys) + ") OR (status='failed' AND (container_id IS NOT NULL OR network_name IS NOT NULL OR egress_id IS NOT NULL))",
                keys,
            ).fetchone()[0])
            if active_count >= self.concurrency_cap:
                db.execute("COMMIT")
                return None
            row = db.execute("SELECT id FROM sandboxes WHERE status='queued' ORDER BY queued_at,id LIMIT 1").fetchone()
            if row is None:
                db.execute("COMMIT")
                return None
            identifier = row["id"]
            db.execute("UPDATE sandboxes SET status='cloning',queue_reason=NULL,agent_status='starting',agent_started_at=NULL,agent_last_heartbeat=NULL,updated_at=? WHERE id=? AND status='queued'", (time.time(), identifier))
            db.execute("COMMIT")
        return self.get(identifier)

    def queue_position(self, identifier: str) -> int | None:
        record = self.get(identifier)
        if not record or record["status"] != "queued":
            return None
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM sandboxes WHERE status='queued' AND (queued_at<? OR (queued_at=? AND id<=?))",
                (record["queued_at"], record["queued_at"], identifier),
            ).fetchone()
        return int(row[0])

    def cancel_queued(self, identifier: str, status: str = "destroyed") -> dict:
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM sandboxes WHERE id=?", (identifier,)).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise StateError("sandbox record does not exist")
            if row["status"] != "queued":
                db.execute("ROLLBACK")
                raise StateError("only a queued sandbox can be cancelled")
            if status not in {"stopped", "destroyed"}:
                db.execute("ROLLBACK")
                raise StateError("queued cancellation status must be stopped or destroyed")
            message = "stopped while waiting in queue" if status == "stopped" else "destroyed while waiting in queue"
            db.execute(
                "UPDATE sandboxes SET status=?,agent_status='stopped',queue_reason=NULL,error=?,expires_at=NULL,updated_at=? WHERE id=? AND status='queued'",
                (status, message, time.time(), identifier),
            )
            db.execute("COMMIT")
        return self.get(identifier)

    def set_queue_reason(self, identifier: str, reason: str | None) -> dict:
        return self.update(identifier, queue_reason=reason)

    def get(self, identifier: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM sandboxes WHERE id=?", (identifier,)).fetchone()
        return self._decode(row) if row else None

    def list(self, limit: int = 100) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM sandboxes ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
        return [self._decode(row) for row in rows]

    def active(self) -> list[dict]:
        keys = tuple(sorted(ACTIVE_STATES))
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM sandboxes WHERE status IN (" + ",".join("?" for _ in keys) + ") OR (status='failed' AND (container_id IS NOT NULL OR network_name IS NOT NULL OR egress_id IS NOT NULL)) ORDER BY created_at",
                keys,
            ).fetchall()
        return [self._decode(row) for row in rows]

    def queued(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM sandboxes WHERE status='queued' ORDER BY queued_at,id").fetchall()
        return [self._decode(row) for row in rows]

    def update(self, identifier: str, **values) -> dict:
        allowed = {
            "status", "commit_sha", "container_id", "container_name", "image", "repo_path",
            "build_kind", "run_command", "app_port", "host_port", "network_name", "egress_id",
            "proxy_ticket_hash", "proxy_url", "error", "expires_at", "metadata", "queue_reason",
            "agent_status", "agent_started_at", "agent_last_heartbeat",
        }
        if not values or set(values) - allowed:
            raise StateError("invalid sandbox state update")
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM sandboxes WHERE id=?", (identifier,)).fetchone()
            if row is None:
                db.execute("ROLLBACK")
                raise StateError("sandbox record does not exist")
            next_status = values.get("status")
            current_status = row["status"]
            if next_status and next_status != current_status and next_status not in TRANSITIONS.get(current_status, set()):
                db.execute("ROLLBACK")
                raise StateError(f"invalid state transition {current_status!r} -> {next_status!r}")
            assignments = []
            parameters = []
            for key, value in values.items():
                if key == "metadata":
                    value = json.dumps(value, separators=(",", ":"))
                elif key == "run_command" and value is not None:
                    value = json.dumps(value, separators=(",", ":"))
                assignments.append(f"{key}=?")
                parameters.append(value)
            assignments.append("updated_at=?")
            parameters.extend([time.time(), identifier])
            db.execute("UPDATE sandboxes SET " + ",".join(assignments) + " WHERE id=?", parameters)
            db.execute("COMMIT")
        record = self.get(identifier)
        if record is None:
            raise StateError("sandbox record disappeared")
        return record

    def expired(self, now: float | None = None) -> list[dict]:
        current = now if now is not None else time.time()
        with self._connect() as db:
            rows = db.execute("SELECT * FROM sandboxes WHERE status='running' AND expires_at IS NOT NULL AND expires_at<=?", (current,)).fetchall()
        return [self._decode(row) for row in rows]

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict:
        value = dict(row)
        value["metadata"] = json.loads(value["metadata"])
        if value.get("run_command"):
            value["run_command"] = json.loads(value["run_command"])
        return value
