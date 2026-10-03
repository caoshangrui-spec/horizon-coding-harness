from decimal import Decimal

import pytest

from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.application.recovery import RecoveryService
from horizon.domain.budget import Usage
from horizon.domain.errors import Conflict
from horizon.domain.model import (
    CampaignBudget,
    ModelCallRecord,
    ModelCallReservation,
    ModelPolicyBinding,
    ModelUsage,
)
from horizon.domain.tools import ToolCallReservation


def campaign() -> CampaignBudget:
    return CampaignBudget(
        campaign_id="recovery-campaign",
        currency="CNY",
        max_cost="3.00",
        max_cost_per_call="1.00",
    )


def policy() -> ModelPolicyBinding:
    return ModelPolicyBinding(
        policy_id="unconfigured",
        provider_id="siliconflow",
        model="model-a",
        campaign_id="recovery-campaign",
        currency="CNY",
        max_run_cost="1.00",
        price_card_hash="a" * 64,
    )


def reservation(request_hash: str = "b" * 64) -> ModelCallReservation:
    return ModelCallReservation(
        call_id="model-call-1",
        request_hash=request_hash,
        provider_id="siliconflow",
        model="model-a",
        currency="CNY",
        reserved_cost="0.20",
    )


def record() -> ModelCallRecord:
    return ModelCallRecord(
        call_id="model-call-1",
        request_hash="b" * 64,
        provider_id="siliconflow",
        model="model-a",
        currency="CNY",
        estimated_cost="0.10",
        response_id="response-1",
        provider_trace_id="trace-1",
        finish_reason="tool_calls",
        usage=ModelUsage(input_tokens=90, output_tokens=10),
    )


def model_amount() -> Usage:
    return Usage(model_calls=1, input_tokens=100, output_tokens=50)


def initialized_ledger(tmp_path) -> CampaignBudgetLedger:
    ledger = CampaignBudgetLedger(tmp_path / "campaign.sqlite3")
    ledger.initialize(campaign(), provider_id="siliconflow", model_id="model-a")
    return ledger


def bind_and_reserve(service, running, ledger, *, campaign_hash: str = "b" * 64):
    run, token = running
    service.bind_model_policy(run.run_id, policy(), token, "bind-model-policy")
    ledger.reserve(campaign(), "model-call-1", campaign_hash, Decimal("0.20"))
    service.reserve_model_call(
        run.run_id,
        reservation(),
        model_amount(),
        token,
        "reserve-model-call",
    )
    return run, token


def test_recovery_repairs_campaign_commit_window_from_run_receipt(tmp_path, service, running):
    ledger = initialized_ledger(tmp_path)
    run, token = bind_and_reserve(service, running, ledger)
    service.settle_model_call(
        run.run_id,
        record(),
        Usage(model_calls=1, input_tokens=90, output_tokens=10),
        token,
        "settle-run-model-call",
    )

    report = RecoveryService(service, ledger).reconcile(run.run_id, token)

    attempt = ledger.attempt("recovery-campaign", "model-call-1")
    assert attempt.status == "settled"
    assert attempt.actual_cost == Decimal("0.10")
    assert report.linked_ledgers_consistent is True
    assert report.safe_to_resume is False
    assert [item.classification for item in report.findings] == [
        "campaign_settlement_repaired",
        "unsafe_agent_boundary",
    ]


def test_recovery_marks_uncertain_model_dispatch_unknown_and_releases_lease(
    tmp_path, service, running
):
    ledger = initialized_ledger(tmp_path)
    run, token = bind_and_reserve(service, running, ledger)

    report = RecoveryService(service, ledger).reconcile(run.run_id, token)

    restored = service.store.get(run.run_id)
    assert restored.unknown_model_calls == {"model-call-1"}
    assert restored.unknown_reservations == {"model-call-1"}
    assert ledger.attempt("recovery-campaign", "model-call-1").status == "unknown"
    assert report.safe_to_resume is False
    assert report.next_action == "manual_reconciliation"
    released = service.release_lease(run.run_id, token, "release-recovery-worker")
    assert released.lease_id is None


def test_recovery_propagates_campaign_unknown_state_into_run(tmp_path, service, running):
    ledger = initialized_ledger(tmp_path)
    run, token = bind_and_reserve(service, running, ledger)
    ledger.mark_unknown("recovery-campaign", "model-call-1", "ConnectionLost")

    report = RecoveryService(service, ledger).reconcile(run.run_id, token)

    restored = service.store.get(run.run_id)
    assert restored.unknown_model_calls == {"model-call-1"}
    assert report.findings[0].action == "run_marked_unknown"


def test_recovery_does_not_invent_missing_model_response(tmp_path, service, running):
    ledger = initialized_ledger(tmp_path)
    run, token = bind_and_reserve(service, running, ledger)
    ledger.settle("recovery-campaign", "model-call-1", Decimal("0.08"), "trace-lost")

    report = RecoveryService(service, ledger).reconcile(run.run_id, token)

    assert report.findings[0].classification == "model_response_unavailable"
    assert service.store.get(run.run_id).unknown_model_calls == {"model-call-1"}
    assert ledger.attempt("recovery-campaign", "model-call-1").actual_cost == Decimal("0.08")


def test_recovery_marks_tool_intent_unknown_without_replaying_it(tmp_path, service, running):
    run, token = running
    ledger = CampaignBudgetLedger(tmp_path / "campaign.sqlite3")
    service.reserve_tool_call(
        run.run_id,
        ToolCallReservation(
            call_id="tool-call-1",
            name="write_file",
            arguments_hash="c" * 64,
            workspace_revision="before",
        ),
        token,
        "reserve-tool-call",
    )

    report = RecoveryService(service, ledger).reconcile(run.run_id, token)

    restored = service.store.get(run.run_id)
    assert restored.unknown_tool_calls == {"tool-call-1"}
    assert restored.unknown_reservations == {"tool-call-1"}
    assert report.findings[0].classification == "tool_effect_unknown"
    assert not restored.tool_calls


def test_recovery_rejects_cross_ledger_identity_mismatch_before_mutation(
    tmp_path, service, running
):
    ledger = initialized_ledger(tmp_path)
    run, token = bind_and_reserve(service, running, ledger, campaign_hash="c" * 64)

    with pytest.raises(Conflict, match="does not match"):
        RecoveryService(service, ledger).reconcile(run.run_id, token)

    restored = service.store.get(run.run_id)
    assert not restored.unknown_reservations
    assert ledger.attempt("recovery-campaign", "model-call-1").status == "reserved"
