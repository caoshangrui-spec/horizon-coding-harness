from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from horizon.domain.recovery import write_recovery_verdict
from horizon.domain.recovery_evaluation import RecoveryMatrixManifest

MANIFEST_ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "recovery"
V1_MANIFEST_PATH = MANIFEST_ROOT / "horizon-write-recovery-v1.yaml"
V2_MANIFEST_PATH = MANIFEST_ROOT / "horizon-recovery-matrix-v2.yaml"
V3_MANIFEST_PATH = MANIFEST_ROOT / "horizon-recovery-matrix-v3.yaml"


def load_manifest_dict(path=V3_MANIFEST_PATH):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("state", "decision", "verdict"),
    [
        ("pre_effect", "accept", "block"),
        ("pre_effect", "rollback", "rollback"),
        ("expected_effect", "accept", "accept"),
        ("expected_effect", "rollback", "rollback"),
        ("diverged", "accept", "block"),
        ("diverged", "rollback", "block"),
    ],
)
def test_write_recovery_policy_is_conservative(state, decision, verdict):
    assert write_recovery_verdict(state, decision) == verdict


def test_frozen_v1_recovery_matrix_remains_readable_with_the_same_digest():
    manifest = RecoveryMatrixManifest.model_validate(load_manifest_dict(V1_MANIFEST_PATH))

    assert manifest.sha256 == "efacbe3f55bcf3efee78ee3397fffb44a3a855d1924be2f9307aa55aa45f77a4"
    assert manifest.schema_version == 1
    hard_crashes = [case for case in manifest.cases if case.kind == "hard_crash"]
    model_crashes = [case for case in manifest.cases if case.kind == "model_response_hard_crash"]
    promotion_crashes = [case for case in manifest.cases if case.kind == "promotion_hard_crash"]
    write_cases = [case for case in manifest.cases if case.kind == "write_state"]
    assert len(hard_crashes) == 1
    assert not model_crashes
    assert not promotion_crashes
    assert len(write_cases) == 18


def test_frozen_v2_adds_one_model_response_crash_and_preserves_write_cross_product():
    manifest = RecoveryMatrixManifest.model_validate(load_manifest_dict(V2_MANIFEST_PATH))
    v1 = RecoveryMatrixManifest.model_validate(load_manifest_dict(V1_MANIFEST_PATH))

    assert manifest.schema_version == 2
    assert manifest.sha256 == "5f11e000760192c5f2ebb827e97745202e77ad162ad0a1166feae426bb5d9eda"
    assert [case for case in manifest.cases if case.kind != "model_response_hard_crash"] == list(
        v1.cases
    )
    assert len([case for case in manifest.cases if case.kind == "hard_crash"]) == 1
    assert len([case for case in manifest.cases if case.kind == "model_response_hard_crash"]) == 1
    assert not [case for case in manifest.cases if case.kind == "promotion_hard_crash"]
    write_cases = [case for case in manifest.cases if case.kind == "write_state"]
    assert len(write_cases) == 18
    assert {(case.tool, case.injected_state, case.decision) for case in write_cases} == {
        (tool, state, decision)
        for tool in ("replace_text", "apply_patch", "create_file")
        for state in ("pre_effect", "expected_effect", "diverged")
        for decision in ("accept", "rollback")
    }


def test_frozen_v3_adds_one_promotion_crash_and_preserves_every_v2_case():
    manifest = RecoveryMatrixManifest.model_validate(load_manifest_dict())
    v2 = RecoveryMatrixManifest.model_validate(load_manifest_dict(V2_MANIFEST_PATH))

    assert manifest.schema_version == 3
    assert manifest.sha256 == "cb7dd9e36adffeb87b6d7cbe99e22149484eeee4d59df0d1bc4f2871b553ca31"
    assert [case for case in manifest.cases if case.kind != "promotion_hard_crash"] == list(
        v2.cases
    )
    assert len([case for case in manifest.cases if case.kind == "promotion_hard_crash"]) == 1


def test_recovery_matrix_rejects_duplicate_state_decision_case():
    raw = deepcopy(load_manifest_dict())
    duplicate = deepcopy(next(case for case in raw["cases"] if case["kind"] == "write_state"))
    duplicate["case_id"] = "duplicate-write-case"
    raw["cases"].append(duplicate)

    with pytest.raises(ValidationError, match="state/decision combinations must be unique"):
        RecoveryMatrixManifest.model_validate(raw)


def test_schema_v1_rejects_the_v2_model_response_crash_case():
    raw = deepcopy(load_manifest_dict(V1_MANIFEST_PATH))
    model_case = deepcopy(
        next(
            case
            for case in load_manifest_dict(V2_MANIFEST_PATH)["cases"]
            if case["kind"] == "model_response_hard_crash"
        )
    )
    raw["cases"].insert(1, model_case)

    with pytest.raises(ValidationError, match="schema v1 requires 0 model-response"):
        RecoveryMatrixManifest.model_validate(raw)


def test_schema_v2_rejects_the_v3_promotion_crash_case():
    raw = deepcopy(load_manifest_dict(V2_MANIFEST_PATH))
    promotion_case = deepcopy(
        next(
            case
            for case in load_manifest_dict(V3_MANIFEST_PATH)["cases"]
            if case["kind"] == "promotion_hard_crash"
        )
    )
    raw["cases"].insert(2, promotion_case)

    with pytest.raises(ValidationError, match="schema v2 requires 0 promotion"):
        RecoveryMatrixManifest.model_validate(raw)
