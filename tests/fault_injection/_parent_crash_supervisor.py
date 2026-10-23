"""Parent controller used to prove durable child identity survives a hard parent exit."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.application.local_process_supervision import LocalProcessAgentSupervisor
from horizon.application.recovery import RecoveryService
from horizon.application.services import HarnessService, LeaseToken
from horizon.application.supervision import SequentialSupervisorConfig


def main() -> None:
    if len(sys.argv) != 12:
        raise SystemExit(
            "usage: _parent_crash_supervisor STORE RUN LEASE WORKER EPOCH ARTIFACTS "
            "CAMPAIGN READY STOP STOPPED CHILD"
        )
    (
        store_arg,
        run_id,
        lease_id,
        worker_id,
        epoch_arg,
        artifacts_arg,
        campaign_arg,
        ready_arg,
        stop_arg,
        stopped_arg,
        child_arg,
    ) = sys.argv[1:]
    store_path = Path(store_arg)

    def launch(boundary, slice_iterations):
        if slice_iterations != 1:
            raise ValueError("parent-crash fixture requires one-iteration slices")
        child_env = os.environ.copy()
        return subprocess.Popen(
            [
                getattr(sys, "_base_executable", sys.executable),
                child_arg,
                boundary.launch_id,
                ready_arg,
                stop_arg,
                stopped_arg,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
        )

    service = HarnessService(SQLiteEventStore(store_path))
    LocalProcessAgentSupervisor(
        lambda: HarnessService(SQLiteEventStore(store_path)),
        launch,
        lambda active_service: RecoveryService(
            active_service,
            CampaignBudgetLedger(Path(campaign_arg)),
            ArtifactStore(Path(artifacts_arg)),
        ),
        config=SequentialSupervisorConfig(slice_iterations=1, max_worker_slices=1),
    ).run(
        run_id,
        service,
        LeaseToken(lease_id=lease_id, worker_id=worker_id, epoch=int(epoch_arg)),
    )


if __name__ == "__main__":
    main()
