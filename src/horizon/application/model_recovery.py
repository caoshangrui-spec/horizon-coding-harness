from __future__ import annotations

from dataclasses import dataclass

from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.common import canonical_json, digest
from horizon.domain.errors import Conflict
from horizon.domain.events import Event
from horizon.domain.model import (
    ModelCallRecord,
    ModelCallReservation,
    ModelResponse,
)
from horizon.domain.ports import ArtifactStorePort, CampaignBudgetPort
from horizon.domain.run import Run
from horizon.domain.tools import ToolCallRecord, ToolCallReservation

LEASE_EVENTS = {"LEASE_RELEASED", "LEASE_ACQUIRED", "LEASE_RENEWED"}
READONLY_RETRY_TOOLS = frozenset({"search_repo", "read_file", "retrieve_code"})
_MODEL_RECEIPT_EVENTS = (
    "BUDGET_RESERVED",
    "MODEL_CALL_RESERVED",
    "BUDGET_SETTLED",
    "MODEL_CALL_SETTLED",
)
_UNKNOWN_TOOL_EVENTS = (
    "BUDGET_RESERVED",
    "TOOL_CALL_RESERVED",
    "BUDGET_USAGE_UNKNOWN",
    "TOOL_CALL_UNKNOWN",
)
_RETRY_RESOLUTION_EVENTS = (
    "BUDGET_SETTLED",
    "TOOL_CALL_SETTLED",
)


@dataclass(frozen=True)
class RecoverableModelTurn:
    reservation: ModelCallReservation
    record: ModelCallRecord


@dataclass(frozen=True)
class RecoverableToolTurn:
    model: RecoverableModelTurn
    reservation: ToolCallReservation
    record: ToolCallRecord | None = None


def persist_model_response(
    response: ModelResponse,
    artifact_store: ArtifactStorePort,
) -> str:
    """Publish and verify the canonical response before any Run receipt references it."""

    payload = canonical_json(response).encode("utf-8")
    expected_ref = digest(response)
    artifact_ref = artifact_store.put(payload)
    if artifact_ref != expected_ref or artifact_store.read(artifact_ref) != payload:
        raise Conflict("Model response artifact verification failed")
    return artifact_ref


def quarantine_unsettled_model_call(
    service: HarnessService,
    campaign_ledger: CampaignBudgetPort,
    *,
    campaign_id: str,
    run_id: str,
    call_id: str,
    token: LeaseToken,
    error_type: str,
) -> bool:
    """Quarantine a dispatched call when no trusted Run response receipt exists."""

    run = service.store.get(run_id)
    if call_id not in run.model_reservations:
        return False
    reservation = run.model_reservations[call_id]
    attempt = campaign_ledger.attempt(campaign_id, call_id)
    if (
        attempt.campaign_id != campaign_id
        or attempt.request_hash != reservation.request_hash
        or attempt.reserved_cost != reservation.reserved_cost
    ):
        raise Conflict("Campaign attempt does not match the quarantined model reservation")
    service.mark_model_call_unknown(
        run_id,
        call_id,
        token,
        f"quarantine_{call_id}",
    )
    if attempt.status == "reserved":
        campaign_ledger.mark_unknown(campaign_id, call_id, error_type)
    return True


def events_after_agent_session(run: Run, events: list[Event]) -> list[Event] | None:
    session = run.agent_session
    if session is None:
        return None
    session_event_seq = session.covered_event_seq + 1
    if session_event_seq > len(events) or events[session_event_seq - 1].event_type not in {
        "AGENT_SESSION_SAVED",
        "AGENT_SESSION_GUIDED",
    }:
        return None
    return events[session_event_seq:]


def _operational_tail(run: Run, events: list[Event]) -> list[Event] | None:
    tail = events_after_agent_session(run, events)
    if tail is None:
        return None
    return [event for event in tail if event.event_type not in LEASE_EVENTS]


def _model_turn_from_prefix(
    run: Run,
    operational: list[Event],
) -> RecoverableModelTurn | None:
    if tuple(event.event_type for event in operational[:4]) != _MODEL_RECEIPT_EVENTS:
        return None
    reservation = ModelCallReservation.model_validate(operational[1].payload["reservation"])
    record = ModelCallRecord.model_validate(operational[3].payload["record"])
    if (
        operational[0].payload.get("reservation_id") != reservation.call_id
        or operational[2].payload.get("reservation_id") != reservation.call_id
        or record.call_id != reservation.call_id
        or not run.model_calls
        or run.model_calls[-1] != record
        or run.model_reservations
    ):
        return None
    return RecoverableModelTurn(reservation=reservation, record=record)


def recoverable_model_turn(run: Run, events: list[Event]) -> RecoverableModelTurn | None:
    operational = _operational_tail(run, events)
    if operational is None or tuple(event.event_type for event in operational) != (
        _MODEL_RECEIPT_EVENTS
    ):
        return None
    turn = _model_turn_from_prefix(run, operational)
    if turn is None or run.reservations:
        return None
    return turn


def _tool_turn_from_prefix(
    run: Run,
    operational: list[Event],
    artifact_store: ArtifactStorePort,
) -> RecoverableToolTurn | None:
    if tuple(event.event_type for event in operational[:8]) != (
        _MODEL_RECEIPT_EVENTS + _UNKNOWN_TOOL_EVENTS
    ):
        return None
    model = _model_turn_from_prefix(run, operational)
    if model is None:
        return None

    reservation = ToolCallReservation.model_validate(operational[5].payload["reservation"])
    if (
        operational[4].payload.get("reservation_id") != reservation.call_id
        or operational[6].payload.get("reservation_id") != reservation.call_id
        or operational[7].payload.get("call_id") != reservation.call_id
        or run.agent_session is None
        or reservation.workspace_revision != run.agent_session.workspace_revision
    ):
        return None

    response = load_recorded_model_response(model, artifact_store)
    if len(response.message.tool_calls) != 1:
        return None
    function = response.message.tool_calls[0].function
    if (
        function.name != reservation.name
        or digest(function.arguments) != reservation.arguments_hash
    ):
        return None

    return RecoverableToolTurn(model=model, reservation=reservation)


def pending_unknown_tool_turn(
    run: Run,
    events: list[Event],
    artifact_store: ArtifactStorePort,
) -> RecoverableToolTurn | None:
    operational = _operational_tail(run, events)
    if operational is None or tuple(event.event_type for event in operational) != (
        _MODEL_RECEIPT_EVENTS + _UNKNOWN_TOOL_EVENTS
    ):
        return None
    turn = _tool_turn_from_prefix(run, operational, artifact_store)
    if turn is None:
        return None
    call_id = turn.reservation.call_id
    if (
        set(run.tool_reservations) != {call_id}
        or run.unknown_tool_calls != {call_id}
        or run.unknown_reservations != {call_id}
    ):
        return None
    return turn


def pending_unknown_readonly_tool_turn(
    run: Run,
    events: list[Event],
    artifact_store: ArtifactStorePort,
) -> RecoverableToolTurn | None:
    turn = pending_unknown_tool_turn(run, events, artifact_store)
    if turn is None or turn.reservation.name not in READONLY_RETRY_TOOLS:
        return None
    return turn


def recoverable_readonly_tool_turn(
    run: Run,
    events: list[Event],
    artifact_store: ArtifactStorePort,
) -> RecoverableToolTurn | None:
    operational = _operational_tail(run, events)
    expected = _MODEL_RECEIPT_EVENTS + _UNKNOWN_TOOL_EVENTS + _RETRY_RESOLUTION_EVENTS
    if operational is None or tuple(event.event_type for event in operational) != expected:
        return None
    turn = _tool_turn_from_prefix(run, operational, artifact_store)
    if turn is None or turn.reservation.name not in READONLY_RETRY_TOOLS:
        return None
    reservation = turn.reservation

    record = ToolCallRecord.model_validate(operational[9].payload["record"])
    if (
        operational[8].payload.get("reservation_id") != reservation.call_id
        or record.call_id != reservation.call_id
        or record.name != reservation.name
        or record.arguments_hash != reservation.arguments_hash
        or record.status != "cancelled"
        or record.recovery_disposition != "retry_readonly"
        or record.workspace_revision_before != reservation.workspace_revision
        or record.workspace_revision_after != reservation.workspace_revision
        or run.reservations
        or run.unknown_reservations
        or run.tool_reservations
        or run.unknown_tool_calls
        or not run.tool_calls
        or run.tool_calls[-1] != record
    ):
        return None
    return RecoverableToolTurn(model=turn.model, reservation=reservation, record=record)


def load_recorded_model_response(
    turn: RecoverableModelTurn,
    artifact_store: ArtifactStorePort,
) -> ModelResponse:
    record = turn.record
    if record.response_artifact_ref is None:
        raise Conflict("Settled model call has no recoverable response artifact")
    response = ModelResponse.model_validate_json(artifact_store.read(record.response_artifact_ref))
    if (
        response.response_id != record.response_id
        or response.model != record.model
        or response.provider_trace_id != record.provider_trace_id
        or response.finish_reason != record.finish_reason
        or response.usage != record.usage
        or response.message.role != "assistant"
    ):
        raise Conflict("Model response artifact does not match its Run receipt")
    return response
