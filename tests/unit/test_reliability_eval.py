from copy import deepcopy
from pathlib import Path

import yaml

from horizon.application.reliability_eval import ReliabilityEvaluator
from horizon.domain.reliability import ReliabilityEvalManifest

MANIFEST_PATH = (
    Path(__file__).resolve().parents[2]
    / "benchmarks"
    / "reliability"
    / "horizon-controller-v1.yaml"
)


def load_manifest_dict():
    return yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_frozen_controller_reliability_manifest_passes_without_external_calls():
    manifest = ReliabilityEvalManifest.model_validate(load_manifest_dict())

    report = ReliabilityEvaluator().evaluate(manifest)

    assert manifest.sha256 == "4d27b02f9254bfe792dbd8a782cb1ba67f4b5f83a420ab96f437870a81d4a66f"
    assert report.case_count == 12
    assert report.passed_case_count == 12
    assert report.case_pass_rate == 1.0
    assert report.policy_decision_count == 41
    assert report.correct_policy_decision_count == 41
    assert report.policy_decision_accuracy == 1.0
    assert report.false_positive_count == 0
    assert report.false_negative_count == 0
    assert report.paid_model_called is False
    assert report.network_called is False
    assert report.repository_code_executed is False
    authority_case = next(
        case for case in report.cases if case.case_id == "reject-tool-authority-expansion"
    )
    assert (
        authority_case.rejection_reason
        == "Plan contains tools outside the task execution authority"
    )


def test_reliability_evaluator_preserves_a_mislabeled_negative_result():
    raw = deepcopy(load_manifest_dict())
    case = next(
        case for case in raw["cases"] if case["case_id"] == "varied-fifth-action-is-not-cycle"
    )
    case["actions"][-1]["expected_decision"] = "soft_block"
    case["actions"][-1]["expected_pattern"] = "alternating_two_action_cycle"
    manifest = ReliabilityEvalManifest.model_validate(raw)

    report = ReliabilityEvaluator().evaluate(manifest)

    assert report.passed_case_count == 11
    assert report.case_pass_rate < 1.0
    assert report.policy_decision_accuracy < 1.0
    assert report.false_negative_count == 1
    failed = next(
        result for result in report.cases if result.case_id == "varied-fifth-action-is-not-cycle"
    )
    assert failed.passed is False
    assert failed.steps[-1].observed_decision == "allow"
