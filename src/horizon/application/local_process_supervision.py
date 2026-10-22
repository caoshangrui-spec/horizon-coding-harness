from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from horizon.application.recovery import RecoveryService
from horizon.application.services import HarnessService, LeaseToken
from horizon.application.supervision import (
    ReapedWorkerBoundary,
    SequentialAgentSupervisor,
    SequentialSupervisorConfig,
)
from horizon.domain.errors import Conflict, HorizonError
from horizon.domain.recovery import RecoveryReport
from horizon.domain.run import Run


@dataclass(frozen=True)
class WorkerProcessReceipt:
    process_id: int
    exit_code: int
    stdout: bytes = b""
    stderr: bytes = b""


@dataclass(frozen=True)
class LocalProcessSupervisionResult:
    run: Run
    disposition: Literal["stopped", "reconciliation_required", "slice_limit_reached"]
    worker_slices: int
    worker_handoffs: int
    slice_limit_reached: bool
    process_receipts: tuple[WorkerProcessReceipt, ...]
    pending_reservation_ids: tuple[str, ...] = ()
    recovery_report: RecoveryReport | None = None


WorkerProcessLauncher = Callable[
    [ReapedWorkerBoundary, int],
    subprocess.Popen[bytes],
]


class LocalAgentWorkerLauncher:
    """Start exactly one importable Horizon child without putting credentials in argv."""

    def __init__(
        self,
        *,
        store_path: Path,
        config_path: Path,
        provider_config_hash: str,
        dotenv_path: Path,
        artifacts_path: Path,
        workspace_path: Path,
        campaign_path: Path,
        staging_path: Path,
        image: str,
        retrieval_path: Path,
        executable: str | None = None,
        python_path: Sequence[str] | None = None,
    ):
        if not image.strip():
            raise ValueError("Agent Worker image cannot be empty")
        if len(provider_config_hash) != 64:
            raise ValueError("Agent Worker provider configuration hash must be SHA-256")
        self.store_path = store_path.resolve()
        self.config_path = config_path.resolve()
        self.provider_config_hash = provider_config_hash
        self.dotenv_path = dotenv_path.resolve()
        self.artifacts_path = artifacts_path.resolve()
        self.workspace_path = workspace_path.resolve()
        self.campaign_path = campaign_path.resolve()
        self.staging_path = staging_path.resolve()
        self.image = image
        self.retrieval_path = retrieval_path.resolve()
        self.executable = executable or getattr(sys, "_base_executable", sys.executable)
        self.python_path = tuple(python_path if python_path is not None else sys.path)

    def command(
        self,
        boundary: ReapedWorkerBoundary,
        slice_iterations: int,
    ) -> tuple[str, ...]:
        session_ref = boundary.agent_session_artifact_ref or "-"
        return (
            self.executable,
            "-m",
            "horizon.application._local_agent_worker",
            str(self.store_path),
            boundary.run_id,
            boundary.lease.lease_id,
            boundary.lease.worker_id,
            str(boundary.lease.epoch),
            str(boundary.launch_event_seq),
            session_ref,
            str(boundary.next_iteration),
            str(self.config_path),
            self.provider_config_hash,
            str(self.dotenv_path),
            str(self.artifacts_path),
            str(self.workspace_path),
            str(self.campaign_path),
            str(self.staging_path),
            self.image,
            str(self.retrieval_path),
            str(slice_iterations),
        )

    def __call__(
        self,
        boundary: ReapedWorkerBoundary,
        slice_iterations: int,
    ) -> subprocess.Popen[bytes]:
        child_env = os.environ.copy()
        import_paths = [str(item) for item in self.python_path if str(item)]
        child_env["PYTHONPATH"] = os.pathsep.join(import_paths)
        return subprocess.Popen(
            self.command(boundary, slice_iterations),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
        )


class LocalProcessAgentSupervisor:
    """Run bounded Agent slices in direct children owned and reaped by this process.

    The parent never treats an exit code as a safe boundary. After every synchronous reap it
    reloads the Run, binds the observation to the exact launch Lease, and lets the existing
    RecoveryService decide whether a new Worker may be started. A pending effect always stops
    the loop with ``reconciliation_required``.
    """

    def __init__(
        self,
        service_factory: Callable[[], HarnessService],
        process_launcher: WorkerProcessLauncher,
        recovery_factory: Callable[[HarnessService], RecoveryService],
        *,
        config: SequentialSupervisorConfig | None = None,
        worker_id_prefix: str = "local-process-supervisor",
    ):
        self.service_factory = service_factory
        self.process_launcher = process_launcher
        self.recovery_factory = recovery_factory
        self.config = config or SequentialSupervisorConfig()
        self.worker_id_prefix = worker_id_prefix

    @staticmethod
    def _unused_runner_factory(service: HarnessService):
        raise AssertionError("Process supervision must never construct an in-process runner")

    @staticmethod
    def _release_failed_launch(
        service: HarnessService,
        boundary: ReapedWorkerBoundary,
        launch_number: int,
    ) -> None:
        current = service.store.get(boundary.run_id)
        token = boundary.lease
        if (
            current.lease_id != token.lease_id
            or current.worker_id != token.worker_id
            or current.lease_epoch != token.epoch
            or current.reservations
        ):
            return
        try:
            service.release_lease(
                boundary.run_id,
                token,
                f"process-supervisor-launch-error-{launch_number}-{uuid4().hex}",
            )
        except HorizonError:
            return

    @staticmethod
    def _receipt(
        process: subprocess.Popen[bytes],
        stdout: bytes | str | None,
        stderr: bytes | str | None,
    ) -> WorkerProcessReceipt:
        if process.returncode is None:
            raise Conflict("Agent Worker was not synchronously reaped")

        def as_bytes(value: bytes | str | None) -> bytes:
            if value is None:
                return b""
            if isinstance(value, bytes):
                return value
            return value.encode("utf-8", errors="replace")

        return WorkerProcessReceipt(
            process_id=process.pid,
            exit_code=process.returncode,
            stdout=as_bytes(stdout),
            stderr=as_bytes(stderr),
        )

    def run(
        self,
        run_id: str,
        service: HarnessService,
        token: LeaseToken,
    ) -> LocalProcessSupervisionResult:
        receipts: list[WorkerProcessReceipt] = []
        worker_handoffs = 0
        handoff = SequentialAgentSupervisor(
            self.service_factory,
            self._unused_runner_factory,
            config=self.config,
            worker_id_prefix=self.worker_id_prefix,
            recovery_factory=self.recovery_factory,
        )

        for slice_number in range(1, self.config.max_worker_slices + 1):
            boundary = ReapedWorkerBoundary.capture(service.store.get(run_id), token)
            try:
                process = self.process_launcher(boundary, self.config.slice_iterations)
            except OSError:
                self._release_failed_launch(service, boundary, slice_number)
                raise
            if not isinstance(process.pid, int) or process.pid < 1:
                raise Conflict("Agent Worker launch did not return a process identity")

            stdout, stderr = process.communicate()
            receipt = self._receipt(process, stdout, stderr)
            receipts.append(receipt)
            last_slice = slice_number == self.config.max_worker_slices
            reaped = handoff.continue_after_reaped_worker(
                boundary,
                service,
                process_id=receipt.process_id,
                exit_code=receipt.exit_code,
                continue_execution=False,
                acquire_next_lease=not last_slice,
            )
            if reaped.disposition == "stopped":
                return LocalProcessSupervisionResult(
                    run=reaped.run,
                    disposition="stopped",
                    worker_slices=slice_number,
                    worker_handoffs=worker_handoffs,
                    slice_limit_reached=False,
                    process_receipts=tuple(receipts),
                    pending_reservation_ids=reaped.pending_reservation_ids,
                    recovery_report=reaped.recovery_report,
                )
            if reaped.disposition == "reconciliation_required":
                return LocalProcessSupervisionResult(
                    run=reaped.run,
                    disposition="reconciliation_required",
                    worker_slices=slice_number,
                    worker_handoffs=worker_handoffs,
                    slice_limit_reached=False,
                    process_receipts=tuple(receipts),
                    pending_reservation_ids=reaped.pending_reservation_ids,
                    recovery_report=reaped.recovery_report,
                )
            if reaped.disposition != "ready":
                raise AssertionError("Process handoff unexpectedly ran an in-process Worker")
            if last_slice:
                return LocalProcessSupervisionResult(
                    run=reaped.run,
                    disposition="slice_limit_reached",
                    worker_slices=slice_number,
                    worker_handoffs=worker_handoffs,
                    slice_limit_reached=True,
                    process_receipts=tuple(receipts),
                    recovery_report=reaped.recovery_report,
                )

            service = self.service_factory()
            durable = service.store.get(run_id)
            if (
                durable.lease_id != reaped.run.lease_id
                or durable.worker_id != reaped.run.worker_id
                or durable.lease_epoch != reaped.run.lease_epoch
            ):
                raise Conflict("New process Worker lease changed before the next launch")
            token = LeaseToken.from_run(durable)
            worker_handoffs += 1

        raise AssertionError("Process supervisor slice loop exhausted without returning")
