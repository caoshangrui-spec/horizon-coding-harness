"""Run one frozen write action and exit after its effect but before its receipt."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from horizon.adapters.model.scripted import ScriptedModelGateway
from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.retrieval.sqlite_fts import SQLiteCodeRetriever
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.agent_loop import AgentLoopConfig, CodingAgentRunner
from horizon.application.run_ab_eval import _OFFLINE_PRICE
from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.common import canonical_json
from horizon.domain.model import CampaignBudget
from horizon.domain.run_evaluation import (
    RUN_AB_HARD_CRASH_EXIT_CODE,
    ScriptedModelAction,
)
from horizon.tools.gateway import WorkspaceToolGateway


class _UnexpectedAcceptanceExecutor:
    def execute(self, workspace, check):
        raise RuntimeError("Run A/B crash worker must stop before protected validation")


class _ExitAfterWriteEffect:
    def __init__(
        self,
        delegate: WorkspaceToolGateway,
        marker_path: Path,
        *,
        expected_tool: str,
        model_call_index: int,
        scripted_action_ref: str,
    ):
        self.delegate = delegate
        self.marker_path = marker_path
        self.expected_tool = expected_tool
        self.model_call_index = model_call_index
        self.scripted_action_ref = scripted_action_ref

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)

    def dispatch_safe(self, name: str, arguments: dict, attempt_id: str | None = None):
        outcome = self.delegate.dispatch_safe(name, arguments, attempt_id)
        if name != self.expected_tool:
            raise ValueError("Run A/B crash worker received an unexpected tool")
        if outcome.status != "success" or not attempt_id:
            raise ValueError("Run A/B crash worker requires a successful durable write effect")
        payload = (
            canonical_json(
                {
                    "attempt_id": attempt_id,
                    "exit_code": RUN_AB_HARD_CRASH_EXIT_CODE,
                    "model_call_index": self.model_call_index,
                    "pid": os.getpid(),
                    "scripted_action_ref": self.scripted_action_ref,
                    "tool": name,
                    "workspace_revision_after": outcome.workspace_revision_after,
                }
            )
            + "\n"
        ).encode("utf-8")
        with self.marker_path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os._exit(RUN_AB_HARD_CRASH_EXIT_CODE)


def main() -> None:
    if len(sys.argv) != 15:
        raise SystemExit(
            "usage: _run_ab_crash_worker STORE RUN LEASE WORKER EPOCH ARTIFACTS "
            "WORKSPACE CAMPAIGN RETRIEVAL MARKER ACTION_REF ARM_ID ACTION_INDEX TOTAL_ACTIONS"
        )
    (
        store_arg,
        run_id,
        lease_id,
        worker_id,
        epoch_arg,
        artifacts_arg,
        workspace_arg,
        campaign_arg,
        retrieval_arg,
        marker_arg,
        action_ref,
        arm_id,
        action_index_arg,
        total_actions_arg,
    ) = sys.argv[1:]
    store = SQLiteEventStore(Path(store_arg))
    service = HarnessService(store)
    run = store.get(run_id)
    if run.plan is None or run.agent_session is None or run.model_policy is None:
        raise ValueError("Run A/B crash worker requires a planned active Agent session")
    active_item = next(
        (item for item in run.plan.items if item.work_item_id == run.agent_session.work_item_id),
        None,
    )
    if active_item is None:
        raise ValueError("Run A/B crash worker cannot find its active WorkItem")

    artifacts = ArtifactStore(Path(artifacts_arg))
    action = ScriptedModelAction.model_validate_json(artifacts.read(action_ref))
    snapshots = SnapshotManager(artifacts)
    workspace = Path(workspace_arg)
    gateway = WorkspaceToolGateway(
        workspace,
        run.task,
        active_item,
        snapshots,
        _UnexpectedAcceptanceExecutor(),
        SQLiteCodeRetriever(Path(retrieval_arg), snapshots),
    )
    action_index = int(action_index_arg)
    total_actions = int(total_actions_arg)
    campaign = CampaignBudget(
        campaign_id=run.model_policy.campaign_id,
        currency=run.model_policy.currency,
        max_cost="10.00",
        max_cost_per_call="1.00",
    )
    runner = CodingAgentRunner(
        service,
        ScriptedModelGateway(arm_id, (action,), start_index=action_index - 1),
        CampaignBudgetLedger(Path(campaign_arg)),
        _ExitAfterWriteEffect(
            gateway,
            Path(marker_arg),
            expected_tool=action.tool,
            model_call_index=action_index,
            scripted_action_ref=action_ref,
        ),
        artifacts,
        provider_id=run.model_policy.provider_id,
        model_id=run.model_policy.model,
        pricing=_OFFLINE_PRICE,
        campaign=campaign,
        config=AgentLoopConfig(
            max_model_iterations=total_actions,
            max_output_tokens=256,
            max_run_cost=run.model_policy.max_run_cost,
        ),
    )
    runner.run(
        run_id,
        LeaseToken(
            lease_id=lease_id,
            worker_id=worker_id,
            epoch=int(epoch_arg),
        ),
        max_iterations_this_invocation=1,
    )
    raise RuntimeError("Run A/B crash worker completed without reaching the crash boundary")


if __name__ == "__main__":
    main()
