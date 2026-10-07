from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Protocol
from uuid import uuid4

from pydantic import Field

from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.common import Contract
from horizon.domain.errors import Conflict, HorizonError
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
    ):
        if not worker_id_prefix.strip():
            raise ValueError("Supervisor worker ID prefix cannot be empty")
        self.service_factory = service_factory
        self.runner_factory = runner_factory
        self.config = config or SequentialSupervisorConfig()
        self.worker_id_prefix = worker_id_prefix.strip()

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
