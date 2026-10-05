from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from horizon.domain.common import Contract, digest
from horizon.domain.recovery import WriteRecoveryDecision, WriteRecoveryState
from horizon.domain.task import Identifier, NonNegativeInt, Text

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Ratio = Annotated[float, Field(ge=0.0, le=1.0)]
NonNegativeMoney = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
RecoveryOutcome = Literal[
    "auto_recovered",
    "safely_blocked",
    "unrecoverable",
    "incorrect_resume",
]
WriteRecoveryTool = Literal["replace_text", "apply_patch", "create_file"]

RECOVERY_MATRIX_EXCLUDED_CLAIMS = (
    "arbitrary_process_or_host_crash_recovery",
    "distributed_exactly_once_execution",
    "untrusted_code_sandboxing",
    "paid_model_quality",
    "complete_fault_injection_matrix",
)


class HardCrashRecoveryEvalCase(Contract):
    kind: Literal["hard_crash"] = "hard_crash"
    case_id: Identifier
    description: Annotated[str, Field(min_length=1, max_length=500)]
    expected_outcome: Literal["auto_recovered"] = "auto_recovered"


class WriteRecoveryEvalCase(Contract):
    kind: Literal["write_state"] = "write_state"
    case_id: Identifier
    description: Annotated[str, Field(min_length=1, max_length=500)]
    tool: WriteRecoveryTool
    injected_state: WriteRecoveryState
    decision: WriteRecoveryDecision
    expected_outcome: Literal["auto_recovered", "safely_blocked"]


RecoveryMatrixCase = Annotated[
    HardCrashRecoveryEvalCase | WriteRecoveryEvalCase,
    Field(discriminator="kind"),
]


class RecoveryMatrixManifest(Contract):
    schema_version: Literal[1] = 1
    benchmark_id: Identifier
    cases: Annotated[tuple[RecoveryMatrixCase, ...], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def validate_cases(self) -> Self:
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Recovery-matrix case IDs must be unique")
        hard_crashes = [case for case in self.cases if isinstance(case, HardCrashRecoveryEvalCase)]
        if len(hard_crashes) != 1:
            raise ValueError("Recovery matrix requires exactly one real hard-crash case")
        write_keys = [
            (case.tool, case.injected_state, case.decision)
            for case in self.cases
            if isinstance(case, WriteRecoveryEvalCase)
        ]
        if len(write_keys) != len(set(write_keys)):
            raise ValueError("Recovery-matrix write state/decision combinations must be unique")
        return self

    @property
    def sha256(self) -> str:
        return digest(self)


class RecoveryMatrixCaseResult(Contract):
    case_id: Identifier
    kind: Literal["hard_crash", "write_state"]
    expected_outcome: RecoveryOutcome
    observed_outcome: RecoveryOutcome
    passed: bool
    duration_ms: NonNegativeInt
    recovery_redispatch_count: NonNegativeInt
    duplicate_side_effect_count: NonNegativeInt
    evidence_ref: Sha256
    evidence_path: Text | None = None
    tool: WriteRecoveryTool | None = None
    injected_state: WriteRecoveryState | None = None
    observed_state: WriteRecoveryState | None = None
    decision: WriteRecoveryDecision | None = None
    actual_process_crash_observed: bool = False
    trace_replay_verified: bool | None = None
    detail: Text

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.passed != (self.expected_outcome == self.observed_outcome):
            raise ValueError("Recovery-matrix case verdict does not match its outcomes")
        if self.kind == "hard_crash":
            if (
                self.tool is not None
                or self.injected_state is not None
                or self.observed_state is not None
                or self.decision is not None
                or self.evidence_path is None
                or self.trace_replay_verified is None
            ):
                raise ValueError("Hard-crash results require process/Trace evidence only")
        elif (
            self.tool is None
            or self.injected_state is None
            or self.observed_state is None
            or self.decision is None
            or self.evidence_path is not None
            or self.actual_process_crash_observed
            or self.trace_replay_verified is not None
        ):
            raise ValueError("Write-state results require a complete state/decision observation")
        return self


class RecoveryMatrixReport(Contract):
    schema_version: Literal[1] = 1
    benchmark_id: Identifier
    manifest_digest: Sha256
    claim_scope: Literal["offline_bounded_recovery_matrix"] = "offline_bounded_recovery_matrix"
    case_count: NonNegativeInt
    passed_case_count: NonNegativeInt
    case_pass_rate: Ratio
    expected_auto_recovery_count: NonNegativeInt
    auto_recovered_count: NonNegativeInt
    recovery_success_rate: Ratio
    expected_safe_block_count: NonNegativeInt
    safely_blocked_count: NonNegativeInt
    safe_block_rate: Ratio
    unrecoverable_count: NonNegativeInt
    incorrect_resume_count: NonNegativeInt
    recovery_redispatch_count: NonNegativeInt
    duplicate_side_effect_count: NonNegativeInt
    actual_process_crash_case_count: NonNegativeInt
    trace_replay_verified_count: NonNegativeInt
    external_cost_cny: NonNegativeMoney = Decimal("0")
    paid_model_called: Literal[False] = False
    network_called: Literal[False] = False
    repository_code_executed: Literal[False] = False
    cases: tuple[RecoveryMatrixCaseResult, ...]
    excluded_claims: tuple[Text, ...] = RECOVERY_MATRIX_EXCLUDED_CLAIMS

    @model_validator(mode="after")
    def validate_totals(self) -> Self:
        expected_auto = [case for case in self.cases if case.expected_outcome == "auto_recovered"]
        expected_block = [case for case in self.cases if case.expected_outcome == "safely_blocked"]
        totals = {
            "case_count": len(self.cases),
            "passed_case_count": sum(case.passed for case in self.cases),
            "expected_auto_recovery_count": len(expected_auto),
            "auto_recovered_count": sum(
                case.observed_outcome == "auto_recovered" for case in self.cases
            ),
            "expected_safe_block_count": len(expected_block),
            "safely_blocked_count": sum(
                case.observed_outcome == "safely_blocked" for case in self.cases
            ),
            "unrecoverable_count": sum(
                case.observed_outcome == "unrecoverable" for case in self.cases
            ),
            "incorrect_resume_count": sum(
                case.observed_outcome == "incorrect_resume" for case in self.cases
            ),
            "recovery_redispatch_count": sum(case.recovery_redispatch_count for case in self.cases),
            "duplicate_side_effect_count": sum(
                case.duplicate_side_effect_count for case in self.cases
            ),
            "actual_process_crash_case_count": sum(
                case.actual_process_crash_observed for case in self.cases
            ),
            "trace_replay_verified_count": sum(
                case.trace_replay_verified is True for case in self.cases
            ),
        }
        for field, expected in totals.items():
            if getattr(self, field) != expected:
                raise ValueError(f"Recovery-matrix {field} does not match case results")
        expected_pass_rate = round(self.passed_case_count / self.case_count, 6)
        expected_recovery_rate = (
            round(
                sum(case.observed_outcome == "auto_recovered" for case in expected_auto)
                / len(expected_auto),
                6,
            )
            if expected_auto
            else 0.0
        )
        expected_block_rate = (
            round(
                sum(case.observed_outcome == "safely_blocked" for case in expected_block)
                / len(expected_block),
                6,
            )
            if expected_block
            else 0.0
        )
        if self.case_pass_rate != expected_pass_rate:
            raise ValueError("Recovery-matrix case pass rate does not match results")
        if self.recovery_success_rate != expected_recovery_rate:
            raise ValueError("Recovery success rate does not match expected-auto cases")
        if self.safe_block_rate != expected_block_rate:
            raise ValueError("Safe-block rate does not match expected-block cases")
        if self.external_cost_cny != 0 or self.paid_model_called or self.network_called:
            raise ValueError("Recovery matrix must remain offline and externally free")
        if tuple(self.excluded_claims) != RECOVERY_MATRIX_EXCLUDED_CLAIMS:
            raise ValueError("Recovery-matrix evidence boundaries must remain explicit")
        return self
