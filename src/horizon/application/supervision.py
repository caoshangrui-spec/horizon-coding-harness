from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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


class LocalWorkerProcess(Protocol):
    """Minimal trusted-parent handle needed to stop and synchronously reap one child."""

    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


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
    lease_transition: Literal["none", "live_release", "expired_takeover"] = "none"
    recovery_report: RecoveryReport | None = None
    supervision: AgentSupervisionResult | None = None


@dataclass(frozen=True)
class CancelledWorkerSupervisionResult:
    run: Run
    disposition: Literal["cancelled", "reconciliation_required"]
    process_id: int
    exit_code: int
    stop_method: Literal["already_exited", "terminate", "kill"]
    pending_reservation_ids: tuple[str, ...]


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

    @staticmethod
    def _validate_launch_boundary(current: Run, boundary: ReapedWorkerBoundary) -> None:
        token = boundary.lease
        if current.seq < boundary.launch_event_seq or current.lease_epoch != token.epoch:
            raise Conflict("Worker evidence does not match the durable lease epoch")
        if (boundary.agent_session_artifact_ref is None) != (boundary.next_iteration == 0):
            raise Conflict("Worker launch boundary has inconsistent Agent session evidence")
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

    def cancel_and_reap_worker(
        self,
        boundary: ReapedWorkerBoundary,
        service: HarnessService,
        process: LocalWorkerProcess,
        *,
        key: str,
        terminate_timeout_seconds: float = 5.0,
    ) -> CancelledWorkerSupervisionResult:
        """Fence cancellation, stop only the bound child, then persist synchronous reap evidence.

        No receipt is written unless ``wait`` returned. If a reserved effect survives the child,
        its exact intent remains untouched and the stopped lease is released for the existing
        recovery path; otherwise the normal cancellation gate closes the Run.
        """

        if (
            isinstance(terminate_timeout_seconds, bool)
            or not isinstance(terminate_timeout_seconds, int | float)
            or not 0 < terminate_timeout_seconds <= 30
        ):
            raise ValueError("Worker termination timeout must be in (0, 30] seconds")
        if not isinstance(process.pid, int) or isinstance(process.pid, bool) or process.pid < 1:
            raise ValueError("Worker process must expose a positive process ID")

        current = service.store.get(boundary.run_id)
        self._validate_launch_boundary(current, boundary)
        token = boundary.lease
        if (
            current.status != RunStatus.RUNNING
            or current.plan is None
            or current.lease_id != token.lease_id
            or current.worker_id != token.worker_id
            or current.lease_epoch != token.epoch
        ):
            raise Conflict("Cancellation requires the exact live Worker launch lease")

        service.request_cancel_worker_stop(
            boundary.run_id,
            token,
            f"{key}-request",
            process_id=process.pid,
            launch_event_seq=boundary.launch_event_seq,
        )

        stop_method: Literal["already_exited", "terminate", "kill"]
        if process.poll() is not None:
            stop_method = "already_exited"
        else:
            stop_method = "terminate"
            try:
                process.terminate()
            except OSError:
                if process.poll() is None:
                    raise
                stop_method = "already_exited"

        try:
            exit_code = process.wait(timeout=terminate_timeout_seconds)
        except subprocess.TimeoutExpired:
            stop_method = "kill"
            try:
                process.kill()
            except OSError:
                if process.poll() is None:
                    raise
                stop_method = "terminate"
            try:
                exit_code = process.wait(timeout=terminate_timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                raise Conflict("Cancelled Worker did not exit after forced termination") from exc

        result = service.record_cancel_worker_stopped(
            boundary.run_id,
            token,
            f"{key}-stopped",
            process_id=process.pid,
            launch_event_seq=boundary.launch_event_seq,
            exit_code=exit_code,
            stop_method=stop_method,
        )
        pending = tuple(sorted(result.reservations))
        if result.status == RunStatus.CANCELLED:
            disposition: Literal["cancelled", "reconciliation_required"] = "cancelled"
        elif result.status == RunStatus.RUNNING and result.cancel_requested and pending:
            disposition = "reconciliation_required"
        else:
            raise Conflict("Stopped Worker cancellation ended outside a recovery-safe boundary")
        return CancelledWorkerSupervisionResult(
            run=result,
            disposition=disposition,
            process_id=process.pid,
            exit_code=exit_code,
            stop_method=stop_method,
            pending_reservation_ids=pending,
        )

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
        self._validate_launch_boundary(current, boundary)

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
        if current.lease_expires_at is None:
            raise Conflict("Reaped worker has no durable lease expiry")
        lease_expired = service.store.clock() >= datetime.fromisoformat(current.lease_expires_at)
        if pending and not lease_expired:
            return ReapedWorkerSupervisionResult(
                run=current,
                disposition="reconciliation_required",
                process_id=process_id,
                exit_code=exit_code,
                pending_reservation_ids=pending,
            )
        recovery_factory = self.recovery_factory
        if not pending and recovery_factory is None:
            raise Conflict("Reaped worker continuation requires a RecoveryService factory")

        handoff_id = uuid4().hex
        active_service = service
        active_token = token
        lease_transition: Literal["none", "live_release", "expired_takeover"] = "none"
        if lease_expired:
            # Reaping proves that the old process can be fenced; it says nothing about an
            # already-dispatched effect. Preserve every pending intent for explicit recovery.
            active_service = self.service_factory()
            current = active_service.takeover_reaped_worker_lease(
                boundary.run_id,
                token,
                f"{self.worker_id_prefix}-reaped-expired-{handoff_id[:8]}",
                f"supervisor-reaped-expired-takeover-{handoff_id}",
                process_id=process_id,
                exit_code=exit_code,
                launch_event_seq=boundary.launch_event_seq,
                ttl_seconds=self.config.lease_ttl_seconds,
            )
            active_token = LeaseToken.from_run(current)
            lease_transition = "expired_takeover"
            if pending:
                if tuple(sorted(current.reservations)) != pending:
                    raise Conflict("Expired reaped-worker takeover changed pending operations")
                return ReapedWorkerSupervisionResult(
                    run=current,
                    disposition="reconciliation_required",
                    process_id=process_id,
                    exit_code=exit_code,
                    pending_reservation_ids=pending,
                    lease_transition=lease_transition,
                )

        if recovery_factory is None:
            raise Conflict("Reaped worker continuation requires a RecoveryService factory")
        recovery = recovery_factory(active_service).reconcile(
            boundary.run_id,
            active_token,
        )
        current = active_service.store.get(boundary.run_id)
        if current.status != RunStatus.RUNNING or not recovery.safe_to_resume:
            return ReapedWorkerSupervisionResult(
                run=current,
                disposition="reconciliation_required",
                process_id=process_id,
                exit_code=exit_code,
                pending_reservation_ids=tuple(sorted(current.reservations)),
                lease_transition=lease_transition,
                recovery_report=recovery,
            )

        if not lease_expired:
            service.release_reaped_worker_lease(
                boundary.run_id,
                token,
                f"supervisor-reaped-release-{handoff_id}",
                process_id=process_id,
                exit_code=exit_code,
                launch_event_seq=boundary.launch_event_seq,
            )
            active_service = self.service_factory()
            durable = active_service.store.get(boundary.run_id)
            if (
                durable.status != RunStatus.RUNNING
                or durable.lease_id is not None
                or durable.reservations
            ):
                raise Conflict("Persisted Run is not quiescent after reaped worker fencing")
            leased = active_service.acquire_lease(
                boundary.run_id,
                f"{self.worker_id_prefix}-reaped-{handoff_id[:8]}",
                f"supervisor-reaped-acquire-{handoff_id}",
                ttl_seconds=self.config.lease_ttl_seconds,
            )
            active_token = LeaseToken.from_run(leased)
            lease_transition = "live_release"
        supervision = self.run(
            boundary.run_id,
            active_service,
            active_token,
        )
        return ReapedWorkerSupervisionResult(
            run=supervision.run,
            disposition="continued",
            process_id=process_id,
            exit_code=exit_code,
            pending_reservation_ids=(),
            lease_transition=lease_transition,
            recovery_report=recovery,
            supervision=supervision,
        )
