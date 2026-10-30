"""Child process fixtures for reaped-worker Supervisor fault injection."""

from __future__ import annotations

import hashlib
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

from horizon.adapters.model.scripted import ScriptedModelGateway
from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.retrieval.sqlite_fts import SQLiteCodeRetriever
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.agent_loop import AgentLoopConfig, CodingAgentRunner
from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.common import digest
from horizon.domain.human import HumanContextRequest
from horizon.domain.model import CampaignBudget, PriceCard
from horizon.domain.run_evaluation import ScriptedModelAction
from horizon.domain.states import RunStatus
from horizon.domain.tools import AcceptanceResult, ToolCallReservation
from horizon.tools.gateway import WorkspaceToolGateway

SAFE_SLICE_EXIT_CODE = 37
PENDING_INTENT_EXIT_CODE = 38
IDLE_EXIT_CODE = 39
CONTEXT_STOP_EXIT_CODE = 40


class _ParserCheck:
    def execute(self, workspace, check):
        content = (workspace / "src/parser.py").read_text(encoding="utf-8")
        passed = "return [] if value == '' else [value]" in content
        output = "1 passed" if passed else "expected [] for empty input"
        return AcceptanceResult(
            check_id=check.id,
            passed=passed,
            exit_code=0 if passed else 1,
            timed_out=False,
            output=output,
            output_hash=hashlib.sha256(output.encode()).hexdigest(),
        )


def _token() -> LeaseToken:
    return LeaseToken(
        lease_id=sys.argv[4],
        worker_id=sys.argv[5],
        epoch=int(sys.argv[6]),
    )


def _safe_slice(service: HarnessService, run_id: str, token: LeaseToken) -> None:
    run = service.store.get(run_id)
    if run.plan is None:
        raise ValueError("Safe-slice worker requires a validated Plan")
    artifacts = ArtifactStore(Path(sys.argv[7]))
    snapshots = SnapshotManager(artifacts)
    workspace = Path(sys.argv[8])
    runner = CodingAgentRunner(
        service,
        ScriptedModelGateway(
            "reaped-safe-slice",
            (
                ScriptedModelAction(
                    tool="read_file",
                    arguments={"path": "src/parser.py", "start_line": 1, "end_line": 2},
                ),
            ),
        ),
        CampaignBudgetLedger(Path(sys.argv[9])),
        WorkspaceToolGateway(
            workspace,
            run.task,
            run.plan.items[0],
            snapshots,
            None,
            SQLiteCodeRetriever(Path(sys.argv[10]), snapshots),
        ),
        artifacts,
        provider_id="fake-provider",
        model_id="fake-model",
        pricing=PriceCard(
            currency="CNY",
            input_per_million="3.00",
            cached_input_per_million="0.30",
            output_per_million="9.00",
            version="test-price",
            source_url="https://example.test/pricing",
        ),
        campaign=CampaignBudget(
            campaign_id="fake-agent-loop",
            currency="CNY",
            max_cost="3.00",
            max_cost_per_call="1.00",
        ),
        config=AgentLoopConfig(
            max_model_iterations=8,
            max_output_tokens=256,
            max_run_cost=Decimal("1.00"),
        ),
    )
    result = runner.run(run_id, token, max_iterations_this_invocation=1)
    if result.agent_session is None or result.agent_session.next_iteration != 2:
        raise ValueError("Safe-slice worker did not persist the expected Agent boundary")
    os._exit(SAFE_SLICE_EXIT_CODE)


def _pending_intent(service: HarnessService, run_id: str, token: LeaseToken) -> None:
    service.reserve_tool_call(
        run_id,
        ToolCallReservation(
            call_id="reaped-pending-read",
            name="read_file",
            arguments_hash=digest({"path": "src/parser.py"}),
            workspace_revision=None,
        ),
        token,
        "reaped-pending-read",
    )
    os._exit(PENDING_INTENT_EXIT_CODE)


def _sequence_slice(service: HarnessService, run_id: str, token: LeaseToken) -> None:
    run = service.store.get(run_id)
    if run.plan is None:
        raise ValueError("Sequence Worker requires a validated Plan")
    completed_calls = len(run.model_calls)
    actions = (
        ScriptedModelAction(
            tool="read_file",
            arguments={"path": "src/parser.py", "start_line": 1, "end_line": 2},
        ),
        ScriptedModelAction(
            tool="replace_text",
            arguments={
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            },
        ),
        ScriptedModelAction(
            tool="submit",
            arguments={"summary": "Process-supervised parser fix."},
        ),
    )
    artifacts = ArtifactStore(Path(sys.argv[7]))
    snapshots = SnapshotManager(artifacts)
    runner = CodingAgentRunner(
        service,
        ScriptedModelGateway(
            "local-process-sequence",
            actions[completed_calls:],
            start_index=completed_calls,
        ),
        CampaignBudgetLedger(Path(sys.argv[9])),
        WorkspaceToolGateway(
            Path(sys.argv[8]),
            run.task,
            run.plan.items[0],
            snapshots,
            _ParserCheck(),
            SQLiteCodeRetriever(Path(sys.argv[10]), snapshots),
        ),
        artifacts,
        provider_id="fake-provider",
        model_id="fake-model",
        pricing=PriceCard(
            currency="CNY",
            input_per_million="3.00",
            cached_input_per_million="0.30",
            output_per_million="9.00",
            version="test-price",
            source_url="https://example.test/pricing",
        ),
        campaign=CampaignBudget(
            campaign_id="fake-agent-loop",
            currency="CNY",
            max_cost="3.00",
            max_cost_per_call="1.00",
        ),
        config=AgentLoopConfig(
            max_model_iterations=8,
            max_output_tokens=256,
            max_run_cost=Decimal("1.00"),
        ),
    )
    runner.run(run_id, token, max_iterations_this_invocation=1)


def _context_stop(service: HarnessService, run_id: str, token: LeaseToken) -> None:
    run = service.store.get(run_id)
    if run.plan is None:
        raise ValueError("Context-stop Worker requires a validated Plan")
    artifacts = ArtifactStore(Path(sys.argv[7]))
    snapshots = SnapshotManager(artifacts)
    runner = CodingAgentRunner(
        service,
        ScriptedModelGateway(
            "context-stop-must-not-dispatch",
            (
                ScriptedModelAction(
                    tool="read_file",
                    arguments={"path": "src/parser.py", "start_line": 1, "end_line": 2},
                ),
            ),
        ),
        CampaignBudgetLedger(Path(sys.argv[9])),
        WorkspaceToolGateway(
            Path(sys.argv[8]),
            run.task,
            run.plan.items[0],
            snapshots,
            _ParserCheck(),
            SQLiteCodeRetriever(Path(sys.argv[10]), snapshots),
        ),
        artifacts,
        provider_id="fake-provider",
        model_id="fake-model",
        pricing=PriceCard(
            currency="CNY",
            input_per_million="3.00",
            cached_input_per_million="0.30",
            output_per_million="9.00",
            version="test-price",
            source_url="https://example.test/pricing",
        ),
        campaign=CampaignBudget(
            campaign_id="fake-agent-loop",
            currency="CNY",
            max_cost="3.00",
            max_cost_per_call="1.00",
        ),
        config=AgentLoopConfig(
            max_model_iterations=8,
            max_output_tokens=256,
            max_run_cost=Decimal("1.00"),
            max_context_chars=2_000,
            max_input_tokens=1_000_000,
        ),
    )
    stopped = runner.run(run_id, token, max_iterations_this_invocation=1)
    if (
        stopped.status != RunStatus.WAITING_FOR_USER
        or not isinstance(stopped.pending_human_request, HumanContextRequest)
        or stopped.lease_id is not None
        or stopped.model_calls
        or stopped.reservations
    ):
        raise ValueError("Context-stop Worker did not persist a clean capacity boundary")
    os._exit(CONTEXT_STOP_EXIT_CODE)


def _wait_for_parent(*, mode: str) -> None:
    ready = Path(sys.argv[7]).parent / f"{mode}.ready"
    ready.write_text(str(os.getpid()), encoding="utf-8")
    while True:
        time.sleep(60)


def main() -> None:
    if len(sys.argv) != 11 or sys.argv[1] not in {
        "safe",
        "sequence",
        "context_stop",
        "pending",
        "idle",
        "wait",
        "pending_wait",
    }:
        raise SystemExit(
            "usage: _reaped_supervisor_worker MODE STORE RUN LEASE WORKER EPOCH "
            "ARTIFACTS WORKSPACE CAMPAIGN RETRIEVAL"
        )
    mode = sys.argv[1]
    expected_gate = os.environ.get("HORIZON_TEST_PROCESS_START_GATE")
    if expected_gate is not None:
        observed_gate = sys.stdin.buffer.readline().decode("utf-8").rstrip("\r\n")
        if observed_gate != expected_gate:
            raise SystemExit("test Process Worker did not receive its exact start gate")
    service = HarnessService(SQLiteEventStore(Path(sys.argv[2])))
    run_id = sys.argv[3]
    token = _token()
    if mode == "sequence":
        _sequence_slice(service, run_id, token)
        return
    if mode == "context_stop":
        _context_stop(service, run_id, token)
    if mode == "safe":
        _safe_slice(service, run_id, token)
    if mode == "idle":
        os._exit(IDLE_EXIT_CODE)
    if mode == "wait":
        _wait_for_parent(mode=mode)
    if mode == "pending_wait":
        service.reserve_tool_call(
            run_id,
            ToolCallReservation(
                call_id="reaped-pending-read",
                name="read_file",
                arguments_hash=digest({"path": "src/parser.py"}),
                workspace_revision=None,
            ),
            token,
            "reaped-pending-read",
        )
        _wait_for_parent(mode=mode)
    _pending_intent(service, run_id, token)


if __name__ == "__main__":
    main()
