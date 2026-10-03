from decimal import Decimal
from pathlib import Path

import pytest

from horizon.adapters.model.config import load_provider_config
from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.application.model_probe import ModelProbeService, build_probe_request
from horizon.domain.errors import ProviderConnectionError
from horizon.domain.model import (
    FunctionCall,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    ToolCall,
)

CONFIG = Path(__file__).resolve().parents[2] / "config/providers/siliconflow.yaml"


class SuccessfulGateway:
    def generate(self, request, trace_id):
        return ModelResponse(
            response_id="response-1",
            model=request.model,
            message=ModelMessage(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        function=FunctionCall(
                            name="horizon_probe",
                            arguments={"value": "HORIZON_OK"},
                        ),
                    ),
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(input_tokens=100, output_tokens=20),
            provider_trace_id="provider-trace",
        )


class FailingGateway:
    def generate(self, request, trace_id):
        raise ProviderConnectionError("timed out")


def request(config):
    return build_probe_request(
        config.model.id,
        max_output_tokens=config.request.probe_max_output_tokens,
        enable_thinking=config.request.enable_thinking,
    )


def test_probe_reserves_then_settles_price_card_cost(tmp_path):
    config = load_provider_config(CONFIG)
    ledger = CampaignBudgetLedger(tmp_path / "campaign.sqlite3")
    result = ModelProbeService(SuccessfulGateway(), ledger).run(
        provider_id=config.provider_id,
        request=request(config),
        pricing=config.pricing,
        budget=config.campaign,
    )
    assert result.passed is True
    assert result.estimated_cost == Decimal("0.00048")
    assert result.campaign.settled_cost == Decimal("0.00048")
    assert result.campaign.reserved_cost == 0


def test_dispatched_failure_becomes_unknown_and_is_not_refunded(tmp_path):
    config = load_provider_config(CONFIG)
    ledger = CampaignBudgetLedger(tmp_path / "campaign.sqlite3")
    with pytest.raises(ProviderConnectionError):
        ModelProbeService(FailingGateway(), ledger).run(
            provider_id=config.provider_id,
            request=request(config),
            pricing=config.pricing,
            budget=config.campaign,
        )
    summary = ledger.summary(config.campaign.campaign_id)
    assert summary.unknown_cost > 0
    assert summary.remaining_cost < config.campaign.max_cost
