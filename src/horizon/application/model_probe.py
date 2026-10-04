from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from horizon.domain.common import canonical_json
from horizon.domain.errors import ProviderError
from horizon.domain.model import (
    CONSERVATIVE_INPUT_TOKEN_ESTIMATOR,
    OPENAI_PAYLOAD_INPUT_TOKEN_ESTIMATOR,
    CampaignBudget,
    InputTokenEstimate,
    ModelMessage,
    ModelProbeResult,
    ModelRequest,
    ModelRequestPayloadEvidence,
    PriceCard,
    ToolDefinition,
)
from horizon.domain.ports import CampaignBudgetPort, ModelGatewayPort

PROBE_TOOL_NAME = "horizon_probe"
PROBE_VALUE = "HORIZON_OK"


def build_probe_request(
    model: str,
    *,
    max_output_tokens: int,
    enable_thinking: bool,
) -> ModelRequest:
    return ModelRequest(
        model=model,
        messages=(
            ModelMessage(
                role="user",
                content=(
                    "Call the horizon_probe tool exactly once with value HORIZON_OK. "
                    "Do not answer in natural language."
                ),
            ),
        ),
        tools=(
            ToolDefinition(
                name=PROBE_TOOL_NAME,
                description="Return a fixed connectivity probe value.",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            ),
        ),
        tool_choice="auto",
        max_output_tokens=max_output_tokens,
        temperature=Decimal("0"),
        enable_thinking=enable_thinking,
    )


def legacy_input_estimate(request: ModelRequest) -> InputTokenEstimate:
    """Rebuild the v1 domain-contract estimate for recovery of historical Runs."""

    request_bytes = len(canonical_json(request).encode("utf-8"))
    return InputTokenEstimate(
        estimator=CONSERVATIVE_INPUT_TOKEN_ESTIMATOR,
        request_bytes=request_bytes,
        token_ceiling=2 * request_bytes + 1024,
    )


def conservative_input_sizing(
    request: ModelRequest,
) -> tuple[InputTokenEstimate, ModelRequestPayloadEvidence]:
    """Measure the exact outbound body and return its conservative token upper bound."""

    payload = request.openai_compatible_payload_evidence()
    return (
        InputTokenEstimate(
            estimator=OPENAI_PAYLOAD_INPUT_TOKEN_ESTIMATOR,
            request_bytes=payload.payload_bytes,
            token_ceiling=2 * payload.payload_bytes + 1024,
        ),
        payload,
    )


def conservative_input_estimate(request: ModelRequest) -> InputTokenEstimate:
    """Return a reproducible upper bound, not a provider-tokenizer prediction."""

    estimate, _ = conservative_input_sizing(request)
    return estimate


def conservative_input_ceiling(request: ModelRequest) -> int:
    # Token count is bounded by source bytes. Doubling covers JSON-in-JSON escaping for tool
    # arguments; the fixed allowance covers provider framing and special tokens.
    return conservative_input_estimate(request).token_ceiling


class ModelProbeService:
    def __init__(self, gateway: ModelGatewayPort, ledger: CampaignBudgetPort):
        self.gateway = gateway
        self.ledger = ledger

    def run(
        self,
        *,
        provider_id: str,
        request: ModelRequest,
        pricing: PriceCard,
        budget: CampaignBudget,
    ) -> ModelProbeResult:
        self.ledger.initialize(budget, provider_id=provider_id, model_id=request.model)
        reserved_cost = pricing.reserve_cost(
            conservative_input_ceiling(request),
            request.max_output_tokens,
        )
        attempt_id = f"attempt_{uuid4().hex}"
        trace_id = f"horizon-{uuid4().hex}"
        self.ledger.reserve(budget, attempt_id, request.sha256, reserved_cost)
        try:
            response = self.gateway.generate(request, trace_id)
        except ProviderError as exc:
            # Once dispatch begins, failure is not proof of zero billing.
            self.ledger.mark_unknown(budget.campaign_id, attempt_id, type(exc).__name__)
            raise
        estimated_cost = pricing.cost_for(response.usage)
        campaign = self.ledger.settle(
            budget.campaign_id,
            attempt_id,
            estimated_cost,
            response.provider_trace_id,
        )
        calls = response.message.tool_calls
        passed = (
            len(calls) == 1
            and calls[0].function.name == PROBE_TOOL_NAME
            and calls[0].function.arguments == {"value": PROBE_VALUE}
        )
        return ModelProbeResult(
            passed=passed,
            provider_id=provider_id,
            model=response.model,
            response_id=response.response_id,
            finish_reason=response.finish_reason,
            provider_trace_id=response.provider_trace_id,
            tool_name=calls[0].function.name if len(calls) == 1 else None,
            usage=response.usage,
            estimated_cost=estimated_cost,
            currency=pricing.currency,
            campaign=campaign,
        )
