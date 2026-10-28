from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from horizon.application.task_registry import IndependentTaskRegistryPreflight
from horizon.domain.errors import IntegrityError
from horizon.domain.task_registry import IndependentTaskRegistry

REGISTRY_PATH = Path(__file__).resolve().parents[2] / "benchmarks" / "independent-tasks-v1.yaml"


def load_registry_dict():
    return yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))


def test_frozen_independent_task_registry_has_honest_offline_readiness_baseline():
    registry = IndependentTaskRegistry.model_validate(load_registry_dict())

    report = IndependentTaskRegistryPreflight().evaluate(
        registry,
        registry_path=REGISTRY_PATH,
    )

    assert registry.sha256 == "1b27f52f3275641b62beb356da465ffdb5f8b9569fc8b65522042dd40d7e9f2b"
    assert report.preflight_passed is True
    assert report.task_count == 24
    assert report.unique_source_task_count == 24
    assert report.development_task_count == 16
    assert report.holdout_task_count == 8
    assert report.frozen_source_recipe_count == 16
    assert report.metadata_only_source_count == 8
    assert report.public_historical_initial_failure_count == 8
    assert report.benchmark_declared_initial_failure_count == 16
    assert report.execution_manifest_count == 8
    assert report.full_checkout_manifest_count == 7
    assert report.dependency_reduced_manifest_count == 1
    assert report.not_prepared_execution_count == 16
    assert report.historical_run_status_reverified_online is False
    assert report.paid_model_called is False
    assert report.network_called is False
    assert report.repository_code_executed is False
    assert any("re-executed" in claim for claim in report.excluded_claims)
    assert any("TaskSpecs" in claim for claim in report.excluded_claims)


def test_registry_rejects_duplicate_upstream_bug_identity():
    raw = deepcopy(load_registry_dict())
    raw["tasks"][1]["bug_id"] = raw["tasks"][0]["bug_id"]

    with pytest.raises(ValidationError, match="source bug identities must be unique"):
        IndependentTaskRegistry.model_validate(raw)


def test_preflight_rejects_case_metadata_drift_from_frozen_selection():
    raw = deepcopy(load_registry_dict())
    raw["tasks"][0]["fixed_commit"] = "0" * 40
    registry = IndependentTaskRegistry.model_validate(raw)

    with pytest.raises(IntegrityError, match="drifted from frozen selection"):
        IndependentTaskRegistryPreflight().evaluate(
            registry,
            registry_path=REGISTRY_PATH,
        )


def test_preflight_rejects_missing_execution_evidence_case():
    raw = deepcopy(load_registry_dict())
    raw["tasks"][0]["evidence"]["execution_case_id"] = "missing-case"
    registry = IndependentTaskRegistry.model_validate(raw)

    with pytest.raises(IntegrityError, match="Execution suite omits registry task"):
        IndependentTaskRegistryPreflight().evaluate(
            registry,
            registry_path=REGISTRY_PATH,
        )
