from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from horizon.domain.common import Contract
from horizon.domain.task import Identifier, Text

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class ModelTarget(Contract):
    provider_id: Identifier
    model_id: Text


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
