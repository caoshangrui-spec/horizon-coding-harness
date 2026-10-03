from decimal import Decimal

import pytest
from pydantic import ValidationError

from horizon.domain.errors import BudgetStop, BudgetStopReason
from horizon.domain.model import (
    CampaignBudget,
    ModelCallReservation,
    ModelMessage,
    ModelUsage,
    PriceCard,
)


def price_card():
    return PriceCard(
        currency="CNY",
        input_per_million="3.00",
        cached_input_per_million="0.30",
        output_per_million="9.00",
        version="test",
        source_url="https://example.test/pricing",
    )


def test_price_card_uses_cached_and_output_rates_exactly():
    usage = ModelUsage(
        input_tokens=1000,
        cached_input_tokens=200,
        output_tokens=100,
    )
    assert price_card().cost_for(usage) == Decimal("0.00336")


def test_usage_rejects_inconsistent_subtotals():
    with pytest.raises(ValidationError, match="Cached"):
        ModelUsage(input_tokens=10, cached_input_tokens=11, output_tokens=0)
    with pytest.raises(ValidationError, match="Reasoning"):
        ModelUsage(input_tokens=0, output_tokens=1, reasoning_tokens=2)


def test_message_roles_enforce_tool_pair_shape():
    with pytest.raises(ValidationError):
        ModelMessage(role="user", content=None)
    with pytest.raises(ValidationError):
        ModelMessage(role="tool", content="ok")
    assert ModelMessage(role="tool", content="ok", tool_call_id="call_1").role == "tool"


def test_campaign_rejects_per_call_limit_above_total():
    with pytest.raises(ValidationError, match="Per-call"):
        CampaignBudget(
            campaign_id="test",
            currency="CNY",
            max_cost="1.00",
            max_cost_per_call="1.01",
        )


def test_budget_stop_requires_matching_scope_and_exceeded_amount():
    with pytest.raises(ValidationError, match="scope"):
        BudgetStop(
            reason_code=BudgetStopReason.RUN_MODEL_COST_LIMIT,
            scope="campaign",
            currency="CNY",
            required_cost="0.11",
            available_cost="0.10",
        )
    with pytest.raises(ValidationError, match="above"):
        BudgetStop(
            reason_code=BudgetStopReason.CAMPAIGN_COST_LIMIT,
            scope="campaign",
            currency="CNY",
            required_cost="0.10",
            available_cost="0.10",
        )


def test_planning_model_reservation_requires_one_content_addressed_context():
    shared = {
        "call_id": "planner-call",
        "purpose": "planning",
        "request_hash": "a" * 64,
        "provider_id": "fake-provider",
        "model": "fake-model",
        "currency": "CNY",
        "reserved_cost": "0.01",
    }
    with pytest.raises(ValidationError, match="planning context"):
        ModelCallReservation(**shared)
    reservation = ModelCallReservation(
        **shared,
        planning_context_ref="b" * 64,
        planning_context_hash="b" * 64,
    )
    assert reservation.purpose == "planning"
    with pytest.raises(ValidationError, match="Execution calls"):
        ModelCallReservation(
            **{**shared, "purpose": "execution"},
            planning_context_ref="b" * 64,
            planning_context_hash="b" * 64,
        )
