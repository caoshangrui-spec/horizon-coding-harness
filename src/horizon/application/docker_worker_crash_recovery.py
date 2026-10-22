from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import yaml

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.sandbox.docker import DockerSandbox
from horizon.adapters.workspace.snapshot import FileSnapshot, SnapshotManager
from horizon.application._docker_agent_worker import EXIT_AFTER_CREATE
from horizon.application.agent_loop import _initial_messages
from horizon.application.docker_created_recovery import (
    _CHECK_COMMAND,
    _CHECK_ID,
    _MARKER_PATH,
    _provider_config,
)
from horizon.application.recovery import RecoveryService
from horizon.application.services import HarnessService, LeaseToken
from horizon.application.supervision import (
    ReapedWorkerBoundary,
    SequentialAgentSupervisor,
    SequentialSupervisorConfig,
)
from horizon.application.terminal_evidence import (
    export_terminal_evidence_pack,
    verify_terminal_evidence_pack,
)
from horizon.application.tool_recovery import ToolRecoveryService
from horizon.domain.agent import AgentSession, AgentSessionRecord
from horizon.domain.common import canonical_json, digest
from horizon.domain.docker_created_recovery import (
    DockerWorkerCrashObservation,
    DockerWorkerCrashResumeEvidence,
    DockerWorkerCrashResumeFile,
    DockerWorkerCrashResumePack,
    DockerWorkerLaunchObservation,
    DockerWorkerRecoveryObservation,
)
from horizon.domain.events import Event
from horizon.domain.model import ModelPolicyBinding
from horizon.domain.plan import Plan, WorkItem
from horizon.domain.run import Run, projection_hash
from horizon.domain.states import RunStatus
from horizon.domain.task import AcceptanceCheck, BudgetSpec, Constraints, Repository, TaskSpec
from horizon.tools.gateway import WorkspaceToolGateway


@dataclass(frozen=True)
class DockerWorkerCrashResumeResult:
    output_dir: Path
    terminal_evidence_pack_path: Path
    evidence_pack_path: Path
    summary_path: Path
    evidence_pack: DockerWorkerCrashResumePack


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_fsynced_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _file_record(root: Path, role: str, path: Path) -> DockerWorkerCrashResumeFile:
    content = path.read_bytes()
    return DockerWorkerCrashResumeFile(
        role=role,
        path=path.relative_to(root).as_posix(),
        sha256=_sha256(content),
        bytes=len(content),
    )


def _role_path(pack_path: Path, files: tuple, role: str) -> Path:
    record = next((item for item in files if item.role == role), None)
    if record is None:
        raise ValueError(f"EvidencePack is missing role: {role}")
    candidate = (pack_path.parent / record.path).resolve(strict=True)
    if not candidate.is_relative_to(pack_path.parent.resolve()):
        raise ValueError("Nested EvidencePack file escapes its directory")
    return candidate


def _task(workspace: Path, revision: str, demo_id: str) -> tuple[TaskSpec, Plan]:
    task = TaskSpec(
        task_id=f"docker-worker-crash-{demo_id[-12:]}",
        title="Recover a Lease-owning Agent Worker at Docker create-to-start",
        objective=(
            "Reap the crashed local Agent Worker, preserve its uncertain check intent, "
            "reconcile the exact created attempt, and continue from a newer Worker epoch."
        ),
        repository=Repository(source="local", path=str(workspace), base_commit=revision),
        constraints=Constraints(
            allowed_paths=("**",),
            denied_paths=(".git/**", ".env", ".env.*", "secrets/**"),
            allowed_tools=("run_check",),
            network="deny",
            requirements=("Never redispatch the uncertain Docker attempt.",),
        ),
        acceptance=(
            AcceptanceCheck(
                id=_CHECK_ID,
                command=_CHECK_COMMAND,
                timeout_seconds=30,
                required=True,
            ),
        ),
        budgets=BudgetSpec(
            max_steps=4,
            max_model_calls=2,
            max_tool_calls=3,
            max_wall_time_seconds=300,
            max_cost_usd="1.00",
            max_input_tokens=20_000,
            max_output_tokens=256,
            max_repair_cycles=0,
        ),
        task_kind="tests",
        execution_mode="workspace_write",
        authority_scope="workspace_write",
        model_policy_id="offline-docker-created-recovery",
        memory_scope=f"docker-worker-crash-{demo_id[-12:]}",
    )
    plan = Plan(
        items=(
            WorkItem(
                work_item_id="recover-reaped-worker-check",
                title="Recover the reaped Worker check",
                objective="Discard the exact inactive attempt and run fresh validation.",
                expected_artifacts=("reaped Worker receipt", "terminal replayable Trace"),
                acceptance_ids=(_CHECK_ID,),
                allowed_tools=("run_check",),
            ),
        )
    )
    plan.check_task(task)
    return task, plan


def _launch_worker(
    *,
    mode: str,
    store_path: Path,
    run: Run,
    token: LeaseToken,
    boundary: ReapedWorkerBoundary,
    artifacts_root: Path,
    workspace: Path,
    campaign_path: Path,
    staging_root: Path,
    image: str,
    image_id: str,
    config_path: Path,
    launch_path: Path,
    gate_path: Path,
    crash_path: Path,
) -> tuple[subprocess.Popen[bytes], DockerWorkerLaunchObservation, bytes, bytes]:
    if run.agent_session is None or boundary.agent_session_artifact_ref is None:
        raise ValueError("Agent Worker launch requires a persisted Agent session")
    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONPATH"] = os.pathsep.join(str(item) for item in sys.path if item)
    process = subprocess.Popen(
        [
            getattr(sys, "_base_executable", sys.executable),
            "-m",
            "horizon.application._docker_agent_worker",
            mode,
            str(store_path),
            run.run_id,
            token.lease_id,
            token.worker_id,
            str(token.epoch),
            str(artifacts_root),
            str(workspace),
            str(campaign_path),
            str(staging_root),
            image,
            str(config_path),
            str(launch_path),
            str(gate_path),
            str(crash_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env,
    )
    observation = DockerWorkerLaunchObservation(
        run_id=run.run_id,
        process_id=process.pid,
        worker_id=token.worker_id,
        lease_id=token.lease_id,
        lease_epoch=token.epoch,
        launch_event_seq=boundary.launch_event_seq,
        agent_session_artifact_ref=boundary.agent_session_artifact_ref,
        next_iteration=boundary.next_iteration,
        image_id=image_id,
    )
    launch_content = (canonical_json(observation) + "\n").encode("utf-8")
    _write_fsynced_new(launch_path, launch_content)
    _write_fsynced_new(gate_path, (_sha256(launch_content) + "\n").encode("ascii"))
    try:
        stdout, stderr = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=10)
        raise ValueError(f"{mode.title()} Agent Worker did not exit within 90 seconds") from None
    return process, observation, stdout, stderr


def _trace_prefix_run(trace_text: str, event_seq: int) -> Run:
    lines = [line for line in trace_text.splitlines() if line]
    if not 1 <= event_seq <= len(lines):
        raise ValueError("Worker launch event sequence is outside the terminal Trace")
    return SQLiteEventStore.replay_jsonl("\n".join(lines[:event_seq]) + "\n")


def _derive_evidence(
    terminal_pack,
    terminal_run: Run,
    events: tuple[Event, ...],
    crash_launch: DockerWorkerLaunchObservation,
    crash_launch_content: bytes,
    crash: DockerWorkerCrashObservation,
    recovery: DockerWorkerRecoveryObservation,
    resume_launch: DockerWorkerLaunchObservation,
    final_manifest_content: bytes,
    resumed_check_artifact: bytes,
    marker_content: bytes,
    trace_text: str,
) -> DockerWorkerCrashResumeEvidence:
    if terminal_run.status != RunStatus.SUCCEEDED or terminal_run.lease_id is not None:
        raise ValueError("Docker Worker recovery evidence requires a lease-free successful Run")
    boundary_run = _trace_prefix_run(trace_text, crash_launch.launch_event_seq)
    if (
        boundary_run.run_id != terminal_run.run_id
        or boundary_run.lease_id != crash_launch.lease_id
        or boundary_run.worker_id != crash_launch.worker_id
        or boundary_run.lease_epoch != crash_launch.lease_epoch
        or boundary_run.agent_session is None
        or boundary_run.agent_session.artifact_ref != crash_launch.agent_session_artifact_ref
        or boundary_run.agent_session.next_iteration != crash_launch.next_iteration
    ):
        raise ValueError("Crash launch process, Lease, epoch, or Agent session is not Trace-bound")
    launch_fields = (
        "run_id",
        "process_id",
        "worker_id",
        "lease_id",
        "lease_epoch",
        "launch_event_seq",
        "agent_session_artifact_ref",
        "next_iteration",
        "image_id",
    )
    if any(getattr(crash, field) != getattr(crash_launch, field) for field in launch_fields):
        raise ValueError("Child crash observation does not match the parent launch record")
    if crash.launch_record_sha256 != _sha256(crash_launch_content):
        raise ValueError("Child crash observation does not bind the persisted launch record")

    takeover_events = [
        event
        for event in events
        if event.event_type == "LEASE_ACQUIRED"
        and event.payload.get("takeover_reason") == "confirmed_reaped_worker_expired"
    ]
    if len(takeover_events) != 1:
        raise ValueError("Terminal Trace must contain one confirmed reaped-worker takeover")
    takeover = takeover_events[0]
    if (
        takeover.seq != recovery.takeover_event_seq
        or takeover.payload.get("previous_lease_id") != crash_launch.lease_id
        or takeover.payload.get("previous_worker_id") != crash_launch.worker_id
        or takeover.payload.get("previous_epoch") != crash_launch.lease_epoch
        or takeover.payload.get("process_id") != crash_launch.process_id
        or takeover.payload.get("exit_code") != EXIT_AFTER_CREATE
        or takeover.payload.get("launch_event_seq") != crash_launch.launch_event_seq
        or takeover.payload.get("epoch") != recovery.recovery_worker_epoch
        or takeover.payload.get("worker_id") != recovery.recovery_worker_id
    ):
        raise ValueError("Reaped-worker takeover is not bound to the crashed PID and Lease")
    takeover_run = _trace_prefix_run(trace_text, takeover.seq)
    if (
        crash.call_id not in takeover_run.tool_reservations
        or any(item.call_id == crash.call_id for item in takeover_run.tool_calls)
        or takeover_run.lease_epoch != recovery.recovery_worker_epoch
        or takeover_run.worker_id != recovery.recovery_worker_id
    ):
        raise ValueError("Reaped-worker takeover did not preserve the pending tool intent")
    if (
        recovery.run_id != terminal_run.run_id
        or recovery.call_id != crash.call_id
        or recovery.process_id != crash.process_id
        or recovery.crashed_worker_epoch != crash.lease_epoch
    ):
        raise ValueError("Recovery observation does not match the crashed tool intent")

    resume_events = [
        event
        for event in events
        if event.event_type == "LEASE_ACQUIRED"
        and event.payload.get("epoch") == resume_launch.lease_epoch
        and event.payload.get("worker_id") == resume_launch.worker_id
    ]
    if (
        len(resume_events) != 1
        or resume_launch.run_id != terminal_run.run_id
        or resume_launch.lease_epoch != terminal_run.lease_epoch
        or resume_launch.lease_epoch <= recovery.recovery_worker_epoch
        or resume_launch.image_id != crash.image_id
    ):
        raise ValueError("Resume Worker identity, epoch, or Docker image is not Trace-bound")
    resume_boundary_run = _trace_prefix_run(trace_text, resume_launch.launch_event_seq)
    if (
        resume_boundary_run.lease_id != resume_launch.lease_id
        or resume_boundary_run.worker_id != resume_launch.worker_id
        or resume_boundary_run.lease_epoch != resume_launch.lease_epoch
        or resume_boundary_run.agent_session is None
        or resume_boundary_run.agent_session.artifact_ref
        != resume_launch.agent_session_artifact_ref
        or resume_boundary_run.agent_session.next_iteration != resume_launch.next_iteration
        or resume_boundary_run.reservations
    ):
        raise ValueError("Resume launch does not match its quiescent Lease/session boundary")

    original = [item for item in terminal_run.tool_calls if item.call_id == crash.call_id]
    resumed_checks = [
        item
        for item in terminal_run.tool_calls
        if item.name == "run_check" and item.call_id != crash.call_id
    ]
    if (
        len(original) != 1
        or original[0].status != "cancelled"
        or original[0].recovery_disposition != "discard_check"
    ):
        raise ValueError("Terminal Trace lacks one exact discarded crash receipt")
    if len(resumed_checks) != 1:
        raise ValueError("Terminal Trace must contain one fresh protected check")
    resumed = resumed_checks[0]
    if (
        resumed.status != "success"
        or resumed.recovery_disposition is not None
        or resumed.workspace_manifest_ref is None
        or resumed.artifact_ref is None
        or resumed.workspace_revision_after is None
    ):
        raise ValueError("Fresh protected check receipt is incomplete")
    reservations = [
        event.payload["reservation"]
        for event in events
        if event.event_type == "TOOL_CALL_RESERVED"
        and event.payload.get("reservation", {}).get("call_id") == crash.call_id
    ]
    if len(reservations) != 1 or reservations[0].get("workspace_revision") is None:
        raise ValueError("Crash tool intent is absent from the terminal Trace")

    manifest = FileSnapshot.model_validate_json(final_manifest_content)
    if (
        _sha256(final_manifest_content) != resumed.workspace_manifest_ref
        or manifest.workspace_revision != resumed.workspace_revision_after
        or _sha256(resumed_check_artifact) != resumed.artifact_ref
    ):
        raise ValueError("Fresh validation artifacts do not match the terminal Trace")
    marker = next((item for item in manifest.files if item.path == _MARKER_PATH), None)
    if (
        marker is None
        or marker_content != b"executed"
        or marker.sha256 != _sha256(marker_content)
        or marker.size_bytes != len(marker_content)
    ):
        raise ValueError("Final manifest does not prove the synthetic command marker")
    expected_artifact = (
        f"check_id={_CHECK_ID}\npassed=true\nexit_code=0\ntimed_out=false\noutput_truncated=false\n"
    ).encode()
    if resumed_check_artifact != expected_artifact:
        raise ValueError("Fresh protected check artifact is not the complete passing result")
    validation = terminal_run.validation or {}
    if _CHECK_ID not in validation.get("passed_check_ids", ()):
        raise ValueError("Terminal Run lacks passing protected validation")
    if (
        terminal_pack.evidence.unknown_effects_present
        or terminal_pack.evidence.open_effects_present
        or terminal_run.reservations
        or terminal_run.unknown_reservations
    ):
        raise ValueError("Terminal Run retains unknown or open effects")

    return DockerWorkerCrashResumeEvidence(
        run_id=terminal_run.run_id,
        task_id=terminal_run.task.task_id,
        crashed_process_id=crash.process_id,
        crashed_worker_id=crash.worker_id,
        crashed_worker_epoch=crash.lease_epoch,
        recovery_worker_id=recovery.recovery_worker_id,
        recovery_worker_epoch=recovery.recovery_worker_epoch,
        resume_worker_id=resume_launch.worker_id,
        resume_worker_epoch=resume_launch.lease_epoch,
        launch_event_seq=crash.launch_event_seq,
        takeover_event_seq=recovery.takeover_event_seq,
        crash_session_artifact_ref=crash.agent_session_artifact_ref,
        crash_session_next_iteration=crash.next_iteration,
        crash_call_id=crash.call_id,
        resumed_check_call_id=resumed.call_id,
        image_id=crash.image_id,
        event_count=terminal_run.seq,
        event_hash=terminal_run.event_hash,
        projection_hash=projection_hash(terminal_run),
        workspace_revision_before=reservations[0]["workspace_revision"],
        workspace_revision_after=manifest.workspace_revision,
        model_call_count=len(terminal_run.model_calls),
        tool_call_count=len(terminal_run.tool_calls),
        simulated_model_cost_cny=terminal_run.model_occupied_cost,
    )


def _summary(evidence: DockerWorkerCrashResumeEvidence) -> str:
    boundaries = "\n".join(f"- `{item}`" for item in evidence.boundaries)
    return (
        "# Lease-owning Agent Worker Docker crash evidence\n\n"
        f"- Run: `{evidence.run_id}`\n"
        f"- Crash Worker PID / epoch / exit: `{evidence.crashed_process_id}` / "
        f"`{evidence.crashed_worker_epoch}` / `{evidence.crashed_worker_exit_code}`\n"
        f"- Recovery / resume epochs: `{evidence.recovery_worker_epoch}` / "
        f"`{evidence.resume_worker_epoch}`\n"
        f"- Supervisor result: `{evidence.supervisor_disposition}` via "
        f"`{evidence.lease_transition}`\n"
        f"- Docker attempt: `{evidence.state_before_recovery} -> "
        f"{evidence.state_after_recovery}`\n"
        f"- Tool calls: `{evidence.crash_call_id} -> {evidence.resumed_check_call_id}`\n"
        f"- Pinned Docker image: `{evidence.image_id}`\n"
        "- Parent launch record was fsynced before the child start gate: `true`\n"
        "- Takeover Trace binds the reaped PID, old Lease, epoch, and launch sequence: `true`\n"
        "- Original call redispatched / unknown effects / open effects: `false / false / false`\n"
        f"- Protected validation / terminal status: `true / {evidence.terminal_status}`\n"
        "- Paid model / network / external cost: `false / false / 0 CNY`\n"
        f"- Simulated model ledger cost: `{evidence.simulated_model_cost_cny} CNY`\n\n"
        "The verifier recursively checks the generic terminal EvidencePack, replays the full "
        "Trace, binds the child-written crash observation to the pre-dispatch launch record, "
        "and requires the takeover event to identify the exact reaped PID and Lease.\n\n"
        "## Claim boundaries\n\n"
        f"{boundaries}\n"
    )


def verify_docker_worker_crash_resume_pack(path: Path) -> DockerWorkerCrashResumePack:
    """Verify the Agent Worker crash/takeover/continuation pack without Docker or its DB."""

    pack_path = path.resolve(strict=True)
    root = pack_path.parent
    pack = DockerWorkerCrashResumePack.model_validate_json(pack_path.read_text(encoding="utf-8"))
    contents: dict[str, bytes] = {}
    paths: dict[str, Path] = {}
    for item in pack.files:
        candidate = (root / item.path).resolve(strict=True)
        if not candidate.is_relative_to(root):
            raise ValueError("Docker Worker crash evidence file escapes its pack directory")
        content = candidate.read_bytes()
        if len(content) != item.bytes or _sha256(content) != item.sha256:
            raise ValueError(f"Docker Worker crash evidence failed integrity: {item.path}")
        contents[item.role] = content
        paths[item.role] = candidate

    terminal_path = paths["terminal_pack"]
    terminal_pack = verify_terminal_evidence_pack(terminal_path)
    trace_path = _role_path(terminal_path, terminal_pack.files, "trace")
    trace_text = trace_path.read_text(encoding="utf-8")
    terminal_run = SQLiteEventStore.replay_jsonl(trace_text)
    events = tuple(Event.model_validate_json(line) for line in trace_text.splitlines() if line)
    crash_launch = DockerWorkerLaunchObservation.model_validate_json(contents["crash_launch"])
    crash = DockerWorkerCrashObservation.model_validate_json(contents["crash_observation"])
    recovery = DockerWorkerRecoveryObservation.model_validate_json(contents["recovery_observation"])
    resume_launch = DockerWorkerLaunchObservation.model_validate_json(contents["resume_launch"])
    expected = _derive_evidence(
        terminal_pack,
        terminal_run,
        events,
        crash_launch,
        contents["crash_launch"],
        crash,
        recovery,
        resume_launch,
        contents["final_workspace_manifest"],
        contents["resumed_check_artifact"],
        contents["command_marker"],
        trace_text,
    )
    if pack.evidence != expected:
        raise ValueError("Docker Worker crash metadata does not match its linked evidence")
    if contents["summary"] != _summary(expected).encode("utf-8"):
        raise ValueError("Docker Worker crash summary does not match its linked evidence")
    return pack


class DockerWorkerCrashResumeRunner:
    """Crash a Lease-owning child at Docker created state, reconcile, and resume to success."""

    def run(self, output_dir: Path, image: str) -> DockerWorkerCrashResumeResult:
        destination = output_dir.resolve()
        destination.mkdir(parents=True, exist_ok=False)
        runtime = destination / "runtime"
        control_root = runtime / ".horizon"
        staging_root = control_root / "staging"
        workspace = staging_root / "docker-worker-crash"
        workspace.mkdir(parents=True)
        workspace.chmod(0o777)
        _write_fsynced_new(workspace / "fixture.txt", b"lease-owning worker crash fixture\n")

        artifacts = ArtifactStore(control_root / "artifacts")
        snapshots = SnapshotManager(artifacts)
        snapshot, _ = snapshots.capture(
            workspace,
            allowed_paths=("**",),
            denied_paths=(".git/**", ".env", ".env.*", "secrets/**"),
        )
        demo_id = f"docker-worker-crash-{uuid4().hex}"
        provider = _provider_config(f"worker-crash-{demo_id[-12:]}")
        config_path = runtime / "provider.yaml"
        _write_fsynced_new(
            config_path,
            yaml.safe_dump(
                provider.model_dump(mode="json"), sort_keys=False, allow_unicode=True
            ).encode("utf-8"),
        )
        task, plan = _task(workspace, snapshot.workspace_revision, demo_id)
        store_path = control_root / "control.sqlite3"
        store = SQLiteEventStore(store_path)
        service = HarnessService(store)
        run = store.create(task, f"create-{demo_id}")
        run = service.set_plan(run.run_id, plan, f"plan-{demo_id}")
        run = service.acquire_lease(
            run.run_id,
            "docker-agent-crash-worker",
            f"crash-lease-{demo_id}",
            ttl_seconds=5,
        )
        crash_token = LeaseToken.from_run(run)
        run = service.transition(run.run_id, RunStatus.RUNNING, crash_token, f"start-{demo_id}")
        run = service.bind_model_policy(
            run.run_id,
            ModelPolicyBinding(
                policy_id=provider.policy_id,
                provider_id=provider.provider_id,
                model=provider.model.id,
                campaign_id=provider.campaign.campaign_id,
                currency=provider.pricing.currency,
                max_run_cost=provider.run_budget.max_cost,
                price_card_hash=digest(provider.pricing),
            ),
            crash_token,
            f"policy-{demo_id}",
        )
        session = AgentSession(
            run_id=run.run_id,
            task_spec_hash=task.sha256,
            plan_version=plan.version,
            work_item_id=plan.items[0].work_item_id,
            next_iteration=1,
            covered_event_seq=run.seq,
            workspace_revision=snapshot.workspace_revision,
            messages=tuple(_initial_messages(run, plan.items[0])),
        )
        session_ref = artifacts.put(canonical_json(session.model_dump(mode="json")).encode())
        run = service.save_agent_session(
            run.run_id,
            AgentSessionRecord(
                artifact_ref=session_ref,
                task_spec_hash=session.task_spec_hash,
                plan_version=session.plan_version,
                work_item_id=session.work_item_id,
                next_iteration=session.next_iteration,
                covered_event_seq=session.covered_event_seq,
                workspace_revision=session.workspace_revision,
                message_count=len(session.messages),
            ),
            crash_token,
            f"session-{demo_id}",
        )
        campaign_path = runtime / provider.ledger_path
        ledger = CampaignBudgetLedger(campaign_path)
        ledger.initialize(
            provider.campaign,
            provider_id=provider.provider_id,
            model_id=provider.model.id,
        )
        sandbox = DockerSandbox(staging_root, image)
        evidence_dir = destination / "worker-evidence"
        evidence_dir.mkdir()
        crash_launch_path = evidence_dir / "crash-launch.json"
        crash_gate_path = runtime / "crash-worker.start"
        crash_path = evidence_dir / "crash-observation.json"
        crash_boundary = ReapedWorkerBoundary.capture(run, crash_token)

        known_call_ids: set[str] = set()
        success = False
        try:
            crashed, crash_launch, _, crash_stderr = _launch_worker(
                mode="crash",
                store_path=store_path,
                run=run,
                token=crash_token,
                boundary=crash_boundary,
                artifacts_root=artifacts.root,
                workspace=workspace,
                campaign_path=campaign_path,
                staging_root=staging_root,
                image=image,
                image_id=sandbox.image_id,
                config_path=config_path,
                launch_path=crash_launch_path,
                gate_path=crash_gate_path,
                crash_path=crash_path,
            )
            if crashed.returncode != EXIT_AFTER_CREATE:
                stderr_tail = crash_stderr.decode(errors="replace")[-2_000:]
                raise ValueError(
                    "Lease-owning Agent Worker did not exit at Docker created state: "
                    f"exit={crashed.returncode}, stderr={stderr_tail!r}"
                )
            crash = DockerWorkerCrashObservation.model_validate_json(crash_path.read_bytes())
            interrupted = SQLiteEventStore(store_path).get(run.run_id)
            pending = list(interrupted.tool_reservations.values())
            if len(pending) != 1 or pending[0].name != "run_check":
                raise ValueError("Crashed Agent Worker did not leave one run_check intent")
            call_id = pending[0].call_id
            known_call_ids.add(call_id)
            if crash.call_id != call_id or sandbox.attempt_status(call_id).state != "created":
                raise ValueError("Crashed Agent Worker did not leave the exact created attempt")
            if (workspace / _MARKER_PATH).exists():
                raise ValueError("Docker command marker exists before the created attempt starts")

            expires_at = interrupted.lease_expires_at
            if expires_at is None:
                raise ValueError("Crashed Agent Worker has no durable Lease expiry")
            expiry = datetime.fromisoformat(expires_at)
            deadline = time.monotonic() + 8
            while SQLiteEventStore(store_path).clock() < expiry:
                if time.monotonic() >= deadline:
                    raise ValueError("Crashed Agent Worker Lease did not expire within its bound")
                time.sleep(0.02)

            supervisor = SequentialAgentSupervisor(
                lambda: HarnessService(SQLiteEventStore(store_path)),
                lambda active_service: (_ for _ in ()).throw(
                    AssertionError("Pending crash intent must not dispatch an Agent slice")
                ),
                config=SequentialSupervisorConfig(
                    slice_iterations=1,
                    max_worker_slices=2,
                    lease_ttl_seconds=120,
                ),
                worker_id_prefix="docker-worker-controller",
                recovery_factory=lambda active_service: RecoveryService(
                    active_service,
                    CampaignBudgetLedger(campaign_path),
                    ArtifactStore(artifacts.root),
                ),
            )
            takeover = supervisor.continue_after_reaped_worker(
                crash_boundary,
                HarnessService(SQLiteEventStore(store_path)),
                process_id=crashed.pid,
                exit_code=crashed.returncode,
            )
            if (
                takeover.disposition != "reconciliation_required"
                or takeover.lease_transition != "expired_takeover"
                or takeover.pending_reservation_ids != (call_id,)
            ):
                raise ValueError("Supervisor did not preserve the pending intent during takeover")
            recovery_token = LeaseToken.from_run(takeover.run)
            service = HarnessService(SQLiteEventStore(store_path))
            blocked = RecoveryService(service, ledger, artifacts).reconcile(
                run.run_id, recovery_token
            )
            unknown_tools = service.store.get(run.run_id).unknown_tool_calls
            if blocked.safe_to_resume or call_id not in unknown_tools:
                raise ValueError("Takeover did not conservatively classify the check as unknown")

            recovery_gateway = WorkspaceToolGateway(
                workspace,
                takeover.run.task,
                takeover.run.plan.items[0],
                snapshots,
                None,
            )
            tool_recovery = ToolRecoveryService(service, artifacts, recovery_gateway)
            before_snapshot, before_manifest_ref = snapshots.capture(
                workspace,
                allowed_paths=task.constraints.allowed_paths,
                denied_paths=task.constraints.denied_paths,
            )
            tool_recovery.validate_check_discard(
                run.run_id,
                call_id,
                current_workspace_revision=before_snapshot.workspace_revision,
                current_workspace_manifest_ref=before_manifest_ref,
                token=recovery_token,
            )
            before_attempt = sandbox.attempt_status(call_id)
            if before_attempt.state != "created" or before_attempt.image_id != sandbox.image_id:
                raise ValueError("Recovery controller did not observe the exact created attempt")
            sandbox.remove_attempt(call_id, expected_state="created")
            after_attempt = sandbox.attempt_status(call_id)
            settled_snapshot, settled_manifest_ref = snapshots.capture(
                workspace,
                allowed_paths=task.constraints.allowed_paths,
                denied_paths=task.constraints.denied_paths,
            )
            if settled_snapshot.workspace_revision != before_snapshot.workspace_revision:
                raise ValueError("Workspace changed while the created attempt was removed")
            resolved = tool_recovery.discard_check_result(
                run.run_id,
                call_id,
                current_workspace_revision=settled_snapshot.workspace_revision,
                current_workspace_manifest_ref=settled_manifest_ref,
                sandbox_stopped=True,
                sandbox_stop_evidence="controller_observed_created",
                token=recovery_token,
            )
            safe = RecoveryService(service, ledger, artifacts).reconcile(run.run_id, recovery_token)
            if not safe.safe_to_resume:
                raise ValueError("Created attempt settlement did not restore a safe Agent boundary")
            recovered_record = next(item for item in resolved.tool_calls if item.call_id == call_id)
            takeover_event = next(
                event
                for event in service.store.events(run.run_id)
                if event.event_type == "LEASE_ACQUIRED"
                and event.payload.get("takeover_reason") == "confirmed_reaped_worker_expired"
            )
            recovery_observation = DockerWorkerRecoveryObservation(
                run_id=run.run_id,
                call_id=call_id,
                process_id=crashed.pid,
                crashed_worker_epoch=crash_token.epoch,
                recovery_worker_id=recovery_token.worker_id,
                recovery_worker_epoch=recovery_token.epoch,
                takeover_event_seq=takeover_event.seq,
                state_after_recovery=after_attempt.state,
                recovery_disposition=recovered_record.recovery_disposition,
                recovered_tool_status=recovered_record.status,
            )
            recovery_path = evidence_dir / "recovery-observation.json"
            _write_fsynced_new(
                recovery_path,
                (canonical_json(recovery_observation) + "\n").encode("utf-8"),
            )

            service.release_lease(
                run.run_id, recovery_token, f"release-recovery-controller-{demo_id}"
            )
            leased = service.acquire_lease(
                run.run_id,
                "docker-agent-resume-worker",
                f"resume-lease-{demo_id}",
                ttl_seconds=120,
            )
            resume_token = LeaseToken.from_run(leased)
            resume_boundary = ReapedWorkerBoundary.capture(leased, resume_token)
            resume_launch_path = evidence_dir / "resume-launch.json"
            resumed, resume_launch, _, resume_stderr = _launch_worker(
                mode="resume",
                store_path=store_path,
                run=leased,
                token=resume_token,
                boundary=resume_boundary,
                artifacts_root=artifacts.root,
                workspace=workspace,
                campaign_path=campaign_path,
                staging_root=staging_root,
                image=image,
                image_id=sandbox.image_id,
                config_path=config_path,
                launch_path=resume_launch_path,
                gate_path=runtime / "resume-worker.start",
                crash_path=runtime / "unused-resume-crash.json",
            )
            if resumed.returncode != 0:
                stderr_tail = resume_stderr.decode(errors="replace")[-2_000:]
                raise ValueError(
                    "Fresh Agent Worker did not resume to success: "
                    f"exit={resumed.returncode}, stderr={stderr_tail!r}"
                )

            final_store = SQLiteEventStore(store_path)
            final = final_store.get(run.run_id)
            if final.status != RunStatus.SUCCEEDED or final.lease_epoch != resume_token.epoch:
                raise ValueError("Fresh Agent Worker did not persist terminal success")
            resumed_checks = [
                item
                for item in final.tool_calls
                if item.name == "run_check" and item.call_id != call_id
            ]
            if len(resumed_checks) != 1:
                raise ValueError("Fresh Agent Worker did not settle one protected check")
            resumed_check = resumed_checks[0]
            known_call_ids.add(resumed_check.call_id)
            if sandbox.attempt_status(resumed_check.call_id).state != "missing":
                raise ValueError("Fresh protected check attempt was not cleaned up")
            if resumed_check.workspace_manifest_ref is None or resumed_check.artifact_ref is None:
                raise ValueError("Fresh protected check omitted its evidence artifacts")
            marker_path = workspace / _MARKER_PATH
            if not marker_path.is_file():
                raise ValueError("Fresh protected check did not create its execution marker")

            terminal = export_terminal_evidence_pack(
                final_store, final.run_id, destination / "terminal"
            )
            final_manifest_path = evidence_dir / "final-workspace-manifest.json"
            check_artifact_path = evidence_dir / "resumed-check-artifact.txt"
            marker_copy_path = evidence_dir / _MARKER_PATH
            _write_fsynced_new(
                final_manifest_path, artifacts.read(resumed_check.workspace_manifest_ref)
            )
            _write_fsynced_new(check_artifact_path, artifacts.read(resumed_check.artifact_ref))
            _write_fsynced_new(marker_copy_path, marker_path.read_bytes())

            trace_text = terminal.trace_path.read_text(encoding="utf-8")
            events = tuple(
                Event.model_validate_json(line) for line in trace_text.splitlines() if line
            )
            evidence = _derive_evidence(
                terminal.evidence_pack,
                SQLiteEventStore.replay_jsonl(trace_text),
                events,
                crash_launch,
                crash_launch_path.read_bytes(),
                crash,
                recovery_observation,
                resume_launch,
                final_manifest_path.read_bytes(),
                check_artifact_path.read_bytes(),
                marker_copy_path.read_bytes(),
                trace_text,
            )
            summary_path = destination / "SUMMARY.md"
            evidence_pack_path = destination / "evidence-pack.json"
            _write_fsynced_new(summary_path, _summary(evidence).encode("utf-8"))
            pack = DockerWorkerCrashResumePack(
                evidence=evidence,
                files=(
                    _file_record(destination, "terminal_pack", terminal.evidence_pack_path),
                    _file_record(destination, "crash_launch", crash_launch_path),
                    _file_record(destination, "crash_observation", crash_path),
                    _file_record(destination, "recovery_observation", recovery_path),
                    _file_record(destination, "resume_launch", resume_launch_path),
                    _file_record(destination, "final_workspace_manifest", final_manifest_path),
                    _file_record(destination, "resumed_check_artifact", check_artifact_path),
                    _file_record(destination, "command_marker", marker_copy_path),
                    _file_record(destination, "summary", summary_path),
                ),
            )
            _write_fsynced_new(evidence_pack_path, (canonical_json(pack) + "\n").encode("utf-8"))
            verified = verify_docker_worker_crash_resume_pack(evidence_pack_path)
            success = True
            return DockerWorkerCrashResumeResult(
                output_dir=destination,
                terminal_evidence_pack_path=terminal.evidence_pack_path,
                evidence_pack_path=evidence_pack_path,
                summary_path=summary_path,
                evidence_pack=verified,
            )
        finally:
            if not success:
                try:
                    current = SQLiteEventStore(store_path).get(run.run_id)
                    known_call_ids.update(current.tool_reservations)
                    known_call_ids.update(
                        item.call_id for item in current.tool_calls if item.name == "run_check"
                    )
                except Exception:
                    pass
                for attempt_id in known_call_ids:
                    try:
                        status = sandbox.attempt_status(attempt_id)
                        if status.state == "running":
                            sandbox.stop_attempt(attempt_id)
                            status = sandbox.attempt_status(attempt_id)
                        if status.state in {"created", "stopped"}:
                            sandbox.remove_attempt(attempt_id, expected_state=status.state)
                    except Exception:
                        pass
