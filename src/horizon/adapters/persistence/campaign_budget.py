from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from horizon.domain.common import timestamp, utc_now
from horizon.domain.errors import (
    BudgetExceeded,
    BudgetStop,
    BudgetStopReason,
    Conflict,
    IntegrityError,
    NotFound,
)
from horizon.domain.model import CampaignAttempt, CampaignBudget, CampaignSummary

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE campaigns(
    campaign_id TEXT PRIMARY KEY,
    currency TEXT NOT NULL,
    max_cost TEXT NOT NULL,
    max_cost_per_call TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE attempts(
    attempt_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns,
    request_hash TEXT NOT NULL,
    reserved_cost TEXT NOT NULL,
    actual_cost TEXT,
    status TEXT NOT NULL CHECK(status IN ('reserved', 'settled', 'unknown')),
    provider_trace_id TEXT,
    error_type TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TRIGGER campaigns_no_update BEFORE UPDATE ON campaigns
BEGIN SELECT RAISE(ABORT, 'campaign definitions are immutable'); END;
CREATE TRIGGER campaigns_no_delete BEFORE DELETE ON campaigns
BEGIN SELECT RAISE(ABORT, 'campaign definitions are immutable'); END;
CREATE TRIGGER attempts_no_delete BEFORE DELETE ON attempts
BEGIN SELECT RAISE(ABORT, 'campaign attempts are append-preserving'); END;
PRAGMA user_version=1;
COMMIT;
"""


class CampaignBudgetLedger:
    """A conservative cross-run ledger for paid provider attempts.

    Reserved and unknown attempts occupy their full ceiling. A settled attempt occupies its
    price-card estimate. No operation deletes history or treats an uncertain call as free.
    """

    def __init__(self, path: str | Path, clock: Callable[[], datetime] = utc_now):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise IntegrityError(f"Unsupported campaign ledger schema version: {version}")
            db.execute("PRAGMA journal_mode=WAL")
            if version == 0:
                db.executescript(SCHEMA)

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
    def _campaign_row(db: sqlite3.Connection, campaign_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM campaigns WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"Campaign not initialized: {campaign_id}")
        return row

    @staticmethod
    def _summary(db: sqlite3.Connection, campaign_id: str) -> CampaignSummary:
        campaign = CampaignBudgetLedger._campaign_row(db, campaign_id)
        settled = Decimal("0")
        reserved = Decimal("0")
        unknown = Decimal("0")
        for row in db.execute(
            "SELECT status, reserved_cost, actual_cost FROM attempts WHERE campaign_id=?",
            (campaign_id,),
        ):
            if row["status"] == "settled":
                settled += Decimal(row["actual_cost"])
            elif row["status"] == "reserved":
                reserved += Decimal(row["reserved_cost"])
            else:
                unknown += Decimal(row["reserved_cost"])
        occupied = settled + reserved + unknown
        maximum = Decimal(campaign["max_cost"])
        return CampaignSummary(
            campaign_id=campaign_id,
            currency=campaign["currency"],
            max_cost=maximum,
            settled_cost=settled,
            reserved_cost=reserved,
            unknown_cost=unknown,
            occupied_cost=occupied,
            remaining_cost=max(Decimal("0"), maximum - occupied),
        )

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> CampaignAttempt:
        return CampaignAttempt(
            attempt_id=row["attempt_id"],
            campaign_id=row["campaign_id"],
            request_hash=row["request_hash"],
            reserved_cost=Decimal(row["reserved_cost"]),
            actual_cost=(Decimal(row["actual_cost"]) if row["actual_cost"] is not None else None),
            status=row["status"],
            provider_trace_id=row["provider_trace_id"],
            error_type=row["error_type"],
        )

    def initialize(
        self,
        budget: CampaignBudget,
        *,
        provider_id: str,
        model_id: str,
    ) -> CampaignSummary:
        with self._write() as db:
            row = db.execute(
                "SELECT * FROM campaigns WHERE campaign_id=?",
                (budget.campaign_id,),
            ).fetchone()
            expected = (
                budget.currency,
                str(budget.max_cost),
                str(budget.max_cost_per_call),
                provider_id,
                model_id,
            )
            if row is None:
                db.execute(
                    "INSERT INTO campaigns VALUES (?,?,?,?,?,?,?)",
                    (*((budget.campaign_id,) + expected), timestamp(self.clock())),
                )
            else:
                actual = tuple(
                    row[key]
                    for key in (
                        "currency",
                        "max_cost",
                        "max_cost_per_call",
                        "provider_id",
                        "model_id",
                    )
                )
                if actual != expected:
                    raise Conflict("Campaign ID already has a different immutable definition")
            return self._summary(db, budget.campaign_id)

    def reserve(
        self,
        budget: CampaignBudget,
        attempt_id: str,
        request_hash: str,
        amount: Decimal,
    ) -> CampaignSummary:
        if amount <= 0:
            raise ValueError("A positive reservation is required")
        if amount > budget.max_cost_per_call:
            raise BudgetExceeded(
                "Provider call reservation exceeds the per-call cost limit",
                stop=BudgetStop(
                    reason_code=BudgetStopReason.CAMPAIGN_CALL_COST_LIMIT,
                    scope="campaign",
                    currency=budget.currency,
                    required_cost=amount,
                    available_cost=budget.max_cost_per_call,
                ),
            )
        with self._write() as db:
            campaign = self._campaign_row(db, budget.campaign_id)
            if (
                campaign["currency"] != budget.currency
                or Decimal(campaign["max_cost"]) != budget.max_cost
                or Decimal(campaign["max_cost_per_call"]) != budget.max_cost_per_call
            ):
                raise Conflict("Campaign budget does not match the persisted definition")
            duplicate = db.execute(
                "SELECT request_hash, reserved_cost FROM attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if duplicate is not None:
                if (
                    duplicate["request_hash"] != request_hash
                    or Decimal(duplicate["reserved_cost"]) != amount
                ):
                    raise Conflict("Attempt ID was already used for a different reservation")
                return self._summary(db, budget.campaign_id)
            summary = self._summary(db, budget.campaign_id)
            if summary.occupied_cost + amount > budget.max_cost:
                raise BudgetExceeded(
                    "Campaign cost ceiling would be exceeded",
                    stop=BudgetStop(
                        reason_code=BudgetStopReason.CAMPAIGN_COST_LIMIT,
                        scope="campaign",
                        currency=budget.currency,
                        required_cost=amount,
                        available_cost=summary.remaining_cost,
                    ),
                )
            now = timestamp(self.clock())
            db.execute(
                "INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id,
                    budget.campaign_id,
                    request_hash,
                    str(amount),
                    None,
                    "reserved",
                    None,
                    None,
                    now,
                    now,
                ),
            )
            return self._summary(db, budget.campaign_id)

    def settle(
        self,
        campaign_id: str,
        attempt_id: str,
        actual_cost: Decimal,
        provider_trace_id: str | None,
    ) -> CampaignSummary:
        if actual_cost < 0:
            raise ValueError("Actual cost cannot be negative")
        per_call_limit: Decimal
        with self._write() as db:
            row = db.execute(
                "SELECT * FROM attempts WHERE attempt_id=? AND campaign_id=?",
                (attempt_id, campaign_id),
            ).fetchone()
            if row is None:
                raise NotFound(f"Campaign attempt not found: {attempt_id}")
            if row["status"] == "settled":
                if (
                    Decimal(row["actual_cost"]) != actual_cost
                    or row["provider_trace_id"] != provider_trace_id
                ):
                    raise Conflict("Attempt was already settled with a different receipt")
                return self._summary(db, campaign_id)
            if row["status"] != "reserved":
                raise Conflict("Unknown usage must be reconciled explicitly before settlement")
            db.execute(
                "UPDATE attempts SET actual_cost=?, status='settled', provider_trace_id=?, "
                "updated_at=? WHERE attempt_id=?",
                (str(actual_cost), provider_trace_id, timestamp(self.clock()), attempt_id),
            )
            summary = self._summary(db, campaign_id)
            per_call_limit = Decimal(self._campaign_row(db, campaign_id)["max_cost_per_call"])
        # The provider may report more usage than was conservatively reserved. Commit the
        # receipt first, then surface the overrun; rolling back would erase a real charge.
        if actual_cost > per_call_limit:
            raise BudgetExceeded("Billed provider usage exceeded the per-call ceiling")
        if summary.occupied_cost > summary.max_cost:
            raise BudgetExceeded("Billed provider usage exceeded the campaign ceiling")
        return summary

    def mark_unknown(
        self,
        campaign_id: str,
        attempt_id: str,
        error_type: str,
    ) -> CampaignSummary:
        with self._write() as db:
            row = db.execute(
                "SELECT status, error_type FROM attempts WHERE attempt_id=? AND campaign_id=?",
                (attempt_id, campaign_id),
            ).fetchone()
            if row is None:
                raise NotFound(f"Campaign attempt not found: {attempt_id}")
            if row["status"] == "settled":
                raise Conflict("A settled attempt cannot become unknown")
            if row["status"] == "unknown":
                if row["error_type"] != error_type:
                    raise Conflict("Attempt already has a different unknown-usage reason")
                return self._summary(db, campaign_id)
            db.execute(
                "UPDATE attempts SET status='unknown', error_type=?, updated_at=? "
                "WHERE attempt_id=?",
                (error_type, timestamp(self.clock()), attempt_id),
            )
            return self._summary(db, campaign_id)

    def attempt(self, campaign_id: str, attempt_id: str) -> CampaignAttempt:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM attempts WHERE attempt_id=? AND campaign_id=?",
                (attempt_id, campaign_id),
            ).fetchone()
            if row is None:
                raise NotFound(f"Campaign attempt not found: {attempt_id}")
            return self._attempt_from_row(row)

    def attempts(self, campaign_id: str) -> tuple[CampaignAttempt, ...]:
        with self._connection() as db:
            self._campaign_row(db, campaign_id)
            rows = db.execute(
                "SELECT * FROM attempts WHERE campaign_id=? ORDER BY created_at, attempt_id",
                (campaign_id,),
            ).fetchall()
            return tuple(self._attempt_from_row(row) for row in rows)

    def summary(self, campaign_id: str) -> CampaignSummary:
        with self._connection() as db:
            return self._summary(db, campaign_id)
