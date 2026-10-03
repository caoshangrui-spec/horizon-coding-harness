import hashlib
import importlib
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.application.run_ab_eval import RunABEvaluator
from horizon.domain.events import Event
from horizon.domain.run_evaluation import RunABEvalManifest, RunABSuiteManifest
from horizon.domain.states import RunStatus
from horizon.domain.tools import AcceptanceResult
from horizon.interfaces.cli.app import app

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "benchmarks" / "run_ab" / "stalled-reader-replan-v1.yaml"
SUITE_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-reduced-v1.yaml"
FULL_SUITE_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-full-checkout-pilot-v1.yaml"
MULTI_STAGE_SUITE_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-multi-stage-pilot-v1.yaml"


class FixtureContentAcceptance:
    """Test-only validator; production CLI uses the existing Docker executor."""

    def execute(self, workspace, check):
        content = (workspace / "src/parser.sh").read_text(encoding="utf-8")
        passed = "printf '[]\\n'" in content and "printf '[%s]\\n'" in content
        output = "2 markers passed" if passed else "parser formatting markers are missing"
        return AcceptanceResult(
            check_id=check.id,
            passed=passed,
            exit_code=0 if passed else 1,
            timed_out=False,
            output=output,
            output_hash=hashlib.sha256(output.encode("utf-8")).hexdigest(),
        )


class ExternalSourceAcceptance:
    """Fast marker checks for the suite CLI test; Docker executes the real Python checks."""

    expected = {
        "cookiecutter-utf8": (
            "cookiecutter/generate.py",
            "open(context_file, encoding='utf-8')",
        ),
        "fastapi-nested-clone": (
            "fastapi/utils.py",
            "use_type.__fields__[f.name] = create_cloned_field(f)",
        ),
        "tqdm-enumerate-start": (
            "tqdm/contrib/__init__.py",
            "enumerate(tqdm_class(iterable, **tqdm_kwargs), start)",
        ),
    }

    def execute(self, workspace, check):
        path, marker = self.expected[check.id]
        passed = marker in (workspace / path).read_text(encoding="utf-8")
        output = "expected repair present" if passed else "expected repair absent"
        return AcceptanceResult(
            check_id=check.id,
            passed=passed,
            exit_code=0 if passed else 1,
            timed_out=False,
            output=output,
            output_hash=hashlib.sha256(output.encode("utf-8")).hexdigest(),
        )


class TwoStageAcceptance:
    """Small fixture contract used to exercise evaluator-owned worker restart injection."""

    def execute(self, workspace, check):
        content = (workspace / "src/parser.sh").read_text(encoding="utf-8")
        markers = {
            "empty-contract": "printf '[]\\n'" in content,
            "nonempty-contract": "printf '[%s]\\n'" in content,
        }
        passed = markers[check.id]
        output = "expected stage marker present" if passed else "expected stage marker absent"
        return AcceptanceResult(
            check_id=check.id,
            passed=passed,
            exit_code=0 if passed else 1,
            timed_out=False,
            output=output,
            output_hash=hashlib.sha256(output.encode("utf-8")).hexdigest(),
        )


def load_manifest():
    return RunABEvalManifest.model_validate(
        yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    )


def test_run_ab_evaluator_compares_full_replayable_runs_without_paid_model(tmp_path):
    manifest = load_manifest()
    fixture = MANIFEST_PATH.parent / manifest.fixture_path
    original = (fixture / "src/parser.sh").read_bytes()

    report = RunABEvaluator(
        FixtureContentAcceptance(),
        validation_backend="fixture-content",
        validation_backend_ref="test-only-marker-contract",
        repository_code_executed=False,
    ).evaluate(
        manifest,
        fixture_source=fixture,
        state_dir=tmp_path / "state",
    )

    assert report.all_expectations_met is True
    assert report.initial_validation_matches_expectation is True
    assert report.initial_failed_checks == ("parser-contract",)
    assert report.initial_validation[0].passed is False
    assert report.treatment_recovered is True
    assert report.success_delta == 1
    assert report.baseline.status == RunStatus.WAITING_FOR_USER
    assert report.baseline.human_request_pattern == "identical_action"
    assert report.baseline.usage.model_calls == 4
    assert report.baseline.usage.tool_calls == 4
    assert report.single_replan.status == RunStatus.SUCCEEDED
    assert report.single_replan.plan_version == 2
    assert report.single_replan.execution_replans == 1
    assert report.single_replan.passed_items == ("repair-parser",)
    assert report.single_replan.final_validation_passed is True
    assert report.single_replan.usage.model_calls == 6
    assert report.single_replan.usage.tool_calls == 7
    assert report.model_call_delta == 2
    assert report.tool_call_delta == 3
    assert report.step_delta == 3
    assert str(report.baseline.model_cost) == "0.00192"
    assert str(report.single_replan.model_cost) == "0.00288"
    assert str(report.model_cost_delta) == "0.00096"
    assert report.paid_model_called is False
    assert report.network_called is False
    assert report.repository_code_executed is False
    assert (fixture / "src/parser.sh").read_bytes() == original

    artifacts = ArtifactStore(tmp_path / "state" / "artifacts")
    for arm in (report.baseline, report.single_replan):
        assert arm.trace_replay_verified is True
        assert arm.source_workspace_unchanged is True
        assert arm.actions_consumed is True
        replayed = SQLiteEventStore.replay_jsonl(artifacts.read(arm.trace_ref).decode("utf-8"))
        assert replayed.run_id == arm.run_id


def test_run_ab_cli_uses_existing_image_contract_and_persists_report(tmp_path, monkeypatch):
    cli_module = importlib.import_module("horizon.interfaces.cli.app")

    class FakeSandbox:
        def __init__(self, staging_root, image):
            self.image_id = "sha256:" + "1" * 64

    monkeypatch.setattr(cli_module, "DockerSandbox", FakeSandbox)
    monkeypatch.setattr(
        cli_module,
        "DockerAcceptanceExecutor",
        lambda sandbox: FixtureContentAcceptance(),
    )
    state = tmp_path / "state"

    result = runner.invoke(
        app,
        [
            "eval",
            "run-ab",
            str(MANIFEST_PATH),
            "--image",
            "fixture:local",
            "--state-dir",
            str(state),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    report = payload["report"]
    assert report["all_expectations_met"] is True
    assert report["initial_validation_matches_expectation"] is True
    assert report["treatment_recovered"] is True
    assert report["validation_backend"] == "docker"
    assert report["validation_backend_ref"] == "sha256:" + "1" * 64
    assert report["paid_model_called"] is False
    report_ref = payload["report_ref"]
    report_path = state / "artifacts" / report_ref[:2] / report_ref
    assert report_path.is_file()
    assert json.loads(report_path.read_text(encoding="utf-8"))["benchmark_id"] == (
        "stalled-reader-replan-v1"
    )


def test_run_ab_rejects_state_inside_fixture_before_creating_it(tmp_path):
    manifest = load_manifest()
    source_fixture = MANIFEST_PATH.parent / manifest.fixture_path
    fixture = tmp_path / "fixture"
    shutil.copytree(source_fixture, fixture)
    state = fixture / ".evaluation-state"

    with pytest.raises(ValueError, match="cannot live inside"):
        RunABEvaluator(
            FixtureContentAcceptance(),
            validation_backend="fixture-content",
            validation_backend_ref="test-only-marker-contract",
            repository_code_executed=False,
        ).evaluate(manifest, fixture_source=fixture, state_dir=state)

    assert not state.exists()


def test_run_ab_restarts_worker_at_persisted_work_item_boundary(tmp_path):
    data = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    data["benchmark_id"] = "worker-boundary-restart-v1"
    data["task"]["acceptance"] = [
        {
            "id": "empty-contract",
            "command": "unused-empty-check",
            "timeout_seconds": 30,
            "required": True,
        },
        {
            "id": "nonempty-contract",
            "command": "unused-nonempty-check",
            "timeout_seconds": 30,
            "required": True,
        },
    ]
    data["initial_plan"] = {
        "version": 1,
        "items": [
            {
                "work_item_id": "guard-empty",
                "title": "Guard empty input",
                "objective": "Render empty input as an empty list",
                "dependencies": [],
                "expected_artifacts": ["empty-contract receipt"],
                "acceptance_ids": ["empty-contract"],
                "allowed_tools": ["replace_text"],
            },
            {
                "work_item_id": "guard-nonempty",
                "title": "Wrap non-empty input",
                "objective": "Render non-empty input as a one-item list",
                "dependencies": ["guard-empty"],
                "expected_artifacts": ["nonempty-contract receipt"],
                "acceptance_ids": ["nonempty-contract"],
                "allowed_tools": ["replace_text"],
            },
        ],
    }
    data["expected_initial_failed_checks"] = ["empty-contract", "nonempty-contract"]
    actions = [
        {
            "tool": "replace_text",
            "arguments": {
                "path": "src/parser.sh",
                "old": "printf '%s\\n' \"$value\"",
                "new": (
                    'if [ -z "$value" ]; then\n'
                    "  printf '[]\\n'\n"
                    "else\n"
                    "  printf '%s\\n' \"$value\"\n"
                    "fi"
                ),
            },
        },
        {"tool": "submit", "arguments": {"summary": "Guarded empty input."}},
        {
            "tool": "replace_text",
            "arguments": {
                "path": "src/parser.sh",
                "old": "printf '%s\\n' \"$value\"",
                "new": "printf '[%s]\\n' \"$value\"",
            },
        },
        {"tool": "submit", "arguments": {"summary": "Wrapped non-empty input."}},
    ]
    data["arms"] = [
        {
            "arm_id": arm_id,
            "role": role,
            "description": "Complete both stages across one persisted worker restart.",
            "expected_status": "SUCCEEDED",
            "expected_plan_version": 1,
            "expected_execution_replans": 0,
            "expected_passed_items": ["guard-empty", "guard-nonempty"],
            "expected_model_calls": 4,
            "expected_tool_calls": 7,
            "expected_steps": 7,
            "restart_after_model_calls": 2,
            "actions": actions,
        }
        for arm_id, role in (
            ("baseline-restart", "baseline"),
            ("treatment-restart", "single_replan"),
        )
    ]
    manifest = RunABEvalManifest.model_validate(data)
    fixture = MANIFEST_PATH.parent / manifest.fixture_path

    report = RunABEvaluator(
        TwoStageAcceptance(),
        validation_backend="fixture-content",
        validation_backend_ref="test-only-two-stage-contract",
        repository_code_executed=False,
    ).evaluate(manifest, fixture_source=fixture, state_dir=tmp_path / "state")

    assert report.all_expectations_met is True
    assert report.initial_failed_checks == ("empty-contract", "nonempty-contract")
    assert report.treatment_recovered is False
    artifacts = ArtifactStore(tmp_path / "state" / "artifacts")
    for arm in (report.baseline, report.single_replan):
        assert arm.status == RunStatus.SUCCEEDED
        assert arm.worker_restarts == 1
        assert arm.final_lease_epoch == 2
        assert arm.passed_items == ("guard-empty", "guard-nonempty")
        assert arm.actions_consumed is True
        assert arm.trace_replay_verified is True
        assert arm.source_workspace_unchanged is True
        events = [
            Event.model_validate_json(line)
            for line in artifacts.read(arm.trace_ref).decode("utf-8").splitlines()
        ]
        restarted_lease = next(
            event
            for event in events
            if event.event_type == "LEASE_ACQUIRED" and event.payload["epoch"] == 2
        )
        assert datetime.fromisoformat(
            restarted_lease.payload["expires_at"]
        ) - datetime.fromisoformat(restarted_lease.created_at) == timedelta(seconds=600)


def test_run_ab_suite_cli_aggregates_source_bound_cases(tmp_path, monkeypatch):
    cli_module = importlib.import_module("horizon.interfaces.cli.app")

    class FakeSandbox:
        def __init__(self, staging_root, image):
            self.image_id = "sha256:" + "2" * 64

    monkeypatch.setattr(cli_module, "DockerSandbox", FakeSandbox)
    monkeypatch.setattr(
        cli_module,
        "DockerAcceptanceExecutor",
        lambda sandbox: ExternalSourceAcceptance(),
    )
    state = tmp_path / "suite-state"

    result = runner.invoke(
        app,
        [
            "eval",
            "run-ab-suite",
            str(SUITE_PATH),
            "--image",
            "fixture:local",
            "--state-dir",
            str(state),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    report = payload["report"]
    assert report["suite_id"] == "bugsinpy-reduced-v1"
    assert report["case_count"] == 3
    assert report["passed_case_count"] == 3
    assert report["initial_failure_confirmed_count"] == 3
    assert report["treatment_recovered_count"] == 3
    assert report["success_delta"] == 3
    assert report["model_call_delta"] == 6
    assert report["tool_call_delta"] == 9
    assert report["step_delta"] == 9
    assert report["model_cost_delta"] == "0.00288"
    assert report["all_expectations_met"] is True
    assert report["paid_model_called"] is False
    assert report["network_called"] is False
    assert report["repository_code_executed"] is True
    assert {case["source"]["project"] for case in report["cases"]} == {
        "cookiecutter",
        "fastapi",
        "tqdm",
    }

    artifacts = ArtifactStore(state / "artifacts")
    suite_ref = payload["report_ref"]
    assert json.loads(artifacts.read(suite_ref))["suite_id"] == "bugsinpy-reduced-v1"
    for case in report["cases"]:
        case_report = json.loads(artifacts.read(case["report_ref"]))
        assert case_report["initial_validation_matches_expectation"] is True
        assert case_report["all_expectations_met"] is True


def test_run_ab_suite_rejects_manifest_with_mismatched_source_commit():
    suite_data = yaml.safe_load(SUITE_PATH.read_text(encoding="utf-8"))
    suite = RunABSuiteManifest.model_validate(suite_data)
    case = suite.cases[0]
    case_path = SUITE_PATH.parent / case.manifest_path
    case_data = yaml.safe_load(case_path.read_text(encoding="utf-8"))
    case_data["task"]["repository"]["base_commit"] = "0" * 40
    case_manifest = RunABEvalManifest.model_validate(case_data)

    with pytest.raises(ValueError, match="source buggy commit must match"):
        case.check_manifest(case_manifest)

    suite_data["cases"][0]["source"]["fix_url"] = (
        "https://github.com/cookiecutter/cookiecutter/commit/" + "0" * 40
    )
    with pytest.raises(ValueError, match="fix URL must identify its fixed commit"):
        RunABSuiteManifest.model_validate(suite_data)


def test_full_checkout_suite_binds_two_projects_to_exact_case_manifests():
    suite = RunABSuiteManifest.model_validate(
        yaml.safe_load(FULL_SUITE_PATH.read_text(encoding="utf-8"))
    )

    assert {case.source.project for case in suite.cases} == {"tqdm", "youtube-dl"}
    assert all(case.source.reduction == "full_checkout" for case in suite.cases)

    for case in suite.cases:
        case_path = FULL_SUITE_PATH.parent / case.manifest_path
        case_manifest = RunABEvalManifest.model_validate(
            yaml.safe_load(case_path.read_text(encoding="utf-8"))
        )
        case.check_manifest(case_manifest)


def test_multi_stage_full_checkout_suite_binds_dependency_order_and_source():
    suite = RunABSuiteManifest.model_validate(
        yaml.safe_load(MULTI_STAGE_SUITE_PATH.read_text(encoding="utf-8"))
    )
    assert len(suite.cases) == 1
    case = suite.cases[0]
    assert case.source.project == "youtube-dl"
    assert case.source.reduction == "full_checkout"

    case_path = MULTI_STAGE_SUITE_PATH.parent / case.manifest_path
    manifest = RunABEvalManifest.model_validate(
        yaml.safe_load(case_path.read_text(encoding="utf-8"))
    )
    case.check_manifest(manifest)

    first, second = manifest.initial_plan.items
    assert second.dependencies == (first.work_item_id,)
    assert {arm.restart_after_model_calls for arm in manifest.arms} == {3}
    assert set(first.acceptance_ids) | set(second.acceptance_ids) == {
        "youtube-dl-unescape-html-behavior",
        "youtube-dl-unescape-html-regression-source",
    }
