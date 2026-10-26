from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from horizon.domain.common import Contract
from horizon.domain.model import Currency, NonNegativeMoney, PositiveMoney
from horizon.domain.task import Identifier, PositiveInt, Text

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class ModelTarget(Contract):
    provider_id: Identifier
    model_id: Text


class ModelCapabilityProfile(Contract):
    target: ModelTarget
    api_type: Literal["openai_chat_completions", "anthropic_messages"] = "openai_chat_completions"
    supports_tools: bool
    supports_structured_output: bool
    max_input_tokens: PositiveInt
    data_boundary: Identifier
    currency: Currency


class ModelFallbackPolicy(Contract):
    enabled: bool = False
    approved_targets: Annotated[tuple[ModelTarget, ...], Field(max_length=1)] = ()
    comparable_evaluation: bool = False


class ModelFallbackRequest(Contract):
    primary: ModelCapabilityProfile
    candidate: ModelCapabilityProfile
    required_tools: bool
    required_structured_output: bool
    required_input_tokens: PositiveInt
    allowed_provider_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    required_data_boundary: Identifier
    candidate_reservation_cost: PositiveMoney
    remaining_run_cost: NonNegativeMoney
    remaining_campaign_cost: NonNegativeMoney
    original_cost_status: Literal["settled", "unknown_reserved"]
    original_cost_retained: bool
    task_spec_hash: Sha256
    effective_task_spec_hash: Sha256
    required_acceptance_hash: Sha256
    effective_required_acceptance_hash: Sha256

    @model_validator(mode="after")
    def check_distinct_target(self) -> Self:
        if self.primary.target == self.candidate.target:
            raise ValueError("A fallback must change the provider/model target")
        if len(self.allowed_provider_ids) != len(set(self.allowed_provider_ids)):
            raise ValueError("Allowed fallback providers must be unique")
        return self


class ModelFallbackDecision(Contract):
    allowed: bool
    reason_code: Literal[
        "allowed",
        "disabled",
        "comparable_evaluation",
        "target_not_approved",
        "contract_changed",
        "provider_not_allowed",
        "data_boundary_mismatch",
        "api_incompatible",
        "tools_incompatible",
        "structured_output_incompatible",
        "context_too_small",
        "currency_mismatch",
        "unknown_cost_not_retained",
        "budget_insufficient",
    ]
    execution_profile_changed: bool

    @model_validator(mode="after")
    def check_shape(self) -> Self:
        if self.allowed != (self.reason_code == "allowed"):
            raise ValueError("Fallback permission must match its reason")
        if self.execution_profile_changed != self.allowed:
            raise ValueError("Only an allowed fallback changes the execution profile")
        return self


def decide_model_fallback(
    policy: ModelFallbackPolicy,
    request: ModelFallbackRequest,
) -> ModelFallbackDecision:
    """Validate one pre-authorized candidate; never dispatch or mutate the budget."""

    reason = "allowed"
    if not policy.enabled:
        reason = "disabled"
    elif policy.comparable_evaluation:
        reason = "comparable_evaluation"
    elif request.candidate.target not in policy.approved_targets:
        reason = "target_not_approved"
    elif (
        request.task_spec_hash != request.effective_task_spec_hash
        or request.required_acceptance_hash != request.effective_required_acceptance_hash
    ):
        reason = "contract_changed"
    elif request.candidate.target.provider_id not in request.allowed_provider_ids:
        reason = "provider_not_allowed"
    elif request.candidate.data_boundary != request.required_data_boundary:
        reason = "data_boundary_mismatch"
    elif request.candidate.api_type != request.primary.api_type:
        reason = "api_incompatible"
    elif request.required_tools and not request.candidate.supports_tools:
        reason = "tools_incompatible"
    elif request.required_structured_output and not request.candidate.supports_structured_output:
        reason = "structured_output_incompatible"
    elif request.candidate.max_input_tokens < request.required_input_tokens:
        reason = "context_too_small"
    elif request.candidate.currency != request.primary.currency:
        reason = "currency_mismatch"
    elif request.original_cost_status == "unknown_reserved" and not request.original_cost_retained:
        reason = "unknown_cost_not_retained"
    elif request.candidate_reservation_cost > min(
        request.remaining_run_cost,
        request.remaining_campaign_cost,
    ):
        reason = "budget_insufficient"
    return ModelFallbackDecision(
        allowed=reason == "allowed",
        reason_code=reason,
        execution_profile_changed=reason == "allowed",
    )


class FallbackRecord(Contract):
    primary: ModelTarget
    fallback: ModelTarget
    reason: Text
    old_config_hash: Sha256
    new_config_hash: Sha256
    reserved_cost: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    execution_profile_changed: Literal[True] = True
    task_spec_hash: Sha256
    required_acceptance_hash: Sha256

    @model_validator(mode="after")
    def check_profile_change(self) -> Self:
        if self.primary == self.fallback or self.old_config_hash == self.new_config_hash:
            raise ValueError("Fallback history must describe an actual execution-profile change")
        return self


class DegradationRecord(Contract):
    operation: Literal["retrieval", "context_summary", "model"]
    original_error_type: Text
    original_error_ref: Sha256 | None = None
    strategy: Text
    quality_flag: Identifier
    task_spec_hash: Sha256
    required_acceptance_hash: Sha256
    effective_required_acceptance_hash: Sha256

    @model_validator(mode="after")
    def required_acceptance_is_immutable(self) -> Self:
        if self.required_acceptance_hash != self.effective_required_acceptance_hash:
            raise ValueError("A degradation cannot weaken required acceptance")
        return self


class RunReliabilitySummary(Contract):
    status: Literal["passed", "failed", "blocked"]
    task_spec_hash: Sha256
    effective_task_spec_hash: Sha256
    required_acceptance_hash: Sha256
    effective_required_acceptance_hash: Sha256
    required_checks_passed: bool
    unknown_acceptance_evidence: bool
    quality_flags: tuple[Identifier, ...] = ()
    fallback_history: tuple[FallbackRecord, ...] = ()
    degradations: tuple[DegradationRecord, ...] = ()
    limitations: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def check_evidence_and_contract(self) -> Self:
        if (
            self.task_spec_hash != self.effective_task_spec_hash
            or self.required_acceptance_hash != self.effective_required_acceptance_hash
        ):
            raise ValueError("Reliability handling cannot change the task or required acceptance")
        if len(self.quality_flags) != len(set(self.quality_flags)):
            raise ValueError("Run quality flags must be unique")
        if self.status == "passed" and (
            not self.required_checks_passed or self.unknown_acceptance_evidence
        ):
            raise ValueError("A passed summary requires known passing acceptance evidence")
        for degradation in self.degradations:
            if (
                degradation.task_spec_hash != self.task_spec_hash
                or degradation.required_acceptance_hash != self.required_acceptance_hash
                or degradation.quality_flag not in self.quality_flags
            ):
                raise ValueError("Degradation evidence is not bound to the Run summary")
        for record in self.fallback_history:
            if (
                record.task_spec_hash != self.task_spec_hash
                or record.required_acceptance_hash != self.required_acceptance_hash
            ):
                raise ValueError("Fallback history is not bound to the Run summary")
        return self
