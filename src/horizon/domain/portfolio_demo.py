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
PORTFOLIO_CRASH_EXIT_CODE = 86

PORTFOLIO_DEMO_LEGACY_EXCLUDED_CLAIMS = (
    "real_model_quality",
    "official_benchmark_score",
    "operating_system_process_crash_survival",
    "untrusted_code_sandboxing",
)

PORTFOLIO_DEMO_EXCLUDED_CLAIMS = (
    "real_model_quality",
    "official_benchmark_score",
    "untrusted_code_sandboxing",
)


class PortfolioDemoVerification(Contract):
    initial_failure_confirmed: bool
    structured_tool_error_observed: bool
    resumed_context_contains_error: bool
    retrieval_evidence_in_model_context: bool | None = None
    evidence_backed_write: bool | None = None
    hard_crash_recovery_verified: bool | None = None
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
        checks: tuple[bool, ...] = (
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
        lineage_checks = (
            self.retrieval_evidence_in_model_context,
            self.evidence_backed_write,
        )
        if any(check is not None for check in lineage_checks):
            if any(check is None for check in lineage_checks):
                raise ValueError("Portfolio demo lineage checks must be supplied together")
            checks += tuple(bool(check) for check in lineage_checks)
        if self.hard_crash_recovery_verified is not None:
            checks += (self.hard_crash_recovery_verified,)
        if self.all_checks_passed != all(checks):
            raise ValueError("Portfolio demo aggregate verdict does not match its checks")
        return self


class PortfolioEvidenceLineage(Contract):
    """Trace-auditable binding from one retrieval result to one exact write."""

    schema_version: Literal[1] = 1
    retrieval_call_id: Identifier
    retrieval_artifact_ref: Sha256
    evidence_index_key: Sha256
    evidence_workspace_revision: Sha256
    write_model_call_id: Identifier
    write_context_projection_ref: Sha256
    write_call_id: Identifier
    write_path: Text
    write_preimage_sha256: Sha256
    matched_chunk_content_hash: Sha256 | None = None
    model_context_contains_retrieval: bool
    target_path_in_evidence: bool
    preimage_in_evidence: bool
    revision_match: bool
    verified: bool

    @model_validator(mode="after")
    def validate_lineage(self) -> Self:
        relative_pattern(self.write_path)
        if any(character in self.write_path for character in "*?[]"):
            raise ValueError("Evidence-backed write path must be literal")
        if self.preimage_in_evidence and not self.target_path_in_evidence:
            raise ValueError("A matched write preimage requires a matched evidence path")
        if self.target_path_in_evidence != (self.matched_chunk_content_hash is not None):
            raise ValueError("Matched evidence paths require a chunk content hash")
        checks = (
            self.model_context_contains_retrieval,
            self.target_path_in_evidence,
            self.preimage_in_evidence,
            self.revision_match,
        )
        if self.verified != all(checks):
            raise ValueError("Evidence-write lineage verdict does not match its checks")
        return self


class PortfolioCrashRecoveryEvidence(Contract):
    """Observed process exit and exact write-effect recovery at one durable tool boundary."""

    schema_version: Literal[1] = 1
    expected_exit_code: Literal[86] = 86
    observed_exit_code: int
    crashed_worker_epoch: PositiveInt
    recovery_worker_epoch: PositiveInt
    pending_tool_call_id: Identifier
    crash_marker_tool_call_id: Identifier
    crash_marker_path: Literal["hard-crash-marker.json"] = "hard-crash-marker.json"
    crash_marker_sha256: Sha256
    pending_tool: Literal["replace_text"] = "replace_text"
    reservation_without_receipt: bool
    expected_effect_present: bool
    conservative_unknown_recorded: bool
    recovery_classification: Literal["tool_effect_unknown"] = "tool_effect_unknown"
    recovery_disposition: Literal["accept_replace"] = "accept_replace"
    exact_effect_accepted: bool
    resumed_context_contains_recovery: bool
    one_recovered_write_receipt: bool
    verified: bool

    @model_validator(mode="after")
    def validate_recovery(self) -> Self:
        checks = (
            self.observed_exit_code == self.expected_exit_code,
            self.recovery_worker_epoch == self.crashed_worker_epoch + 1,
            self.pending_tool_call_id == self.crash_marker_tool_call_id,
            self.reservation_without_receipt,
            self.expected_effect_present,
            self.conservative_unknown_recorded,
            self.exact_effect_accepted,
            self.resumed_context_contains_recovery,
            self.one_recovered_write_receipt,
        )
        if self.verified != all(checks):
            raise ValueError("Portfolio crash-recovery verdict does not match its evidence")
        return self


class PortfolioDemoReport(Contract):
    schema_version: Literal[1, 2, 3] = 3
    demo_id: Identifier
    run_id: Identifier
    status: RunStatus
    claim_scope: Literal["offline_deterministic_harness_demo"] = (
        "offline_deterministic_harness_demo"
    )
    recovery_mode: Literal["durable_worker_handoff", "durable_handoff_and_hard_crash"] = (
        "durable_handoff_and_hard_crash"
    )
    worker_handoffs: PositiveInt = 2
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
    evidence_lineage: PortfolioEvidenceLineage | None = None
    crash_recovery: PortfolioCrashRecoveryEvidence | None = None
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
        if self.schema_version >= 2 and (
            self.evidence_lineage is None
            or self.verification.retrieval_evidence_in_model_context is None
            or self.verification.evidence_backed_write is None
        ):
            raise ValueError("Portfolio demo schema v2 requires evidence-write lineage")
        if self.evidence_lineage is not None and (
            self.verification.retrieval_evidence_in_model_context
            != self.evidence_lineage.model_context_contains_retrieval
            or self.verification.evidence_backed_write != self.evidence_lineage.verified
        ):
            raise ValueError("Portfolio demo lineage does not match verification checks")
        if self.schema_version == 3 and (
            self.recovery_mode != "durable_handoff_and_hard_crash"
            or self.worker_handoffs != 2
            or self.crash_recovery is None
            or self.verification.hard_crash_recovery_verified is None
        ):
            raise ValueError("Portfolio demo schema v3 requires hard-crash recovery evidence")
        if self.schema_version < 3 and (
            self.recovery_mode != "durable_worker_handoff"
            or self.worker_handoffs != 1
            or self.crash_recovery is not None
            or self.verification.hard_crash_recovery_verified is not None
        ):
            raise ValueError("Legacy portfolio reports cannot claim hard-crash recovery")
        if self.crash_recovery is not None and (
            self.verification.hard_crash_recovery_verified != self.crash_recovery.verified
        ):
            raise ValueError("Portfolio crash recovery does not match verification checks")
        expected_exclusions = (
            PORTFOLIO_DEMO_EXCLUDED_CLAIMS
            if self.schema_version == 3
            else PORTFOLIO_DEMO_LEGACY_EXCLUDED_CLAIMS
        )
        if tuple(self.excluded_claims) != expected_exclusions:
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
    schema_version: Literal[1, 2] = 2
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
        expected_exclusions = (
            PORTFOLIO_DEMO_EXCLUDED_CLAIMS
            if self.schema_version == 2
            else PORTFOLIO_DEMO_LEGACY_EXCLUDED_CLAIMS
        )
        if tuple(self.excluded_claims) != expected_exclusions:
            raise ValueError("Portfolio EvidencePack boundaries must remain explicit")
        return self
