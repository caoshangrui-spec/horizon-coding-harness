from decimal import Decimal

import pytest

from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.domain.budget import Usage
from horizon.domain.errors import (
    BudgetExceeded,
    Conflict,
    IntegrityError,
    RunCancellationRequested,
    RunDeadlineExceeded,
)
from horizon.domain.states import RunStatus
from horizon.domain.tools import ToolCallRecord, ToolCallReservation


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
    with pytest.raises(RunDeadlineExceeded, match="deadline") as stopped:
        service.reserve(run.run_id, "call", Usage(model_calls=1), token, "late")
    assert stopped.value.run_id == run.run_id
    persisted = store.get(run.run_id)
    assert persisted.status == RunStatus.FAILED
    assert persisted.failure_reason == "wall_clock_limit"
    result = service.expire(run.run_id, "expire")
    assert result.deadline_at == run.deadline_at
    assert result.status == RunStatus.FAILED


def test_deadline_preserves_unclassified_intent_until_recovery(store, service, running, clock):
    run, token = running
    service.reserve(
        run.run_id,
        "uncertain",
        Usage(model_calls=1, cost_usd="0.50"),
        token,
        "reserve-uncertain",
    )
    clock.advance(3601)

    with pytest.raises(Conflict, match="unclassified or recoverable tool operations"):
        service.expire(run.run_id, "unsafe-expire")
    preserved = store.get(run.run_id)
    assert preserved.status == RunStatus.RUNNING
    assert set(preserved.reservations) == {"uncertain"}

    recovered = service.acquire_recovery_lease(
        run.run_id,
        "recovery-worker",
        "recovery-lease",
        prior_worker_stopped=True,
    )
    recovery_token = type(token).from_run(recovered)
    classified = service.mark_usage_unknown(
        run.run_id,
        "uncertain",
        recovery_token,
        "classify-uncertain",
    )
    assert classified.status == RunStatus.RUNNING
    assert classified.unknown_reservations == {"uncertain"}

    result = service.expire(run.run_id, "safe-expire")
    assert result.status == RunStatus.FAILED
    assert result.failure_reason == "wall_clock_limit"
    assert result.occupied.cost_usd == Decimal("0.50")


def test_late_receipt_settles_after_deadline_before_terminalization(store, service, running, clock):
    run, token = running
    service.reserve(
        run.run_id,
        "late-receipt",
        Usage(model_calls=1, cost_usd="0.50"),
        token,
        "reserve-late-receipt",
    )
    clock.advance(3601)
    recovered = service.acquire_recovery_lease(
        run.run_id,
        "receipt-worker",
        "receipt-lease",
        prior_worker_stopped=True,
    )
    recovery_token = type(token).from_run(recovered)

    settled = service.settle(
        run.run_id,
        "late-receipt",
        Usage(model_calls=1, cost_usd="0.25"),
        recovery_token,
        "settle-late-receipt",
    )
    assert settled.status == RunStatus.RUNNING
    assert not settled.reservations
    assert settled.usage.cost_usd == Decimal("0.25")

    result = service.release_lease(run.run_id, recovery_token, "release-after-receipt")
    assert result.status == RunStatus.FAILED
    assert result.failure_reason == "wall_clock_limit"


def test_cancel_waits_for_late_receipt_and_fences_new_work(store, service, running):
    run, token = running
    service.reserve(
        run.run_id,
        "late-receipt",
        Usage(model_calls=1, cost_usd="0.50"),
        token,
        "reserve-late-receipt",
    )

    pending = service.cancel(run.run_id, "cancel-with-inflight-call")
    assert pending.status == RunStatus.RUNNING
    assert pending.cancel_requested is True
    assert pending.lease_id == token.lease_id
    assert pending.as_dict()["cancel_requested"] is True
    with pytest.raises(RunCancellationRequested) as stopped:
        service.reserve(
            run.run_id,
            "forbidden-new-call",
            Usage(model_calls=1),
            token,
            "reserve-after-cancel",
        )
    assert stopped.value.run_id == run.run_id

    cancelled = service.settle(
        run.run_id,
        "late-receipt",
        Usage(model_calls=1, cost_usd="0.25"),
        token,
        "settle-late-receipt-after-cancel",
    )
    assert cancelled.status == RunStatus.CANCELLED
    assert cancelled.cancel_requested is False
    assert cancelled.lease_id is None
    assert cancelled.usage.cost_usd == Decimal("0.25")
    assert not cancelled.reservations
    assert "cancel_requested" not in cancelled.as_dict()
    assert [event.event_type for event in store.events(run.run_id)[-2:]] == [
        "BUDGET_SETTLED",
        "CANCEL_REQUESTED",
    ]
    replayed = SQLiteEventStore.replay_jsonl(store.export_jsonl(run.run_id))
    assert replayed.as_dict() == cancelled.as_dict()


def test_cancel_records_exact_check_stop_and_waits_for_tool_recovery(
    store,
    service,
    running,
):
    run, token = running
    reservation = ToolCallReservation(
        call_id="pending-check",
        name="run_check",
        arguments_hash="a" * 64,
        workspace_revision="before",
        workspace_manifest_ref="b" * 64,
    )
    service.reserve_tool_call(
        run.run_id,
        reservation,
        token,
        "reserve-pending-check",
    )
    pending = service.cancel(run.run_id, "cancel-pending-check")
    assert pending.cancel_requested is True

    stopped = service.record_cancel_sandbox_stopped(
        run.run_id,
        reservation.call_id,
        "horizon-check-owned",
        "sha256:" + "c" * 64,
        "record-stopped-check",
    )
    assert stopped.cancel_requested is True
    assert stopped.cancel_stop_receipts == [
        {
            "call_id": reservation.call_id,
            "container_name": "horizon-check-owned",
            "image_id": "sha256:" + "c" * 64,
            "source": "local_control",
        }
    ]

    unknown = service.mark_tool_call_unknown(
        run.run_id,
        reservation.call_id,
        token,
        "mark-cancelled-check-unknown",
    )
    assert unknown.status == RunStatus.RUNNING
    assert unknown.cancel_requested is True
    assert unknown.unknown_tool_calls == {reservation.call_id}

    cancelled = service.settle_tool_call(
        run.run_id,
        ToolCallRecord(
            call_id=reservation.call_id,
            name=reservation.name,
            arguments_hash=reservation.arguments_hash,
            status="cancelled",
            output_hash="d" * 64,
            workspace_revision_before="before",
            workspace_revision_after="before",
            artifact_ref="d" * 64,
            workspace_manifest_ref=reservation.workspace_manifest_ref,
            recovery_disposition="discard_check",
        ),
        token,
        "settle-cancelled-check",
    )
    assert cancelled.status == RunStatus.CANCELLED
    assert cancelled.tool_calls[-1].status == "cancelled"
    assert cancelled.cancel_stop_receipts == stopped.cancel_stop_receipts
    assert SQLiteEventStore.replay_jsonl(store.export_jsonl(run.run_id)).as_dict() == (
        cancelled.as_dict()
    )


def test_deadline_does_not_hide_recoverable_tool_effect(store, service, running, clock):
    run, token = running
    service.reserve_tool_call(
        run.run_id,
        ToolCallReservation(
            call_id="pending-tool",
            name="replace_text",
            arguments_hash="a" * 64,
            workspace_revision="before",
            workspace_manifest_ref="b" * 64,
        ),
        token,
        "reserve-pending-tool",
    )
    clock.advance(3601)
    recovered = service.acquire_recovery_lease(
        run.run_id,
        "tool-recovery-worker",
        "tool-recovery-lease",
        prior_worker_stopped=True,
    )
    recovery_token = type(token).from_run(recovered)
    classified = service.mark_tool_call_unknown(
        run.run_id,
        "pending-tool",
        recovery_token,
        "classify-pending-tool",
    )

    preserved = service.expire_if_safe(run.run_id, "preserve-tool-recovery")
    assert preserved.status == RunStatus.RUNNING
    assert preserved.unknown_tool_calls == {"pending-tool"}
    assert preserved.lease_id == classified.lease_id
    with pytest.raises(Conflict, match="recoverable tool operations"):
        service.expire(run.run_id, "reject-tool-terminalization")
