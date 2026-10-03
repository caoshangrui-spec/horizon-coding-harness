from __future__ import annotations

from horizon.domain.errors import ProviderError
from horizon.domain.model import (
    FunctionCall,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
)
from horizon.domain.run_evaluation import ScriptedModelAction


class ScriptedModelGateway:
    """Deterministic offline model adapter used only by frozen harness evaluations."""

    def __init__(
        self,
        arm_id: str,
        actions: tuple[ScriptedModelAction, ...],
        *,
        start_index: int = 0,
    ):
        if start_index < 0:
            raise ValueError("Scripted model start index cannot be negative")
        self.arm_id = arm_id
        self.actions = list(actions)
        self.start_index = start_index
        self.requests: list[ModelRequest] = []

    @property
    def consumed(self) -> bool:
        return not self.actions

    def generate(self, request: ModelRequest, trace_id: str) -> ModelResponse:
        if not self.actions:
            raise ProviderError("Frozen scripted model action list is exhausted")
        action = self.actions.pop(0)
        available = {tool.name for tool in request.tools}
        if action.tool not in available:
            raise ProviderError(
                f"Frozen scripted action {action.tool!r} is absent from the current Tool Schema"
            )
        self.requests.append(request)
        index = self.start_index + len(self.requests)
        return ModelResponse(
            response_id=f"{self.arm_id}-response-{index}",
            model=request.model,
            message=ModelMessage(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        id=f"{self.arm_id}-tool-{index}",
                        function=FunctionCall(
                            name=action.tool,
                            arguments=action.arguments,
                        ),
                    ),
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(
                input_tokens=action.input_tokens,
                output_tokens=action.output_tokens,
            ),
            provider_trace_id=f"offline-{self.arm_id}-{index}-{trace_id[-8:]}",
        )
