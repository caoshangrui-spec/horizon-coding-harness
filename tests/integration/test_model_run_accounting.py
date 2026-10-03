from decimal import Decimal

import pytest

from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.domain.budget import Usage
from horizon.domain.errors import BudgetExceeded, BudgetStopReason
from horizon.domain.model import (
    ModelCallRecord,
    ModelCallReservation,
    ModelPolicyBinding,
    ModelUsage,
)
from horizon.domain.states import RunStatus
from horizon.domain.tools import ToolCallRecord, ToolCallReservation


def policy(max_run_cost="0.50"):
    return ModelPolicyBinding(
        policy_id="unconfigured",
        provider_id="siliconflow",
        model="deepseek-ai/DeepSeek-V4-Flash",
        campaign_id="integration",
        currency="CNY",
        max_run_cost=max_run_cost,
        price_card_hash="a" * 64,
    )


def reservation(call_id="model-call-1", cost="0.20"):
    return ModelCallReservation(
        call_id=call_id,
        request_hash="b" * 64,
        provider_id="siliconflow",
        model="deepseek-ai/DeepSeek-V4-Flash",
        currency="CNY",
        reserved_cost=cost,
    )


def record(call_id="model-call-1", cost="0.10"):
    return ModelCallRecord(
        call_id=call_id,
        request_hash="b" * 64,
        provider_id="siliconflow",
        model="deepseek-ai/DeepSeek-V4-Flash",
        currency="CNY",
        estimated_cost=cost,
        response_id="response-1",
        provider_trace_id="trace-1",
        finish_reason="tool_calls",
        usage=ModelUsage(input_tokens=90, output_tokens=10),
    )


def test_model_policy_reservation_and_settlement_survive_restart(store, service, running):
    run, token = running
    service.bind_model_policy(run.run_id, policy(), token, "bind")
    amount = Usage(model_calls=1, input_tokens=100, output_tokens=50)
    service.reserve_model_call(run.run_id, reservation(), amount, token, "reserve-model")
    restored = SQLiteEventStore(store.path, clock=store.clock).get(run.run_id)
    assert restored.model_occupied_cost == Decimal("0.20")
    assert restored.occupied.model_calls == 1

    actual = Usage(model_calls=1, input_tokens=90, output_tokens=10)
    settled = service.settle_model_call(
        run.run_id,
        record(),
        actual,
        token,
        "settle-model",
    )
    assert settled.model_occupied_cost == Decimal("0.10")
    assert settled.usage.model_calls == 1
    assert settled.model_calls[0].response_id == "response-1"
    assert not settled.model_reservations


def test_run_cny_limit_blocks_before_model_dispatch(service, running):
    run, token = running
    service.bind_model_policy(run.run_id, policy("0.10"), token, "bind")
    with pytest.raises(BudgetExceeded, match="Per-run") as failure:
        service.reserve_model_call(
            run.run_id,
            reservation(cost="0.11"),
            Usage(model_calls=1, input_tokens=10, output_tokens=10),
            token,
            "too-expensive",
        )
    assert failure.value.stop is not None
    assert failure.value.stop.reason_code == BudgetStopReason.RUN_MODEL_COST_LIMIT
    assert failure.value.stop.required_cost == Decimal("0.11")
    assert failure.value.stop.available_cost == Decimal("0.10")


def test_unknown_model_call_keeps_both_ledgers_occupied(service, running):
    run, token = running
    service.bind_model_policy(run.run_id, policy(), token, "bind")
    service.reserve_model_call(
        run.run_id,
        reservation(),
        Usage(model_calls=1, input_tokens=100, output_tokens=50),
        token,
        "reserve-model",
    )
    unknown = service.mark_model_call_unknown(run.run_id, "model-call-1", token, "unknown")
    assert unknown.unknown_model_calls == {"model-call-1"}
    assert unknown.model_occupied_cost == Decimal("0.20")
    with pytest.raises(BudgetExceeded, match="Unknown"):
        service.reserve_model_call(
            run.run_id,
            reservation("model-call-2", "0.10"),
            Usage(model_calls=1, input_tokens=10, output_tokens=10),
            token,
            "blocked",
        )


def test_billed_cny_overrun_is_recorded_then_run_fails(service, running):
    run, token = running
    service.bind_model_policy(run.run_id, policy("0.50"), token, "bind")
    service.reserve_model_call(
        run.run_id,
        reservation(cost="0.20"),
        Usage(model_calls=1, input_tokens=100, output_tokens=50),
        token,
        "reserve-model",
    )
    result = service.settle_model_call(
        run.run_id,
        record(cost="0.60"),
        Usage(model_calls=1, input_tokens=90, output_tokens=10),
        token,
        "settle-model",
    )
    assert result.status == RunStatus.FAILED
    assert result.model_occupied_cost == Decimal("0.60")
    assert result.failure_reason == "usage_overrun"


def test_tool_intent_and_outcome_are_replayable(service, running):
    run, token = running
    intent = ToolCallReservation(
        call_id="tool-call-1",
        name="read_file",
        arguments_hash="c" * 64,
        workspace_revision="tree-before",
    )
    reserved = service.reserve_tool_call(run.run_id, intent, token, "reserve-tool")
    assert reserved.occupied.tool_calls == 1
    outcome = ToolCallRecord(
        call_id="tool-call-1",
        name="read_file",
        arguments_hash="c" * 64,
        status="success",
        output_hash="d" * 64,
        workspace_revision_before="tree-before",
        workspace_revision_after="tree-before",
    )
    settled = service.settle_tool_call(run.run_id, outcome, token, "settle-tool")
    assert settled.usage.tool_calls == 1
    assert settled.usage.steps == 1
    assert settled.tool_calls == [outcome]
    assert not settled.tool_reservations
