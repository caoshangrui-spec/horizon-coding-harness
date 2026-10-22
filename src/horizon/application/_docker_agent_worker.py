"""Real child Agent Worker used by the Docker create-to-start crash contract."""

from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

import yaml

from horizon.adapters.model.config import ProviderConfig
from horizon.adapters.model.scripted import ScriptedModelGateway
from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.sandbox.docker import DockerSandbox
from horizon.adapters.sandbox.validation import DockerAcceptanceExecutor
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.agent_loop import AgentLoopConfig, CodingAgentRunner
from horizon.application.docker_created_recovery import _CHECK_ID, _MARKER_PATH
from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.common import canonical_json
from horizon.domain.docker_created_recovery import (
    DockerWorkerCrashObservation,
    DockerWorkerLaunchObservation,
)
from horizon.domain.run_evaluation import ScriptedModelAction
from horizon.domain.states import RunStatus
from horizon.tools.gateway import WorkspaceToolGateway

EXIT_AFTER_CREATE = 34


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_fsynced_new(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _read_start_gate(
    launch_path: Path,
    gate_path: Path,
) -> tuple[DockerWorkerLaunchObservation, bytes]:
    deadline = time.monotonic() + 15
    while not gate_path.is_file():
        if time.monotonic() >= deadline:
            raise RuntimeError("Agent Worker start gate was not released")
        time.sleep(0.01)
    launch_content = launch_path.read_bytes()
    expected_hash = gate_path.read_text(encoding="utf-8").strip()
    if expected_hash != _sha256(launch_content):
        raise ValueError("Agent Worker start gate does not bind the persisted launch record")
    return DockerWorkerLaunchObservation.model_validate_json(launch_content), launch_content


class _ExitAfterCreatedAttempt(DockerSandbox):
    def __init__(
        self,
        staging_root: Path,
        image: str,
        *,
        launch: DockerWorkerLaunchObservation,
        launch_record_sha256: str,
        crash_path: Path,
    ):
        super().__init__(staging_root, image)
        self.launch = launch
        self.launch_record_sha256 = launch_record_sha256
        self.crash_path = crash_path
        self.attempt_id: str | None = None

    def execute(self, workspace, request, *, attempt_id=None):
        if attempt_id is None:
            raise ValueError("Crash Worker requires a durable Docker attempt ID")
        self.attempt_id = attempt_id
        return super().execute(workspace, request, attempt_id=attempt_id)

    def _launch_attached_start(self, name: str):
        call_id = self.attempt_id
        if call_id is None:
            raise RuntimeError("Crash Worker reached Docker start without an attempt identity")
        status = self.attempt_status(call_id)
        if status.state != "created" or status.container_name != name:
            raise RuntimeError("Crash Worker did not observe the exact created Docker attempt")
        observation = DockerWorkerCrashObservation(
            run_id=self.launch.run_id,
            call_id=call_id,
            process_id=os.getpid(),
            worker_id=self.launch.worker_id,
            lease_id=self.launch.lease_id,
            lease_epoch=self.launch.lease_epoch,
            launch_event_seq=self.launch.launch_event_seq,
            agent_session_artifact_ref=self.launch.agent_session_artifact_ref,
            next_iteration=self.launch.next_iteration,
            launch_record_sha256=self.launch_record_sha256,
            image_id=self.image_id,
            container_name=name,
            command_marker_path=_MARKER_PATH,
        )
        _write_fsynced_new(
            self.crash_path,
            (canonical_json(observation) + "\n").encode("utf-8"),
        )
        os._exit(EXIT_AFTER_CREATE)


def main() -> None:
    if len(sys.argv) != 16 or sys.argv[1] not in {"crash", "resume"}:
        raise SystemExit(
            "usage: _docker_agent_worker MODE STORE RUN LEASE WORKER EPOCH ARTIFACTS "
            "WORKSPACE CAMPAIGN STAGING IMAGE CONFIG LAUNCH GATE CRASH_OBSERVATION"
        )
    (
        mode,
        store_arg,
        run_id,
        lease_id,
        worker_id,
        epoch_arg,
        artifacts_arg,
        workspace_arg,
        campaign_arg,
        staging_arg,
        image,
        config_arg,
        launch_arg,
        gate_arg,
        crash_arg,
    ) = sys.argv[1:]

    launch_path = Path(launch_arg)
    launch, launch_content = _read_start_gate(launch_path, Path(gate_arg))
    token = LeaseToken(
        lease_id=lease_id,
        worker_id=worker_id,
        epoch=int(epoch_arg),
    )
    if (
        launch.run_id != run_id
        or launch.process_id != os.getpid()
        or launch.lease_id != token.lease_id
        or launch.worker_id != token.worker_id
        or launch.lease_epoch != token.epoch
    ):
        raise ValueError("Agent Worker process does not match its persisted launch boundary")

    store = SQLiteEventStore(Path(store_arg))
    service = HarnessService(store)
    run = store.get(run_id)
    if run.plan is None or run.agent_session is None or run.model_policy is None:
        raise ValueError("Docker Agent Worker requires a planned active Agent session")
    if (
        run.lease_id != token.lease_id
        or run.worker_id != token.worker_id
        or run.lease_epoch != token.epoch
        or run.agent_session.artifact_ref != launch.agent_session_artifact_ref
        or run.agent_session.next_iteration != launch.next_iteration
    ):
        raise ValueError("Durable Run no longer matches the Agent Worker launch boundary")
    active_item = next(
        item for item in run.plan.items if item.work_item_id == run.agent_session.work_item_id
    )
    provider = ProviderConfig.model_validate(
        yaml.safe_load(Path(config_arg).read_text(encoding="utf-8"))
    )
    artifacts = ArtifactStore(Path(artifacts_arg))
    snapshots = SnapshotManager(artifacts)
    if mode == "crash":
        sandbox = _ExitAfterCreatedAttempt(
            Path(staging_arg),
            image,
            launch=launch,
            launch_record_sha256=_sha256(launch_content),
            crash_path=Path(crash_arg),
        )
        actions = (
            ScriptedModelAction(
                tool="run_check",
                arguments={"check_id": _CHECK_ID},
                input_tokens=12,
                output_tokens=8,
            ),
        )
        arm_id = "docker-agent-crash"
    else:
        sandbox = DockerSandbox(Path(staging_arg), image)
        actions = (
            ScriptedModelAction(
                tool="submit",
                arguments={
                    "summary": "Recovered the reaped Worker and ran fresh protected validation."
                },
                input_tokens=12,
                output_tokens=8,
            ),
        )
        arm_id = "docker-agent-resume"
    if sandbox.image_id != launch.image_id:
        raise ValueError("Docker image identity changed after the Worker launch boundary")

    runner = CodingAgentRunner(
        service,
        ScriptedModelGateway(arm_id, actions, start_index=len(run.model_calls)),
        CampaignBudgetLedger(Path(campaign_arg)),
        WorkspaceToolGateway(
            Path(workspace_arg),
            run.task,
            active_item,
            snapshots,
            DockerAcceptanceExecutor(sandbox),
        ),
        artifacts,
        provider_id=provider.provider_id,
        model_id=provider.model.id,
        pricing=provider.pricing,
        campaign=provider.campaign,
        config=AgentLoopConfig(
            max_model_iterations=run.task.budgets.max_model_calls,
            max_output_tokens=provider.request.max_output_tokens,
            max_run_cost=provider.run_budget.max_cost,
            max_context_chars=provider.request.max_context_chars,
            max_input_tokens=provider.request.max_input_tokens,
            preserve_recent_context_units=provider.request.preserve_recent_context_units,
        ),
    )
    result = runner.run(run_id, token)
    if mode == "crash":
        raise RuntimeError("Crash Worker completed without reaching the injected boundary")
    if result.status != RunStatus.SUCCEEDED:
        raise RuntimeError("Resume Worker did not reach terminal success")


if __name__ == "__main__":
    main()
