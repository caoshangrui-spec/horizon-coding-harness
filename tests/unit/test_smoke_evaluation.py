from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from horizon.application.smoke_evaluation import RealModelSmokePreflight
from horizon.domain.errors import IntegrityError
from horizon.domain.smoke_evaluation import RealModelSmokeSuite

ROOT = Path(__file__).resolve().parents[2]
SUITE_PATH = ROOT / "benchmarks" / "real-model-smoke-v1.yaml"


def _suite_data() -> dict:
    return yaml.safe_load(SUITE_PATH.read_text(encoding="utf-8"))


def test_real_model_smoke_preflight_freezes_five_distinct_zero_cost_tasks():
    suite = RealModelSmokeSuite.model_validate(_suite_data())

    report = RealModelSmokePreflight().evaluate(suite, suite_path=SUITE_PATH)

    assert [task.task_id for task in report.tasks] == [
        "httpie-1-unique-download-filename",
        "luigi-2-beam-target-path",
        "sanic-2-asyncio-start-serving",
        "thefuck-2-executable-path-separator",
        "tornado-2-redirect-put-without-body",
    ]
    assert len({task.project.casefold() for task in report.tasks}) == 5
    assert report.frozen_source_recipe_count == 5
    assert report.full_checkout_manifest_count == 5
    assert report.public_historical_initial_failure_count == 5
    assert report.max_model_calls == 14
    assert report.max_repair_cycles == 2
    assert report.max_execution_replans == 1
    assert report.structural_preflight_passed is True
    assert report.paid_launch_ready is False
    assert report.paid_model_called is False
    assert report.network_called is False
    assert report.repository_code_executed is False
    assert report.external_cost_cny == 0


def test_smoke_preflight_rejects_registry_digest_drift():
    data = _suite_data()
    data["registry_digest"] = "0" * 64
    suite = RealModelSmokeSuite.model_validate(data)

    with pytest.raises(IntegrityError, match="registry digest"):
        RealModelSmokePreflight().evaluate(suite, suite_path=SUITE_PATH)


def test_smoke_preflight_rejects_holdout_substitution():
    data = _suite_data()
    data["tasks"][0]["task_id"] = "cookiecutter-4-failing-hook"
    suite = RealModelSmokeSuite.model_validate(data)

    with pytest.raises(IntegrityError, match="not in development"):
        RealModelSmokePreflight().evaluate(suite, suite_path=SUITE_PATH)


def test_smoke_suite_rejects_prior_paid_task_and_unapproved_cost_cap():
    excluded = _suite_data()
    excluded["tasks"][0]["task_id"] = "httpie-4-host-header-overwrite"
    with pytest.raises(ValidationError, match="explicit exclusion"):
        RealModelSmokeSuite.model_validate(excluded)

    cost_bound = _suite_data()
    cost_bound["execution_policy"]["max_cost_cny"] = "0.10"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RealModelSmokeSuite.model_validate(cost_bound)
