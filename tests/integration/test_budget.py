from decimal import Decimal

import pytest

from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.domain.budget import Usage
from horizon.domain.errors import BudgetExceeded, IntegrityError
from horizon.domain.states import RunStatus


def test_reservation_survives_restart_and_settles_decimal_exactly(store, service, running):
    run, token = running
    held = Usage(model_calls=1, input_tokens=100, output_tokens=50, cost_usd=Decimal("0.30"))
    service.reserve(run.run_id, "call_1", held, token, "reserve")
    restored = SQLiteEventStore(store.path, clock=store.clock).get(run.run_id)
    assert restored.occupied.cost_usd == Decimal("0.30")
    actual = Usage(model_calls=1, input_tokens=90, output_tokens=10, cost_usd=Decimal("0.11"))
    result = service.settle(run.run_id, "call_1", actual, token, "settle")
    assert result.occupied.cost_usd == Decimal("0.11")
    assert result.occupied.model_calls == 1
    assert not result.reservations
    assert service.settle(run.run_id, "call_1", actual, token, "settle").seq == result.seq


@pytest.mark.parametrize(
    "amount",
    [
        Usage(cost_usd=Decimal("1.01")),
        Usage(model_calls=11),
        Usage(tool_calls=21),
        Usage(steps=11),
        Usage(input_tokens=200001),
        Usage(output_tokens=30001),
        Usage(repair_cycles=5),
    ],
)
def test_all_hard_limits_block_before_dispatch(store, service, running, amount):
    run, token = running
    before = store.get(run.run_id).seq
    with pytest.raises(BudgetExceeded):
        service.reserve(run.run_id, "overflow", amount, token, "reserve")
    assert store.get(run.run_id).seq == before


def test_unknown_usage_is_not_refunded(store, service, running):
    run, token = running
    service.reserve(run.run_id, "unknown", Usage(model_calls=1, cost_usd="0.50"), token, "reserve")
    result = service.mark_usage_unknown(run.run_id, "unknown", token, "unknown")
    assert result.occupied.cost_usd == Decimal("0.50")
    assert result.unknown_reservations == {"unknown"}
    with pytest.raises(BudgetExceeded, match="Unknown"):
        service.reserve(run.run_id, "next", Usage(model_calls=1), token, "next")


def test_billed_overrun_is_recorded_then_run_fails(store, service, running):
    run, token = running
    service.reserve(run.run_id, "call", Usage(model_calls=1, cost_usd="0.50"), token, "reserve")
    result = service.settle(
        run.run_id, "call", Usage(model_calls=1, cost_usd="1.10"), token, "settle"
    )
    assert result.usage.cost_usd == Decimal("1.10")
    assert result.status == RunStatus.FAILED
    assert result.failure_reason == "usage_overrun"


def test_attempts_cannot_be_refunded(store, service, running):
    run, token = running
    service.reserve(run.run_id, "call", Usage(model_calls=1), token, "reserve")
    with pytest.raises(IntegrityError, match="refunded"):
        service.settle(run.run_id, "call", Usage(), token, "settle")


def test_downtime_does_not_reset_deadline(store, service, running, clock):
    run, token = running
    clock.advance(3601)
    with pytest.raises(BudgetExceeded, match="deadline"):
        service.reserve(run.run_id, "call", Usage(model_calls=1), token, "late")
    result = service.expire(run.run_id, "expire")
    assert result.deadline_at == run.deadline_at
    assert result.status == RunStatus.FAILED
