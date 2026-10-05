"""Exit after a model response returns but before its Artifact is published."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.agent_loop import AgentLoopConfig, CodingAgentRunner
from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.model import (
    CampaignBudget,
    FunctionCall,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    PriceCard,
    ToolCall,
)
from horizon.domain.recovery_evaluation import (
    MODEL_RESPONSE_CRASH_EXIT_CODE,
    MODEL_RESPONSE_CRASH_RESPONSE_ID,
)
from horizon.tools.gateway import WorkspaceToolGateway


class ReturnedResponseModel:
    def __init__(self, marker: Path):
        self.marker = marker

    def generate(self, request, trace_id):
        with self.marker.open("x", encoding="utf-8") as stream:
            stream.write(trace_id)
            stream.flush()
            os.fsync(stream.fileno())
        return ModelResponse(
            response_id=MODEL_RESPONSE_CRASH_RESPONSE_ID,
            model=request.model,
            message=ModelMessage(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        id="hard-exit-preartifact-provider-call",
                        function=FunctionCall(
                            name="read_file",
                            arguments={"path": "src/parser.py"},
                        ),
                    ),
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(input_tokens=101, output_tokens=20),
            provider_trace_id="hard-exit-preartifact-provider-trace",
        )


class ExitBeforeResponseArtifact:
    def __init__(self, delegate):
        self.delegate = delegate

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def put(self, content):
        if f'"response_id":"{MODEL_RESPONSE_CRASH_RESPONSE_ID}"'.encode() in content:
            os._exit(MODEL_RESPONSE_CRASH_EXIT_CODE)
        return self.delegate.put(content)


def main() -> None:
    def clock():
        return datetime.fromisoformat(sys.argv[7])

    store = SQLiteEventStore(Path(sys.argv[1]), clock=clock)
    service = HarnessService(store)
    run = store.get(sys.argv[2])
    token = LeaseToken(
        lease_id=sys.argv[3],
        worker_id=sys.argv[4],
        epoch=int(sys.argv[5]),
    )
    artifacts = ArtifactStore(Path(sys.argv[6]))
    assert run.plan is not None
    tools = WorkspaceToolGateway(
        Path(sys.argv[8]),
        run.task,
        run.plan.items[0],
        SnapshotManager(artifacts),
        None,
    )
    runner = CodingAgentRunner(
        service,
        ReturnedResponseModel(Path(sys.argv[10])),
        CampaignBudgetLedger(Path(sys.argv[9]), clock=clock),
        tools,
        ExitBeforeResponseArtifact(artifacts),
        provider_id="fake-provider",
        model_id="fake-model",
        pricing=PriceCard(
            currency="CNY",
            input_per_million="3.00",
            cached_input_per_million="0.30",
            output_per_million="9.00",
            version="hard-exit-test",
            source_url="https://example.test/pricing",
        ),
        campaign=CampaignBudget(
            campaign_id="hard-exit-agent",
            currency="CNY",
            max_cost="3.00",
            max_cost_per_call="1.00",
        ),
        config=AgentLoopConfig(max_model_iterations=8, max_output_tokens=256),
    )
    runner.run(run.run_id, token)


if __name__ == "__main__":
    main()
