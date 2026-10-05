from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from horizon.domain.recovery import write_recovery_verdict
from horizon.domain.recovery_evaluation import RecoveryMatrixManifest

MANIFEST_PATH = (
    Path(__file__).resolve().parents[2]
    / "benchmarks"
    / "recovery"
    / "horizon-write-recovery-v1.yaml"
)


def load_manifest_dict():
    return yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))


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


def test_frozen_recovery_matrix_covers_all_write_state_decisions_and_one_hard_crash():
    manifest = RecoveryMatrixManifest.model_validate(load_manifest_dict())

    assert manifest.sha256 == "efacbe3f55bcf3efee78ee3397fffb44a3a855d1924be2f9307aa55aa45f77a4"
    hard_crashes = [case for case in manifest.cases if case.kind == "hard_crash"]
    write_cases = [case for case in manifest.cases if case.kind == "write_state"]
    assert len(hard_crashes) == 1
    assert len(write_cases) == 18
    assert {(case.tool, case.injected_state, case.decision) for case in write_cases} == {
        (tool, state, decision)
        for tool in ("replace_text", "apply_patch", "create_file")
        for state in ("pre_effect", "expected_effect", "diverged")
        for decision in ("accept", "rollback")
    }


def test_recovery_matrix_rejects_duplicate_state_decision_case():
    raw = deepcopy(load_manifest_dict())
    duplicate = deepcopy(raw["cases"][1])
    duplicate["case_id"] = "duplicate-write-case"
    raw["cases"].append(duplicate)

    with pytest.raises(ValidationError, match="state/decision combinations must be unique"):
        RecoveryMatrixManifest.model_validate(raw)
