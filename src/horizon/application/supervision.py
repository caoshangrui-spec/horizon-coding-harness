from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol
from uuid import uuid4

from pydantic import Field, StrictInt

from horizon.application.recovery import RecoveryService
from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.common import Contract
from horizon.domain.errors import Conflict, HorizonError
from horizon.domain.recovery import RecoveryReport
from horizon.domain.run import Run
from horizon.domain.states import RunStatus


class AgentSliceRunner(Protocol):
    def run(
        self,
        run_id: str,
        token: LeaseToken,
        *,
        max_iterations_this_invocation: int | None = None,
    ) -> Run: ...


class SequentialSupervisorConfig(Contract):
    """Bounds one local supervisor invocation without widening the Run budgets."""

    slice_iterations: Annotated[int, Field(ge=1, le=50)] = 1
    max_worker_slices: Annotated[int, Field(ge=1, le=50)] = 50
    lease_ttl_seconds: Annotated[int, Field(ge=1, le=600)] = 600


@dataclass(frozen=True)
class AgentSupervisionResult:
    run: Run
    worker_slices: int
    worker_handoffs: int
    slice_limit_reached: bool


class ReapedWorkerBoundary(Contract):
    """Durable identity captured immediately before a local child Worker is launched."""

    run_id: str
    lease: LeaseToken
    launch_event_seq: Annotated[StrictInt, Field(ge=1)]
    agent_session_artifact_ref: str | None = None
    next_iteration: Annotated[StrictInt, Field(ge=0)] = 0

    @classmethod
    def capture(cls, run: Run, token: LeaseToken) -> ReapedWorkerBoundary:
        if (
            run.status != RunStatus.RUNNING
            or run.plan is None
            or run.lease_id != token.lease_id
            or run.worker_id != token.worker_id
            or run.lease_epoch != token.epoch
            or run.reservations
        ):
            raise Conflict("Worker launch boundary requires a quiescent current RUNNING lease")
        session = run.agent_session
        return cls(
            run_id=run.run_id,
            lease=token,
            launch_event_seq=run.seq,
            agent_session_artifact_ref=session.artifact_ref if session is not None else None,
            next_iteration=session.next_iteration if session is not None else 0,
        )


@dataclass(frozen=True)
class ReapedWorkerSupervisionResult:
    run: Run
    disposition: Literal["continued", "reconciliation_required", "stopped"]
    process_id: int
    exit_code: int
    pending_reservation_ids: tuple[str, ...]
    recovery_report: RecoveryReport | None = None
    supervision: AgentSupervisionResult | None = None


class SequentialAgentSupervisor:
    """Drive quiescent Agent slices through durable worker handoffs.

    The supervisor only hands off after ``CodingAgentRunner`` returns a RUNNING Run with a
    newer persisted AgentSession. Unexpected exceptions are deliberately not converted into
    a handoff: their lease and any durable intent remain available to the existing recovery
    flow. Each successful handoff releases the old lease, reopens worker-owned adapters through
    the supplied factories, and acquires a new lease epoch before the next model iteration.
    """

    def __init__(
        self,
        service_factory: Callable[[], HarnessService],
        runner_factory: Callable[[HarnessService], AgentSliceRunner],
        *,
        config: SequentialSupervisorConfig | None = None,
        worker_id_prefix: str = "local-supervisor",
        recovery_factory: Callable[[HarnessService], RecoveryService] | None = None,
    ):
        if not worker_id_prefix.strip():
            raise ValueError("Supervisor worker ID prefix cannot be empty")
        self.service_factory = service_factory
        self.runner_factory = runner_factory
        self.config = config or SequentialSupervisorConfig()
        self.worker_id_prefix = worker_id_prefix.strip()
        self.recovery_factory = recovery_factory

    @staticmethod
    def _iteration(run: Run) -> int:
        return run.agent_session.next_iteration if run.agent_session is not None else 0

    @staticmethod
    def _release_after_handled_error(
        service: HarnessService,
        run_id: str,
        token: LeaseToken,
        key: str,
    ) -> None:
        latest = service.store.get(run_id)
        if (
            latest.terminal
            or latest.lease_id != token.lease_id
            or latest.lease_epoch != token.epoch
            or set(latest.reservations) - latest.unknown_reservations
        ):
            return
        try:
            service.release_lease(run_id, token, key)
        except HorizonError:
            # Preserve the original controller error. A later recovery pass will fence a lease
            # that could not be released safely.
            return

    def run(
        self,
        run_id: str,
        service: HarnessService,
        token: LeaseToken,
    ) -> AgentSupervisionResult:
        supervision_id = uuid4().hex
        worker_slices = 0
        worker_handoffs = 0

        for slice_number in range(1, self.config.max_worker_slices + 1):
            before = service.store.get(run_id)
            if (
                before.status != RunStatus.RUNNING
                or before.plan is None
                or before.lease_id != token.lease_id
                or before.lease_epoch != token.epoch
            ):
                raise Conflict("Supervisor requires the current live RUNNING worker lease")
            before_iteration = self._iteration(before)

            try:
                runner = self.runner_factory(service)
                result = runner.run(
                    run_id,
                    token,
                    max_iterations_this_invocation=self.config.slice_iterations,
                )
            except HorizonError:
                self._release_after_handled_error(
                    service,
                    run_id,
                    token,
                    f"supervisor-error-release-{supervision_id}-{slice_number}",
                )
                raise

            worker_slices += 1
            if result.status != RunStatus.RUNNING:
                return AgentSupervisionResult(
                    run=result,
                    worker_slices=worker_slices,
                    worker_handoffs=worker_handoffs,
                    slice_limit_reached=False,
                )

            if result.agent_session is None or self._iteration(result) <= before_iteration:
                self._release_after_handled_error(
                    service,
                    run_id,
                    token,
                    f"supervisor-no-progress-release-{supervision_id}-{slice_number}",
                )
                raise Conflict("Agent worker slice returned without a newer persisted session")

            released = service.release_lease(
                run_id,
                token,
                f"supervisor-yield-{supervision_id}-{slice_number}",
            )
            if slice_number == self.config.max_worker_slices:
                return AgentSupervisionResult(
                    run=released,
                    worker_slices=worker_slices,
                    worker_handoffs=worker_handoffs,
                    slice_limit_reached=True,
                )

            service = self.service_factory()
            durable = service.store.get(run_id)
            if (
                durable.status != RunStatus.RUNNING
                or durable.lease_id is not None
                or durable.reservations
            ):
                raise Conflict("Persisted Run is not quiescent at the supervisor handoff boundary")
            leased = service.acquire_lease(
                run_id,
                f"{self.worker_id_prefix}-{supervision_id[:8]}-{slice_number + 1}",
                f"supervisor-acquire-{supervision_id}-{slice_number + 1}",
                ttl_seconds=self.config.lease_ttl_seconds,
            )
            token = LeaseToken.from_run(leased)
            worker_handoffs += 1

        raise AssertionError("Supervisor slice loop exhausted without returning")

    def continue_after_reaped_worker(
        self,
        boundary: ReapedWorkerBoundary,
        service: HarnessService,
        *,
        process_id: int,
        exit_code: int,
    ) -> ReapedWorkerSupervisionResult:
        """Continue only after a trusted parent has synchronously reaped its child Worker.

        A live process check cannot be reconstructed from durable state, so callers must invoke
        this method only after ``wait``/``communicate`` returned for ``process_id``. The method
        binds that observation to the exact launch Lease, refuses every pending reservation, and
        delegates the final safe-boundary decision to ``RecoveryService`` before fencing.
        """

        if process_id < 1:
            raise ValueError("Reaped worker process ID must be positive")
        current = service.store.get(boundary.run_id)
        token = boundary.lease
        if current.seq < boundary.launch_event_seq or current.lease_epoch != token.epoch:
            raise Conflict("Reaped worker evidence does not match the durable lease epoch")
        if (boundary.agent_session_artifact_ref is None) != (boundary.next_iteration == 0):
            raise Conflict("Reaped worker launch boundary has inconsistent Agent session evidence")
        if boundary.agent_session_artifact_ref is not None:
            session = current.agent_session
            if (
                session is None
                or session.next_iteration < boundary.next_iteration
                or (
                    session.next_iteration == boundary.next_iteration
                    and session.artifact_ref != boundary.agent_session_artifact_ref
                )
            ):
                raise Conflict("Durable Agent session regressed after the Worker launch boundary")

        if current.terminal or current.status == RunStatus.WAITING_FOR_USER:
            if current.lease_id is not None:
                raise Conflict("Stopped reaped worker Run still has an unexpected live lease")
            return ReapedWorkerSupervisionResult(
                run=current,
                disposition="stopped",
                process_id=process_id,
                exit_code=exit_code,
                pending_reservation_ids=tuple(sorted(current.reservations)),
            )

        if (
            current.lease_id != token.lease_id
            or current.worker_id != token.worker_id
            or current.lease_epoch != token.epoch
        ):
            raise Conflict("Reaped worker no longer owns the exact launch lease")

        pending = tuple(sorted(current.reservations))
        if pending:
            return ReapedWorkerSupervisionResult(
                run=current,
                disposition="reconciliation_required",
                process_id=process_id,
                exit_code=exit_code,
                pending_reservation_ids=pending,
            )
        if self.recovery_factory is None:
            raise Conflict("Reaped worker continuation requires a RecoveryService factory")

        recovery = self.recovery_factory(service).reconcile(boundary.run_id, token)
        current = service.store.get(boundary.run_id)
        if current.status != RunStatus.RUNNING or not recovery.safe_to_resume:
            return ReapedWorkerSupervisionResult(
                run=current,
                disposition="reconciliation_required",
                process_id=process_id,
                exit_code=exit_code,
                pending_reservation_ids=tuple(sorted(current.reservations)),
                recovery_report=recovery,
            )

        handoff_id = uuid4().hex
        service.release_reaped_worker_lease(
            boundary.run_id,
            token,
            f"supervisor-reaped-release-{handoff_id}",
            process_id=process_id,
            exit_code=exit_code,
            launch_event_seq=boundary.launch_event_seq,
        )
        replacement_service = self.service_factory()
        durable = replacement_service.store.get(boundary.run_id)
        if (
            durable.status != RunStatus.RUNNING
            or durable.lease_id is not None
            or durable.reservations
        ):
            raise Conflict("Persisted Run is not quiescent after reaped worker fencing")
        leased = replacement_service.acquire_lease(
            boundary.run_id,
            f"{self.worker_id_prefix}-reaped-{handoff_id[:8]}",
            f"supervisor-reaped-acquire-{handoff_id}",
            ttl_seconds=self.config.lease_ttl_seconds,
        )
        supervision = self.run(
            boundary.run_id,
            replacement_service,
            LeaseToken.from_run(leased),
        )
        return ReapedWorkerSupervisionResult(
            run=supervision.run,
            disposition="continued",
            process_id=process_id,
            exit_code=exit_code,
            pending_reservation_ids=(),
            recovery_report=recovery,
            supervision=supervision,
        )
