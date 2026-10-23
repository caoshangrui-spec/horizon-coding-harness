from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.application.local_process_supervision import (
    LocalProcessAgentSupervisor,
    WorkerProcessLauncher,
)
from horizon.application.recovery import RecoveryService
from horizon.application.services import HarnessService, LeaseToken
from horizon.application.supervision import ReapedWorkerBoundary, SequentialSupervisorConfig
from horizon.application.terminal_evidence import (
    TerminalEvidenceResult,
    export_terminal_evidence_pack,
    verify_terminal_evidence_pack,
)
from horizon.domain.common import canonical_json, digest
from horizon.domain.errors import Conflict
from horizon.domain.events import Event
from horizon.domain.plan import Plan, WorkItem
from horizon.domain.process_timeout_demo import (
    ProcessTimeoutDemoEvidence,
    ProcessTimeoutEvidenceFile,
    ProcessTimeoutEvidencePack,
    ProcessTimeoutReadyObservation,
    ProcessTimeoutSupervisionObservation,
)
from horizon.domain.run import projection_hash
from horizon.domain.states import RunStatus
from horizon.domain.task import AcceptanceCheck, BudgetSpec, Constraints, Repository, TaskSpec


@dataclass(frozen=True)
class ProcessTimeoutDemoResult:
    output_dir: Path
    evidence_pack_path: Path
    ready_observation_path: Path
    supervision_observation_path: Path
    terminal_evidence_pack_path: Path
    trace_path: Path
    final_state_path: Path
    summary_path: Path
    evidence_pack: ProcessTimeoutEvidencePack


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)


def _file_record(root: Path, role: str, path: Path) -> ProcessTimeoutEvidenceFile:
    content = path.read_bytes()
    return ProcessTimeoutEvidenceFile(
        role=role,
        path=path.relative_to(root).as_posix(),
        sha256=_sha256(content),
        bytes=len(content),
    )


def _task_and_plan(demo_id: str) -> tuple[TaskSpec, Plan]:
    check = AcceptanceCheck(
        id="never-executed",
        command="process-timeout-demo:never-executed",
        timeout_seconds=30,
        required=True,
    )
    task = TaskSpec(
        task_id=f"process-timeout-{demo_id[-12:]}",
        title="Cancel one timed-out direct child Worker safely",
        objective=(
            "Start one local direct child, trigger a bounded parent-side slice timeout, "
            "persist the exact cancellation fence, and synchronously reap the child."
        ),
        repository=Repository(
            source="local",
            path=".",
            base_commit=digest({"demo_id": demo_id, "fixture": "no-repository-code"}),
        ),
        constraints=Constraints(
            allowed_paths=("**",),
            denied_paths=(".git/**", ".env", ".env.*", "secrets/**"),
            allowed_tools=("read_file",),
            network="deny",
            requirements=(
                "Do not call a model, tool, network, Docker, or repository command.",
                "Stop only the exact direct child bound to the active Lease.",
            ),
        ),
        acceptance=(check,),
        budgets=BudgetSpec(
            max_steps=1,
            max_model_calls=1,
            max_tool_calls=1,
            max_wall_time_seconds=120,
            max_cost_usd="0.01",
            max_input_tokens=1,
            max_output_tokens=1,
            max_repair_cycles=0,
        ),
        task_kind="tests",
        execution_mode="read_only",
        authority_scope="read_only",
        model_policy_id="offline-process-timeout-demo",
        memory_scope=f"process-timeout-{demo_id[-12:]}",
    )
    plan = Plan(
        items=(
            WorkItem(
                work_item_id="wait-for-timeout",
                title="Wait at the direct-child launch boundary",
                objective="Remain idle until the parent-side slice timeout cancels this Worker.",
                expected_artifacts=("replayable cancellation evidence",),
                acceptance_ids=(check.id,),
                allowed_tools=("read_file",),
            ),
        )
    )
    plan.check_task(task)
    return task, plan


class _ReadyWorkerLauncher:
    """Launch the silent demo child and return only after its durable identity handshake."""

    def __init__(
        self,
        store_path: Path,
        ready_path: Path,
        *,
        executable: str | None = None,
        python_path: Sequence[str] | None = None,
        ready_timeout_seconds: float = 10.0,
    ):
        self.store_path = store_path.resolve()
        self.ready_path = ready_path.resolve()
        self.executable = executable or getattr(sys, "_base_executable", sys.executable)
        self.python_path = tuple(python_path if python_path is not None else sys.path)
        self.ready_timeout_seconds = ready_timeout_seconds

    @staticmethod
    def _reap_failed_launch(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        return process.communicate()

    def __call__(
        self,
        boundary: ReapedWorkerBoundary,
        slice_iterations: int,
    ) -> subprocess.Popen[bytes]:
        if slice_iterations != 1:
            raise ValueError("Process-timeout demo requires exactly one idle Worker slice")
        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = os.pathsep.join(
            str(item) for item in self.python_path if str(item)
        )
        process = subprocess.Popen(
            (
                self.executable,
                "-m",
                "horizon.application._process_timeout_demo_worker",
                str(self.store_path),
                boundary.run_id,
                boundary.lease.lease_id,
                boundary.lease.worker_id,
                str(boundary.lease.epoch),
                str(boundary.launch_event_seq),
                str(self.ready_path),
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
        )
        deadline = time.monotonic() + self.ready_timeout_seconds
        try:
            while time.monotonic() < deadline:
                if self.ready_path.is_file():
                    ready = ProcessTimeoutReadyObservation.model_validate_json(
                        self.ready_path.read_text(encoding="utf-8")
                    )
                    if (
                        ready.run_id != boundary.run_id
                        or ready.lease_id != boundary.lease.lease_id
                        or ready.worker_id != boundary.lease.worker_id
                        or ready.epoch != boundary.lease.epoch
                        or ready.launch_event_seq != boundary.launch_event_seq
                        or ready.process_id != process.pid
                    ):
                        raise Conflict(
                            "Demo Worker ready marker does not match its launch boundary"
                        )
                    return process
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
                    raise Conflict(
                        "Demo Worker exited before its ready marker"
                        + (f": {detail}" if detail else "")
                    )
                time.sleep(0.01)
        except BaseException:
            self._reap_failed_launch(process)
            raise
        self._reap_failed_launch(process)
        raise Conflict("Demo Worker did not publish its ready marker before the handshake timeout")


def _one_event(events: tuple[Event, ...], event_type: str) -> Event:
    matches = tuple(event for event in events if event.event_type == event_type)
    if len(matches) != 1:
        raise ValueError(f"Process-timeout Trace requires exactly one {event_type} event")
    return matches[0]


def _derive_evidence(
    ready: ProcessTimeoutReadyObservation,
    observation: ProcessTimeoutSupervisionObservation,
    terminal_pack_path: Path,
) -> ProcessTimeoutDemoEvidence:
    terminal_pack = verify_terminal_evidence_pack(terminal_pack_path)
    terminal_root = terminal_pack_path.parent
    trace_record = next(item for item in terminal_pack.files if item.role == "trace")
    trace_path = (terminal_root / trace_record.path).resolve(strict=True)
    events = tuple(
        Event.model_validate_json(line)
        for line in trace_path.read_text(encoding="utf-8").splitlines()
        if line
    )
    replayed = SQLiteEventStore.replay_jsonl(trace_path.read_text(encoding="utf-8"))
    if replayed.status != RunStatus.CANCELLED:
        raise ValueError("Process-timeout Trace must end in CANCELLED")
    if observation.run_id != replayed.run_id or ready.run_id != replayed.run_id:
        raise ValueError("Process observations do not identify the terminal Trace Run")
    if ready.launch_event_seq > len(events):
        raise ValueError("Child ready marker points beyond the terminal Trace")
    launch_event = events[ready.launch_event_seq - 1]
    if (
        launch_event.event_hash != ready.launch_event_hash
        or launch_event.event_type != "STATE_CHANGED"
        or launch_event.payload != {"from": "READY", "to": "RUNNING"}
    ):
        raise ValueError("Child ready marker does not bind the durable RUNNING launch event")

    fence = _one_event(events, "CANCEL_WORKER_STOP_PENDING")
    stopped = _one_event(events, "CANCEL_WORKER_STOPPED")
    released = _one_event(events, "LEASE_RELEASED")
    cancelled = _one_event(events, "CANCEL_REQUESTED")
    target = {
        "lease_id": ready.lease_id,
        "worker_id": ready.worker_id,
        "epoch": ready.epoch,
        "process_id": ready.process_id,
        "launch_event_seq": ready.launch_event_seq,
        "source": "local_control",
    }
    if any(fence.payload.get(key) != value for key, value in target.items()):
        raise ValueError("Cancellation fence does not match the ready direct child")
    if any(stopped.payload.get(key) != value for key, value in target.items()):
        raise ValueError("Worker stop receipt does not match the ready direct child")
    if (
        observation.process_id != ready.process_id
        or stopped.payload.get("exit_code") != observation.exit_code
        or stopped.payload.get("stop_method") != observation.stop_method
        or stopped.payload.get("lease_release") != "parent"
        or released.payload.get("release_reason") != "cancelled_reaped_worker"
        or released.payload.get("process_id") != ready.process_id
        or released.payload.get("exit_code") != observation.exit_code
        or released.payload.get("stop_method") != observation.stop_method
        or released.payload.get("launch_event_seq") != ready.launch_event_seq
        or cancelled.payload != {"source": "local_control", "recovery_safe": True}
    ):
        raise ValueError("Supervisor observation does not match the cancellation Trace")
    if any(event.event_type.startswith(("MODEL_", "TOOL_")) for event in events):
        raise ValueError("Process-timeout demo Trace unexpectedly contains a model or tool event")
    if (
        replayed.model_calls
        or replayed.tool_calls
        or replayed.reservations
        or replayed.cancel_worker_stop is not None
        or len(replayed.cancel_worker_stop_receipts) != 1
        or terminal_pack.evidence.unknown_effects_present
        or terminal_pack.evidence.open_effects_present
    ):
        raise ValueError("Process-timeout demo did not end at a clean cancellation boundary")

    return ProcessTimeoutDemoEvidence(
        run_id=replayed.run_id,
        task_id=replayed.task.task_id,
        lease_id=ready.lease_id,
        worker_id=ready.worker_id,
        lease_epoch=ready.epoch,
        launch_event_seq=ready.launch_event_seq,
        launch_event_hash=ready.launch_event_hash,
        process_id=ready.process_id,
        exit_code=observation.exit_code,
        timeout_seconds=observation.timeout_seconds,
        stop_method=observation.stop_method,
        cancellation_fence_event_seq=fence.seq,
        worker_stopped_event_seq=stopped.seq,
        lease_released_event_seq=released.seq,
        terminal_cancellation_event_seq=cancelled.seq,
        event_count=replayed.seq,
        event_hash=replayed.event_hash,
        projection_hash=projection_hash(replayed),
    )


def _summary(evidence: ProcessTimeoutDemoEvidence) -> str:
    boundaries = "\n".join(f"- `{item}`" for item in evidence.boundaries)
    return (
        "# Horizon Direct-Child Timeout Evidence\n\n"
        f"- Run: `{evidence.run_id}`\n"
        f"- Terminal status: `{evidence.terminal_status}`\n"
        f"- Direct child PID: `{evidence.process_id}`\n"
        f"- Worker lease: `{evidence.lease_id}` / epoch `{evidence.lease_epoch}`\n"
        f"- Parent-side slice timeout: `{evidence.timeout_seconds}` seconds\n"
        f"- Stop result: `{evidence.stop_method}` / exit `{evidence.exit_code}`\n"
        f"- Durable ordering: launch `{evidence.launch_event_seq}` < fence "
        f"`{evidence.cancellation_fence_event_seq}` < stopped "
        f"`{evidence.worker_stopped_event_seq}` < lease released "
        f"`{evidence.lease_released_event_seq}` < terminal cancellation "
        f"`{evidence.terminal_cancellation_event_seq}`\n"
        "- Model/tool/network/Docker/repository execution: `false`\n"
        "- External cost: `CNY 0`\n\n"
        "The verifier recursively checks the terminal EvidencePack, replays its append-only "
        "Trace without the control database, and binds the child-ready and parent-supervision "
        "observations to the exact PID, Lease, epoch, launch event, stop receipt, and terminal "
        "projection.\n\n"
        "## Claim boundaries\n\n"
        f"{boundaries}\n"
    )


def verify_process_timeout_evidence_pack(path: Path) -> ProcessTimeoutEvidencePack:
    """Verify the root files and recursively replay the nested terminal EvidencePack."""

    pack_path = path.resolve(strict=True)
    root = pack_path.parent
    pack = ProcessTimeoutEvidencePack.model_validate_json(pack_path.read_text(encoding="utf-8"))
    contents: dict[str, bytes] = {}
    paths: dict[str, Path] = {}
    for item in pack.files:
        candidate = (root / item.path).resolve(strict=True)
        if not candidate.is_relative_to(root):
            raise ValueError("Process-timeout evidence file escapes its pack directory")
        content = candidate.read_bytes()
        if len(content) != item.bytes or _sha256(content) != item.sha256:
            raise ValueError(
                f"Process-timeout evidence file failed integrity verification: {item.path}"
            )
        contents[item.role] = content
        paths[item.role] = candidate

    ready = ProcessTimeoutReadyObservation.model_validate_json(contents["ready_observation"])
    observation = ProcessTimeoutSupervisionObservation.model_validate_json(
        contents["supervision_observation"]
    )
    expected = _derive_evidence(ready, observation, paths["terminal_pack"])
    if pack.evidence != expected:
        raise ValueError("Process-timeout EvidencePack metadata does not match its Trace")
    if contents["summary"] != _summary(expected).encode("utf-8"):
        raise ValueError("Process-timeout summary does not match its replayed Trace")
    return pack


class ProcessTimeoutDemoRunner:
    """Exercise one real direct child timeout and export a DB-independent EvidencePack."""

    def run(
        self,
        output_dir: Path,
        *,
        worker_timeout_seconds: float = 0.2,
    ) -> ProcessTimeoutDemoResult:
        if (
            isinstance(worker_timeout_seconds, bool)
            or not isinstance(worker_timeout_seconds, int | float)
            or not 0 < worker_timeout_seconds <= 5
        ):
            raise ValueError("Demo Worker timeout must be in (0, 5] seconds")
        timeout_seconds = float(worker_timeout_seconds)
        destination = output_dir.resolve()
        if destination.exists():
            raise FileExistsError(f"Evidence directory already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)

        demo_id = f"timeout-{uuid4().hex}"
        with tempfile.TemporaryDirectory(
            prefix=f".{destination.name}-runtime-",
            dir=destination.parent,
        ) as raw_runtime:
            runtime = Path(raw_runtime)
            store_path = runtime / "control.sqlite3"
            ready_runtime_path = runtime / "child-ready.json"
            store = SQLiteEventStore(store_path)
            service = HarnessService(store)
            task, plan = _task_and_plan(demo_id)
            run = store.create(task, f"{demo_id}-create")
            run = service.set_plan(run.run_id, plan, f"{demo_id}-plan")
            run = service.acquire_lease(
                run.run_id,
                "process-timeout-demo-worker",
                f"{demo_id}-lease",
                ttl_seconds=120,
            )
            token = LeaseToken.from_run(run)
            run = service.transition(run.run_id, RunStatus.RUNNING, token, f"{demo_id}-start")
            token = LeaseToken.from_run(run)

            ledger = CampaignBudgetLedger(runtime / "campaign.sqlite3")
            launcher: WorkerProcessLauncher = _ReadyWorkerLauncher(
                store_path,
                ready_runtime_path,
            )
            supervisor = LocalProcessAgentSupervisor(
                service_factory=lambda: HarnessService(SQLiteEventStore(store_path)),
                process_launcher=launcher,
                recovery_factory=lambda active_service: RecoveryService(active_service, ledger),
                config=SequentialSupervisorConfig(
                    slice_iterations=1,
                    max_worker_slices=1,
                    lease_ttl_seconds=120,
                ),
                worker_id_prefix="process-timeout-demo",
            )
            supervised = supervisor.run(
                run.run_id,
                service,
                token,
                worker_timeout_seconds=timeout_seconds,
                terminate_timeout_seconds=2.0,
            )
            if (
                supervised.disposition != "cancelled"
                or supervised.run.status != RunStatus.CANCELLED
                or supervised.worker_slices != 1
                or supervised.worker_handoffs != 0
                or len(supervised.process_receipts) != 1
                or supervised.interruption is None
                or supervised.interruption.reason != "worker_timeout"
            ):
                raise ValueError("Process-timeout demo did not reach its expected cancellation")
            receipt = supervised.process_receipts[0]
            if receipt.stdout or receipt.stderr:
                raise ValueError("Process-timeout demo Worker must remain silent")
            ready_content = ready_runtime_path.read_bytes()
            ready = ProcessTimeoutReadyObservation.model_validate_json(ready_content)
            observation = ProcessTimeoutSupervisionObservation(
                run_id=run.run_id,
                process_id=receipt.process_id,
                exit_code=receipt.exit_code,
                timeout_seconds=timeout_seconds,
                stop_method=supervised.interruption.stop_method,
            )

            destination.mkdir(parents=False, exist_ok=False)
            ready_path = destination / "child-ready.json"
            observation_path = destination / "supervision-observation.json"
            summary_path = destination / "SUMMARY.md"
            evidence_pack_path = destination / "evidence-pack.json"
            _write_new(ready_path, ready_content)
            _write_new(
                observation_path,
                (canonical_json(observation) + "\n").encode("utf-8"),
            )
            terminal: TerminalEvidenceResult = export_terminal_evidence_pack(
                store,
                run.run_id,
                destination / "terminal",
            )
            evidence = _derive_evidence(ready, observation, terminal.evidence_pack_path)
            _write_new(summary_path, _summary(evidence).encode("utf-8"))
            pack = ProcessTimeoutEvidencePack(
                evidence=evidence,
                files=(
                    _file_record(destination, "ready_observation", ready_path),
                    _file_record(destination, "supervision_observation", observation_path),
                    _file_record(
                        destination,
                        "terminal_pack",
                        terminal.evidence_pack_path,
                    ),
                    _file_record(destination, "summary", summary_path),
                ),
            )
            _write_new(evidence_pack_path, (canonical_json(pack) + "\n").encode("utf-8"))
            verified = verify_process_timeout_evidence_pack(evidence_pack_path)

        return ProcessTimeoutDemoResult(
            output_dir=destination,
            evidence_pack_path=evidence_pack_path,
            ready_observation_path=ready_path,
            supervision_observation_path=observation_path,
            terminal_evidence_pack_path=terminal.evidence_pack_path,
            trace_path=terminal.trace_path,
            final_state_path=terminal.final_state_path,
            summary_path=summary_path,
            evidence_pack=verified,
        )
