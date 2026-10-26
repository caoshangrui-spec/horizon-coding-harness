from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, StrictInt

from horizon.domain.common import Contract, digest, timestamp, utc_now
from horizon.domain.errors import Conflict, IntegrityError, PolicyDenied, ProviderError
from horizon.domain.task import Identifier, Text
from horizon.reliability.retry import (
    RetryDecision,
    RetryPolicy,
    RetryState,
    classify_provider_failure,
    decide_retry,
)

BREAKER_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_health_schema(
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    version INTEGER NOT NULL
);
INSERT OR IGNORE INTO provider_health_schema(singleton, version) VALUES (1, 1);
CREATE TABLE IF NOT EXISTS provider_health(
    key_hash TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    credential_ref TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('closed', 'open', 'half_open')),
    consecutive_failures INTEGER NOT NULL CHECK(consecutive_failures >= 0),
    next_probe_at TEXT,
    probe_token TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_health_events(
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash TEXT NOT NULL REFERENCES provider_health(key_hash),
    event_type TEXT NOT NULL,
    failure_class TEXT,
    retry_action TEXT,
    retry_reason TEXT,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS provider_health_events_key
    ON provider_health_events(key_hash, event_id);
"""

BreakerState = Literal["closed", "open", "half_open"]
ProviderOperation = Literal["planning", "execution", "probe", "intent", "summarization"]


class ProviderHealthKey(Contract):
    endpoint: Text
    provider_id: Identifier
    model_id: Text
    operation: ProviderOperation
    credential_ref: Identifier

    @property
    def sha256(self) -> str:
        return digest(self)


class CircuitBreakerPolicy(Contract):
    failure_threshold: Annotated[StrictInt, Field(ge=1, le=10)] = 3
    cooldown_seconds: Annotated[StrictInt, Field(ge=1, le=3600)] = 30
    half_open_lease_seconds: Annotated[StrictInt, Field(ge=1, le=300)] = 30


class BreakerPermit(Contract):
    key_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    state_at_acquire: Literal["closed", "half_open"]
    probe_token: str | None = None


class ProviderHealthSnapshot(Contract):
    key: ProviderHealthKey
    state: BreakerState
    consecutive_failures: Annotated[StrictInt, Field(ge=0)]
    next_probe_at: datetime | None = None
    updated_at: datetime | None = None


class ProviderHealthEvent(Contract):
    event_id: Annotated[StrictInt, Field(gt=0)]
    event_type: Literal[
        "initialized",
        "transient_failure",
        "nontransient_failure_ignored",
        "opened",
        "probe_claimed",
        "probe_lease_expired",
        "probe_abandoned",
        "closed",
        "success",
        "late_result_ignored",
    ]
    failure_class: str | None = None
    retry_action: Literal["retry", "stop"] | None = None
    retry_reason: str | None = None
    occurred_at: datetime


class CircuitOpenError(PolicyDenied):
    def __init__(self, snapshot: ProviderHealthSnapshot):
        self.snapshot = snapshot
        next_probe = (
            snapshot.next_probe_at.astimezone(UTC).isoformat()
            if snapshot.next_probe_at is not None
            else "unknown"
        )
        super().__init__(
            f"Provider circuit is {snapshot.state}; next probe is not available before {next_probe}"
        )


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise IntegrityError("Provider health timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


class ProviderHealthStore:
    """SQLite projection plus append-only events for a single-host circuit breaker."""

    def __init__(self, path: str | Path, clock: Callable[[], datetime] = utc_now):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(BREAKER_SCHEMA)
            version = db.execute(
                "SELECT version FROM provider_health_schema WHERE singleton=1"
            ).fetchone()[0]
            if version != 1:
                raise IntegrityError(f"Unsupported provider health schema version: {version}")

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _write(self):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _key_values(key: ProviderHealthKey) -> tuple[str, str, str, str, str]:
        return (
            key.endpoint,
            key.provider_id,
            key.model_id,
            key.operation,
            key.credential_ref,
        )

    @staticmethod
    def _row_key(row: sqlite3.Row) -> tuple[str, str, str, str, str]:
        return tuple(
            row[name]
            for name in ("endpoint", "provider_id", "model_id", "operation", "credential_ref")
        )

    def _row(self, db: sqlite3.Connection, key: ProviderHealthKey) -> sqlite3.Row | None:
        row = db.execute(
            "SELECT * FROM provider_health WHERE key_hash=?",
            (key.sha256,),
        ).fetchone()
        if row is not None and self._row_key(row) != self._key_values(key):
            raise IntegrityError("Provider health key hash collision or row corruption")
        return row

    @staticmethod
    def _event(
        db: sqlite3.Connection,
        key_hash: str,
        event_type: str,
        occurred_at: str,
        *,
        failure_class: str | None = None,
        decision: RetryDecision | None = None,
    ) -> None:
        db.execute(
            "INSERT INTO provider_health_events"
            "(key_hash,event_type,failure_class,retry_action,retry_reason,occurred_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                key_hash,
                event_type,
                failure_class,
                decision.action if decision else None,
                decision.reason_code if decision else None,
                occurred_at,
            ),
        )

    def _initialize(
        self,
        db: sqlite3.Connection,
        key: ProviderHealthKey,
        now: str,
    ) -> sqlite3.Row:
        db.execute(
            "INSERT INTO provider_health VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (key.sha256, *self._key_values(key), "closed", 0, None, None, now),
        )
        self._event(db, key.sha256, "initialized", now)
        row = self._row(db, key)
        if row is None:
            raise IntegrityError("Provider health row was not created")
        return row

    @staticmethod
    def _snapshot_from_row(key: ProviderHealthKey, row: sqlite3.Row) -> ProviderHealthSnapshot:
        return ProviderHealthSnapshot(
            key=key,
            state=row["state"],
            consecutive_failures=row["consecutive_failures"],
            next_probe_at=_parse_time(row["next_probe_at"]),
            updated_at=_parse_time(row["updated_at"]),
        )

    def snapshot(self, key: ProviderHealthKey) -> ProviderHealthSnapshot:
        with self._connection() as db:
            row = self._row(db, key)
            if row is None:
                return ProviderHealthSnapshot(
                    key=key,
                    state="closed",
                    consecutive_failures=0,
                )
            return self._snapshot_from_row(key, row)

    def acquire(
        self,
        key: ProviderHealthKey,
        policy: CircuitBreakerPolicy,
    ) -> BreakerPermit:
        now_value = self.clock()
        now = timestamp(now_value)
        with self._write() as db:
            row = self._row(db, key) or self._initialize(db, key, now)
            if row["state"] == "closed":
                return BreakerPermit(key_hash=key.sha256, state_at_acquire="closed")

            if row["state"] == "half_open":
                updated_at = _parse_time(row["updated_at"])
                if updated_at is None:
                    raise IntegrityError("Half-open provider health row lacks an update time")
                if now_value < updated_at + timedelta(seconds=policy.half_open_lease_seconds):
                    raise CircuitOpenError(self._snapshot_from_row(key, row))
                self._event(db, key.sha256, "probe_lease_expired", now)

            next_probe_at = _parse_time(row["next_probe_at"])
            if row["state"] == "open" and (next_probe_at is None or now_value < next_probe_at):
                raise CircuitOpenError(self._snapshot_from_row(key, row))

            probe_token = f"probe_{uuid4().hex}"
            db.execute(
                "UPDATE provider_health SET state='half_open', probe_token=?, updated_at=? "
                "WHERE key_hash=?",
                (probe_token, now, key.sha256),
            )
            self._event(db, key.sha256, "probe_claimed", now)
            return BreakerPermit(
                key_hash=key.sha256,
                state_at_acquire="half_open",
                probe_token=probe_token,
            )

    def _checked_row(
        self,
        db: sqlite3.Connection,
        key: ProviderHealthKey,
        permit: BreakerPermit,
    ) -> sqlite3.Row:
        if permit.key_hash != key.sha256:
            raise Conflict("Circuit-breaker permit belongs to a different provider key")
        row = self._row(db, key)
        if row is None:
            raise Conflict("Circuit-breaker permit has no provider health row")
        if permit.probe_token is not None and (
            row["state"] != "half_open" or row["probe_token"] != permit.probe_token
        ):
            raise Conflict("Half-open circuit-breaker permit is stale")
        return row

    def abandon(
        self,
        key: ProviderHealthKey,
        permit: BreakerPermit,
    ) -> None:
        if permit.probe_token is None:
            return
        now = timestamp(self.clock())
        with self._write() as db:
            self._checked_row(db, key, permit)
            db.execute(
                "UPDATE provider_health SET state='open', next_probe_at=?, probe_token=NULL, "
                "updated_at=? WHERE key_hash=?",
                (now, now, key.sha256),
            )
            self._event(db, key.sha256, "probe_abandoned", now)

    def record_success(self, key: ProviderHealthKey, permit: BreakerPermit) -> None:
        now = timestamp(self.clock())
        with self._write() as db:
            row = self._checked_row(db, key, permit)
            if permit.probe_token is None and row["state"] != "closed":
                self._event(db, key.sha256, "late_result_ignored", now)
                return
            db.execute(
                "UPDATE provider_health SET state='closed', consecutive_failures=0, "
                "next_probe_at=NULL, probe_token=NULL, updated_at=? WHERE key_hash=?",
                (now, key.sha256),
            )
            self._event(db, key.sha256, "success", now)
            if row["state"] != "closed" or row["consecutive_failures"]:
                self._event(db, key.sha256, "closed", now)

    def record_failure(
        self,
        key: ProviderHealthKey,
        permit: BreakerPermit,
        policy: CircuitBreakerPolicy,
        *,
        transient: bool,
        failure_class: str,
        decision: RetryDecision,
    ) -> None:
        now_value = self.clock()
        now = timestamp(now_value)
        with self._write() as db:
            row = self._checked_row(db, key, permit)
            if permit.probe_token is None and row["state"] != "closed":
                self._event(
                    db,
                    key.sha256,
                    "late_result_ignored",
                    now,
                    failure_class=failure_class,
                    decision=decision,
                )
                return
            if not transient:
                if permit.probe_token is not None:
                    db.execute(
                        "UPDATE provider_health SET state='closed', consecutive_failures=0, "
                        "next_probe_at=NULL, probe_token=NULL, updated_at=? WHERE key_hash=?",
                        (now, key.sha256),
                    )
                self._event(
                    db,
                    key.sha256,
                    "nontransient_failure_ignored",
                    now,
                    failure_class=failure_class,
                    decision=decision,
                )
                if permit.probe_token is not None:
                    self._event(db, key.sha256, "closed", now)
                return

            failures = row["consecutive_failures"] + 1
            self._event(
                db,
                key.sha256,
                "transient_failure",
                now,
                failure_class=failure_class,
                decision=decision,
            )
            if permit.probe_token is not None or failures >= policy.failure_threshold:
                next_probe = timestamp(now_value + timedelta(seconds=policy.cooldown_seconds))
                db.execute(
                    "UPDATE provider_health SET state='open', consecutive_failures=?, "
                    "next_probe_at=?, probe_token=NULL, updated_at=? WHERE key_hash=?",
                    (failures, next_probe, now, key.sha256),
                )
                self._event(
                    db,
                    key.sha256,
                    "opened",
                    now,
                    failure_class=failure_class,
                    decision=decision,
                )
            else:
                db.execute(
                    "UPDATE provider_health SET consecutive_failures=?, updated_at=? "
                    "WHERE key_hash=?",
                    (failures, now, key.sha256),
                )

    def events(self, key: ProviderHealthKey) -> tuple[ProviderHealthEvent, ...]:
        with self._connection() as db:
            row = self._row(db, key)
            if row is None:
                return ()
            rows = db.execute(
                "SELECT * FROM provider_health_events WHERE key_hash=? ORDER BY event_id",
                (key.sha256,),
            ).fetchall()
            return tuple(
                ProviderHealthEvent(
                    event_id=item["event_id"],
                    event_type=item["event_type"],
                    failure_class=item["failure_class"],
                    retry_action=item["retry_action"],
                    retry_reason=item["retry_reason"],
                    occurred_at=_parse_time(item["occurred_at"]),
                )
                for item in rows
            )


class ProviderReliabilityController:
    """Auditable pre-dispatch breaker and post-dispatch failure classifier."""

    def __init__(
        self,
        store: ProviderHealthStore,
        key: ProviderHealthKey,
        *,
        breaker_policy: CircuitBreakerPolicy | None = None,
        retry_policy: RetryPolicy | None = None,
    ):
        self.store = store
        self.key = key
        self.breaker_policy = breaker_policy or CircuitBreakerPolicy()
        self.retry_policy = retry_policy or RetryPolicy()

    def acquire(self) -> BreakerPermit:
        return self.store.acquire(self.key, self.breaker_policy)

    def abandon(self, permit: BreakerPermit) -> None:
        self.store.abandon(self.key, permit)

    def record_success(self, permit: BreakerPermit) -> None:
        self.store.record_success(self.key, permit)

    def record_provider_failure(
        self,
        permit: BreakerPermit,
        error: ProviderError,
    ) -> RetryDecision:
        failure = classify_provider_failure(error)
        decision = decide_retry(
            self.retry_policy,
            RetryState(attempts_completed=1),
            failure,
            budget_allows_next_attempt=True,
            deadline_allows_wait=True,
        )
        self.store.record_failure(
            self.key,
            permit,
            self.breaker_policy,
            transient=failure.transient,
            failure_class=failure.category,
            decision=decision,
        )
        return decision
