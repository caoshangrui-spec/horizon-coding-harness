from __future__ import annotations

from collections import Counter
from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, model_validator

from horizon.domain.common import Contract, digest
from horizon.domain.errors import BudgetStopReason
from horizon.domain.states import TERMINAL, RunStatus
from horizon.domain.task import Identifier, PositiveInt, Text, relative_pattern
from horizon.domain.terminal_evidence import Sha256

Count = Annotated[StrictInt, Field(ge=0)]
Ratio = Annotated[float, Field(ge=0.0, le=1.0)]

TERMINAL_SUITE_BOUNDARIES_V1 = (
    "suite_aggregates_existing_terminal_runs_without_executing_tasks",
    "terminal_capture_verified_is_not_task_success",
    "failed_cancelled_unknown_and_open_effects_remain_visible",
    "self_consistent_hashes_are_not_origin_authentication",
)
TERMINAL_SUITE_BOUNDARIES = (
    *TERMINAL_SUITE_BOUNDARIES_V1,
    "task_success_is_reported_separately_from_quality_flags",
    "reliability_aggregates_only_verified_child_evidence",
)


class TerminalSuiteCase(Contract):
    case_id: Identifier
    run_id: Identifier


class TerminalSuiteManifest(Contract):
    schema_version: Literal[1] = 1
    suite_id: Identifier
    cases: Annotated[tuple[TerminalSuiteCase, ...], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def validate_cases(self) -> Self:
        case_ids = [case.case_id for case in self.cases]
        run_ids = [case.run_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Terminal-suite case IDs must be unique")
        if len(run_ids) != len(set(run_ids)):
            raise ValueError("Terminal-suite Run IDs must be unique")
        return self

    @property
    def sha256(self) -> str:
        return digest(self)


class TerminalSuiteReasonCount(Contract):
    reason: Text
    run_count: PositiveInt


class TerminalSuiteCaseResult(Contract):
    sequence: Annotated[StrictInt, Field(gt=0, le=100)]
    case_id: Identifier
    run_id: Identifier
    task_id: Identifier
    status: RunStatus
    task_succeeded: bool
    failure_reason: Text | None = None
    budget_stop_reason: BudgetStopReason | None = None
    event_count: PositiveInt
    event_hash: Sha256
    projection_hash: Sha256
    unknown_effects_present: bool
    open_effects_present: bool
    reliability_summary_present: bool = False
    required_checks_passed: bool | None = None
    acceptance_evidence_unknown: bool | None = None
    quality_flags: tuple[Identifier, ...] = ()
    fallback_count: Count = 0
    degradation_count: Count = 0
    terminal_capture_verified: Literal[True] = True
    evidence_pack_path: Text
    evidence_pack_sha256: Sha256
    evidence_pack_bytes: PositiveInt

    @model_validator(mode="after")
    def validate_case(self) -> Self:
        if self.status not in TERMINAL:
            raise ValueError("Terminal-suite cases require terminal Run statuses")
        if self.task_succeeded != (self.status == RunStatus.SUCCEEDED):
            raise ValueError("Terminal-suite task success must match SUCCEEDED status")
        if (self.status == RunStatus.FAILED) != (self.failure_reason is not None):
            raise ValueError("Only failed terminal-suite cases carry failure reasons")
        if self.budget_stop_reason is not None:
            if self.status != RunStatus.FAILED:
                raise ValueError("A budget stop must belong to a failed Run")
            if self.failure_reason != self.budget_stop_reason.value:
                raise ValueError("Budget-stop reason must match the Run failure reason")
        if len(self.quality_flags) != len(set(self.quality_flags)):
            raise ValueError("Terminal-suite quality flags must be unique")
        if not self.reliability_summary_present:
            if (
                self.required_checks_passed is not None
                or self.acceptance_evidence_unknown is not None
                or self.quality_flags
                or self.fallback_count
                or self.degradation_count
            ):
                raise ValueError("Legacy terminal-suite cases cannot carry reliability fields")
        else:
            if self.required_checks_passed is None or self.acceptance_evidence_unknown is None:
                raise ValueError("A terminal-suite reliability summary requires acceptance fields")
            flags = set(self.quality_flags)
            if ("acceptance_evidence_unknown" in flags) != self.acceptance_evidence_unknown:
                raise ValueError("Terminal-suite acceptance evidence flag does not match its value")
            required_checks_failed = (
                not self.acceptance_evidence_unknown and not self.required_checks_passed
            )
            if ("required_checks_failed" in flags) != required_checks_failed:
                raise ValueError("Terminal-suite required-check flag does not match its values")
            if self.status == RunStatus.SUCCEEDED and (
                not self.required_checks_passed or self.acceptance_evidence_unknown
            ):
                raise ValueError("A successful terminal-suite case requires known passing checks")
            if self.degradation_count and not self.quality_flags:
                raise ValueError("A degraded terminal-suite case requires a quality flag")
        relative_pattern(self.evidence_pack_path)
        if any(character in self.evidence_pack_path for character in "*?[]"):
            raise ValueError("Terminal-suite EvidencePack paths must be literal")
        return self


class TerminalSuiteReport(Contract):
    schema_version: Literal[1, 2] = 2
    suite_id: Identifier
    manifest_digest: Sha256
    claim_scope: Literal["offline_existing_terminal_run_aggregation"] = (
        "offline_existing_terminal_run_aggregation"
    )
    case_count: PositiveInt
    succeeded_run_count: Count
    failed_run_count: Count
    cancelled_run_count: Count
    task_success_rate: Ratio
    terminal_capture_verified_count: Count
    unknown_effect_run_count: Count
    open_effect_run_count: Count
    reliability_summary_verified_count: Count = 0
    unknown_acceptance_run_count: Count = 0
    required_check_failure_run_count: Count = 0
    quality_flagged_run_count: Count = 0
    clean_success_run_count: Count = 0
    quality_flagged_success_run_count: Count = 0
    fallback_run_count: Count = 0
    degradation_run_count: Count = 0
    retrieval_degraded_run_count: Count = 0
    quality_flag_counts: tuple[TerminalSuiteReasonCount, ...] = ()
    failure_reason_counts: tuple[TerminalSuiteReasonCount, ...] = ()
    budget_stop_reason_counts: tuple[TerminalSuiteReasonCount, ...] = ()
    paid_model_called: Literal[False] = False
    network_called: Literal[False] = False
    tool_called: Literal[False] = False
    repository_code_executed: Literal[False] = False
    runs_executed: Literal[False] = False
    cases: Annotated[tuple[TerminalSuiteCaseResult, ...], Field(min_length=1, max_length=100)]
    boundaries: tuple[Text, ...] = TERMINAL_SUITE_BOUNDARIES

    @model_validator(mode="after")
    def validate_totals(self) -> Self:
        totals = {
            "case_count": len(self.cases),
            "succeeded_run_count": sum(case.status == RunStatus.SUCCEEDED for case in self.cases),
            "failed_run_count": sum(case.status == RunStatus.FAILED for case in self.cases),
            "cancelled_run_count": sum(case.status == RunStatus.CANCELLED for case in self.cases),
            "terminal_capture_verified_count": sum(
                case.terminal_capture_verified for case in self.cases
            ),
            "unknown_effect_run_count": sum(case.unknown_effects_present for case in self.cases),
            "open_effect_run_count": sum(case.open_effects_present for case in self.cases),
        }
        reliability_totals = {
            "reliability_summary_verified_count": sum(
                case.reliability_summary_present for case in self.cases
            ),
            "unknown_acceptance_run_count": sum(
                case.acceptance_evidence_unknown is True for case in self.cases
            ),
            "required_check_failure_run_count": sum(
                case.reliability_summary_present
                and case.acceptance_evidence_unknown is False
                and case.required_checks_passed is False
                for case in self.cases
            ),
            "quality_flagged_run_count": sum(bool(case.quality_flags) for case in self.cases),
            "clean_success_run_count": sum(
                case.status == RunStatus.SUCCEEDED and not case.quality_flags for case in self.cases
            ),
            "quality_flagged_success_run_count": sum(
                case.status == RunStatus.SUCCEEDED and bool(case.quality_flags)
                for case in self.cases
            ),
            "fallback_run_count": sum(case.fallback_count > 0 for case in self.cases),
            "degradation_run_count": sum(case.degradation_count > 0 for case in self.cases),
            "retrieval_degraded_run_count": sum(
                "retrieval_degraded" in case.quality_flags for case in self.cases
            ),
        }
        for field, expected in totals.items():
            if getattr(self, field) != expected:
                raise ValueError(f"Terminal-suite {field} does not match case results")
        if self.schema_version == 1:
            if any(case.reliability_summary_present for case in self.cases) or any(
                getattr(self, field) != 0 for field in reliability_totals
            ):
                raise ValueError("Terminal-suite schema v1 cannot carry reliability summaries")
            if self.quality_flag_counts:
                raise ValueError("Terminal-suite schema v1 cannot carry quality-flag counts")
        else:
            if reliability_totals["reliability_summary_verified_count"] != len(self.cases):
                raise ValueError("Terminal-suite schema v2 requires every reliability summary")
            for field, expected in reliability_totals.items():
                if getattr(self, field) != expected:
                    raise ValueError(f"Terminal-suite {field} does not match case results")
            expected_quality_flags = _reason_counts(
                flag for case in self.cases for flag in case.quality_flags
            )
            if self.quality_flag_counts != expected_quality_flags:
                raise ValueError("Terminal-suite quality-flag counts do not match case results")
            if (
                self.clean_success_run_count + self.quality_flagged_success_run_count
                != self.succeeded_run_count
            ):
                raise ValueError("Terminal-suite success quality split is incomplete")
        expected_rate = round(self.succeeded_run_count / self.case_count, 6)
        if self.task_success_rate != expected_rate:
            raise ValueError("Terminal-suite task success rate does not match case results")
        if [case.sequence for case in self.cases] != list(range(1, self.case_count + 1)):
            raise ValueError("Terminal-suite case sequence must be contiguous and ordered")
        case_ids = [case.case_id for case in self.cases]
        run_ids = [case.run_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)) or len(run_ids) != len(set(run_ids)):
            raise ValueError("Terminal-suite case and Run IDs must be unique")
        expected_failure_reasons = _reason_counts(
            case.failure_reason for case in self.cases if case.failure_reason is not None
        )
        if self.failure_reason_counts != expected_failure_reasons:
            raise ValueError("Terminal-suite failure-reason counts do not match case results")
        expected_budget_stops = _reason_counts(
            case.budget_stop_reason.value
            for case in self.cases
            if case.budget_stop_reason is not None
        )
        if self.budget_stop_reason_counts != expected_budget_stops:
            raise ValueError("Terminal-suite budget-stop counts do not match case results")
        expected_boundaries = (
            TERMINAL_SUITE_BOUNDARIES_V1 if self.schema_version == 1 else TERMINAL_SUITE_BOUNDARIES
        )
        if tuple(self.boundaries) != expected_boundaries:
            raise ValueError("Terminal-suite claim boundaries must remain explicit")
        return self


def _reason_counts(reasons) -> tuple[TerminalSuiteReasonCount, ...]:
    counts = Counter(reasons)
    return tuple(
        TerminalSuiteReasonCount(reason=reason, run_count=count)
        for reason, count in sorted(counts.items())
    )


class TerminalSuiteFile(Contract):
    role: Literal["manifest", "report", "summary"]
    path: Text
    sha256: Sha256
    bytes: PositiveInt

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        relative_pattern(self.path)
        if any(character in self.path for character in "*?[]"):
            raise ValueError("Terminal-suite file paths must be literal")
        return self


class TerminalSuitePack(Contract):
    schema_version: Literal[1, 2] = 2
    pack_type: Literal["horizon.terminal-suite"] = "horizon.terminal-suite"
    report: TerminalSuiteReport
    files: Annotated[tuple[TerminalSuiteFile, ...], Field(min_length=3, max_length=3)]

    @model_validator(mode="after")
    def validate_files(self) -> Self:
        roles = [item.role for item in self.files]
        paths = [item.path.casefold() for item in self.files]
        if set(roles) != {"manifest", "report", "summary"}:
            raise ValueError("Terminal-suite pack requires manifest, report, and summary files")
        if len(paths) != len(set(paths)):
            raise ValueError("Terminal-suite pack paths must be unique")
        if self.schema_version != self.report.schema_version:
            raise ValueError("Terminal-suite pack and report schema versions must match")
        return self
