from copy import deepcopy

import pytest
from pydantic import ValidationError

from horizon.reliability.fallback import (
    FallbackRecord,
    ModelCapabilityProfile,
    ModelFallbackDecision,
    ModelFallbackPolicy,
    ModelFallbackRequest,
    ModelTarget,
    decide_model_fallback,
)

TASK_HASH = "a" * 64
ACCEPTANCE_HASH = "b" * 64
PRIMARY = ModelTarget(provider_id="primary-provider", model_id="model-primary")
FALLBACK = ModelTarget(provider_id="fallback-provider", model_id="model-fallback")


def profile(
    target: ModelTarget,
    *,
    tools=True,
    structured=True,
    tokens=100_000,
    boundary="public-code",
    currency="CNY",
    api_type="openai_chat_completions",
):
    return ModelCapabilityProfile(
        target=target,
        api_type=api_type,
        supports_tools=tools,
        supports_structured_output=structured,
        max_input_tokens=tokens,
        data_boundary=boundary,
        currency=currency,
    )


def request_dict():
    return {
        "primary": profile(PRIMARY).model_dump(mode="json"),
        "candidate": profile(FALLBACK).model_dump(mode="json"),
        "required_tools": True,
        "required_structured_output": True,
        "required_input_tokens": 80_000,
        "allowed_provider_ids": ["fallback-provider"],
        "required_data_boundary": "public-code",
        "candidate_reservation_cost": "0.10",
        "remaining_run_cost": "0.20",
        "remaining_campaign_cost": "0.30",
        "original_cost_status": "unknown_reserved",
        "original_cost_retained": True,
        "task_spec_hash": TASK_HASH,
        "effective_task_spec_hash": TASK_HASH,
        "required_acceptance_hash": ACCEPTANCE_HASH,
        "effective_required_acceptance_hash": ACCEPTANCE_HASH,
    }


def enabled_policy(**updates):
    raw = {
        "enabled": True,
        "approved_targets": [FALLBACK.model_dump(mode="json")],
        "comparable_evaluation": False,
    }
    raw.update(updates)
    return ModelFallbackPolicy.model_validate(raw)


def decision(*, request_updates=None, policy=None):
    raw = deepcopy(request_dict())
    for key, value in (request_updates or {}).items():
        raw[key] = value
    return decide_model_fallback(
        policy or enabled_policy(),
        ModelFallbackRequest.model_validate(raw),
    )


def test_exactly_one_preapproved_compatible_candidate_can_pass_policy():
    result = decision()

    assert result.allowed is True
    assert result.reason_code == "allowed"
    assert result.execution_profile_changed is True
    with pytest.raises(ValidationError, match="permission must match"):
        ModelFallbackDecision(
            allowed=True,
            reason_code="disabled",
            execution_profile_changed=True,
        )


@pytest.mark.parametrize(
    ("request_updates", "policy", "reason"),
    [
        ({}, ModelFallbackPolicy(enabled=False), "disabled"),
        ({}, enabled_policy(comparable_evaluation=True), "comparable_evaluation"),
        ({}, ModelFallbackPolicy(enabled=True), "target_not_approved"),
        (
            {"effective_required_acceptance_hash": "c" * 64},
            None,
            "contract_changed",
        ),
        ({"allowed_provider_ids": ["primary-provider"]}, None, "provider_not_allowed"),
        ({"required_data_boundary": "private-code"}, None, "data_boundary_mismatch"),
        (
            {
                "candidate": profile(
                    FALLBACK,
                    api_type="anthropic_messages",
                ).model_dump(mode="json")
            },
            None,
            "api_incompatible",
        ),
        (
            {"candidate": profile(FALLBACK, tools=False).model_dump(mode="json")},
            None,
            "tools_incompatible",
        ),
        (
            {"candidate": profile(FALLBACK, structured=False).model_dump(mode="json")},
            None,
            "structured_output_incompatible",
        ),
        (
            {"candidate": profile(FALLBACK, tokens=40_000).model_dump(mode="json")},
            None,
            "context_too_small",
        ),
        (
            {"candidate": profile(FALLBACK, currency="USD").model_dump(mode="json")},
            None,
            "currency_mismatch",
        ),
        ({"original_cost_retained": False}, None, "unknown_cost_not_retained"),
        ({"remaining_run_cost": "0.09"}, None, "budget_insufficient"),
    ],
)
def test_fallback_rejects_every_authority_capability_and_budget_violation(
    request_updates,
    policy,
    reason,
):
    result = decision(request_updates=request_updates, policy=policy)

    assert result.allowed is False
    assert result.reason_code == reason
    assert result.execution_profile_changed is False


def test_policy_rejects_a_fallback_chain_longer_than_one_target():
    with pytest.raises(ValidationError):
        ModelFallbackPolicy(
            enabled=True,
            approved_targets=(
                FALLBACK,
                ModelTarget(provider_id="third-provider", model_id="third-model"),
            ),
        )


def test_request_rejects_using_the_primary_as_its_own_fallback():
    raw = request_dict()
    raw["candidate"] = profile(PRIMARY).model_dump(mode="json")

    with pytest.raises(ValidationError, match="change the provider/model target"):
        ModelFallbackRequest.model_validate(raw)


def test_fallback_history_rejects_a_noop_profile_change():
    with pytest.raises(ValidationError, match="actual execution-profile change"):
        FallbackRecord(
            primary=PRIMARY,
            fallback=PRIMARY,
            reason="invalid no-op",
            old_config_hash="c" * 64,
            new_config_hash="c" * 64,
            reserved_cost="0.10",
            task_spec_hash=TASK_HASH,
            required_acceptance_hash=ACCEPTANCE_HASH,
        )
