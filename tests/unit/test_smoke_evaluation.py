from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from horizon.application.smoke_evaluation import RealModelSmokePreflight
from horizon.domain.errors import IntegrityError
from horizon.domain.smoke_evaluation import (
    RealModelSmokeEvaluatorManifest,
    RealModelSmokeSuite,
)

ROOT = Path(__file__).resolve().parents[2]
SUITE_PATH = ROOT / "benchmarks" / "real-model-smoke-v1.yaml"
EVALUATOR_PATH = ROOT / "benchmarks" / "real-model-smoke-evaluator-v1.yaml"


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


def test_checked_in_smoke_evaluator_is_suite_bound_and_points_at_repair_regions():
    suite = RealModelSmokeSuite.model_validate(_suite_data())
    evaluator = RealModelSmokeEvaluatorManifest.model_validate(
        yaml.safe_load(EVALUATOR_PATH.read_text(encoding="utf-8"))
    )

    assert evaluator.model_visible is False
    assert evaluator.suite_id == suite.suite_id
    assert evaluator.suite_digest == suite.sha256
    assert {task.smoke_task_id for task in evaluator.tasks} == {
        task.task_id for task in suite.tasks
    }
    for evaluator_task in evaluator.tasks:
        case_root = ROOT / "benchmarks" / "run_ab" / "full" / evaluator_task.smoke_task_id
        manifest = yaml.safe_load((case_root / "manifest.yaml").read_text(encoding="utf-8"))
        replacements = [
            action["arguments"]
            for arm in manifest["arms"]
            for action in arm["actions"]
            if action.get("tool") == "replace_text"
        ]
        matched = False
        for location in evaluator_task.target_locations:
            source = (case_root / "fixture" / location.path).read_text(encoding="utf-8")
            for replacement in replacements:
                if replacement["path"] != location.path:
                    continue
                cursor = 0
                while (offset := source.find(replacement["old"], cursor)) >= 0:
                    start = source.count("\n", 0, offset) + 1
                    end = start + replacement["old"].count("\n")
                    matched = matched or (start <= location.end_line and end >= location.start_line)
                    cursor = offset + 1
        assert matched, evaluator_task.smoke_task_id


def test_smoke_evaluator_cannot_be_marked_model_visible():
    data = yaml.safe_load(EVALUATOR_PATH.read_text(encoding="utf-8"))
    data["model_visible"] = True

    with pytest.raises(ValidationError, match="Input should be False"):
        RealModelSmokeEvaluatorManifest.model_validate(data)
