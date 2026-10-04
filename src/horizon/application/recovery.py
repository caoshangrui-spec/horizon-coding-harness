from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from horizon.application.model_recovery import (
    LEASE_EVENTS,
    RecoverableModelTurn,
    events_after_agent_session,
    load_recorded_model_response,
    recoverable_model_turn,
    recoverable_readonly_tool_turn,
)
from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.errors import Conflict, IntegrityError
from horizon.domain.human import HumanGuidanceRequest, HumanPlanRequest
from horizon.domain.model import CampaignAttempt, ModelCallRecord, ModelCallReservation
from horizon.domain.ports import ArtifactStorePort, CampaignBudgetPort
from horizon.domain.recovery import RecoveryFinding, RecoveryReport
from horizon.domain.run import Run
from horizon.domain.states import RunStatus


@dataclass(frozen=True)
class _RecoveryAction:
    kind: Literal[
        "settle_campaign",
        "settle_campaign_zero",
        "mark_campaign_unknown",
        "mark_model_unknown",
        "mark_tool_unknown",
        "mark_budget_unknown",
    ]
    operation_id: str
    record: ModelCallRecord | None = None


class RecoveryService:
    """Reconcile Run-linked intents without replaying an uncertain external effect."""

    def __init__(
        self,
        service: HarnessService,
        campaign_ledger: CampaignBudgetPort,
        artifact_store: ArtifactStorePort | None = None,
    ):
        self.service = service
        self.campaign_ledger = campaign_ledger
        self.artifact_store = artifact_store

    @staticmethod
    def _validate_attempt(
        attempt: CampaignAttempt,
        reservation: ModelCallReservation,
        campaign_id: str,
    ) -> None:
        if (
            attempt.attempt_id != reservation.call_id
            or attempt.campaign_id != campaign_id
            or attempt.request_hash != reservation.request_hash
            or attempt.reserved_cost != reservation.reserved_cost
        ):
            raise Conflict(
                f"Campaign attempt does not match Run reservation: {reservation.call_id}"
            )

    def _historical_model_reservations(self, run: Run) -> dict[str, ModelCallReservation]:
        reservations: dict[str, ModelCallReservation] = {}
        for event in self.service.store.events(run.run_id):
            if event.event_type != "MODEL_CALL_RESERVED":
                continue
            reservation = ModelCallReservation.model_validate(event.payload["reservation"])
            if reservation.call_id in reservations:
                raise IntegrityError(f"Duplicate historical model intent: {reservation.call_id}")
            reservations[reservation.call_id] = reservation
        return reservations

    def _is_safe_agent_boundary(self, run: Run) -> bool:
        if run.status == RunStatus.READY:
            return run.plan is not None and not run.reservations
        if run.status == RunStatus.PLANNING:
            if run.plan is not None or run.reservations:
                return False
            planning_records = [
                record for record in run.model_calls if record.purpose == "planning"
            ]
            if not planning_records:
                return not run.model_calls
            if (
                len(planning_records) != 1
                or len(run.model_calls) != 1
                or self.artifact_store is None
                or run.model_policy is None
            ):
                return False
            record = planning_records[0]
            reservation = self._historical_model_reservations(run).get(record.call_id)
            if reservation is None or reservation.purpose != "planning":
                return False
            attempt = self.campaign_ledger.attempt(
                run.model_policy.campaign_id,
                record.call_id,
            )
            if (
                attempt.status != "settled"
                or attempt.request_hash != reservation.request_hash
                or attempt.reserved_cost != reservation.reserved_cost
                or attempt.actual_cost != record.estimated_cost
                or attempt.provider_trace_id != record.provider_trace_id
            ):
                return False
            load_recorded_model_response(
                RecoverableModelTurn(reservation=reservation, record=record),
                self.artifact_store,
            )
            return True
        if run.status != RunStatus.RUNNING or run.agent_session is None or run.reservations:
            return False
        events = self.service.store.events(run.run_id)
        tail = events_after_agent_session(run, events)
        if tail is None:
            return False
        if all(event.event_type in LEASE_EVENTS for event in tail):
            return True
        turn = recoverable_model_turn(run, events)
        if turn is None and self.artifact_store is not None:
            retry = recoverable_readonly_tool_turn(run, events, self.artifact_store)
            turn = retry.model if retry is not None else None
        if (
            turn is None
            or self.artifact_store is None
            or turn.record.response_artifact_ref is None
            or run.model_policy is None
        ):
            return False
        attempt = self.campaign_ledger.attempt(
            run.model_policy.campaign_id,
            turn.record.call_id,
        )
        if (
            attempt.status != "settled"
            or attempt.request_hash != turn.reservation.request_hash
            or attempt.reserved_cost != turn.reservation.reserved_cost
            or attempt.actual_cost != turn.record.estimated_cost
            or attempt.provider_trace_id != turn.record.provider_trace_id
        ):
            return False
        load_recorded_model_response(turn, self.artifact_store)
        return True

    def _verify_linked_ledgers(
        self,
        run: Run,
        historical: dict[str, ModelCallReservation],
    ) -> None:
        if not run.model_reservations and not run.model_calls:
            return
        if run.model_policy is None:
            raise IntegrityError("Run-linked model attempts require a bound model policy")
        campaign_id = run.model_policy.campaign_id
        for call_id, reservation in run.model_reservations.items():
            attempt = self.campaign_ledger.attempt(campaign_id, call_id)
            self._validate_attempt(attempt, reservation, campaign_id)
            if (
                call_id not in run.unknown_reservations
                or call_id not in run.unknown_model_calls
                or attempt.status not in {"unknown", "settled"}
            ):
                raise IntegrityError(f"Recovery left a model intent unresolved: {call_id}")
        for record in run.model_calls:
            reservation = historical.get(record.call_id)
            if reservation is None:
                raise IntegrityError(f"Model receipt has no historical intent: {record.call_id}")
            attempt = self.campaign_ledger.attempt(campaign_id, record.call_id)
            self._validate_attempt(attempt, reservation, campaign_id)
            if (
                attempt.status != "settled"
                or attempt.actual_cost != record.estimated_cost
                or attempt.provider_trace_id != record.provider_trace_id
            ):
                raise IntegrityError(f"Recovery left linked ledgers inconsistent: {record.call_id}")

    def reconcile(self, run_id: str, token: LeaseToken) -> RecoveryReport:
        run = self.service.store.get(run_id)
        self.service.check_worker(run, token)
        historical = self._historical_model_reservations(run)
        if (run.model_reservations or run.model_calls) and run.model_policy is None:
            raise IntegrityError("Run-linked model attempts require a bound model policy")

        actions: list[_RecoveryAction] = []
        findings: list[RecoveryFinding] = []
        campaign_id = run.model_policy.campaign_id if run.model_policy else None

        if campaign_id is not None:
            known_model_ids = set(historical)
            run_attempt_prefix = f"model_{run.run_id}_"
            for attempt in self.campaign_ledger.attempts(campaign_id):
                if (
                    not attempt.attempt_id.startswith(run_attempt_prefix)
                    or attempt.attempt_id in known_model_ids
                ):
                    continue
                if attempt.status == "reserved":
                    actions.append(_RecoveryAction("settle_campaign_zero", attempt.attempt_id))
                    findings.append(
                        RecoveryFinding(
                            operation_id=attempt.attempt_id,
                            operation_kind="model",
                            classification="campaign_only_reservation_released",
                            action="campaign_settled_zero_before_dispatch",
                            blocking=False,
                            detail=(
                                "The campaign reservation has no matching Run intent, so the "
                                "provider dispatch gate was never reached and the hold is released."
                            ),
                        )
                    )
                elif (
                    attempt.status == "unknown"
                    or attempt.actual_cost != 0
                    or attempt.provider_trace_id is not None
                ):
                    raise Conflict(
                        f"Campaign-only attempt has nonzero or unknown usage: {attempt.attempt_id}"
                    )

        for call_id, reservation in run.model_reservations.items():
            if campaign_id is None:
                raise IntegrityError("Model reservation has no campaign binding")
            attempt = self.campaign_ledger.attempt(campaign_id, call_id)
            self._validate_attempt(attempt, reservation, campaign_id)
            run_unknown = call_id in run.unknown_model_calls
            if attempt.status == "reserved":
                if not run_unknown:
                    actions.append(_RecoveryAction("mark_model_unknown", call_id))
                actions.append(_RecoveryAction("mark_campaign_unknown", call_id))
                action = (
                    "campaign_marked_unknown" if run_unknown else "run_and_campaign_marked_unknown"
                )
                findings.append(
                    RecoveryFinding(
                        operation_id=call_id,
                        operation_kind="model",
                        client_trace_id=reservation.client_trace_id,
                        classification="model_effect_unknown",
                        action=action,
                        blocking=True,
                        detail=(
                            "A durable model intent exists without a trusted response; the call "
                            "is not retried automatically."
                        ),
                    )
                )
            elif attempt.status == "unknown":
                if not run_unknown:
                    actions.append(_RecoveryAction("mark_model_unknown", call_id))
                findings.append(
                    RecoveryFinding(
                        operation_id=call_id,
                        operation_kind="model",
                        client_trace_id=reservation.client_trace_id,
                        classification="model_effect_unknown",
                        action="run_marked_unknown" if not run_unknown else "none",
                        blocking=True,
                        detail="The campaign already classifies this model dispatch as unknown.",
                    )
                )
            else:
                if not run_unknown:
                    actions.append(_RecoveryAction("mark_model_unknown", call_id))
                findings.append(
                    RecoveryFinding(
                        operation_id=call_id,
                        operation_kind="model",
                        client_trace_id=reservation.client_trace_id,
                        classification="model_response_unavailable",
                        action="run_marked_unknown" if not run_unknown else "none",
                        blocking=True,
                        detail=(
                            "Campaign cost is settled, but the Run has no trusted model response "
                            "record to continue from."
                        ),
                    )
                )

        for record in run.model_calls:
            if campaign_id is None:
                raise IntegrityError("Model receipt has no campaign binding")
            reservation = historical.get(record.call_id)
            if reservation is None:
                raise IntegrityError(f"Model receipt has no historical intent: {record.call_id}")
            attempt = self.campaign_ledger.attempt(campaign_id, record.call_id)
            self._validate_attempt(attempt, reservation, campaign_id)
            if attempt.status == "reserved":
                actions.append(_RecoveryAction("settle_campaign", record.call_id, record))
                findings.append(
                    RecoveryFinding(
                        operation_id=record.call_id,
                        operation_kind="model",
                        client_trace_id=reservation.client_trace_id,
                        classification="campaign_settlement_repaired",
                        action="campaign_settled_from_run_receipt",
                        blocking=False,
                        detail=(
                            "The trusted Run receipt deterministically settles the matching "
                            "campaign reservation."
                        ),
                    )
                )
            elif attempt.status == "settled":
                if (
                    attempt.actual_cost != record.estimated_cost
                    or attempt.provider_trace_id != record.provider_trace_id
                ):
                    raise Conflict(
                        f"Campaign settlement conflicts with Run receipt: {record.call_id}"
                    )
            else:
                raise Conflict(f"Campaign marks a trusted Run receipt as unknown: {record.call_id}")

        model_ids = set(run.model_reservations)
        tool_ids = set(run.tool_reservations)
        for call_id in sorted(tool_ids):
            tool_unknown = call_id in run.unknown_tool_calls
            if not tool_unknown:
                actions.append(_RecoveryAction("mark_tool_unknown", call_id))
            findings.append(
                RecoveryFinding(
                    operation_id=call_id,
                    operation_kind="tool",
                    classification="tool_effect_unknown",
                    action="run_tool_marked_unknown" if not tool_unknown else "none",
                    blocking=True,
                    detail=(
                        "A tool intent has no durable receipt; workspace side effects must be "
                        "inspected before any retry."
                    ),
                )
            )

        for reservation_id in sorted(set(run.reservations) - model_ids - tool_ids):
            already_unknown = reservation_id in run.unknown_reservations
            if not already_unknown:
                actions.append(_RecoveryAction("mark_budget_unknown", reservation_id))
            findings.append(
                RecoveryFinding(
                    operation_id=reservation_id,
                    operation_kind="budget",
                    classification="operation_effect_unknown",
                    action="run_budget_marked_unknown" if not already_unknown else "none",
                    blocking=True,
                    detail="The generic operation has an intent but no durable settlement.",
                )
            )

        for action in actions:
            key = f"recovery_{action.kind}_{action.operation_id}"
            if action.kind == "settle_campaign":
                assert campaign_id is not None and action.record is not None
                self.campaign_ledger.settle(
                    campaign_id,
                    action.operation_id,
                    action.record.estimated_cost,
                    action.record.provider_trace_id,
                )
            elif action.kind == "settle_campaign_zero":
                assert campaign_id is not None
                self.campaign_ledger.settle(
                    campaign_id,
                    action.operation_id,
                    Decimal("0"),
                    None,
                )
            elif action.kind == "mark_campaign_unknown":
                assert campaign_id is not None
                self.campaign_ledger.mark_unknown(
                    campaign_id,
                    action.operation_id,
                    "RecoveryUncertainDispatch",
                )
            elif action.kind == "mark_model_unknown":
                self.service.mark_model_call_unknown(run_id, action.operation_id, token, key)
            elif action.kind == "mark_tool_unknown":
                self.service.mark_tool_call_unknown(run_id, action.operation_id, token, key)
            else:
                self.service.mark_usage_unknown(run_id, action.operation_id, token, key)

        current = self.service.store.get(run_id)
        self._verify_linked_ledgers(current, historical)
        waiting_for_plan = (
            current.status == RunStatus.WAITING_FOR_USER
            and current.resume_state == RunStatus.PLANNING
            and isinstance(current.pending_human_request, HumanPlanRequest)
            and not current.reservations
        )
        waiting_for_guidance = (
            current.status == RunStatus.WAITING_FOR_USER
            and current.resume_state == RunStatus.RUNNING
            and isinstance(current.pending_human_request, HumanGuidanceRequest)
            and not current.reservations
        )
        safe_to_resume = self._is_safe_agent_boundary(current)
        if (
            not safe_to_resume
            and not waiting_for_plan
            and not waiting_for_guidance
            and not any(finding.blocking for finding in findings)
        ):
            findings.append(
                RecoveryFinding(
                    operation_id="agent-session",
                    operation_kind="agent_session",
                    classification="unsafe_agent_boundary",
                    action="none",
                    blocking=True,
                    detail=(
                        "The Run is not at a persisted safe turn boundary; automatic continuation "
                        "would lose or duplicate part of a turn."
                    ),
                )
            )

        return RecoveryReport(
            run_id=run_id,
            linked_ledgers_consistent=True,
            safe_to_resume=safe_to_resume,
            requires_human=not safe_to_resume,
            next_action=(
                "resume"
                if safe_to_resume
                else "provide_replacement_plan"
                if waiting_for_plan
                else "provide_operator_guidance"
                if waiting_for_guidance
                else "manual_reconciliation"
            ),
            remaining_reservations=tuple(sorted(current.reservations)),
            unknown_reservations=tuple(sorted(current.unknown_reservations)),
            findings=tuple(findings),
        )
