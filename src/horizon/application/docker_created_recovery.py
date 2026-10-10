from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import yaml

from horizon.adapters.model.config import (
    ProviderConfig,
    ProviderModelConfig,
    RequestPolicy,
    RunBudgetPolicy,
)
from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.sandbox.docker import DockerSandbox
from horizon.adapters.workspace.snapshot import FileSnapshot, SnapshotManager
from horizon.application._docker_created_crash_worker import EXIT_AFTER_CREATE
from horizon.application.recovery import RecoveryService
from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.agent import AgentSession, AgentSessionRecord
from horizon.domain.budget import Usage
from horizon.domain.common import canonical_json, digest
from horizon.domain.docker_created_recovery import (
    DockerCreatedCrashObservation,
    DockerCreatedRecoveryEvidence,
    DockerCreatedRecoveryFile,
    DockerCreatedRecoveryPack,
)
from horizon.domain.events import Event
from horizon.domain.model import (
    CampaignBudget,
    FunctionCall,
    ModelCallRecord,
    ModelCallReservation,
    ModelMessage,
    ModelPolicyBinding,
    ModelResponse,
    ModelUsage,
    PriceCard,
    ToolCall,
)
from horizon.domain.plan import Plan, WorkItem
from horizon.domain.run import Run, projection_hash
from horizon.domain.states import RunStatus
from horizon.domain.task import AcceptanceCheck, BudgetSpec, Constraints, Repository, TaskSpec
from horizon.domain.tools import ToolCallReservation

_MARKER_PATH = "never-started.txt"
_CHECK_ID = "never-started"
_CHECK_COMMAND = "/bin/sh -c 'printf executed > /workspace/never-started.txt'"


@dataclass(frozen=True)
class DockerCreatedRecoveryResult:
    output_dir: Path
    runtime_dir: Path
    evidence_dir: Path
    evidence_pack_path: Path
    trace_path: Path
    final_state_path: Path
    summary_path: Path
    evidence_pack: DockerCreatedRecoveryPack


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)


def _file_record(
    root: Path,
    role: str,
    path: Path,
) -> DockerCreatedRecoveryFile:
    content = path.read_bytes()
    return DockerCreatedRecoveryFile(
        role=role,
        path=path.relative_to(root).as_posix(),
        sha256=_sha256(content),
        bytes=len(content),
    )


def _provider_config(campaign_id: str) -> ProviderConfig:
    return ProviderConfig(
        policy_id="offline-docker-created-recovery",
        provider_id="offline-demo",
        api_type="openai_chat_completions",
        base_url="https://offline.invalid/v1",
        credential_env="HORIZON_OFFLINE_DEMO_KEY",
        model=ProviderModelConfig(id="scripted-docker-recovery-v1"),
        request=RequestPolicy(
            timeout_seconds=30,
            max_output_tokens=128,
            probe_max_output_tokens=32,
            max_attempts=1,
            max_context_chars=10_000,
            max_input_tokens=20_000,
            preserve_recent_context_units=2,
        ),
        pricing=PriceCard(
            currency="CNY",
            input_per_million="0",
            cached_input_per_million="0",
            output_per_million="0",
            version="offline-scripted-v1",
            source_url="https://offline.invalid/horizon-docker-recovery",
        ),
        campaign=CampaignBudget(
            campaign_id=campaign_id,
            currency="CNY",
            max_cost="1.00",
            max_cost_per_call="1.00",
        ),
        run_budget=RunBudgetPolicy(currency="CNY", max_cost="1.00"),
        ledger_path=".horizon/campaign.sqlite3",
        fallback_enabled=False,
    )


def _task(workspace: Path, revision: str, demo_id: str) -> tuple[TaskSpec, Plan]:
    check = AcceptanceCheck(
        id=_CHECK_ID,
        command=_CHECK_COMMAND,
        timeout_seconds=30,
        required=True,
    )
    task = TaskSpec(
        task_id=f"docker-created-{demo_id[-12:]}",
        title="Reconcile a never-started Docker validation attempt",
        objective=(
            "Recover after the validation Worker exits between Docker create and start without "
            "executing or replaying the uncertain check."
        ),
        repository=Repository(
            source="local",
            path=str(workspace),
            base_commit=revision,
        ),
        constraints=Constraints(
            allowed_paths=("**",),
            denied_paths=(".git/**", ".env", ".env.*", "secrets/**"),
            allowed_tools=("run_check",),
            network="deny",
            requirements=("Do not infer a check result when Docker start never occurred.",),
        ),
        acceptance=(check,),
        budgets=BudgetSpec(
            max_steps=4,
            max_model_calls=2,
            max_tool_calls=2,
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
        memory_scope=f"docker-created-{demo_id[-12:]}",
    )
    plan = Plan(
        items=(
            WorkItem(
                work_item_id="reconcile-created-check",
                title="Reconcile the never-started check",
                objective="Discard only the exact inactive Docker attempt and persist a receipt.",
                expected_artifacts=("recovery receipt", "replayable Trace"),
                acceptance_ids=(_CHECK_ID,),
                allowed_tools=("run_check",),
            ),
        )
    )
    plan.check_task(task)
    return task, plan


def _expected_container_name(call_id: str) -> str:
    owner = hashlib.sha256(f"horizon-check-v1:{call_id}".encode()).hexdigest()
    return f"horizon-check-{owner[:32]}"


def _event_sequence(events: tuple[Event, ...], call_id: str) -> tuple[int, int, int]:
    reserved = [
        event.seq
        for event in events
        if event.event_type == "TOOL_CALL_RESERVED"
        and event.payload.get("reservation", {}).get("call_id") == call_id
    ]
    unknown = [
        event.seq
        for event in events
        if event.event_type == "TOOL_CALL_UNKNOWN"
        and event.payload.get("call_id") == call_id
    ]
    settled = [
        event.seq
        for event in events
        if event.event_type == "TOOL_CALL_SETTLED"
        and event.payload.get("record", {}).get("call_id") == call_id
    ]
    if (
        len(reserved) != 1
        or len(unknown) != 1
        or len(settled) != 1
        or not reserved[0] < unknown[0] < settled[0]
    ):
        raise ValueError(
            "Docker recovery Trace must contain intent-before-unknown-before-receipt ordering"
        )
    return reserved[0], unknown[0], settled[0]


def _derive_evidence(
    run: Run,
    events: tuple[Event, ...],
    crash: DockerCreatedCrashObservation,
    receipt: dict[str, object],
    manifest_content: bytes,
    artifact_content: bytes,
) -> DockerCreatedRecoveryEvidence:
    records = [record for record in run.tool_calls if record.call_id == crash.call_id]
    if len(records) != 1:
        raise ValueError("Docker recovery Trace requires one exact tool receipt")
    record = records[0]
    if (
        run.status != RunStatus.RUNNING
        or run.agent_session is None
        or run.reservations
        or run.model_reservations
        or run.tool_reservations
        or run.unknown_reservations
        or run.unknown_model_calls
        or run.unknown_tool_calls
    ):
        raise ValueError("Docker recovery final Run is not a quiescent resumable boundary")
    if (
        record.name != "run_check"
        or record.status != "cancelled"
        or record.recovery_disposition != "discard_check"
        or record.artifact_ref is None
        or record.workspace_manifest_ref is None
        or record.workspace_revision_before is None
        or record.workspace_revision_after is None
    ):
        raise ValueError("Docker recovery tool receipt does not match discard_check")
    if record.workspace_revision_before != record.workspace_revision_after:
        raise ValueError("Docker recovery changed the workspace revision")

    manifest_sha256 = _sha256(manifest_content)
    artifact_sha256 = _sha256(artifact_content)
    if (
        record.workspace_manifest_ref != manifest_sha256
        or record.artifact_ref != artifact_sha256
        or record.output_hash != artifact_sha256
    ):
        raise ValueError("Docker recovery artifacts do not match the Trace receipt")
    manifest = FileSnapshot.model_validate_json(manifest_content)
    if (
        manifest.workspace_revision != record.workspace_revision_after
        or any(entry.path == crash.command_marker_path for entry in manifest.files)
    ):
        raise ValueError("Docker recovery manifest contains an executed marker or wrong revision")
    try:
        artifact = json.loads(artifact_content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Docker recovery artifact is not valid JSON") from exc
    if (
        not isinstance(artifact, dict)
        or set(artifact)
        != {"decision", "effect", "original_call_id", "tool", "workspace_revision"}
        or artifact.get("decision") != "discard_check"
        or artifact.get("original_call_id") != crash.call_id
        or artifact.get("tool") != "run_check"
        or artifact.get("workspace_revision") != record.workspace_revision_after
        or "State.Status=created" not in str(artifact.get("effect"))
        or "without inferring pass or failure" not in str(artifact.get("effect"))
    ):
        raise ValueError("Docker recovery artifact does not record the bounded disposition")

    required_receipt = {
        "run_id": run.run_id,
        "safe_to_resume": True,
        "requires_human": False,
        "next_action": "resume",
        "remaining_reservations": [],
        "unknown_reservations": [],
        "decision": "discard_check",
        "resolved_tool_call_id": crash.call_id,
        "resolved_tool": "run_check",
        "workspace_revision": record.workspace_revision_after,
        "check_sandbox_resolution": "controller_observed_created_and_removed",
        "check_sandbox_cleanup_error": None,
        "check_sandbox_container_name": crash.container_name,
        "recovered_check_result": None,
        "network_called": False,
        "paid_model_called": False,
    }
    if any(receipt.get(key) != value for key, value in required_receipt.items()):
        raise ValueError("Docker recovery CLI receipt does not match the final disposition")
    if crash.container_name != _expected_container_name(crash.call_id):
        raise ValueError("Docker recovery container name is not derived from its durable call ID")
    _event_sequence(events, crash.call_id)

    return DockerCreatedRecoveryEvidence(
        run_id=run.run_id,
        task_id=run.task.task_id,
        call_id=crash.call_id,
        event_count=run.seq,
        event_hash=run.event_hash,
        projection_hash=projection_hash(run),
        workspace_revision_before=record.workspace_revision_before,
        workspace_revision_after=record.workspace_revision_after,
        recovery_artifact_sha256=artifact_sha256,
        worker_exit_code=crash.worker_exit_code,
        image_id=crash.image_id,
        container_name=crash.container_name,
    )


def _summary(evidence: DockerCreatedRecoveryEvidence) -> str:
    boundaries = "\n".join(f"- `{item}`" for item in evidence.boundaries)
    return (
        "# Docker created-state crash recovery evidence\n\n"
        f"- Run: `{evidence.run_id}`\n"
        f"- Tool call: `{evidence.call_id}`\n"
        f"- Worker hard-exit code: `{evidence.worker_exit_code}`\n"
        f"- Docker image ID: `{evidence.image_id}`\n"
        f"- Attempt transition: `{evidence.state_before_recovery} -> "
        f"{evidence.state_after_recovery}`\n"
        f"- Recovery disposition: `{evidence.recovery_disposition}`\n"
        f"- Tool receipt status: `{evidence.tool_status}`\n"
        f"- Workspace revision unchanged: `true`\n"
        f"- Command marker absent: `true`\n"
        f"- Trace replay verified: `true`\n"
        f"- Safe to resume: `true`\n"
        f"- Paid model / network / repository code: `false / false / false`\n"
        f"- External cost: `0 CNY`\n\n"
        "The offline verifier rehashes every listed file, replays the JSONL Trace, compares the "
        "exact final projection, and re-derives the bounded recovery claim from the durable "
        "tool receipt, workspace manifest, crash observation, and CLI receipt.\n\n"
        "## Claim boundaries\n\n"
        f"{boundaries}\n"
    )


def verify_docker_created_recovery_pack(path: Path) -> DockerCreatedRecoveryPack:
    """Verify the pack offline without Docker, the control database, or a model provider."""

    pack_path = path.resolve(strict=True)
    root = pack_path.parent
    pack = DockerCreatedRecoveryPack.model_validate_json(
        pack_path.read_text(encoding="utf-8")
    )
    contents: dict[str, bytes] = {}
    for item in pack.files:
        candidate = (root / item.path).resolve(strict=True)
        if not candidate.is_relative_to(root):
            raise ValueError("Docker recovery evidence file escapes its pack directory")
        content = candidate.read_bytes()
        if len(content) != item.bytes or _sha256(content) != item.sha256:
            raise ValueError(
                f"Docker recovery evidence file failed integrity verification: {item.path}"
            )
        contents[item.role] = content

    trace_text = contents["trace"].decode("utf-8")
    replayed = SQLiteEventStore.replay_jsonl(trace_text)
    expected_state = (canonical_json(replayed.as_dict()) + "\n").encode("utf-8")
    if contents["final_state"] != expected_state:
        raise ValueError("Docker recovery final state does not match its replayed Trace")
    events = tuple(Event.model_validate_json(line) for line in trace_text.splitlines() if line)
    crash = DockerCreatedCrashObservation.model_validate_json(contents["crash_observation"])
    try:
        receipt = json.loads(contents["recovery_receipt"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Docker recovery CLI receipt is not valid JSON") from exc
    if not isinstance(receipt, dict):
        raise ValueError("Docker recovery CLI receipt must be a JSON object")
    expected = _derive_evidence(
        replayed,
        events,
        crash,
        receipt,
        contents["workspace_manifest"],
        contents["recovery_artifact"],
    )
    if pack.evidence != expected:
        raise ValueError("Docker recovery EvidencePack metadata does not match its Trace")
    if contents["summary"] != _summary(expected).encode("utf-8"):
        raise ValueError("Docker recovery summary does not match its replayed Trace")
    return pack


class DockerCreatedRecoveryRunner:
    """Exercise and package one exact Docker create-to-start recovery boundary."""

    def run(self, output_dir: Path, image: str) -> DockerCreatedRecoveryResult:
        destination = output_dir.resolve()
        destination.mkdir(parents=True, exist_ok=False)
        runtime = destination / "runtime"
        evidence_dir = destination / "evidence"
        control_root = runtime / ".horizon"
        staging_root = control_root / "staging"
        workspace = staging_root / "docker-created-recovery"
        workspace.mkdir(parents=True)
        _write_new(workspace / "fixture.txt", b"bounded docker recovery fixture\n")

        artifacts = ArtifactStore(control_root / "artifacts")
        snapshots = SnapshotManager(artifacts)
        snapshot, manifest_ref = snapshots.capture(
            workspace,
            allowed_paths=("**",),
            denied_paths=(".git/**", ".env", ".env.*", "secrets/**"),
        )
        demo_id = f"docker-created-{uuid4().hex}"
        provider = _provider_config(f"created-{demo_id[-12:]}")
        config_path = runtime / "provider.yaml"
        _write_new(
            config_path,
            yaml.safe_dump(
                provider.model_dump(mode="json"),
                sort_keys=False,
                allow_unicode=True,
            ).encode("utf-8"),
        )
        task, plan = _task(workspace, snapshot.workspace_revision, demo_id)
        db_path = control_root / "control.sqlite3"
        store = SQLiteEventStore(db_path)
        service = HarnessService(store)
        run = store.create(task, f"create-{demo_id}")
        run = service.set_plan(run.run_id, plan, f"plan-{demo_id}")
        run = service.acquire_lease(
            run.run_id,
            "docker-created-worker",
            f"lease-{demo_id}",
            ttl_seconds=120,
        )
        token = LeaseToken.from_run(run)
        run = service.transition(run.run_id, RunStatus.RUNNING, token, f"start-{demo_id}")
        policy = ModelPolicyBinding(
            policy_id=provider.policy_id,
            provider_id=provider.provider_id,
            model=provider.model.id,
            campaign_id=provider.campaign.campaign_id,
            currency=provider.pricing.currency,
            max_run_cost=provider.run_budget.max_cost,
            price_card_hash=digest(provider.pricing),
        )
        run = service.bind_model_policy(run.run_id, policy, token, f"policy-{demo_id}")
        session = AgentSession(
            run_id=run.run_id,
            task_spec_hash=task.sha256,
            plan_version=plan.version,
            work_item_id=plan.items[0].work_item_id,
            next_iteration=1,
            covered_event_seq=run.seq,
            workspace_revision=snapshot.workspace_revision,
            messages=(
                ModelMessage(role="system", content="Run only the bounded acceptance check."),
                ModelMessage(role="user", content="Verify the fixture without network access."),
            ),
        )
        session_ref = artifacts.put(
            canonical_json(session.model_dump(mode="json")).encode("utf-8")
        )
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
            token,
            f"session-{demo_id}",
        )

        ledger_path = runtime / provider.ledger_path
        ledger = CampaignBudgetLedger(ledger_path)
        ledger.initialize(
            provider.campaign,
            provider_id=provider.provider_id,
            model_id=provider.model.id,
        )
        model_call = ModelCallReservation(
            call_id=f"model_{run.run_id}_created",
            request_hash=digest({"demo_id": demo_id, "action": "run_check"}),
            provider_id=provider.provider_id,
            model=provider.model.id,
            currency=provider.pricing.currency,
            reserved_cost=Decimal("0.01"),
        )
        ledger.reserve(
            provider.campaign,
            model_call.call_id,
            model_call.request_hash,
            model_call.reserved_cost,
        )
        run = service.reserve_model_call(
            run.run_id,
            model_call,
            Usage(model_calls=1, input_tokens=12, output_tokens=8),
            token,
            f"reserve-model-{demo_id}",
        )
        response = ModelResponse(
            response_id=f"offline-response-{demo_id[-12:]}",
            model=provider.model.id,
            message=ModelMessage(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        id="offline-run-check",
                        function=FunctionCall(
                            name="run_check",
                            arguments={"check_id": _CHECK_ID},
                        ),
                    ),
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(input_tokens=12, output_tokens=8),
            provider_trace_id=f"offline-{demo_id[-12:]}",
        )
        response_content = canonical_json(response.model_dump(mode="json")).encode("utf-8")
        response_ref = artifacts.put(response_content)
        actual_cost = provider.pricing.cost_for(response.usage)
        run = service.settle_model_call(
            run.run_id,
            ModelCallRecord(
                call_id=model_call.call_id,
                request_hash=model_call.request_hash,
                provider_id=provider.provider_id,
                model=provider.model.id,
                currency=provider.pricing.currency,
                estimated_cost=actual_cost,
                response_id=response.response_id,
                response_artifact_ref=response_ref,
                provider_trace_id=response.provider_trace_id,
                finish_reason=response.finish_reason,
                usage=response.usage,
            ),
            Usage(model_calls=1, input_tokens=12, output_tokens=8),
            token,
            f"settle-model-{demo_id}",
        )
        ledger.settle(
            provider.campaign.campaign_id,
            model_call.call_id,
            actual_cost,
            response.provider_trace_id,
        )
        call_id = f"tool_{run.run_id}_created"
        reservation = ToolCallReservation(
            call_id=call_id,
            name="run_check",
            arguments_hash=digest({"check_id": _CHECK_ID}),
            workspace_revision=snapshot.workspace_revision,
            workspace_manifest_ref=manifest_ref,
        )
        service.reserve_tool_call(
            run.run_id,
            reservation,
            token,
            f"reserve-check-{demo_id}",
        )

        sandbox = DockerSandbox(staging_root, image)
        marker = workspace / _MARKER_PATH
        success = False
        try:
            crashed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "horizon.application._docker_created_crash_worker",
                    str(staging_root),
                    str(workspace),
                    image,
                    call_id,
                ],
                capture_output=True,
                timeout=45,
                check=False,
            )
            if crashed.returncode != EXIT_AFTER_CREATE:
                stderr = crashed.stderr.decode(errors="replace")[-2_000:]
                raise ValueError(
                    "Docker crash worker did not stop after create: "
                    f"exit={crashed.returncode}, stderr={stderr!r}"
                )
            before = sandbox.attempt_status(call_id)
            if before.state != "created" or marker.exists():
                raise ValueError(
                    "Docker crash boundary did not leave one created, never-started attempt"
                )
            interrupted = store.get(run.run_id)
            if call_id not in interrupted.tool_reservations or interrupted.tool_calls:
                raise ValueError("Docker crash did not preserve exactly one unsettled tool intent")
            blocked = RecoveryService(service, ledger, artifacts).reconcile(run.run_id, token)
            if blocked.safe_to_resume or call_id not in store.get(run.run_id).unknown_tool_calls:
                raise ValueError("Docker crash was not conservatively classified as unknown")
            service.release_lease(run.run_id, token, f"release-crashed-{demo_id}")

            resolved = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "horizon",
                    "--db",
                    str(db_path),
                    "agent",
                    "resolve-tool",
                    run.run_id,
                    call_id,
                    "--discard-check",
                    "--image",
                    image,
                    "--config",
                    str(config_path),
                ],
                cwd=runtime,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                capture_output=True,
                timeout=60,
                check=False,
            )
            if resolved.returncode != 0:
                stdout = resolved.stdout.decode(errors="replace")[-2_000:]
                stderr = resolved.stderr.decode(errors="replace")[-2_000:]
                raise ValueError(
                    "Docker recovery CLI failed: "
                    f"exit={resolved.returncode}, stdout={stdout!r}, stderr={stderr!r}"
                )
            try:
                receipt = json.loads(resolved.stdout)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Docker recovery CLI did not emit one JSON receipt") from exc
            after = sandbox.attempt_status(call_id)
            if after.state != "missing" or marker.exists():
                raise ValueError("Docker recovery did not remove the inactive exact attempt")
            crash = DockerCreatedCrashObservation(
                call_id=call_id,
                image_id=before.image_id,
                container_name=before.container_name,
                command_marker_path=_MARKER_PATH,
            )

            store = SQLiteEventStore(db_path)
            final = store.get(run.run_id)
            trace_content = store.export_jsonl(run.run_id).encode("utf-8")
            replayed = SQLiteEventStore.replay_jsonl(trace_content.decode("utf-8"))
            if replayed.as_dict() != final.as_dict():
                raise ValueError("Docker recovery live and replayed projections differ")
            record = next(
                (item for item in final.tool_calls if item.call_id == call_id),
                None,
            )
            if (
                record is None
                or record.artifact_ref is None
                or record.workspace_manifest_ref is None
            ):
                raise ValueError("Docker recovery did not persist its receipt artifacts")
            manifest_content = artifacts.read(record.workspace_manifest_ref)
            artifact_content = artifacts.read(record.artifact_ref)
            receipt_content = (canonical_json(receipt) + "\n").encode("utf-8")
            crash_content = (canonical_json(crash) + "\n").encode("utf-8")
            evidence = _derive_evidence(
                final,
                tuple(store.events(run.run_id)),
                crash,
                receipt,
                manifest_content,
                artifact_content,
            )

            evidence_dir.mkdir(parents=True, exist_ok=False)
            trace_path = evidence_dir / "trace.jsonl"
            final_state_path = evidence_dir / "final-run.json"
            crash_path = evidence_dir / "crash-observation.json"
            receipt_path = evidence_dir / "recovery-receipt.json"
            manifest_path = evidence_dir / "workspace-manifest.json"
            artifact_path = evidence_dir / "recovery-artifact.json"
            summary_path = evidence_dir / "SUMMARY.md"
            evidence_pack_path = evidence_dir / "evidence-pack.json"
            _write_new(trace_path, trace_content)
            _write_new(
                final_state_path,
                (canonical_json(final.as_dict()) + "\n").encode("utf-8"),
            )
            _write_new(crash_path, crash_content)
            _write_new(receipt_path, receipt_content)
            _write_new(manifest_path, manifest_content)
            _write_new(artifact_path, artifact_content)
            _write_new(summary_path, _summary(evidence).encode("utf-8"))
            pack = DockerCreatedRecoveryPack(
                evidence=evidence,
                files=(
                    _file_record(evidence_dir, "trace", trace_path),
                    _file_record(evidence_dir, "final_state", final_state_path),
                    _file_record(evidence_dir, "crash_observation", crash_path),
                    _file_record(evidence_dir, "recovery_receipt", receipt_path),
                    _file_record(evidence_dir, "workspace_manifest", manifest_path),
                    _file_record(evidence_dir, "recovery_artifact", artifact_path),
                    _file_record(evidence_dir, "summary", summary_path),
                ),
            )
            _write_new(evidence_pack_path, (canonical_json(pack) + "\n").encode("utf-8"))
            verified = verify_docker_created_recovery_pack(evidence_pack_path)
            success = True
            return DockerCreatedRecoveryResult(
                output_dir=destination,
                runtime_dir=runtime,
                evidence_dir=evidence_dir,
                evidence_pack_path=evidence_pack_path,
                trace_path=trace_path,
                final_state_path=final_state_path,
                summary_path=summary_path,
                evidence_pack=verified,
            )
        finally:
            if not success:
                status = sandbox.attempt_status(call_id)
                if status.state == "running":
                    sandbox.stop_attempt(call_id)
                    status = sandbox.attempt_status(call_id)
                if status.state in {"created", "stopped"}:
                    sandbox.remove_attempt(call_id, expected_state=status.state)
