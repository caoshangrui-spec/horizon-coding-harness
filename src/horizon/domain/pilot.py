from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from horizon.domain.common import Contract, canonical_json, digest
from horizon.domain.model import Currency, PositiveMoney
from horizon.domain.run_evaluation import ExternalTaskSource
from horizon.domain.task import Identifier, TaskSpec, Text, relative_pattern
from horizon.domain.tools import AcceptanceResult, Digest

PrivateLiteral = Annotated[str, Field(min_length=4, max_length=500)]


class SolutionIsolationPolicy(Contract):
    """Private markers that must never enter the model-visible TaskSpec."""

    acceptance_visibility: Literal["visible"] = "visible"
    forbidden_task_literals: Annotated[tuple[PrivateLiteral, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_literals(self) -> Self:
        folded = [value.casefold() for value in self.forbidden_task_literals]
        if len(folded) != len(set(folded)):
            raise ValueError("Private solution markers must be unique")
        return self


class RealModelPilotManifest(Contract):
    schema_version: Literal[1] = 1
    pilot_id: Identifier
    source_path: Text
    source: ExternalTaskSource
    task: TaskSpec
    provider_policy_id: Identifier
    currency: Currency
    max_paid_cost: PositiveMoney
    expected_initial_failed_checks: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    auto_plan: Literal[True] = True
    solution_isolation: SolutionIsolationPolicy

    @model_validator(mode="after")
    def validate_pilot(self) -> Self:
        relative_pattern(self.source_path)
        if any(character in self.source_path for character in "*?[]"):
            raise ValueError("Pilot source_path must be a literal directory")
        if self.source.reduction != "full_checkout":
            raise ValueError("Real-model pilot requires a full upstream checkout")
        if self.task.repository.source != "local":
            raise ValueError("Real-model pilot requires a local frozen source")
        if self.task.repository.base_commit != self.source.buggy_commit:
            raise ValueError("Pilot TaskSpec must bind the frozen buggy commit")
        if self.task.model_policy_id != self.provider_policy_id:
            raise ValueError("Pilot TaskSpec and provider policy IDs must match")
        if self.task.constraints.network != "deny":
            raise ValueError("Pilot repository execution must deny network access")
        if (
            self.task.execution_mode != "workspace_write"
            or self.task.authority_scope != "workspace_write"
        ):
            raise ValueError("Pilot must explicitly grant bounded workspace-write authority")
        acceptance = {check.id: check for check in self.task.acceptance}
        expected = set(self.expected_initial_failed_checks)
        if len(expected) != len(self.expected_initial_failed_checks):
            raise ValueError("Expected initial failed checks must be unique")
        if not expected <= set(acceptance):
            raise ValueError("Expected initial failures must name TaskSpec acceptance checks")
        if any(not acceptance[check_id].required for check_id in expected):
            raise ValueError("Expected initial failures must be required checks")

        visible = canonical_json(self.task).casefold()
        private_values = (
            self.source.fixed_commit,
            self.source.fix_url,
            *self.solution_isolation.forbidden_task_literals,
        )
        if any(value.casefold() in visible for value in private_values):
            raise ValueError("Model-visible TaskSpec contains private solution information")
        return self

    @property
    def sha256(self) -> str:
        return digest(self)


class RealModelPilotPreflightReport(Contract):
    schema_version: Literal[1] = 1
    pilot_id: Identifier
    manifest_digest: Digest
    source: ExternalTaskSource
    source_git_head: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
    source_snapshot_revision: Digest
    source_manifest_ref: Digest
    source_unchanged: bool
    full_checkout_verified: bool
    git_metadata_excluded: bool
    prepared_task_digest: Digest
    prepared_task_ref: Digest
    provider_config_digest: Digest
    # Optional only so already-frozen v1 reports remain readable. New launches
    # require this field and must regenerate older preflights before paid use.
    harness_source_digest: Digest | None = None
    provider_policy_id: Identifier
    provider_id: Identifier
    model_id: Text
    validation_backend: Literal["docker"] = "docker"
    validation_backend_ref: Text
    currency: Currency
    run_cost_cap: PositiveMoney
    campaign_remaining_cost: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
    max_model_calls: int
    initial_validation: tuple[AcceptanceResult, ...]
    expected_initial_failed_checks: tuple[Identifier, ...]
    observed_initial_failed_checks: tuple[Identifier, ...]
    initial_failure_matches: bool
    acceptance_visibility: Literal["visible"] = "visible"
    forbidden_literal_hashes: tuple[Digest, ...]
    solution_isolation_passed: bool
    fallback_enabled: Literal[False] = False
    implicit_provider_retry_enabled: Literal[False] = False
    credential_loaded: Literal[False] = False
    paid_model_called: Literal[False] = False
    network_called: Literal[False] = False
    repository_code_executed: Literal[True] = True
    ready: bool

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        observed = tuple(
            sorted(result.check_id for result in self.initial_validation if not result.passed)
        )
        if observed != tuple(sorted(self.observed_initial_failed_checks)):
            raise ValueError("Observed initial failures do not match validation receipts")
        matches = tuple(sorted(self.expected_initial_failed_checks)) == observed
        if self.initial_failure_matches != matches:
            raise ValueError("Initial-failure verdict does not match validation receipts")
        expected_ready = all(
            (
                self.source_unchanged,
                self.full_checkout_verified,
                self.git_metadata_excluded,
                self.initial_failure_matches,
                self.solution_isolation_passed,
                self.campaign_remaining_cost >= self.run_cost_cap,
            )
        )
        if self.ready != expected_ready:
            raise ValueError("Pilot readiness does not match its evidence gates")
        if self.max_model_calls < 2:
            raise ValueError("Auto-plan pilot requires at least two model calls")
        return self
