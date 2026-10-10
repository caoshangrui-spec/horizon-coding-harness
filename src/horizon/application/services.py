from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from horizon.domain.agent import AgentSessionRecord
from horizon.domain.budget import Usage
from horizon.domain.common import Contract, digest, timestamp
from horizon.domain.errors import (
    BudgetExceeded,
    BudgetStop,
    BudgetStopReason,
    Conflict,
    InvalidTransition,
    LeaseConflict,
    RunCancellationRequested,
    RunDeadlineExceeded,
)
from horizon.domain.events import NewEvent
from horizon.domain.human import (
    HumanGuidanceDecision,
    HumanGuidanceRequest,
    HumanPlanDecision,
    HumanPlanRequest,
    NoProgressPattern,
    matches_no_progress_evidence,
)
from horizon.domain.model import (
    ModelCallRecord,
    ModelCallReservation,
    ModelPolicyBinding,
    ModelRequestBudgetEvidence,
)
from horizon.domain.plan import (
    MAX_EXECUTION_REPLANS,
    ExecutionReplanProposal,
    ExecutionReplanRecord,
    Plan,
    check_execution_replan,
)
from horizon.domain.ports import EventStorePort
from horizon.domain.promotion import PromotionIntent, PromotionReceipt, WorkspaceOrigin
from horizon.domain.run import Run
from horizon.domain.states import RunStatus
from horizon.domain.task import TaskSpec
from horizon.domain.tools import ToolCallRecord, ToolCallReservation


class LeaseToken(Contract):
    lease_id: str
    worker_id: str
    epoch: int

    @classmethod
    def from_run(cls, run: Run) -> LeaseToken:
        if not run.lease_id or not run.worker_id:
            raise LeaseConflict("Run does not have a lease")
        return cls(lease_id=run.lease_id, worker_id=run.worker_id, epoch=run.lease_epoch)


class HarnessService:
    def __init__(self, store: EventStorePort):
        self.store = store

    def _active(self, run: Run) -> None:
        if run.status == RunStatus.CANCELLED or run.cancel_requested:
            raise RunCancellationRequested(run.run_id)
        if run.terminal:
            raise InvalidTransition(f"Run is terminal: {run.status}")
        if self.store.clock() >= datetime.fromisoformat(run.deadline_at):
            raise RunDeadlineExceeded(run.run_id)

    @staticmethod
    def _nonterminal(run: Run) -> None:
        if run.terminal:
            raise InvalidTransition(f"Run is terminal: {run.status}")

    def _check_lease(self, run: Run, token: LeaseToken) -> None:
        if (
            run.lease_id != token.lease_id
            or run.worker_id != token.worker_id
            or run.lease_epoch != token.epoch
            or run.lease_expires_at is None
            or self.store.clock() >= datetime.fromisoformat(run.lease_expires_at)
        ):
            raise LeaseConflict("Worker lease is missing, expired or fenced")

    def check_worker(self, run: Run, token: LeaseToken) -> None:
        self._active(run)
        self._check_lease(run, token)

    def check_recovery_worker(self, run: Run, token: LeaseToken) -> None:
        """Fence receipt/recovery writes without authorizing new work after a stop request."""

        self._nonterminal(run)
        self._check_lease(run, token)

    def _command(
        self,
        run_id: str,
        key: str,
        request: dict[str, Any],
        decide: Callable[[Run], Iterable[NewEvent]],
    ) -> Run:
        """Close a deadline or cancellation only after the command leaves a safe boundary."""

        try:
            result = self.store.command(run_id, key, request, decide)
        except RunCancellationRequested:
            self.cancel_if_safe(run_id, f"cancel_{digest({'command_key': key})}")
            raise
        except RunDeadlineExceeded:
            self.expire_if_safe(run_id, f"deadline_{digest({'command_key': key})}")
            raise
        if result.cancel_requested:
            return self.cancel_if_safe(run_id, f"cancel_{digest({'command_key': key})}")
        return result

    def set_plan(
        self,
        run_id: str,
        plan: Plan,
        key: str,
        token: LeaseToken | None = None,
        *,
        source_model_call_id: str | None = None,
    ) -> Run:
        request = {
            "operation": "plan",
            "plan": plan.model_dump(mode="json"),
            "source_model_call_id": source_model_call_id,
        }

        def decide(run):
            self._active(run)
            if run.lease_id:
                if token is None:
                    raise LeaseConflict("A running worker owns the plan")
                self.check_worker(run, token)
            plan.check_task(run.task)
            if source_model_call_id is not None and not any(
                record.call_id == source_model_call_id and record.purpose == "planning"
                for record in run.model_calls
            ):
                raise Conflict("Generated plan requires a settled planning model receipt")
            proposed = []
            if run.status == RunStatus.CREATED:
                proposed.append(
                    NewEvent(
                        event_type="STATE_CHANGED",
                        payload={
                            "from": "CREATED",
                            "to": "PLANNING",
                        },
                    )
                )
            proposed.append(
                NewEvent(
                    event_type="PLAN_REVISED" if run.plan else "PLAN_CREATED",
                    payload=request,
                )
            )
            if run.status in {RunStatus.CREATED, RunStatus.PLANNING}:
                proposed.append(
                    NewEvent(
                        event_type="STATE_CHANGED",
                        payload={
                            "from": "PLANNING",
                            "to": "READY",
                        },
                    )
                )
            return proposed

        return self._command(run_id, key, request, decide)

    def request_replacement_plan(
        self,
        run_id: str,
        source_model_call_id: str,
        detail: str,
        token: LeaseToken,
        key: str,
    ) -> Run:
        detail = detail.strip()[:2000]
        if not detail:
            detail = "Generated Plan failed controller validation"
        request_data = {
            "operation": "request_replacement_plan",
            "source_model_call_id": source_model_call_id,
            "detail": detail,
            "token": token.model_dump(),
        }

        def decide(run):
            self.check_worker(run, token)
            if run.status != RunStatus.PLANNING or run.plan is not None or run.reservations:
                raise Conflict("Replacement Plan fallback requires quiescent PLANNING")
            record = next(
                (
                    item
                    for item in run.model_calls
                    if item.call_id == source_model_call_id and item.purpose == "planning"
                ),
                None,
            )
            if record is None or record.response_artifact_ref is None:
                raise Conflict("Replacement Plan fallback requires a persisted planner response")
            request = HumanPlanRequest(
                request_id=f"hpr_{digest((run_id, source_model_call_id))[:32]}",
                detail=detail,
                source_model_call_id=source_model_call_id,
                response_artifact_ref=record.response_artifact_ref,
                task_spec_hash=run.task.sha256,
                requested_plan_version=1,
            )
            return [
                NewEvent(
                    event_type="HUMAN_REQUEST_CREATED",
                    payload={"request": request.model_dump(mode="json")},
                ),
                NewEvent(
                    event_type="STATE_CHANGED",
                    payload={"from": RunStatus.PLANNING, "to": RunStatus.WAITING_FOR_USER},
                ),
                NewEvent(event_type="LEASE_RELEASED", payload=token.model_dump()),
            ]

        return self._command(run_id, key, request_data, decide)

    def resolve_replacement_plan(
        self,
        run_id: str,
        plan: Plan,
        key: str,
        *,
        actor: str = "local_cli",
    ) -> Run:
        request_data = {
            "operation": "resolve_replacement_plan",
            "plan": plan.model_dump(mode="json"),
            "actor": actor,
        }

        def decide(run):
            self._active(run)
            if run.lease_id or run.reservations:
                raise Conflict("Replacement Plan decision requires a quiescent Run")
            request = run.pending_human_request
            if (
                run.status != RunStatus.WAITING_FOR_USER
                or run.resume_state != RunStatus.PLANNING
                or request is None
                or request.kind != "replacement_plan_required"
            ):
                raise Conflict("Run is not waiting for a replacement Plan")
            if actor != "local_cli":
                raise Conflict("Only the trusted local CLI can resolve this request")
            if plan.version != request.requested_plan_version:
                raise Conflict("Replacement Plan version does not match the request")
            plan.check_task(run.task)
            decision = HumanPlanDecision(
                decision_id=f"hpd_{digest((request.request_id, plan.sha256))[:32]}",
                request_id=request.request_id,
                actor="local_cli",
                plan_hash=plan.sha256,
                task_spec_hash=run.task.sha256,
            )
            return [
                NewEvent(
                    event_type="HUMAN_DECISION_RECORDED",
                    payload={"decision": decision.model_dump(mode="json")},
                ),
                NewEvent(
                    event_type="STATE_CHANGED",
                    payload={"from": RunStatus.WAITING_FOR_USER, "to": RunStatus.PLANNING},
                ),
                NewEvent(
                    event_type="PLAN_CREATED",
                    payload={
                        "operation": "plan",
                        "plan": plan.model_dump(mode="json"),
                        "source_model_call_id": None,
                    },
                ),
                NewEvent(
                    event_type="STATE_CHANGED",
                    payload={"from": RunStatus.PLANNING, "to": RunStatus.READY},
                ),
            ]

        return self._command(run_id, key, request_data, decide)

    def request_operator_guidance(
        self,
        run_id: str,
        source_tool_call_id: str,
        detail: str,
        pattern: NoProgressPattern,
        token: LeaseToken,
        key: str,
    ) -> Run:
        detail = detail.strip()[:2000]
        if not detail:
            detail = "The Agent continued an exact no-progress pattern after controller feedback"
        request_data = {
            "operation": "request_operator_guidance",
            "source_tool_call_id": source_tool_call_id,
            "detail": detail,
            "pattern": pattern,
            "token": token.model_dump(),
        }

        def decide(run):
            self.check_worker(run, token)
            if (
                run.status != RunStatus.RUNNING
                or run.plan is None
                or run.agent_session is None
                or run.reservations
            ):
                raise Conflict("Operator guidance requires a quiescent running Agent session")
            if run.pending_human_request is not None:
                raise Conflict("Run already has a pending human request")
            record = run.tool_calls[-1] if run.tool_calls else None
            session = run.agent_session
            if (
                record is None
                or record.artifact_ref is None
                or session is None
                or not matches_no_progress_evidence(
                    run.tool_calls,
                    run.no_progress_reset_tool_count,
                    pattern,
                    source_tool_call_id,
                    record.artifact_ref,
                    session.workspace_revision,
                    detail,
                )
            ):
                raise Conflict("Operator guidance requires the latest no-progress evidence")
            request = HumanGuidanceRequest(
                request_id=f"hgr_{digest((run_id, source_tool_call_id))[:32]}",
                pattern=pattern,
                detail=detail,
                source_tool_call_id=source_tool_call_id,
                evidence_artifact_ref=record.artifact_ref,
                task_spec_hash=run.task.sha256,
                plan_version=run.plan.version,
                plan_hash=run.plan.sha256,
                work_item_id=session.work_item_id,
                workspace_revision=session.workspace_revision,
                agent_session_artifact_ref=session.artifact_ref,
                next_iteration=session.next_iteration,
            )
            return [
                NewEvent(
                    event_type="HUMAN_REQUEST_CREATED",
                    payload={"request": request.model_dump(mode="json")},
                ),
                NewEvent(
                    event_type="STATE_CHANGED",
                    payload={"from": RunStatus.RUNNING, "to": RunStatus.WAITING_FOR_USER},
                ),
                NewEvent(event_type="LEASE_RELEASED", payload=token.model_dump()),
            ]

        return self._command(run_id, key, request_data, decide)

    def resolve_operator_guidance(
        self,
        run_id: str,
        session: AgentSessionRecord,
        token: LeaseToken,
        key: str,
        *,
        actor: str = "local_cli",
    ) -> Run:
        request_data = {
            "operation": "resolve_operator_guidance",
            "session": session.model_dump(mode="json"),
            "token": token.model_dump(),
            "actor": actor,
        }

        def decide(run):
            self.check_worker(run, token)
            request = run.pending_human_request
            if (
                run.status != RunStatus.WAITING_FOR_USER
                or run.resume_state != RunStatus.RUNNING
                or not isinstance(request, HumanGuidanceRequest)
                or run.plan is None
                or run.agent_session is None
                or run.reservations
            ):
                raise Conflict("Run is not waiting for operator guidance")
            if actor != "local_cli":
                raise Conflict("Only the trusted local CLI can supply operator guidance")
            current = run.agent_session
            if (
                session.artifact_ref == current.artifact_ref
                or session.task_spec_hash != request.task_spec_hash
                or session.plan_version != request.plan_version
                or session.work_item_id != request.work_item_id
                or session.workspace_revision != request.workspace_revision
                or session.next_iteration != request.next_iteration
                or session.covered_event_seq != run.seq + 2
                or session.message_count <= current.message_count
            ):
                raise Conflict("Guided Agent session does not match the pending request")
            decision = HumanGuidanceDecision(
                decision_id=f"hgd_{digest((request.request_id, session.artifact_ref))[:32]}",
                request_id=request.request_id,
                actor="local_cli",
                guided_session_artifact_ref=session.artifact_ref,
                task_spec_hash=request.task_spec_hash,
                plan_version=request.plan_version,
                workspace_revision=request.workspace_revision,
                next_iteration=request.next_iteration,
            )
            return [
                NewEvent(
                    event_type="HUMAN_DECISION_RECORDED",
                    payload={"decision": decision.model_dump(mode="json")},
                ),
                NewEvent(
                    event_type="STATE_CHANGED",
                    payload={"from": RunStatus.WAITING_FOR_USER, "to": RunStatus.RUNNING},
                ),
                NewEvent(
                    event_type="AGENT_SESSION_GUIDED",
                    payload={"session": session.model_dump(mode="json")},
                ),
                NewEvent(event_type="LEASE_RELEASED", payload=token.model_dump()),
            ]

        return self._command(run_id, key, request_data, decide)

    def bind_workspace_origin(self, run_id: str, origin: WorkspaceOrigin, key: str) -> Run:
        request = {
            "operation": "bind_workspace_origin",
            "origin": origin.model_dump(mode="json"),
        }

        def decide(run):
            self._active(run)
            if run.status != RunStatus.CREATED or run.lease_id:
                raise Conflict("Workspace origin must be bound before planning or leasing")
            if run.workspace_origin is not None:
                if run.workspace_origin != origin:
                    raise Conflict("Run already uses a different workspace origin")
                return []
            if run.task.repository.base_commit != origin.source_revision:
                raise Conflict("Workspace origin does not match the TaskSpec base revision")
            return [
                NewEvent(
                    event_type="WORKSPACE_ORIGIN_BOUND",
                    payload={"origin": origin.model_dump(mode="json")},
                )
            ]

        return self._command(run_id, key, request, decide)

    def acquire_lease(
        self,
        run_id: str,
        worker_id: str,
        key: str,
        ttl_seconds: int = 60,
        *,
        prior_worker_stopped: bool = False,
        confirmed_stopped_process_id: int | None = None,
    ) -> Run:
        return self._acquire_lease(
            run_id,
            worker_id,
            key,
            ttl_seconds,
            prior_worker_stopped=prior_worker_stopped,
            confirmed_stopped_process_id=confirmed_stopped_process_id,
            recovery=False,
        )

    def acquire_recovery_lease(
        self,
        run_id: str,
        worker_id: str,
        key: str,
        ttl_seconds: int = 60,
        *,
        prior_worker_stopped: bool = False,
        confirmed_stopped_process_id: int | None = None,
    ) -> Run:
        """Acquire a fenced lease for reconciliation without authorizing new work."""

        return self._acquire_lease(
            run_id,
            worker_id,
            key,
            ttl_seconds,
            prior_worker_stopped=prior_worker_stopped,
            confirmed_stopped_process_id=confirmed_stopped_process_id,
            recovery=True,
        )

    def _acquire_lease(
        self,
        run_id: str,
        worker_id: str,
        key: str,
        ttl_seconds: int,
        *,
        prior_worker_stopped: bool,
        confirmed_stopped_process_id: int | None,
        recovery: bool,
    ) -> Run:
        if not worker_id or not 1 <= ttl_seconds <= 600:
            raise ValueError("Worker ID and a lease TTL in [1, 600] are required")
        if confirmed_stopped_process_id is not None and (
            not prior_worker_stopped
            or not isinstance(confirmed_stopped_process_id, int)
            or isinstance(confirmed_stopped_process_id, bool)
            or confirmed_stopped_process_id < 1
        ):
            raise ValueError(
                "A confirmed stopped process ID requires prior-worker-stopped evidence"
            )
        request = {
            "operation": "acquire_recovery_lease" if recovery else "acquire_lease",
            "worker_id": worker_id,
            "ttl": ttl_seconds,
            "prior_worker_stopped": prior_worker_stopped,
        }
        if confirmed_stopped_process_id is not None:
            request["confirmed_stopped_process_id"] = confirmed_stopped_process_id

        def decide(run):
            if recovery:
                self._nonterminal(run)
            else:
                self._active(run)
            cancel_target = run.cancel_worker_stop
            if cancel_target is not None and (not recovery or not prior_worker_stopped):
                raise LeaseConflict("A cancellation-bound Worker must be stopped before takeover")
            if cancel_target is not None and (
                confirmed_stopped_process_id != cancel_target["process_id"]
            ):
                raise LeaseConflict(
                    "Old Worker stop confirmation must match the cancellation target PID"
                )
            if cancel_target is None and confirmed_stopped_process_id is not None:
                raise LeaseConflict(
                    "A stopped process ID can confirm only a cancellation-bound Worker"
                )
            now = self.store.clock()
            if run.lease_id:
                if now < datetime.fromisoformat(run.lease_expires_at):
                    raise LeaseConflict("Another live worker owns this run")
                if not prior_worker_stopped:
                    raise LeaseConflict("TTL expiry is not proof that the old process stopped")
            events = []
            if cancel_target is not None:
                target_matches_live_lease = (
                    cancel_target["lease_id"],
                    cancel_target["worker_id"],
                    cancel_target["epoch"],
                ) == (run.lease_id, run.worker_id, run.lease_epoch)
                target_matches_released_lease = (
                    run.lease_id is None
                    and run.worker_id is None
                    and run.lease_expires_at is None
                    and cancel_target["epoch"] == run.lease_epoch
                )
                if not target_matches_live_lease and not target_matches_released_lease:
                    raise LeaseConflict("Cancellation target no longer matches the stopped Worker")
                events.append(
                    NewEvent(
                        event_type="CANCEL_WORKER_STOPPED",
                        payload={
                            **cancel_target,
                            "exit_code": None,
                            "stop_method": "operator_confirmed",
                            "lease_release": (
                                "takeover" if target_matches_live_lease else "already_released"
                            ),
                        },
                        occurred_at=now,
                    )
                )
            events.append(
                NewEvent(
                    event_type="LEASE_ACQUIRED",
                    payload={
                        "lease_id": f"lease_{uuid4().hex}",
                        "worker_id": worker_id,
                        "epoch": run.lease_epoch + 1,
                        "expires_at": timestamp(now + timedelta(seconds=ttl_seconds)),
                    },
                    occurred_at=now,
                )
            )
            return events

        return self._command(run_id, key, request, decide)

    def takeover_reaped_worker_lease(
        self,
        run_id: str,
        previous_token: LeaseToken,
        worker_id: str,
        key: str,
        *,
        process_id: int,
        exit_code: int,
        launch_event_seq: int,
        ttl_seconds: int = 60,
    ) -> Run:
        """Fence an expired exact Lease after a trusted parent reaped its child Worker."""

        if not worker_id or not 1 <= ttl_seconds <= 600 or process_id < 1 or launch_event_seq < 1:
            raise ValueError(
                "Expired reaped-worker takeover requires worker/process evidence and a valid TTL"
            )
        request = {
            "operation": "takeover_reaped_worker_lease",
            "previous_token": previous_token.model_dump(),
            "worker_id": worker_id,
            "ttl": ttl_seconds,
            "process_id": process_id,
            "exit_code": exit_code,
            "launch_event_seq": launch_event_seq,
        }

        def decide(run):
            # A reaped-worker takeover only fences a proven-stopped process. It may be needed
            # after the wall-clock deadline to classify an already-dispatched effect.
            self._nonterminal(run)
            now = self.store.clock()
            if run.status != RunStatus.RUNNING or run.plan is None:
                raise LeaseConflict("Expired reaped-worker takeover requires a RUNNING Run")
            if run.cancel_worker_stop is not None:
                raise LeaseConflict("A cancellation-bound Worker cannot use reaped takeover")
            if (
                run.lease_id != previous_token.lease_id
                or run.worker_id != previous_token.worker_id
                or run.lease_epoch != previous_token.epoch
                or run.lease_expires_at is None
            ):
                raise LeaseConflict("Reaped worker no longer owns the exact expired lease")
            if now < datetime.fromisoformat(run.lease_expires_at):
                raise LeaseConflict("Reaped worker lease is still live")
            if launch_event_seq > run.seq:
                raise Conflict("Reaped worker launch boundary is ahead of the durable Run")
            return [
                NewEvent(
                    event_type="LEASE_ACQUIRED",
                    payload={
                        "lease_id": f"lease_{uuid4().hex}",
                        "worker_id": worker_id,
                        "epoch": run.lease_epoch + 1,
                        "expires_at": timestamp(now + timedelta(seconds=ttl_seconds)),
                        "takeover_reason": "confirmed_reaped_worker_expired",
                        "previous_lease_id": previous_token.lease_id,
                        "previous_worker_id": previous_token.worker_id,
                        "previous_epoch": previous_token.epoch,
                        "process_id": process_id,
                        "exit_code": exit_code,
                        "launch_event_seq": launch_event_seq,
                        "observed_event_seq": run.seq,
                    },
                    occurred_at=now,
                )
            ]

        return self._command(run_id, key, request, decide)

    def renew_lease(self, run_id: str, token: LeaseToken, key: str, ttl_seconds: int = 60) -> Run:
        if not 1 <= ttl_seconds <= 600:
            raise ValueError("Lease TTL must be in [1, 600]")
        request = {"operation": "renew_lease", "token": token.model_dump(), "ttl": ttl_seconds}

        def decide(run):
            self.check_worker(run, token)
            now = self.store.clock()
            return [
                NewEvent(
                    event_type="LEASE_RENEWED",
                    payload={
                        "lease_id": token.lease_id,
                        "epoch": token.epoch,
                        "expires_at": timestamp(now + timedelta(seconds=ttl_seconds)),
                    },
                    occurred_at=now,
                )
            ]

        return self._command(run_id, key, request, decide)

    def release_lease(self, run_id: str, token: LeaseToken, key: str) -> Run:
        request = {"operation": "release_lease", "token": token.model_dump()}

        def decide(run):
            self.check_recovery_worker(run, token)
            if set(run.reservations) - run.unknown_reservations:
                raise LeaseConflict("Cannot release a worker with unsettled operations")
            return [NewEvent(event_type="LEASE_RELEASED", payload=token.model_dump())]

        released = self._command(run_id, key, request, decide)
        if released.terminal:
            return released
        return self.expire_if_safe(
            run_id,
            f"release_deadline_{digest({'command_key': key})}",
        )

    def release_reaped_worker_lease(
        self,
        run_id: str,
        token: LeaseToken,
        key: str,
        *,
        process_id: int,
        exit_code: int,
        launch_event_seq: int,
    ) -> Run:
        """Fence an exact local worker after its parent synchronously reaped it.

        Unlike the generic recovery release, this path requires a completely empty reservation
        set. The caller is responsible for proving a safe Agent boundary before invoking it; the
        process evidence is retained in the normal lease event for trace replay and audit.
        """

        if process_id < 1 or launch_event_seq < 1:
            raise ValueError("Reaped worker evidence requires a process ID and launch event")
        request = {
            "operation": "release_reaped_worker_lease",
            "token": token.model_dump(),
            "process_id": process_id,
            "exit_code": exit_code,
            "launch_event_seq": launch_event_seq,
        }

        def decide(run):
            self.check_recovery_worker(run, token)
            if run.status != RunStatus.RUNNING or run.agent_session is None:
                raise LeaseConflict("Reaped worker release requires a running Agent session")
            if run.reservations:
                raise LeaseConflict("Cannot auto-fence a reaped worker with pending operations")
            if launch_event_seq > run.seq:
                raise Conflict("Reaped worker launch boundary is ahead of the durable Run")
            return [
                NewEvent(
                    event_type="LEASE_RELEASED",
                    payload={
                        **token.model_dump(),
                        "release_reason": "confirmed_reaped_worker",
                        "process_id": process_id,
                        "exit_code": exit_code,
                        "launch_event_seq": launch_event_seq,
                        "safe_event_seq": run.seq,
                    },
                )
            ]

        released = self._command(run_id, key, request, decide)
        if released.terminal:
            return released
        return self.expire_if_safe(
            run_id,
            f"reaped_release_deadline_{digest({'command_key': key})}",
        )

    def transition(self, run_id: str, target: RunStatus, token: LeaseToken, key: str) -> Run:
        request = {"operation": "transition", "to": target.value, "token": token.model_dump()}

        def decide(run):
            self.check_worker(run, token)
            if run.status == RunStatus.VALIDATING and target == RunStatus.RUNNING:
                raise Conflict(
                    "Use atomic work-item advancement to leave validation for another item"
                )
            return [
                NewEvent(event_type="STATE_CHANGED", payload={"from": run.status, "to": target})
            ]

        return self._command(run_id, key, request, decide)

    def cancel(self, run_id: str, key: str) -> Run:
        def decide(run):
            if run.terminal:
                return []
            if run.cancel_requested:
                return []
            if self._cancellation_blocked(run):
                return [NewEvent(event_type="CANCEL_PENDING", payload={"source": "local_control"})]
            return [
                NewEvent(
                    event_type="CANCEL_REQUESTED",
                    payload={"source": "local_control", "recovery_safe": True},
                )
            ]

        requested = self.store.command(run_id, key, {"operation": "cancel"}, decide)
        if requested.cancel_requested:
            return self.cancel_if_safe(run_id, f"cancel_finalize_{digest({'command_key': key})}")
        return requested

    @staticmethod
    def _cancellation_blocked(run: Run) -> bool:
        return bool(
            set(run.reservations) - run.unknown_reservations
            or run.tool_reservations
            or run.cancel_worker_stop is not None
        )

    def request_cancel_worker_stop(
        self,
        run_id: str,
        token: LeaseToken,
        key: str,
        *,
        process_id: int,
        launch_event_seq: int,
    ) -> Run:
        """Persist a cancellation fence for one exact local Worker process.

        The caller must publish this fence before asking the operating system to stop the
        process. The bound Worker then becomes a cancellation blocker in its own right, even
        when it has not reserved a model or tool effect yet.
        """

        if (
            not isinstance(process_id, int)
            or isinstance(process_id, bool)
            or process_id < 1
            or not isinstance(launch_event_seq, int)
            or isinstance(launch_event_seq, bool)
            or launch_event_seq < 1
        ):
            raise ValueError("Worker cancellation requires a process ID and launch event")
        request = {
            "operation": "request_cancel_worker_stop",
            "token": token.model_dump(),
            "process_id": process_id,
            "launch_event_seq": launch_event_seq,
        }
        target = {
            **token.model_dump(),
            "process_id": process_id,
            "launch_event_seq": launch_event_seq,
            "source": "local_control",
        }

        def decide(run):
            self._nonterminal(run)
            if run.status != RunStatus.RUNNING or run.plan is None:
                raise Conflict("Worker cancellation requires an active planned Run")
            if (
                run.lease_id != token.lease_id
                or run.worker_id != token.worker_id
                or run.lease_epoch != token.epoch
            ):
                raise LeaseConflict("Worker cancellation target no longer owns the exact lease")
            if launch_event_seq > run.seq:
                raise Conflict("Worker cancellation launch boundary is ahead of the durable Run")
            if run.cancel_worker_stop is not None:
                if run.cancel_worker_stop != target:
                    raise Conflict("A different Worker process is already bound to cancellation")
                return []
            return [NewEvent(event_type="CANCEL_WORKER_STOP_PENDING", payload=target)]

        return self._command(run_id, key, request, decide)

    def record_cancel_worker_stopped(
        self,
        run_id: str,
        token: LeaseToken,
        key: str,
        *,
        process_id: int,
        launch_event_seq: int,
        exit_code: int,
        stop_method: str,
    ) -> Run:
        """Record synchronous reap evidence and fence the exact stopped Worker lease."""

        if (
            not isinstance(process_id, int)
            or isinstance(process_id, bool)
            or process_id < 1
            or not isinstance(launch_event_seq, int)
            or isinstance(launch_event_seq, bool)
            or launch_event_seq < 1
            or not isinstance(exit_code, int)
            or isinstance(exit_code, bool)
            or stop_method not in {"already_exited", "terminate", "kill"}
        ):
            raise ValueError("Worker stop receipt requires exact process and exit evidence")
        request = {
            "operation": "record_cancel_worker_stopped",
            "token": token.model_dump(),
            "process_id": process_id,
            "launch_event_seq": launch_event_seq,
            "exit_code": exit_code,
            "stop_method": stop_method,
        }
        target = {
            **token.model_dump(),
            "process_id": process_id,
            "launch_event_seq": launch_event_seq,
            "source": "local_control",
        }

        def decide(run):
            self._nonterminal(run)
            if not run.cancel_requested or run.cancel_worker_stop != target:
                raise Conflict("Worker stop receipt does not match the pending cancellation")
            lease_active = (run.lease_id, run.worker_id, run.lease_epoch) == (
                token.lease_id,
                token.worker_id,
                token.epoch,
            )
            lease_released = (
                run.lease_id is None
                and run.worker_id is None
                and run.lease_expires_at is None
                and run.lease_epoch == token.epoch
            )
            if not lease_active and not lease_released:
                raise LeaseConflict("Stopped Worker no longer owns the exact cancellation lease")
            receipt = {
                **target,
                "exit_code": exit_code,
                "stop_method": stop_method,
                "lease_release": "parent" if lease_active else "already_released",
            }
            events = [NewEvent(event_type="CANCEL_WORKER_STOPPED", payload=receipt)]
            if lease_active:
                events.append(
                    NewEvent(
                        event_type="LEASE_RELEASED",
                        payload={
                            **token.model_dump(),
                            "release_reason": "cancelled_reaped_worker",
                            "process_id": process_id,
                            "exit_code": exit_code,
                            "stop_method": stop_method,
                            "launch_event_seq": launch_event_seq,
                            "safe_event_seq": run.seq + 1,
                        },
                    )
                )
            return events

        return self._command(run_id, key, request, decide)

    def cancel_if_safe(self, run_id: str, key: str) -> Run:
        """Finish a durable cancellation only after external effects are classified."""

        current = self.store.get(run_id)
        if current.terminal or not current.cancel_requested:
            return current
        if self._cancellation_blocked(current):
            return current

        def decide(run):
            if run.terminal or not run.cancel_requested:
                return []
            if self._cancellation_blocked(run):
                return []
            return [
                NewEvent(
                    event_type="CANCEL_REQUESTED",
                    payload={"source": "local_control", "recovery_safe": True},
                )
            ]

        return self.store.command(
            run_id,
            key,
            {"operation": "cancel_if_safe"},
            decide,
        )

    def record_cancel_sandbox_stopped(
        self,
        run_id: str,
        call_id: str,
        container_name: str,
        image_id: str,
        key: str,
    ) -> Run:
        request = {
            "operation": "record_cancel_sandbox_stopped",
            "call_id": call_id,
            "container_name": container_name,
            "image_id": image_id,
        }

        def decide(run):
            if not run.cancel_requested:
                raise Conflict("Run has no pending cancellation")
            reservation = run.tool_reservations.get(call_id)
            if reservation is None or reservation.name != "run_check":
                raise Conflict("Cancellation stop receipt requires the exact pending run_check")
            existing = next(
                (item for item in run.cancel_stop_receipts if item["call_id"] == call_id),
                None,
            )
            if existing is not None:
                if existing["container_name"] != container_name or existing["image_id"] != image_id:
                    raise Conflict("Cancellation stop receipt conflicts with prior evidence")
                return []
            return [
                NewEvent(
                    event_type="CANCEL_SANDBOX_STOPPED",
                    payload={**request, "source": "local_control"},
                )
            ]

        return self._command(run_id, key, request, decide)

    def expire(self, run_id: str, key: str) -> Run:
        current = self.store.get(run_id)
        if current.cancel_requested:
            return self.cancel_if_safe(run_id, f"cancel_before_expire_{digest({'key': key})}")

        def decide(run):
            if run.terminal or self.store.clock() < datetime.fromisoformat(run.deadline_at):
                return []
            if set(run.reservations) - run.unknown_reservations or run.tool_reservations:
                raise Conflict(
                    "Run deadline cannot terminalize unclassified or recoverable tool "
                    "operations; reconciliation is required"
                )
            return [NewEvent(event_type="RUN_FAILED", payload={"reason": "wall_clock_limit"})]

        return self.store.command(run_id, key, {"operation": "expire"}, decide)

    def expire_if_safe(self, run_id: str, key: str) -> Run:
        """Terminalize an expired Run only when no remaining effect needs operator recovery."""

        current = self.store.get(run_id)
        if current.cancel_requested:
            return self.cancel_if_safe(run_id, f"cancel_before_expire_{digest({'key': key})}")
        if current.terminal or self.store.clock() < datetime.fromisoformat(current.deadline_at):
            return current
        if set(current.reservations) - current.unknown_reservations or current.tool_reservations:
            return current
        return self.expire(run_id, key)

    def reserve(
        self,
        run_id: str,
        reservation_id: str,
        amount: Usage,
        token: LeaseToken,
        key: str,
    ) -> Run:
        request = {
            "operation": "reserve",
            "reservation_id": reservation_id,
            "amount": amount.model_dump(mode="json"),
            "token": token.model_dump(),
        }

        def decide(run):
            self.check_worker(run, token)
            if run.status not in {
                RunStatus.PLANNING,
                RunStatus.RUNNING,
                RunStatus.VALIDATING,
                RunStatus.COMPACTING,
                RunStatus.REPAIRING,
            }:
                raise InvalidTransition("This phase cannot dispatch budgeted operations")
            if run.unknown_reservations and run.task.budgets.unknown_cost_policy == "block":
                raise BudgetExceeded("Unknown usage must be reconciled before another operation")
            run.occupied.plus(amount).check(run.task.budgets)
            return [NewEvent(event_type="BUDGET_RESERVED", payload=request)]

        return self._command(run_id, key, request, decide)

    def bind_model_policy(
        self,
        run_id: str,
        policy: ModelPolicyBinding,
        token: LeaseToken,
        key: str,
    ) -> Run:
        request = {
            "operation": "bind_model_policy",
            "policy": policy.model_dump(mode="json"),
            "token": token.model_dump(),
        }

        def decide(run):
            self.check_worker(run, token)
            if run.model_policy is not None:
                if run.model_policy != policy:
                    raise Conflict("Run already uses a different model policy")
                return []
            if run.task.model_policy_id != policy.policy_id:
                raise Conflict("TaskSpec model_policy_id does not match the selected policy")
            return [
                NewEvent(
                    event_type="MODEL_POLICY_BOUND",
                    payload={"policy": request["policy"]},
                )
            ]

        return self._command(run_id, key, request, decide)

    def reserve_model_call(
        self,
        run_id: str,
        reservation: ModelCallReservation,
        amount: Usage,
        token: LeaseToken,
        key: str,
    ) -> Run:
        if amount.model_calls != 1 or amount.cost_usd != Decimal("0"):
            raise ValueError("A model reservation needs one call; CNY is tracked separately")
        request = {
            "operation": "reserve_model_call",
            "reservation": reservation.as_dict(),
            "amount": amount.model_dump(mode="json"),
            "token": token.model_dump(),
        }

        def decide(run):
            self.check_worker(run, token)
            if run.status not in {
                RunStatus.PLANNING,
                RunStatus.RUNNING,
                RunStatus.COMPACTING,
                RunStatus.REPAIRING,
            }:
                raise InvalidTransition("This phase cannot dispatch a model call")
            if run.unknown_reservations and run.task.budgets.unknown_cost_policy == "block":
                raise BudgetExceeded("Unknown usage must be reconciled before another operation")
            if run.model_policy is None:
                raise Conflict("Model policy must be bound before dispatch")
            if (
                reservation.provider_id != run.model_policy.provider_id
                or reservation.model != run.model_policy.model
                or reservation.currency != run.model_policy.currency
            ):
                raise Conflict("Model reservation does not match the bound policy")
            if run.model_occupied_cost + reservation.reserved_cost > run.model_policy.max_run_cost:
                raise BudgetExceeded(
                    "Per-run model cost ceiling would be exceeded",
                    stop=BudgetStop(
                        reason_code=BudgetStopReason.RUN_MODEL_COST_LIMIT,
                        scope="run",
                        currency=run.model_policy.currency,
                        required_cost=reservation.reserved_cost,
                        available_cost=max(
                            Decimal("0"),
                            run.model_policy.max_run_cost - run.model_occupied_cost,
                        ),
                    ),
                )
            run.occupied.plus(amount).check(run.task.budgets)
            budget_payload = {
                "operation": "reserve",
                "reservation_id": reservation.call_id,
                "amount": amount.model_dump(mode="json"),
                "token": token.model_dump(),
            }
            return [
                NewEvent(event_type="BUDGET_RESERVED", payload=budget_payload),
                NewEvent(
                    event_type="MODEL_CALL_RESERVED",
                    payload={"reservation": reservation.as_dict()},
                ),
            ]

        return self._command(run_id, key, request, decide)

    def settle_model_call(
        self,
        run_id: str,
        record: ModelCallRecord,
        actual: Usage,
        token: LeaseToken,
        key: str,
    ) -> Run:
        if (
            actual.model_calls != 1
            or actual.cost_usd != Decimal("0")
            or actual.input_tokens != record.usage.input_tokens
            or actual.output_tokens != record.usage.output_tokens
        ):
            raise ValueError("Model usage settlement does not match the trusted provider record")
        request = {
            "operation": "settle_model_call",
            "record": record.model_dump(mode="json"),
            "actual": actual.model_dump(mode="json"),
            "token": token.model_dump(),
        }

        def decide(run):
            self.check_recovery_worker(run, token)
            reservation = run.model_reservations.get(record.call_id)
            if reservation is None or record.call_id not in run.reservations:
                raise Conflict("Unknown or already settled model call")
            events = [
                NewEvent(
                    event_type="BUDGET_SETTLED",
                    payload={
                        "operation": "settle",
                        "reservation_id": record.call_id,
                        "actual": actual.model_dump(mode="json"),
                        "token": token.model_dump(),
                    },
                ),
                NewEvent(
                    event_type="MODEL_CALL_SETTLED",
                    payload={"record": record.model_dump(mode="json")},
                ),
            ]
            occupied = run.usage.plus(actual)
            for key_, value in run.reservations.items():
                if key_ != record.call_id:
                    occupied = occupied.plus(value)
            model_cost = run.model_occupied_cost - reservation.reserved_cost + record.estimated_cost
            if occupied.exceeded(run.task.budgets) or (
                run.model_policy is not None and model_cost > run.model_policy.max_run_cost
            ):
                events.append(
                    NewEvent(event_type="RUN_FAILED", payload={"reason": "usage_overrun"})
                )
            return events

        return self._command(run_id, key, request, decide)

    def mark_model_call_unknown(
        self,
        run_id: str,
        call_id: str,
        token: LeaseToken,
        key: str,
    ) -> Run:
        request = {
            "operation": "model_call_unknown",
            "call_id": call_id,
            "token": token.model_dump(),
        }

        def decide(run):
            self.check_recovery_worker(run, token)
            if call_id not in run.model_reservations:
                raise Conflict("Unknown model reservation")
            events = []
            if call_id not in run.unknown_reservations:
                events.append(
                    NewEvent(
                        event_type="BUDGET_USAGE_UNKNOWN",
                        payload={
                            "operation": "unknown_usage",
                            "reservation_id": call_id,
                            "token": token.model_dump(),
                        },
                    )
                )
            if call_id not in run.unknown_model_calls:
                events.append(
                    NewEvent(event_type="MODEL_CALL_UNKNOWN", payload={"call_id": call_id})
                )
            return events

        return self._command(run_id, key, request, decide)

    def reserve_tool_call(
        self,
        run_id: str,
        reservation: ToolCallReservation,
        token: LeaseToken,
        key: str,
    ) -> Run:
        amount = Usage(tool_calls=1, steps=1)
        request = {
            "operation": "reserve_tool_call",
            "reservation": reservation.model_dump(mode="json"),
            "token": token.model_dump(),
        }

        def decide(run):
            self.check_worker(run, token)
            if run.status not in {RunStatus.RUNNING, RunStatus.REPAIRING}:
                raise InvalidTransition("This phase cannot dispatch a tool call")
            if run.unknown_reservations and run.task.budgets.unknown_cost_policy == "block":
                raise BudgetExceeded("Unknown usage must be reconciled before another operation")
            run.occupied.plus(amount).check(run.task.budgets)
            return [
                NewEvent(
                    event_type="BUDGET_RESERVED",
                    payload={
                        "operation": "reserve",
                        "reservation_id": reservation.call_id,
                        "amount": amount.model_dump(mode="json"),
                        "token": token.model_dump(),
                    },
                ),
                NewEvent(
                    event_type="TOOL_CALL_RESERVED",
                    payload={"reservation": reservation.model_dump(mode="json")},
                ),
            ]

        return self._command(run_id, key, request, decide)

    def settle_tool_call(
        self,
        run_id: str,
        record: ToolCallRecord,
        token: LeaseToken,
        key: str,
    ) -> Run:
        actual = Usage(tool_calls=1, steps=1)
        request = {
            "operation": "settle_tool_call",
            "record": record.model_dump(mode="json"),
            "token": token.model_dump(),
        }

        def decide(run):
            self.check_recovery_worker(run, token)
            if record.call_id not in run.tool_reservations:
                raise Conflict("Unknown or already settled tool call")
            return [
                NewEvent(
                    event_type="BUDGET_SETTLED",
                    payload={
                        "operation": "settle",
                        "reservation_id": record.call_id,
                        "actual": actual.model_dump(mode="json"),
                        "token": token.model_dump(),
                    },
                ),
                NewEvent(
                    event_type="TOOL_CALL_SETTLED",
                    payload={"record": record.model_dump(mode="json")},
                ),
            ]

        return self._command(run_id, key, request, decide)

    def apply_execution_replan(
        self,
        run_id: str,
        proposal: ExecutionReplanProposal,
        source_model_call_id: str,
        reservation: ToolCallReservation,
        record: ToolCallRecord,
        next_session: AgentSessionRecord,
        token: LeaseToken,
        key: str,
    ) -> Run:
        """Atomically account for a controller-only replan and publish its new session."""

        amount = Usage(tool_calls=1, steps=1)
        request = {
            "operation": "apply_execution_replan",
            "proposal": proposal.model_dump(mode="json"),
            "source_model_call_id": source_model_call_id,
            "reservation": reservation.model_dump(mode="json"),
            "record": record.model_dump(mode="json"),
            "next_session": next_session.model_dump(mode="json"),
            "token": token.model_dump(),
        }

        def decide(run):
            self.check_worker(run, token)
            if (
                run.status != RunStatus.RUNNING
                or run.plan is None
                or run.agent_session is None
                or run.reservations
                or run.pending_human_request is not None
            ):
                raise Conflict("Execution replan requires a quiescent running Agent session")
            if len(run.execution_replans) >= MAX_EXECUTION_REPLANS:
                raise Conflict("Execution replan budget is exhausted")
            model_record = run.model_calls[-1] if run.model_calls else None
            if (
                model_record is None
                or model_record.call_id != source_model_call_id
                or model_record.purpose != "execution"
                or model_record.response_artifact_ref is None
            ):
                raise Conflict("Execution replan requires the latest settled model response")
            plan = proposal.plan(run.plan.version + 1)
            check_execution_replan(run.plan, plan, run.passed_items, run.task)
            current_session = run.agent_session
            ready = plan.ready_items(run.passed_items)
            if (
                reservation.call_id != record.call_id
                or reservation.name != "revise_plan"
                or record.name != reservation.name
                or reservation.arguments_hash != proposal.sha256
                or record.arguments_hash != proposal.sha256
                or reservation.workspace_revision != current_session.workspace_revision
                or record.status != "success"
                or record.workspace_revision_before != current_session.workspace_revision
                or record.workspace_revision_after != current_session.workspace_revision
                or record.artifact_ref is None
                or record.output_hash != record.artifact_ref
                or record.workspace_manifest_ref != reservation.workspace_manifest_ref
                or next_session.task_spec_hash != run.task.sha256
                or next_session.plan_version != plan.version
                or next_session.work_item_id != ready[0].work_item_id
                or next_session.next_iteration != current_session.next_iteration + 1
                or next_session.covered_event_seq != run.seq + 7
                or next_session.workspace_revision != current_session.workspace_revision
            ):
                raise Conflict("Execution replan artifacts do not match the current run boundary")
            if next_session.next_iteration > run.task.budgets.max_model_calls + 1:
                raise Conflict("Execution replan session exceeds the model-call budget")
            if run.unknown_reservations and run.task.budgets.unknown_cost_policy == "block":
                raise BudgetExceeded("Unknown usage must be reconciled before replanning")
            run.occupied.plus(amount).check(run.task.budgets)
            preserved = tuple(
                item.work_item_id
                for item in run.plan.items
                if item.work_item_id in run.passed_items
            )
            replan = ExecutionReplanRecord(
                source_model_call_id=source_model_call_id,
                source_tool_call_id=record.call_id,
                proposal_hash=proposal.sha256,
                reason=proposal.reason,
                old_plan_version=run.plan.version,
                old_plan_hash=run.plan.sha256,
                new_plan_version=plan.version,
                new_plan_hash=plan.sha256,
                workspace_revision=current_session.workspace_revision,
                preserved_work_item_ids=preserved,
            )
            return [
                NewEvent(
                    event_type="BUDGET_RESERVED",
                    payload={
                        "operation": "execution_replan",
                        "reservation_id": reservation.call_id,
                        "amount": amount.model_dump(mode="json"),
                        "token": token.model_dump(),
                    },
                ),
                NewEvent(
                    event_type="TOOL_CALL_RESERVED",
                    payload={"reservation": reservation.model_dump(mode="json")},
                ),
                NewEvent(
                    event_type="BUDGET_SETTLED",
                    payload={
                        "operation": "execution_replan",
                        "reservation_id": reservation.call_id,
                        "actual": amount.model_dump(mode="json"),
                        "token": token.model_dump(),
                    },
                ),
                NewEvent(
                    event_type="TOOL_CALL_SETTLED",
                    payload={"record": record.model_dump(mode="json")},
                ),
                NewEvent(
                    event_type="STATE_CHANGED",
                    payload={"from": RunStatus.RUNNING, "to": RunStatus.REPAIRING},
                ),
                NewEvent(
                    event_type="PLAN_REVISED",
                    payload={
                        "operation": "execution_replan",
                        "plan": plan.model_dump(mode="json"),
                        "source_model_call_id": source_model_call_id,
                        "execution_replan": replan.model_dump(mode="json"),
                    },
                ),
                NewEvent(
                    event_type="STATE_CHANGED",
                    payload={"from": RunStatus.REPAIRING, "to": RunStatus.RUNNING},
                ),
                NewEvent(
                    event_type="AGENT_SESSION_SAVED",
                    payload={"session": next_session.model_dump(mode="json")},
                ),
            ]

        return self._command(run_id, key, request, decide)

    def settle_tool_call_and_save_session(
        self,
        run_id: str,
        record: ToolCallRecord,
        session: AgentSessionRecord,
        token: LeaseToken,
        key: str,
    ) -> Run:
        """Atomically close a recovered tool intent and publish its completed Agent turn."""
        actual = Usage(tool_calls=1, steps=1)
        request = {
            "operation": "settle_tool_call_and_save_session",
            "record": record.model_dump(mode="json"),
            "session": session.model_dump(mode="json"),
            "token": token.model_dump(),
        }

        def decide(run):
            self.check_recovery_worker(run, token)
            if record.call_id not in run.tool_reservations:
                raise Conflict("Unknown or already settled tool call")
            if run.status != RunStatus.RUNNING or run.plan is None:
                raise Conflict("Recovered Agent session requires a running planned Run")
            if (
                session.task_spec_hash != run.task.sha256
                or session.plan_version != run.plan.version
                or session.work_item_id not in {item.work_item_id for item in run.plan.items}
            ):
                raise Conflict("Recovered Agent session does not match the current task and plan")
            if session.next_iteration > run.task.budgets.max_model_calls + 1:
                raise Conflict("Recovered Agent session exceeds the TaskSpec model-call budget")
            if run.agent_session is not None and (
                session.next_iteration <= run.agent_session.next_iteration
            ):
                raise Conflict("Recovered Agent session iteration must advance")
            # This command appends budget settlement, tool receipt, then session publication.
            if session.covered_event_seq != run.seq + 2:
                raise Conflict("Recovered Agent session does not cover its atomic tool settlement")
            return [
                NewEvent(
                    event_type="BUDGET_SETTLED",
                    payload={
                        "operation": "settle",
                        "reservation_id": record.call_id,
                        "actual": actual.model_dump(mode="json"),
                        "token": token.model_dump(),
                    },
                ),
                NewEvent(
                    event_type="TOOL_CALL_SETTLED",
                    payload={"record": record.model_dump(mode="json")},
                ),
                NewEvent(
                    event_type="AGENT_SESSION_SAVED",
                    payload={"session": session.model_dump(mode="json")},
                ),
            ]

        return self._command(run_id, key, request, decide)

    def mark_tool_call_unknown(
        self,
        run_id: str,
        call_id: str,
        token: LeaseToken,
        key: str,
    ) -> Run:
        request = {
            "operation": "tool_call_unknown",
            "call_id": call_id,
            "token": token.model_dump(),
        }

        def decide(run):
            self.check_recovery_worker(run, token)
            if call_id not in run.tool_reservations:
                raise Conflict("Unknown tool reservation")
            events = []
            if call_id not in run.unknown_reservations:
                events.append(
                    NewEvent(
                        event_type="BUDGET_USAGE_UNKNOWN",
                        payload={
                            "operation": "unknown_usage",
                            "reservation_id": call_id,
                            "token": token.model_dump(),
                        },
                    )
                )
            if call_id not in run.unknown_tool_calls:
                events.append(
                    NewEvent(event_type="TOOL_CALL_UNKNOWN", payload={"call_id": call_id})
                )
            return events

        return self._command(run_id, key, request, decide)

    def save_agent_session(
        self,
        run_id: str,
        session: AgentSessionRecord,
        token: LeaseToken,
        key: str,
    ) -> Run:
        request = {
            "operation": "save_agent_session",
            "session": session.model_dump(mode="json"),
            "token": token.model_dump(),
        }

        def decide(run):
            self.check_recovery_worker(run, token)
            if run.status != RunStatus.RUNNING or run.plan is None or run.reservations:
                raise Conflict("Agent session requires a quiescent running phase")
            if (
                session.task_spec_hash != run.task.sha256
                or session.plan_version != run.plan.version
                or session.work_item_id not in {item.work_item_id for item in run.plan.items}
            ):
                raise Conflict("Agent session does not match the current task and plan")
            if session.next_iteration > run.task.budgets.max_model_calls + 1:
                raise Conflict("Agent session exceeds the TaskSpec model-call budget")
            if session.covered_event_seq != run.seq:
                raise Conflict("Agent session does not cover the latest committed event")
            if run.agent_session is not None and (
                session.next_iteration <= run.agent_session.next_iteration
            ):
                raise Conflict("Agent session iteration must advance")
            return [
                NewEvent(
                    event_type="AGENT_SESSION_SAVED",
                    payload={"session": session.model_dump(mode="json")},
                )
            ]

        return self._command(run_id, key, request, decide)

    def settle(
        self,
        run_id: str,
        reservation_id: str,
        actual: Usage,
        token: LeaseToken,
        key: str,
    ) -> Run:
        request = {
            "operation": "settle",
            "reservation_id": reservation_id,
            "actual": actual.model_dump(mode="json"),
            "token": token.model_dump(),
        }

        def decide(run):
            # Late receipts are handled by explicit recovery; a stale worker cannot write them.
            self.check_recovery_worker(run, token)
            if reservation_id not in run.reservations:
                raise Conflict("Unknown or already settled reservation")
            events = [NewEvent(event_type="BUDGET_SETTLED", payload=request)]
            occupied = run.usage.plus(actual)
            for key_, value in run.reservations.items():
                if key_ != reservation_id:
                    occupied = occupied.plus(value)
            if occupied.exceeded(run.task.budgets):
                events.append(
                    NewEvent(event_type="RUN_FAILED", payload={"reason": "usage_overrun"})
                )
            return events

        return self._command(run_id, key, request, decide)

    def mark_usage_unknown(
        self,
        run_id: str,
        reservation_id: str,
        token: LeaseToken,
        key: str,
    ) -> Run:
        request = {
            "operation": "unknown_usage",
            "reservation_id": reservation_id,
            "token": token.model_dump(),
        }

        def decide(run):
            self.check_recovery_worker(run, token)
            return [NewEvent(event_type="BUDGET_USAGE_UNKNOWN", payload=request)]

        return self._command(run_id, key, request, decide)

    def record_validation(
        self,
        run_id: str,
        passed_check_ids: tuple[str, ...],
        evidence_ref: str,
        token: LeaseToken,
        key: str,
    ) -> Run:
        request = {
            "operation": "record_validation",
            "passed_check_ids": passed_check_ids,
            "evidence_ref": evidence_ref,
            "token": token.model_dump(),
        }

        def decide(run):
            self.check_recovery_worker(run, token)
            if run.status != RunStatus.VALIDATING or run.workspace_revision is None:
                raise InvalidTransition("Validation requires a checkpointed validating run")
            return [
                NewEvent(
                    event_type="VALIDATION_RECORDED",
                    payload={
                        "task_spec_hash": run.task.sha256,
                        "workspace_revision": run.workspace_revision,
                        "passed_check_ids": passed_check_ids,
                        "evidence_ref": evidence_ref,
                    },
                )
            ]

        return self._command(run_id, key, request, decide)

    def pass_work_item(
        self,
        run_id: str,
        work_item_id: str,
        token: LeaseToken,
        key: str,
    ) -> Run:
        request = {
            "operation": "pass_work_item",
            "work_item_id": work_item_id,
            "token": token.model_dump(),
        }

        def decide(run):
            self.check_worker(run, token)
            return [
                NewEvent(
                    event_type="WORK_ITEM_PASSED",
                    payload={"work_item_id": work_item_id},
                )
            ]

        return self._command(run_id, key, request, decide)

    def advance_work_item_and_save_session(
        self,
        run_id: str,
        work_item_id: str,
        next_session: AgentSessionRecord,
        token: LeaseToken,
        key: str,
    ) -> Run:
        """Atomically pass one item, return to RUNNING, and publish the next item session."""
        request = {
            "operation": "advance_work_item_and_save_session",
            "work_item_id": work_item_id,
            "next_session": next_session.model_dump(mode="json"),
            "token": token.model_dump(),
        }

        def decide(run):
            self.check_worker(run, token)
            if run.status != RunStatus.VALIDATING or run.plan is None:
                raise Conflict("Work item advancement requires a validating planned Run")
            ready = {item.work_item_id: item for item in run.plan.ready_items(run.passed_items)}
            current = ready.get(work_item_id)
            report = run.validation
            if (
                current is None
                or report is None
                or not set(current.acceptance_ids) <= set(report["passed_check_ids"])
            ):
                raise Conflict("Current work item lacks passing validation evidence")
            prospective = set(run.passed_items) | {work_item_id}
            remaining = run.plan.ready_items(prospective)
            if not remaining or len(prospective) == len(run.plan.items):
                raise Conflict("No next dependency-ready work item remains")
            next_ids = {item.work_item_id for item in remaining}
            if (
                next_session.task_spec_hash != run.task.sha256
                or next_session.plan_version != run.plan.version
                or next_session.work_item_id not in next_ids
                or next_session.next_iteration
                <= (run.agent_session.next_iteration if run.agent_session else 0)
                or next_session.covered_event_seq != run.seq + 2
                or next_session.workspace_revision != run.workspace_revision
                or run.reservations
            ):
                raise Conflict("Next Agent session does not match the work item boundary")
            return [
                NewEvent(
                    event_type="WORK_ITEM_PASSED",
                    payload={"work_item_id": work_item_id},
                ),
                NewEvent(
                    event_type="STATE_CHANGED",
                    payload={"from": RunStatus.VALIDATING, "to": RunStatus.RUNNING},
                ),
                NewEvent(
                    event_type="AGENT_SESSION_SAVED",
                    payload={"session": next_session.model_dump(mode="json")},
                ),
            ]

        return self._command(run_id, key, request, decide)

    def pass_work_item_and_succeed(
        self,
        run_id: str,
        work_item_id: str,
        token: LeaseToken,
        key: str,
    ) -> Run:
        """Atomically pass the final item and enter SUCCEEDED."""
        request = {
            "operation": "pass_work_item_and_succeed",
            "work_item_id": work_item_id,
            "token": token.model_dump(),
        }

        def decide(run):
            self.check_worker(run, token)
            if run.plan is None or len(run.passed_items) + 1 != len(run.plan.items):
                raise Conflict("Final completion requires exactly one remaining work item")
            return [
                NewEvent(
                    event_type="WORK_ITEM_PASSED",
                    payload={"work_item_id": work_item_id},
                ),
                NewEvent(
                    event_type="STATE_CHANGED",
                    payload={"from": RunStatus.VALIDATING, "to": RunStatus.SUCCEEDED},
                ),
            ]

        return self._command(run_id, key, request, decide)

    def fail(
        self,
        run_id: str,
        reason: str,
        token: LeaseToken,
        key: str,
    ) -> Run:
        if not reason:
            raise ValueError("Failure reason is required")
        request = {
            "operation": "fail",
            "reason": reason,
            "token": token.model_dump(),
        }

        def decide(run):
            self.check_worker(run, token)
            return [NewEvent(event_type="RUN_FAILED", payload={"reason": reason})]

        return self._command(run_id, key, request, decide)

    def fail_budget_stop(
        self,
        run_id: str,
        stop: BudgetStop,
        token: LeaseToken,
        key: str,
        *,
        model_request_budget: ModelRequestBudgetEvidence | None = None,
    ) -> Run:
        """Atomically terminalize a deterministic, pre-dispatch monetary stop."""

        request = {
            "operation": "fail_budget_stop",
            "budget_stop": stop.model_dump(mode="json"),
            "token": token.model_dump(),
        }
        if model_request_budget is not None:
            request["model_request_budget"] = model_request_budget.as_dict()

        def decide(run):
            self.check_worker(run, token)
            if run.reservations:
                raise Conflict("Budget stop requires a quiescent Run")
            if model_request_budget is not None:
                expected_status = (
                    RunStatus.PLANNING
                    if model_request_budget.purpose == "planning"
                    else RunStatus.RUNNING
                )
                if run.status != expected_status:
                    raise Conflict("Model request budget evidence does not match the Run phase")
                if model_request_budget.call_id in run.model_reservations or any(
                    record.call_id == model_request_budget.call_id for record in run.model_calls
                ):
                    raise Conflict("Budget-stopped model request was already dispatched")
            payload = {
                "reason": stop.reason_code.value,
                "budget_stop": stop.model_dump(mode="json"),
            }
            if model_request_budget is not None:
                payload["model_request_budget"] = model_request_budget.as_dict()
            return [
                NewEvent(
                    event_type="RUN_FAILED",
                    payload=payload,
                )
            ]

        return self._command(run_id, key, request, decide)

    def reserve_promotion(self, run_id: str, intent: PromotionIntent, key: str) -> Run:
        request = {
            "operation": "reserve_promotion",
            "intent": intent.model_dump(mode="json"),
        }

        def decide(run):
            if run.status != RunStatus.SUCCEEDED or run.workspace_origin is None:
                raise Conflict("Promotion requires a successful Run with a bound origin")
            if run.lease_id or run.reservations:
                raise Conflict("Promotion requires a quiescent Run")
            if run.promotion_receipt is not None:
                raise Conflict("Run has already been promoted")
            if run.promotion_intent is not None:
                if run.promotion_intent != intent:
                    raise Conflict("Run already has a different pending promotion")
                return []
            if (
                intent.plan.source_path_hash != run.workspace_origin.source_path_hash
                or intent.plan.source_revision_before != run.workspace_origin.source_revision
                or intent.plan.source_manifest_ref_before
                != run.workspace_origin.source_manifest_ref
                or intent.plan.git_head_before != run.workspace_origin.git_head
                or intent.plan.candidate_revision != run.workspace_revision
                or run.last_checkpoint is None
                or intent.plan.candidate_manifest_ref != run.last_checkpoint["manifest_sha256"]
            ):
                raise Conflict("Promotion intent does not match successful Run evidence")
            return [
                NewEvent(
                    event_type="PROMOTION_RESERVED",
                    payload={"intent": intent.model_dump(mode="json")},
                )
            ]

        return self._command(run_id, key, request, decide)

    def settle_promotion(self, run_id: str, receipt: PromotionReceipt, key: str) -> Run:
        request = {
            "operation": "settle_promotion",
            "receipt": receipt.model_dump(mode="json"),
        }

        def decide(run):
            if run.promotion_intent is None or run.promotion_receipt is not None:
                raise Conflict("Promotion has no pending intent or is already settled")
            if (
                receipt.promotion_id != run.promotion_intent.promotion_id
                or receipt.plan_hash != run.promotion_intent.plan.sha256
                or receipt.source_revision_after != run.promotion_intent.plan.candidate_revision
                or receipt.git_head_after != run.promotion_intent.plan.git_head_before
            ):
                raise Conflict("Promotion receipt does not match its intent")
            return [
                NewEvent(
                    event_type="PROMOTION_SETTLED",
                    payload={"receipt": receipt.model_dump(mode="json")},
                )
            ]

        return self.store.command(run_id, key, request, decide)

    def amend(self, run_id: str, task: TaskSpec, user_source: str, key: str) -> Run:
        request = {
            "operation": "amend",
            "task": task.model_dump(mode="json"),
            "user_source": user_source,
        }

        def decide(run):
            if run.terminal or run.lease_id or run.reservations:
                raise Conflict(
                    "Amendment requires a nonterminal run with no worker or pending action"
                )
            if run.workspace_origin is not None and task.repository != run.task.repository:
                raise Conflict("A staged Run cannot amend its bound repository identity")
            deadline = datetime.fromisoformat(run.created_at) + timedelta(
                seconds=task.budgets.max_wall_time_seconds,
            )
            return [
                NewEvent(
                    event_type="TASK_SPEC_AMENDED",
                    payload={
                        **request,
                        "deadline_at": timestamp(deadline),
                    },
                )
            ]

        return self.store.command(run_id, key, request, decide)
