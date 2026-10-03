from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from horizon.domain.budget import Usage
from horizon.domain.common import Contract
from horizon.domain.states import RunStatus
from horizon.domain.task import Identifier, NonNegativeInt, PositiveInt, Text, relative_pattern
from horizon.domain.tools import AcceptanceResult

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
NonNegativeMoney = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]

PORTFOLIO_DEMO_EXCLUDED_CLAIMS = (
    "real_model_quality",
    "official_benchmark_score",
    "operating_system_process_crash_survival",
    "untrusted_code_sandboxing",
)


class PortfolioDemoVerification(Contract):
    initial_failure_confirmed: bool
    structured_tool_error_observed: bool
    resumed_context_contains_error: bool
    final_validation_passed: bool
    trace_replay_verified: bool
    source_workspace_unchanged: bool
    staging_workspace_changed: bool
    scripted_actions_consumed: bool
    no_unknown_calls: bool
    no_open_reservations: bool
    all_checks_passed: bool

    @model_validator(mode="after")
    def validate_aggregate(self) -> Self:
        checks = (
            self.initial_failure_confirmed,
            self.structured_tool_error_observed,
            self.resumed_context_contains_error,
            self.final_validation_passed,
            self.trace_replay_verified,
            self.source_workspace_unchanged,
            self.staging_workspace_changed,
            self.scripted_actions_consumed,
            self.no_unknown_calls,
            self.no_open_reservations,
        )
        if self.all_checks_passed != all(checks):
            raise ValueError("Portfolio demo aggregate verdict does not match its checks")
        return self


class PortfolioDemoReport(Contract):
    schema_version: Literal[1] = 1
    demo_id: Identifier
    run_id: Identifier
    status: RunStatus
    claim_scope: Literal["offline_deterministic_harness_demo"] = (
        "offline_deterministic_harness_demo"
    )
    recovery_mode: Literal["durable_worker_handoff"] = "durable_worker_handoff"
    worker_handoffs: Literal[1] = 1
    final_lease_epoch: PositiveInt
    event_count: NonNegativeInt
    tool_sequence: tuple[Identifier, ...]
    tool_statuses: tuple[Literal["success", "error", "unknown", "cancelled"], ...]
    initial_validation: tuple[AcceptanceResult, ...]
    final_validation: tuple[AcceptanceResult, ...]
    usage: Usage
    simulated_model_cost: NonNegativeMoney
    model_currency: Literal["CNY", "USD"]
    external_cost_cny: NonNegativeMoney = Decimal("0")
    paid_model_called: Literal[False] = False
    network_called: Literal[False] = False
    repository_code_executed: Literal[False] = False
    projection_hash: Sha256
    trace_sha256: Sha256
    final_run_sha256: Sha256
    source_revision: Sha256
    source_manifest_ref: Sha256
    workspace_revision: Sha256
    workspace_manifest_ref: Sha256
    structured_error_artifact_ref: Sha256
    verification: PortfolioDemoVerification
    excluded_claims: tuple[Text, ...] = PORTFOLIO_DEMO_EXCLUDED_CLAIMS

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if len(self.tool_sequence) != len(self.tool_statuses):
            raise ValueError("Portfolio demo tool names and statuses must have equal length")
        if self.status == RunStatus.SUCCEEDED and not self.verification.final_validation_passed:
            raise ValueError("A successful portfolio demo requires final validation evidence")
        if self.external_cost_cny != 0 or self.paid_model_called or self.network_called:
            raise ValueError("The portfolio demo must remain offline and externally free")
        if tuple(self.excluded_claims) != PORTFOLIO_DEMO_EXCLUDED_CLAIMS:
            raise ValueError("Portfolio demo evidence boundaries must remain explicit")
        return self


class PortfolioEvidenceFile(Contract):
    role: Literal["report", "trace", "final_state", "summary"]
    path: Text
    sha256: Sha256
    bytes: NonNegativeInt

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        relative_pattern(self.path)
        if any(character in self.path for character in "*?[]"):
            raise ValueError("Portfolio evidence paths must be literal")
        return self


class PortfolioEvidencePack(Contract):
    schema_version: Literal[1] = 1
    pack_type: Literal["horizon.portfolio-demo"] = "horizon.portfolio-demo"
    demo_id: Identifier
    run_id: Identifier
    files: Annotated[tuple[PortfolioEvidenceFile, ...], Field(min_length=4, max_length=4)]
    all_checks_passed: bool
    claim_scope: Literal["offline_deterministic_harness_demo"] = (
        "offline_deterministic_harness_demo"
    )
    external_cost_cny: NonNegativeMoney = Decimal("0")
    paid_model_called: Literal[False] = False
    network_called: Literal[False] = False
    excluded_claims: tuple[Text, ...] = PORTFOLIO_DEMO_EXCLUDED_CLAIMS

    @model_validator(mode="after")
    def validate_files(self) -> Self:
        roles = [item.role for item in self.files]
        paths = [item.path.casefold() for item in self.files]
        if set(roles) != {"report", "trace", "final_state", "summary"}:
            raise ValueError("Portfolio EvidencePack requires all four evidence roles")
        if len(paths) != len(set(paths)):
            raise ValueError("Portfolio EvidencePack paths must be unique")
        if self.external_cost_cny != 0 or self.paid_model_called or self.network_called:
            raise ValueError("The portfolio EvidencePack must remain offline and externally free")
        if tuple(self.excluded_claims) != PORTFOLIO_DEMO_EXCLUDED_CLAIMS:
            raise ValueError("Portfolio EvidencePack boundaries must remain explicit")
        return self
