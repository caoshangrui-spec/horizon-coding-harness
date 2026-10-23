"""One bounded production Agent slice executed in a directly owned local child process."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from horizon.adapters.model.config import load_provider_config, resolve_credential
from horizon.adapters.model.openai_compatible import OpenAICompatibleModelGateway
from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.retrieval.sqlite_fts import SQLiteCodeRetriever
from horizon.adapters.sandbox.docker import DockerSandbox
from horizon.adapters.sandbox.validation import DockerAcceptanceExecutor
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.agent_loop import AgentLoopConfig, CodingAgentRunner
from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.common import canonical_json, digest
from horizon.domain.states import RunStatus
from horizon.tools.gateway import WorkspaceToolGateway


def _active_plan_item(run):
    if run.plan is None:
        raise ValueError("Agent Worker requires a validated Plan")
    if run.agent_session is not None:
        item = next(
            (
                candidate
                for candidate in run.plan.items
                if candidate.work_item_id == run.agent_session.work_item_id
            ),
            None,
        )
        if (
            item is None
            or item.work_item_id in run.passed_items
            or not set(item.dependencies) <= run.passed_items
        ):
            raise ValueError("Persisted Agent session has no active work item")
        return item
    ready = run.plan.ready_items(run.passed_items)
    if not ready:
        raise ValueError("Agent Worker has no dependency-ready work item")
    return ready[0]


def main() -> None:
    if len(sys.argv) != 20:
        raise SystemExit(
            "usage: _local_agent_worker STORE RUN LEASE WORKER EPOCH LAUNCH_SEQ LAUNCH_ID "
            "SESSION_REF NEXT_ITERATION CONFIG CONFIG_HASH DOTENV ARTIFACTS WORKSPACE CAMPAIGN "
            "STAGING IMAGE RETRIEVAL SLICE_ITERATIONS"
        )
    (
        store_arg,
        run_id,
        lease_id,
        worker_id,
        epoch_arg,
        launch_seq_arg,
        launch_id,
        session_ref_arg,
        next_iteration_arg,
        config_arg,
        provider_config_hash,
        dotenv_arg,
        artifacts_arg,
        workspace_arg,
        campaign_arg,
        staging_arg,
        image,
        retrieval_arg,
        slice_iterations_arg,
    ) = sys.argv[1:]
    token = LeaseToken(
        lease_id=lease_id,
        worker_id=worker_id,
        epoch=int(epoch_arg),
    )
    launch_event_seq = int(launch_seq_arg)
    next_iteration = int(next_iteration_arg)
    slice_iterations = int(slice_iterations_arg)
    if not 1 <= slice_iterations <= 50:
        raise ValueError("Agent Worker slice size must be in [1, 50]")

    # No model, tool, store, or workspace object may be opened until the parent has persisted
    # both the PID-bound launch and its authorization, then releases this one-shot gate.
    start_gate = sys.stdin.buffer.readline().decode("utf-8", errors="strict").rstrip("\r\n")
    if start_gate != launch_id:
        raise SystemExit("Agent Worker start gate was not authorized by its parent")

    store = SQLiteEventStore(Path(store_arg))
    service = HarnessService(store)
    run = store.get(run_id)
    expected_session_ref = None if session_ref_arg == "-" else session_ref_arg
    current_session_ref = run.agent_session.artifact_ref if run.agent_session is not None else None
    current_iteration = run.agent_session.next_iteration if run.agent_session is not None else 0
    process_launch = run.process_worker_launch
    expected_process_launch = {
        "launch_id": launch_id,
        "lease_id": token.lease_id,
        "worker_id": token.worker_id,
        "epoch": token.epoch,
        "process_id": os.getpid(),
        "launch_event_seq": launch_event_seq,
        "source": "local_process_supervisor",
    }
    if (
        run.status != RunStatus.RUNNING
        or run.lease_id != token.lease_id
        or run.worker_id != token.worker_id
        or run.lease_epoch != token.epoch
        or run.reservations
        or run.cancel_requested
        or process_launch is None
        or any(process_launch.get(name) != value for name, value in expected_process_launch.items())
        or process_launch.get("recorded_event_seq") != launch_event_seq + 1
        or process_launch.get("start_authorized") is not True
        or process_launch.get("authorized_event_seq") != launch_event_seq + 2
        or run.seq < process_launch["authorized_event_seq"]
        or current_session_ref != expected_session_ref
        or current_iteration != next_iteration
    ):
        raise ValueError("Durable Run no longer matches the Agent Worker launch boundary")
    if run.task.repository.source != "local" or run.task.repository.path is None:
        raise ValueError("Agent Worker requires a local staging workspace")
    workspace = Path(workspace_arg).resolve(strict=True)
    if workspace != Path(run.task.repository.path).resolve(strict=True):
        raise ValueError("Agent Worker workspace does not match the durable TaskSpec")

    config_path = Path(config_arg).resolve(strict=True)
    provider = load_provider_config(config_path)
    if digest(provider) != provider_config_hash:
        raise ValueError("Agent Worker provider configuration changed after parent validation")
    credential = resolve_credential(provider, Path(dotenv_arg))
    campaign_path = Path(campaign_arg).resolve()
    if campaign_path != Path(provider.ledger_path).resolve():
        raise ValueError("Agent Worker campaign ledger does not match the provider config")

    artifacts = ArtifactStore(Path(artifacts_arg))
    snapshots = SnapshotManager(artifacts)
    tools = WorkspaceToolGateway(
        workspace,
        run.task,
        _active_plan_item(run),
        snapshots,
        DockerAcceptanceExecutor(DockerSandbox(Path(staging_arg), image)),
        SQLiteCodeRetriever(Path(retrieval_arg), snapshots),
    )
    planning_calls = sum(record.purpose == "planning" for record in run.model_calls)
    runner = CodingAgentRunner(
        service,
        OpenAICompatibleModelGateway(provider, credential),
        CampaignBudgetLedger(campaign_path),
        tools,
        artifacts,
        provider_id=provider.provider_id,
        model_id=provider.model.id,
        pricing=provider.pricing,
        campaign=provider.campaign,
        config=AgentLoopConfig(
            max_model_iterations=min(8, run.task.budgets.max_model_calls - planning_calls),
            max_output_tokens=min(512, provider.request.max_output_tokens),
            max_run_cost=provider.run_budget.max_cost,
            enable_thinking=provider.request.enable_thinking,
            max_context_chars=provider.request.max_context_chars,
            max_input_tokens=provider.request.max_input_tokens,
            preserve_recent_context_units=provider.request.preserve_recent_context_units,
        ),
    )
    result = runner.run(
        run_id,
        token,
        max_iterations_this_invocation=slice_iterations,
    )
    report = {
        "schema_version": "1.0",
        "run_id": result.run_id,
        "status": result.status,
        "process_id": os.getpid(),
        "check_cleanup_failures": tools.check_cleanup_failures,
    }
    sys.stdout.write(canonical_json(report))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
