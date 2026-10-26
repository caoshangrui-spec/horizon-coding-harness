import pytest
from pydantic import ValidationError

from horizon.reliability.fallback import (
    DegradationRecord,
    FallbackRecord,
    ModelTarget,
    RunReliabilitySummary,
)

TASK_HASH = "a" * 64
ACCEPTANCE_HASH = "b" * 64
ERROR_REF = "c" * 64


def degradation(**updates):
    raw = {
        "operation": "retrieval",
        "original_error_type": "FTS5Unavailable",
        "original_error_ref": ERROR_REF,
        "strategy": "bounded lexical scan",
        "quality_flag": "retrieval_degraded",
        "task_spec_hash": TASK_HASH,
        "required_acceptance_hash": ACCEPTANCE_HASH,
        "effective_required_acceptance_hash": ACCEPTANCE_HASH,
    }
    raw.update(updates)
    return DegradationRecord.model_validate(raw)


def test_degraded_pass_preserves_original_error_quality_flag_and_acceptance():
    record = degradation()
    summary = RunReliabilitySummary(
        status="passed",
        task_spec_hash=TASK_HASH,
        effective_task_spec_hash=TASK_HASH,
        required_acceptance_hash=ACCEPTANCE_HASH,
        effective_required_acceptance_hash=ACCEPTANCE_HASH,
        required_checks_passed=True,
        unknown_acceptance_evidence=False,
        quality_flags=("retrieval_degraded",),
        degradations=(record,),
        limitations=("FTS5 unavailable; bounded lexical scan used",),
    )

    assert summary.status == "passed"
    assert summary.degradations[0].original_error_type == "FTS5Unavailable"
    assert summary.degradations[0].original_error_ref == ERROR_REF
    assert summary.quality_flags == ("retrieval_degraded",)
    assert summary.required_acceptance_hash == summary.effective_required_acceptance_hash


def test_degradation_cannot_change_required_acceptance():
    with pytest.raises(ValidationError, match="cannot weaken required acceptance"):
        degradation(effective_required_acceptance_hash="d" * 64)


@pytest.mark.parametrize(
    ("required_checks_passed", "unknown_acceptance_evidence"),
    [(False, False), (True, True), (False, True)],
)
def test_passed_summary_rejects_failed_or_unknown_acceptance_evidence(
    required_checks_passed,
    unknown_acceptance_evidence,
):
    with pytest.raises(ValidationError, match="known passing acceptance evidence"):
        RunReliabilitySummary(
            status="passed",
            task_spec_hash=TASK_HASH,
            effective_task_spec_hash=TASK_HASH,
            required_acceptance_hash=ACCEPTANCE_HASH,
            effective_required_acceptance_hash=ACCEPTANCE_HASH,
            required_checks_passed=required_checks_passed,
            unknown_acceptance_evidence=unknown_acceptance_evidence,
        )


def test_summary_rejects_unflagged_degradation_and_unbound_fallback_history():
    with pytest.raises(ValidationError, match="Degradation evidence"):
        RunReliabilitySummary(
            status="blocked",
            task_spec_hash=TASK_HASH,
            effective_task_spec_hash=TASK_HASH,
            required_acceptance_hash=ACCEPTANCE_HASH,
            effective_required_acceptance_hash=ACCEPTANCE_HASH,
            required_checks_passed=False,
            unknown_acceptance_evidence=True,
            degradations=(degradation(),),
        )

    fallback = FallbackRecord(
        primary=ModelTarget(provider_id="provider-a", model_id="model-a"),
        fallback=ModelTarget(provider_id="provider-b", model_id="model-b"),
        reason="transient provider failure",
        old_config_hash="d" * 64,
        new_config_hash="e" * 64,
        reserved_cost="0.10",
        task_spec_hash="f" * 64,
        required_acceptance_hash=ACCEPTANCE_HASH,
    )
    with pytest.raises(ValidationError, match="Fallback history"):
        RunReliabilitySummary(
            status="failed",
            task_spec_hash=TASK_HASH,
            effective_task_spec_hash=TASK_HASH,
            required_acceptance_hash=ACCEPTANCE_HASH,
            effective_required_acceptance_hash=ACCEPTANCE_HASH,
            required_checks_passed=False,
            unknown_acceptance_evidence=False,
            fallback_history=(fallback,),
        )
