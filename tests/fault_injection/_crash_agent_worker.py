"""Subprocess worker used only by fault-injection tests."""

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
from horizon.tools.gateway import WorkspaceToolGateway


class WriteModel:
    def __init__(self, tool_name: str):
        self.tool_name = tool_name

    def generate(self, request, trace_id):
        if self.tool_name == "create_file":
            arguments = {
                "path": "src/generated.py",
                "content": "def generated():\n    return '你好'\n",
            }
        elif self.tool_name == "apply_patch":
            arguments = {
                "edits": [
                    {
                        "path": "src/parser.py",
                        "old": "return [value]",
                        "new": "return [] if value == '' else [value]",
                    },
                    {
                        "path": "tests/test_parser.py",
                        "old": "# protected by controller",
                        "new": "# protected by controller\n# patch effect",
                    },
                ]
            }
        else:
            arguments = {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            }
        return ModelResponse(
            response_id="hard-exit-write-response",
            model=request.model,
            message=ModelMessage(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        id="hard-exit-write-provider-call",
                        function=FunctionCall(
                            name=self.tool_name,
                            arguments=arguments,
                        ),
                    ),
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(input_tokens=101, output_tokens=20),
            provider_trace_id="hard-exit-write-trace",
        )


class ExitAfterWriteEffect:
    def __init__(self, delegate):
        self.delegate = delegate

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def dispatch_safe(self, name, arguments, attempt_id=None):
        self.delegate.dispatch_safe(name, arguments, attempt_id)
        os._exit(26)


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
    real_tools = WorkspaceToolGateway(
        Path(sys.argv[8]),
        run.task,
        run.plan.items[0],
        SnapshotManager(artifacts),
        None,
    )
    tool_name = sys.argv[10] if len(sys.argv) > 10 else "replace_text"
    runner = CodingAgentRunner(
        service,
        WriteModel(tool_name),
        CampaignBudgetLedger(Path(sys.argv[9]), clock=clock),
        ExitAfterWriteEffect(real_tools),
        artifacts,
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
