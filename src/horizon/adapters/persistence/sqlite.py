from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from horizon.domain.common import canonical_json, digest, timestamp, utc_now
from horizon.domain.errors import Conflict, IntegrityError, NotFound, PolicyDenied
from horizon.domain.events import Event, NewEvent
from horizon.domain.run import Run, project
from horizon.domain.task import TaskSpec

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE tasks(task_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE task_specs(
    task_id TEXT NOT NULL REFERENCES tasks, spec_version INTEGER NOT NULL,
    spec_json TEXT NOT NULL, spec_sha256 TEXT NOT NULL,
    PRIMARY KEY(task_id, spec_version)
);
CREATE TABLE runs(
    run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks, created_at TEXT NOT NULL
);
CREATE TABLE events(
    event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs,
    seq INTEGER NOT NULL CHECK(seq > 0), event_json TEXT NOT NULL,
    UNIQUE(run_id, seq)
);
CREATE TABLE run_views(
    run_id TEXT PRIMARY KEY REFERENCES runs,
    last_event_seq INTEGER NOT NULL, state_json TEXT NOT NULL
);
CREATE TABLE commands(
    scope TEXT NOT NULL, command_key TEXT NOT NULL, request_hash TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs, result_seq INTEGER NOT NULL,
    PRIMARY KEY(scope, command_key)
);
CREATE TABLE checkpoints(
    checkpoint_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs,
    event_seq INTEGER NOT NULL, manifest_sha256 TEXT NOT NULL, workspace_revision TEXT NOT NULL
);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER specs_no_update BEFORE UPDATE ON task_specs
BEGIN SELECT RAISE(ABORT, 'task specifications are immutable'); END;
CREATE TRIGGER specs_no_delete BEFORE DELETE ON task_specs
BEGIN SELECT RAISE(ABORT, 'task specifications are immutable'); END;
PRAGMA user_version=1;
COMMIT;
"""


class SQLiteEventStore:
    """One SQLite transaction covers events, idempotency receipts and derived views."""

    def __init__(
        self, path: str | Path, clock: Callable[[], datetime] = utc_now, *, read_only: bool = False
    ):
        self.path = Path(path)
        self.clock = clock
        self.read_only = read_only
        if read_only and not self.path.is_file():
            raise NotFound("Control database does not exist")
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise IntegrityError(f"Unsupported database schema version: {version}")
            if read_only:
                if version != 1:
                    raise IntegrityError("Control database has not been initialized")
                return
            db.execute("PRAGMA journal_mode=WAL")
            if version == 0:
                try:
                    db.executescript(SCHEMA)
                except sqlite3.OperationalError:
                    if db.in_transaction:
                        db.rollback()
                    if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                        raise

    @contextmanager
    def _connection(self):
        target = self.path.resolve().as_uri() + "?mode=ro" if self.read_only else str(self.path)
        db = sqlite3.connect(target, timeout=5, isolation_level=None, uri=self.read_only)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _write(self):
        if self.read_only:
            raise PolicyDenied("This connection is read-only")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _events(db: sqlite3.Connection, run_id: str, at: int | None = None) -> list[Event]:
        if db.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
            raise NotFound(f"Run not found: {run_id}")
        rows = db.execute(
            "SELECT event_json FROM events WHERE run_id=? AND seq<=? ORDER BY seq",
            (run_id, at if at is not None else 2**63 - 1),
        ).fetchall()
        try:
            return [Event.model_validate_json(row[0]) for row in rows]
        except ValueError as exc:
            raise IntegrityError("Invalid event schema") from exc

    def events(self, run_id: str, at: int | None = None) -> list[Event]:
        with self._connection() as db:
            return self._events(db, run_id, at)

    def get(self, run_id: str, at: int | None = None) -> Run:
        # The cache is never the recovery authority, even when its cursor looks current.
        return project(self.events(run_id, at))

    @staticmethod
    def _save_spec(db: sqlite3.Connection, task: TaskSpec) -> None:
        row = db.execute(
            "SELECT spec_sha256 FROM task_specs WHERE task_id=? AND spec_version=?",
            (task.task_id, task.spec_version),
        ).fetchone()
        if row:
            if row[0] != task.sha256:
                raise Conflict("Task ID/version already identifies a different immutable contract")
            return
        db.execute(
            "INSERT INTO task_specs VALUES (?,?,?,?)",
            (task.task_id, task.spec_version, canonical_json(task), task.sha256),
        )

    @staticmethod
    def _receipt(db, scope: str, key: str, request_hash: str) -> tuple[str, int] | None:
        row = db.execute(
            "SELECT request_hash, run_id, result_seq FROM commands WHERE scope=? AND command_key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise Conflict("Idempotency key was already used for a different request")
        return row["run_id"], row["result_seq"]

    def _append(
        self, db, run_id: str, previous: list[Event], proposed: Iterable[NewEvent], key: str
    ):
        events = list(previous)
        transaction_time = self.clock()
        now = timestamp(transaction_time)
        if events and now < events[-1].created_at:
            raise IntegrityError("Clock moved backwards; refusing non-monotonic history")
        for item in proposed:
            event_time = timestamp(item.occurred_at or transaction_time)
            if event_time > now or (events and event_time < events[-1].created_at):
                raise IntegrityError("Event occurrence time is outside the append interval")
            body = {
                **item.model_dump(mode="json"),
                "event_id": f"evt_{uuid4().hex}",
                "run_id": run_id,
                "seq": len(events) + 1,
                "created_at": event_time,
                "causation_id": events[-1].event_id if events else None,
                "correlation_id": key,
                "previous_hash": events[-1].event_hash if events else "",
                "event_hash": "",
            }
            event = Event(**body)
            event = event.model_copy(update={"event_hash": event.calculated_hash()})
            events.append(event)
        state = project(events)  # validate the complete proposed transition before any event write
        for event in events[len(previous) :]:
            if event.event_type == "TASK_SPEC_AMENDED":
                self._save_spec(db, TaskSpec.model_validate(event.payload["task"]))
            db.execute(
                "INSERT INTO events VALUES (?,?,?,?)",
                (event.event_id, run_id, event.seq, canonical_json(event)),
            )
            if event.event_type == "CHECKPOINT_COMMITTED":
                p = event.payload
                db.execute(
                    "INSERT INTO checkpoints VALUES (?,?,?,?,?)",
                    (
                        p["checkpoint_id"],
                        run_id,
                        p["event_seq"],
                        p["manifest_sha256"],
                        p["workspace_revision"],
                    ),
                )
        db.execute(
            "INSERT INTO run_views VALUES (?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
            "last_event_seq=excluded.last_event_seq, state_json=excluded.state_json",
            (run_id, state.seq, canonical_json(state.as_dict())),
        )
        return state

    def create(self, task: TaskSpec, key: str) -> Run:
        request_hash = digest({"operation": "create", "task": task.model_dump(mode="json")})
        with self._write() as db:
            duplicate = self._receipt(db, "create", key, request_hash)
            if duplicate:
                return project(self._events(db, *duplicate))
            now = self.clock()
            run_id = f"run_{uuid4().hex}"
            db.execute("INSERT OR IGNORE INTO tasks VALUES (?,?)", (task.task_id, timestamp(now)))
            self._save_spec(db, task)
            db.execute("INSERT INTO runs VALUES (?,?,?)", (run_id, task.task_id, timestamp(now)))
            event = NewEvent(
                event_type="RUN_CREATED",
                payload={
                    "task": task.model_dump(mode="json"),
                    "deadline_at": timestamp(
                        now + timedelta(seconds=task.budgets.max_wall_time_seconds)
                    ),
                },
            )
            state = self._append(db, run_id, [], [event], key)
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?,?)",
                ("create", key, request_hash, run_id, state.seq),
            )
            return state

    def command(
        self,
        run_id: str,
        key: str,
        request: dict[str, Any],
        decide: Callable[[Run], Iterable[NewEvent]],
        expected_seq: int | None = None,
    ) -> Run:
        request_hash = digest(request)
        with self._write() as db:
            duplicate = self._receipt(db, run_id, key, request_hash)
            if duplicate:
                return project(self._events(db, *duplicate))
            previous = self._events(db, run_id)
            state = project(previous)
            if expected_seq is not None and state.seq != expected_seq:
                raise Conflict("Event stream changed; reload before retrying the operation")
            events = list(decide(state))
            state = self._append(db, run_id, previous, events, key)
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?,?)",
                (run_id, key, request_hash, run_id, state.seq),
            )
            return state

    def rebuild_view(self, run_id: str) -> Run:
        with self._write() as db:
            state = project(self._events(db, run_id))
            db.execute(
                "INSERT INTO run_views VALUES (?,?,?) ON CONFLICT(run_id) DO UPDATE SET "
                "last_event_seq=excluded.last_event_seq, state_json=excluded.state_json",
                (run_id, state.seq, canonical_json(state.as_dict())),
            )
            return state

    def list_runs(self) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute("SELECT run_id FROM runs ORDER BY created_at, run_id").fetchall()
            return [project(self._events(db, row[0])).as_dict() for row in rows]

    def export_jsonl(self, run_id: str) -> str:
        events = self.events(run_id)
        project(events)
        return "".join(canonical_json(event) + "\n" for event in events)

    @staticmethod
    def replay_jsonl(content: str) -> Run:
        try:
            events = [
                Event.model_validate(json.loads(line)) for line in content.splitlines() if line
            ]
        except ValueError as exc:
            raise IntegrityError("Malformed trace JSONL") from exc
        return project(events)
