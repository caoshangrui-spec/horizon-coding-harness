"""Internal subprocess for the offline portfolio hard-crash demonstration."""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

from horizon.adapters.model.scripted import ScriptedModelGateway
from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.retrieval.sqlite_fts import SQLiteCodeRetriever
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.agent_loop import AgentLoopConfig, CodingAgentRunner
from horizon.application.portfolio_demo import (
    _OFFLINE_PRICE,
    _WRITE_PATH,
    _WRITE_PREIMAGE,
    _ParserAcceptanceExecutor,
)
from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.common import canonical_json
from horizon.domain.model import CampaignBudget
from horizon.domain.portfolio_demo import PORTFOLIO_CRASH_EXIT_CODE
from horizon.domain.run_evaluation import ScriptedModelAction
from horizon.tools.gateway import WorkspaceToolGateway


class _ExitAfterReplaceEffect:
    def __init__(self, delegate: WorkspaceToolGateway, marker_path: Path):
        self.delegate = delegate
        self.marker_path = marker_path

    def __getattr__(self, name: str):
        return getattr(self.delegate, name)

    def dispatch_safe(self, name: str, arguments: dict, attempt_id: str | None = None):
        result = self.delegate.dispatch_safe(name, arguments, attempt_id)
        if name == "replace_text":
            if not attempt_id:
                raise ValueError("Portfolio crash worker requires a durable tool attempt ID")
            payload = (
                canonical_json(
                    {
                        "attempt_id": attempt_id,
                        "exit_code": PORTFOLIO_CRASH_EXIT_CODE,
                        "pid": os.getpid(),
                        "tool": name,
                    }
                )
                + "\n"
            ).encode("utf-8")
            with self.marker_path.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os._exit(PORTFOLIO_CRASH_EXIT_CODE)
        return result


def main() -> None:
    if len(sys.argv) != 11:
        raise SystemExit(
            "usage: _portfolio_crash_worker STORE RUN LEASE WORKER EPOCH ARTIFACTS "
            "WORKSPACE CAMPAIGN RETRIEVAL MARKER"
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
    ) = sys.argv[1:]
    store = SQLiteEventStore(Path(store_arg))
    service = HarnessService(store)
    run = store.get(run_id)
    if run.plan is None or run.agent_session is None or run.model_policy is None:
        raise ValueError("Portfolio crash worker requires a planned active Agent session")
    active_item = next(
        (item for item in run.plan.items if item.work_item_id == run.agent_session.work_item_id),
        None,
    )
    if active_item is None:
        raise ValueError("Portfolio crash worker cannot find its active WorkItem")

    artifacts = ArtifactStore(Path(artifacts_arg))
    snapshots = SnapshotManager(artifacts)
    workspace = Path(workspace_arg)
    gateway = WorkspaceToolGateway(
        workspace,
        run.task,
        active_item,
        snapshots,
        _ParserAcceptanceExecutor(),
        SQLiteCodeRetriever(Path(retrieval_arg), snapshots),
    )
    actions = (
        ScriptedModelAction(
            tool="read_file",
            arguments={"path": "src/parser.py", "start_line": 1, "end_line": 2},
        ),
        ScriptedModelAction(
            tool="replace_text",
            arguments={
                "path": _WRITE_PATH,
                "old": _WRITE_PREIMAGE,
                "new": "return [] if value == '' else [value]",
            },
        ),
    )
    campaign = CampaignBudget(
        campaign_id=run.model_policy.campaign_id,
        currency=run.model_policy.currency,
        max_cost="10.00",
        max_cost_per_call="1.00",
    )
    runner = CodingAgentRunner(
        service,
        ScriptedModelGateway("portfolio", actions, start_index=2),
        CampaignBudgetLedger(Path(campaign_arg)),
        _ExitAfterReplaceEffect(gateway, Path(marker_arg)),
        artifacts,
        provider_id=run.model_policy.provider_id,
        model_id=run.model_policy.model,
        pricing=_OFFLINE_PRICE,
        campaign=campaign,
        config=AgentLoopConfig(
            max_model_iterations=6,
            max_output_tokens=256,
            max_run_cost=Decimal("10.00"),
        ),
    )
    runner.run(
        run_id,
        LeaseToken(
            lease_id=lease_id,
            worker_id=worker_id,
            epoch=int(epoch_arg),
        ),
    )
    raise RuntimeError("Portfolio crash worker completed without reaching the crash boundary")


if __name__ == "__main__":
    main()
