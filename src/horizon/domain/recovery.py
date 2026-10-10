from typing import Literal

from horizon.domain.common import Contract
from horizon.domain.model import ClientTraceId
from horizon.domain.task import Text

WriteRecoveryState = Literal["pre_effect", "expected_effect", "diverged"]
WriteRecoveryDecision = Literal["accept", "rollback"]
WriteRecoveryVerdict = Literal["accept", "rollback", "block"]


def write_recovery_verdict(
    state: WriteRecoveryState,
    decision: WriteRecoveryDecision,
) -> WriteRecoveryVerdict:
    """Return the only safe action for an observed deterministic write state."""

    if decision == "accept":
        return "accept" if state == "expected_effect" else "block"
    return "rollback" if state in {"pre_effect", "expected_effect"} else "block"


class RecoveryFinding(Contract):
    operation_id: Text
    operation_kind: Literal["model", "tool", "budget", "agent_session"]
    client_trace_id: ClientTraceId | None = None
    classification: Literal[
        "campaign_settlement_repaired",
        "campaign_only_reservation_released",
        "model_effect_unknown",
        "model_response_unavailable",
        "tool_effect_unknown",
        "operation_effect_unknown",
        "unsafe_agent_boundary",
    ]
    action: Literal[
        "campaign_settled_from_run_receipt",
        "campaign_settled_zero_before_dispatch",
        "run_and_campaign_marked_unknown",
        "campaign_marked_unknown",
        "run_marked_unknown",
        "run_tool_marked_unknown",
        "run_budget_marked_unknown",
        "none",
    ]
    blocking: bool
    detail: Text


class RecoveryReport(Contract):
    run_id: Text
    linked_ledgers_consistent: bool
    safe_to_resume: bool
    requires_human: bool
    next_action: Literal[
        "resume",
        "provide_replacement_plan",
        "provide_operator_guidance",
        "manual_reconciliation",
        "cancelled",
    ]
    remaining_reservations: tuple[str, ...]
    unknown_reservations: tuple[str, ...]
    findings: tuple[RecoveryFinding, ...]
