from __future__ import annotations

from dataclasses import dataclass

from horizon.domain.common import digest
from horizon.domain.run import Run
from horizon.domain.run_reliability import (
    DegradationRecord,
    FallbackRecord,
    RunReliabilitySummary,
)
from horizon.domain.states import RunStatus


@dataclass(frozen=True)
class RunReliabilityFacts:
    """Status-independent reliability facts derived from one replayable Run projection."""

    task_spec_hash: str
    required_acceptance_hash: str
    required_checks_passed: bool
    unknown_acceptance_evidence: bool
    quality_flags: tuple[str, ...]
    fallback_history: tuple[FallbackRecord, ...]
    degradations: tuple[DegradationRecord, ...]
    limitations: tuple[str, ...]


def derive_run_reliability_facts(run: Run) -> RunReliabilityFacts:
    required_ids = tuple(check.id for check in run.task.acceptance if check.required)
    required_acceptance_hash = digest(required_ids)
    report = run.validation
    current_validation = bool(
        report is not None
        and report["task_spec_hash"] == run.task.sha256
        and report["workspace_revision"] == run.workspace_revision
        and report["evidence_ref"]
    )
    required_checks_passed = bool(
        current_validation and set(required_ids) <= set(report["passed_check_ids"])
    )
    unknown_acceptance_evidence = not current_validation
    unknown_effects_present = bool(
        run.unknown_reservations or run.unknown_model_calls or run.unknown_tool_calls
    )
    open_effects_present = bool(
        run.reservations
        or run.model_reservations
        or run.tool_reservations
        or (run.promotion_intent is not None and run.promotion_receipt is None)
        or run.process_worker_launch is not None
    )
    quality_flags: list[str] = []
    limitations = [
        "Fallback and degradation history includes only records bound into the replayed Trace."
    ]
    if unknown_acceptance_evidence:
        quality_flags.append("acceptance_evidence_unknown")
        limitations.append(
            "Current-task acceptance evidence is unavailable; required-check outcome is unknown."
        )
    elif not required_checks_passed:
        quality_flags.append("required_checks_failed")
        limitations.append("Current validation did not pass every required acceptance check.")
    if unknown_effects_present:
        quality_flags.append("unknown_effects_present")
        limitations.append("One or more external effects have unknown outcomes.")
    if open_effects_present:
        quality_flags.append("open_effects_present")
        limitations.append("One or more reservations, promotions, or process effects remain open.")
    for degradation in run.reliability_degradations:
        if degradation.quality_flag not in quality_flags:
            quality_flags.append(degradation.quality_flag)
        limitations.append(
            f"{degradation.operation} used {degradation.strategy}; original reason(s): "
            f"{degradation.original_error_type}."
        )
    return RunReliabilityFacts(
        task_spec_hash=run.task.sha256,
        required_acceptance_hash=required_acceptance_hash,
        required_checks_passed=required_checks_passed,
        unknown_acceptance_evidence=unknown_acceptance_evidence,
        quality_flags=tuple(quality_flags),
        fallback_history=(),
        degradations=tuple(run.reliability_degradations),
        limitations=tuple(limitations),
    )


def build_terminal_run_reliability_summary(run: Run) -> RunReliabilitySummary:
    facts = derive_run_reliability_facts(run)
    status = {
        RunStatus.SUCCEEDED: "passed",
        RunStatus.FAILED: "failed",
        RunStatus.CANCELLED: "blocked",
    }[run.status]
    return RunReliabilitySummary(
        status=status,
        task_spec_hash=facts.task_spec_hash,
        effective_task_spec_hash=facts.task_spec_hash,
        required_acceptance_hash=facts.required_acceptance_hash,
        effective_required_acceptance_hash=facts.required_acceptance_hash,
        required_checks_passed=facts.required_checks_passed,
        unknown_acceptance_evidence=facts.unknown_acceptance_evidence,
        quality_flags=facts.quality_flags,
        fallback_history=facts.fallback_history,
        degradations=facts.degradations,
        limitations=facts.limitations,
    )
