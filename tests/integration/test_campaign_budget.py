from decimal import Decimal

import pytest

from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.domain.errors import BudgetExceeded, BudgetStopReason, Conflict
from horizon.domain.model import CampaignBudget


def budget():
    return CampaignBudget(
        campaign_id="live-integration",
        currency="CNY",
        max_cost="3.00",
        max_cost_per_call="1.00",
    )


def test_campaign_reservation_settlement_and_restart(tmp_path):
    path = tmp_path / "campaign.sqlite3"
    ledger = CampaignBudgetLedger(path)
    ledger.initialize(budget(), provider_id="siliconflow", model_id="model-a")
    held = ledger.reserve(budget(), "attempt-1", "hash-1", Decimal("0.40"))
    assert held.reserved_cost == Decimal("0.40")
    settled = ledger.settle("live-integration", "attempt-1", Decimal("0.12"), "trace-1")
    assert settled.settled_cost == Decimal("0.12")
    assert settled.remaining_cost == Decimal("2.88")
    attempt = ledger.attempt("live-integration", "attempt-1")
    assert attempt.status == "settled"
    assert attempt.actual_cost == Decimal("0.12")
    assert attempt.provider_trace_id == "trace-1"
    assert ledger.attempts("live-integration") == (attempt,)
    assert CampaignBudgetLedger(path).summary("live-integration") == settled


def test_unknown_attempt_keeps_full_reservation(tmp_path):
    ledger = CampaignBudgetLedger(tmp_path / "campaign.sqlite3")
    ledger.initialize(budget(), provider_id="siliconflow", model_id="model-a")
    ledger.reserve(budget(), "attempt-1", "hash-1", Decimal("0.75"))
    summary = ledger.mark_unknown("live-integration", "attempt-1", "Timeout")
    assert summary.unknown_cost == Decimal("0.75")
    assert summary.remaining_cost == Decimal("2.25")
    with pytest.raises(Conflict, match="reconciled"):
        ledger.settle("live-integration", "attempt-1", Decimal("0"), None)


def test_campaign_and_per_call_hard_limits_block_before_insert(tmp_path):
    ledger = CampaignBudgetLedger(tmp_path / "campaign.sqlite3")
    ledger.initialize(budget(), provider_id="siliconflow", model_id="model-a")
    with pytest.raises(BudgetExceeded, match="per-call") as per_call:
        ledger.reserve(budget(), "too-large", "hash", Decimal("1.01"))
    assert per_call.value.stop is not None
    assert per_call.value.stop.reason_code == BudgetStopReason.CAMPAIGN_CALL_COST_LIMIT
    assert per_call.value.stop.required_cost == Decimal("1.01")
    assert per_call.value.stop.available_cost == Decimal("1.00")
    for index in range(3):
        ledger.reserve(budget(), f"attempt-{index}", f"hash-{index}", Decimal("1.00"))
    with pytest.raises(BudgetExceeded, match="Campaign") as campaign:
        ledger.reserve(budget(), "overflow", "hash-overflow", Decimal("0.01"))
    assert campaign.value.stop is not None
    assert campaign.value.stop.reason_code == BudgetStopReason.CAMPAIGN_COST_LIMIT
    assert campaign.value.stop.required_cost == Decimal("0.01")
    assert campaign.value.stop.available_cost == Decimal("0.00")


def test_campaign_definition_and_attempt_ids_are_immutable(tmp_path):
    ledger = CampaignBudgetLedger(tmp_path / "campaign.sqlite3")
    ledger.initialize(budget(), provider_id="siliconflow", model_id="model-a")
    ledger.reserve(budget(), "attempt-1", "hash-1", Decimal("0.20"))
    repeated = ledger.reserve(budget(), "attempt-1", "hash-1", Decimal("0.20"))
    assert repeated.reserved_cost == Decimal("0.20")
    with pytest.raises(Conflict, match="different reservation"):
        ledger.reserve(budget(), "attempt-1", "hash-2", Decimal("0.20"))
    with pytest.raises(Conflict, match="definition"):
        ledger.initialize(budget(), provider_id="siliconflow", model_id="model-b")


def test_repeated_settlement_requires_the_same_provider_receipt(tmp_path):
    ledger = CampaignBudgetLedger(tmp_path / "campaign.sqlite3")
    ledger.initialize(budget(), provider_id="siliconflow", model_id="model-a")
    ledger.reserve(budget(), "attempt-1", "hash-1", Decimal("0.20"))
    ledger.settle("live-integration", "attempt-1", Decimal("0.10"), "trace-1")
    with pytest.raises(Conflict, match="different receipt"):
        ledger.settle("live-integration", "attempt-1", Decimal("0.10"), "trace-2")


def test_billed_overrun_is_committed_before_error(tmp_path):
    ledger = CampaignBudgetLedger(tmp_path / "campaign.sqlite3")
    ledger.initialize(budget(), provider_id="siliconflow", model_id="model-a")
    ledger.reserve(budget(), "attempt-1", "hash-1", Decimal("0.20"))
    with pytest.raises(BudgetExceeded, match="per-call"):
        ledger.settle("live-integration", "attempt-1", Decimal("1.10"), "trace")
    summary = ledger.summary("live-integration")
    assert summary.settled_cost == Decimal("1.10")
    assert summary.reserved_cost == 0
