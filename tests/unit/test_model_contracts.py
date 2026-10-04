from decimal import Decimal

import pytest
from pydantic import ValidationError

from horizon.domain.errors import BudgetStop, BudgetStopReason
from horizon.domain.model import (
    CampaignBudget,
    InputTokenBudget,
    InputTokenEstimate,
    ModelCallReservation,
    ModelMessage,
    ModelRequest,
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


def test_input_token_budget_binds_estimator_formula_and_hard_limit():
    estimate = InputTokenEstimate(request_bytes=1_000, token_ceiling=3_024)
    budget = InputTokenBudget(max_input_tokens=4_000, estimate=estimate)

    assert budget.estimate.estimator == "request_utf8_bytes_x2_plus_1024_v1"
    with pytest.raises(ValidationError, match="declared estimator"):
        InputTokenEstimate(request_bytes=1_000, token_ceiling=3_023)
    with pytest.raises(ValidationError, match="exceeds"):
        InputTokenBudget(max_input_tokens=3_000, estimate=estimate)


def test_model_reservation_omits_absent_budget_for_historical_wire_compatibility():
    shared = {
        "call_id": "planner-call",
        "purpose": "planning",
        "request_hash": "a" * 64,
        "provider_id": "fake-provider",
        "model": "fake-model",
        "currency": "CNY",
        "reserved_cost": "0.01",
        "planning_context_ref": "b" * 64,
        "planning_context_hash": "b" * 64,
    }

    historical = ModelCallReservation(**shared)
    current = ModelCallReservation(
        **shared,
        input_token_budget=InputTokenBudget(
            max_input_tokens=4_000,
            estimate=InputTokenEstimate(request_bytes=1_000, token_ceiling=3_024),
        ),
    )

    assert "input_token_budget" not in historical.as_dict()
    assert current.as_dict()["input_token_budget"]["estimate"]["token_ceiling"] == 3_024


def test_model_reservation_builds_pre_dispatch_budget_evidence():
    current = ModelCallReservation(
        call_id="execution-call",
        request_hash="a" * 64,
        provider_id="fake-provider",
        model="fake-model",
        currency="CNY",
        reserved_cost="0.01",
        input_token_budget=InputTokenBudget(
            max_input_tokens=4_000,
            estimate=InputTokenEstimate(request_bytes=1_000, token_ceiling=3_024),
        ),
    )

    evidence = current.budget_evidence(512)

    assert evidence.call_id == current.call_id
    assert evidence.purpose == "execution"
    assert evidence.request_hash == current.request_hash
    assert evidence.input_token_budget == current.input_token_budget
    assert evidence.output_token_ceiling == 512
    with pytest.raises(ValueError, match="input-token estimate"):
        current.model_copy(update={"input_token_budget": None}).budget_evidence(512)


def test_openai_payload_estimator_requires_exact_wire_evidence():
    request = ModelRequest(
        model="fake-model",
        messages=(ModelMessage(role="user", content="检查 parser 🧪"),),
        max_output_tokens=512,
    )
    payload = request.openai_compatible_payload_evidence()
    estimate = InputTokenEstimate(
        estimator="openai_payload_utf8_bytes_x2_plus_1024_v2",
        request_bytes=payload.payload_bytes,
        token_ceiling=2 * payload.payload_bytes + 1024,
    )
    shared = {
        "call_id": "execution-wire-call",
        "request_hash": request.sha256,
        "provider_id": "fake-provider",
        "model": request.model,
        "currency": "CNY",
        "reserved_cost": "0.01",
        "input_token_budget": InputTokenBudget(
            max_input_tokens=4_000,
            estimate=estimate,
        ),
    }

    reservation = ModelCallReservation(**shared, request_payload=payload)

    assert reservation.as_dict()["request_payload"]["payload_sha256"] == payload.payload_sha256
    with pytest.raises(ValidationError, match="requires matching request payload evidence"):
        ModelCallReservation(**shared)
    with pytest.raises(ValidationError, match="Legacy request estimator"):
        ModelCallReservation(
            **{
                **shared,
                "input_token_budget": InputTokenBudget(
                    max_input_tokens=4_000,
                    estimate=InputTokenEstimate(request_bytes=100, token_ceiling=1_224),
                ),
            },
            request_payload=payload,
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
