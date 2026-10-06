import hashlib
import importlib
import json
import shutil
import subprocess
import sys
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
SUITE_V1_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-reduced-v1.yaml"
SUITE_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-reduced-v2.yaml"
FULL_SUITE_V1_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-full-checkout-pilot-v1.yaml"
FULL_SUITE_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-full-checkout-pilot-v2.yaml"
LUIGI_FULL_MANIFEST_PATH = (
    ROOT / "benchmarks" / "run_ab" / "full" / "luigi-1-metrics-handler" / "manifest.yaml"
)
MULTI_STAGE_V1_SUITE_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-multi-stage-pilot-v1.yaml"
MULTI_STAGE_V2_SUITE_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-multi-stage-pilot-v2.yaml"
MULTI_STAGE_SUITE_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-multi-stage-pilot-v3.yaml"
LUIGI_MULTI_STAGE_MANIFEST_PATH = (
    ROOT / "benchmarks" / "run_ab" / "full" / "luigi-1-metrics-handler" / "multi-stage-v3.yaml"
)


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


class TrustedFixturePythonAcceptance:
    """Execute only the frozen dependency-free Python fixture checks in this test process."""

    def execute(self, workspace, check):
        parts = check.command.split()
        assert parts[:2] == ["python", "-B"] and len(parts) == 3
        completed = subprocess.run(
            [sys.executable, "-B", parts[2]],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=check.timeout_seconds,
        )
        output = completed.stdout + completed.stderr
        return AcceptanceResult(
            check_id=check.id,
            passed=completed.returncode == 0,
            exit_code=completed.returncode,
            timed_out=False,
            output=output,
            output_hash=hashlib.sha256(output.encode("utf-8")).hexdigest(),
        )


class MultiStageAcceptance:
    """Small fixture contract used to exercise evaluator-owned worker restart injection."""

    def execute(self, workspace, check):
        content = (workspace / "src/parser.sh").read_text(encoding="utf-8")
        empty_guarded = "printf '[]\\n'" in content
        nonempty_wrapped = "printf '[%s]\\n'" in content
        markers = {
            "empty-contract": empty_guarded,
            "nonempty-contract": nonempty_wrapped,
            "combined-contract": empty_guarded and nonempty_wrapped,
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


@pytest.mark.parametrize(
    ("restart_points", "message"),
    [
        ([2, 2], "unique and strictly increasing"),
        ([3, 2], "unique and strictly increasing"),
        ([7], "leave at least one scripted action"),
    ],
)
def test_run_ab_rejects_invalid_worker_restart_points(restart_points, message):
    data = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    data["arms"][0]["restart_after_model_calls"] = restart_points

    with pytest.raises(ValueError, match=message):
        RunABEvalManifest.model_validate(data)


@pytest.mark.parametrize(
    ("restart_after_model_calls", "expected_restarts"),
    [(2, 1), ([2, 4], 2)],
)
def test_run_ab_restarts_workers_at_persisted_work_item_boundaries(
    tmp_path,
    restart_after_model_calls,
    expected_restarts,
):
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
        {
            "id": "combined-contract",
            "command": "unused-combined-check",
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
            {
                "work_item_id": "verify-combined",
                "title": "Verify both parser contracts",
                "objective": "Run the final combined parser contract",
                "dependencies": ["guard-nonempty"],
                "expected_artifacts": ["combined-contract receipt"],
                "acceptance_ids": ["combined-contract"],
                "allowed_tools": ["run_check"],
            },
        ],
    }
    data["expected_initial_failed_checks"] = [
        "empty-contract",
        "nonempty-contract",
        "combined-contract",
    ]
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
        {"tool": "submit", "arguments": {"summary": "Verified both parser contracts."}},
    ]
    data["arms"] = [
        {
            "arm_id": arm_id,
            "role": role,
            "description": "Complete three stages across persisted worker boundaries.",
            "expected_status": "SUCCEEDED",
            "expected_plan_version": 1,
            "expected_execution_replans": 0,
            "expected_passed_items": ["guard-empty", "guard-nonempty", "verify-combined"],
            "expected_model_calls": 5,
            "expected_tool_calls": 10,
            "expected_steps": 10,
            "restart_after_model_calls": restart_after_model_calls,
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
        MultiStageAcceptance(),
        validation_backend="fixture-content",
        validation_backend_ref="test-only-two-stage-contract",
        repository_code_executed=False,
    ).evaluate(manifest, fixture_source=fixture, state_dir=tmp_path / "state")

    assert report.all_expectations_met is True
    assert report.initial_failed_checks == (
        "combined-contract",
        "empty-contract",
        "nonempty-contract",
    )
    assert report.treatment_recovered is False
    artifacts = ArtifactStore(tmp_path / "state" / "artifacts")
    for arm in (report.baseline, report.single_replan):
        assert arm.status == RunStatus.SUCCEEDED
        assert arm.worker_restarts == expected_restarts
        assert arm.final_lease_epoch == expected_restarts + 1
        assert arm.passed_items == ("guard-empty", "guard-nonempty", "verify-combined")
        assert arm.actions_consumed is True
        assert arm.trace_replay_verified is True
        assert arm.source_workspace_unchanged is True
        events = [
            Event.model_validate_json(line)
            for line in artifacts.read(arm.trace_ref).decode("utf-8").splitlines()
        ]
        restarted_leases = [
            event
            for event in events
            if event.event_type == "LEASE_ACQUIRED" and event.payload["epoch"] > 1
        ]
        assert [event.payload["epoch"] for event in restarted_leases] == list(
            range(2, expected_restarts + 2)
        )
        for restarted_lease in restarted_leases:
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
        lambda sandbox: TrustedFixturePythonAcceptance(),
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
    assert report["suite_id"] == "bugsinpy-reduced-v2"
    assert report["case_count"] == 5
    assert report["passed_case_count"] == 5
    assert report["initial_failure_confirmed_count"] == 5
    assert report["treatment_recovered_count"] == 5
    assert report["success_delta"] == 5
    assert report["model_call_delta"] == 10
    assert report["tool_call_delta"] == 15
    assert report["step_delta"] == 15
    assert report["model_cost_delta"] == "0.00480"
    assert report["all_expectations_met"] is True
    assert report["paid_model_called"] is False
    assert report["network_called"] is False
    assert report["repository_code_executed"] is True
    assert {case["source"]["project"] for case in report["cases"]} == {
        "cookiecutter",
        "fastapi",
        "luigi",
        "tqdm",
        "tornado",
    }

    artifacts = ArtifactStore(state / "artifacts")
    suite_ref = payload["report_ref"]
    assert json.loads(artifacts.read(suite_ref))["suite_id"] == "bugsinpy-reduced-v2"
    for case in report["cases"]:
        case_report = json.loads(artifacts.read(case["report_ref"]))
        assert case_report["initial_validation_matches_expectation"] is True
        assert case_report["all_expectations_met"] is True


def test_reduced_v2_suite_preserves_the_frozen_v1_cases_and_digests():
    v1 = RunABSuiteManifest.model_validate(
        yaml.safe_load(SUITE_V1_PATH.read_text(encoding="utf-8"))
    )
    v2 = RunABSuiteManifest.model_validate(yaml.safe_load(SUITE_PATH.read_text(encoding="utf-8")))

    assert v1.sha256 == "d41d0b6edbe69f9029235e118d386a8fc77500ef55e36c6e053a656092e13b9b"
    assert v2.sha256 == "0cd34fadbebec928049f5b43ae505ff375b80b1bd624341ac469686495419d90"
    assert list(v2.cases[:3]) == list(v1.cases)
    assert [case.case_id for case in v2.cases[3:]] == [
        "luigi-1-metrics-handler",
        "tornado-1-websocket-nodelay",
    ]


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


def test_full_checkout_v2_preserves_v1_and_binds_three_exact_case_manifests():
    v1 = RunABSuiteManifest.model_validate(
        yaml.safe_load(FULL_SUITE_V1_PATH.read_text(encoding="utf-8"))
    )
    v2 = RunABSuiteManifest.model_validate(
        yaml.safe_load(FULL_SUITE_PATH.read_text(encoding="utf-8"))
    )

    assert v1.sha256 == "e6856e2e974cd855b535c9bae46bdd2795c3e5cb0bbdafcbd481ef54d649d69e"
    assert v2.sha256 == "be359684b9e3adfd676ed06c457197f7aff42bdcd1dbb3913321cebf5ef857b4"
    assert list(v2.cases[:2]) == list(v1.cases)
    assert {case.source.project for case in v2.cases} == {"luigi", "tqdm", "youtube-dl"}
    assert all(case.source.reduction == "full_checkout" for case in v2.cases)

    for case in v2.cases:
        case_path = FULL_SUITE_PATH.parent / case.manifest_path
        case_manifest = RunABEvalManifest.model_validate(
            yaml.safe_load(case_path.read_text(encoding="utf-8"))
        )
        case.check_manifest(case_manifest)


def test_luigi_full_checkout_repair_is_line_ending_independent():
    manifest = RunABEvalManifest.model_validate(
        yaml.safe_load(LUIGI_FULL_MANIFEST_PATH.read_text(encoding="utf-8"))
    )
    edits = [action for action in manifest.arms[1].actions if action.tool == "replace_text"]
    assert len(edits) == 2

    lines = [
        "    def get(self):",
        "        metrics = self._scheduler._state._metrics_collector.generate_latest()",
        "        if metrics:",
        "            metrics.configure_http_handler(self)",
        "            self.write(metrics)",
    ]
    for newline in ("\n", "\r\n"):
        source = newline.join(lines)
        for edit in edits:
            assert source.count(edit.arguments["old"]) == 1
            source = source.replace(edit.arguments["old"], edit.arguments["new"])
        assert "metrics = metrics_collector.generate_latest()" in source
        assert "metrics_collector.configure_http_handler(self)" in source


def test_three_stage_full_checkout_suite_preserves_v1_v2_and_binds_dependency_order():
    v1 = RunABSuiteManifest.model_validate(
        yaml.safe_load(MULTI_STAGE_V1_SUITE_PATH.read_text(encoding="utf-8"))
    )
    v2 = RunABSuiteManifest.model_validate(
        yaml.safe_load(MULTI_STAGE_V2_SUITE_PATH.read_text(encoding="utf-8"))
    )
    suite = RunABSuiteManifest.model_validate(
        yaml.safe_load(MULTI_STAGE_SUITE_PATH.read_text(encoding="utf-8"))
    )
    assert v1.sha256 == "e4afeee636e571010c65557757770b8c1b489227e3c3809314371e35527b7569"
    assert v2.sha256 == "c57ae62f6a4bbed7a7ba22e4169822887564c41272f9e440ca0f1c2bd4d458d3"
    assert suite.sha256 == "4f1d9c5596b5acb06986f082a42fa328f25e8954d44f56a9d02ddb50149314dd"
    assert list(suite.cases[:1]) == list(v2.cases)
    assert {case.source.project for case in suite.cases} == {"luigi", "youtube-dl"}
    assert all(case.source.reduction == "full_checkout" for case in suite.cases)

    expected_digests = {
        "youtube-dl-3-unescape-html-three-stage-full": (
            "ae87c1d42c3a4464e6167ee72f885bac45997806a06f67c98cd9942404448da4"
        ),
        "luigi-1-metrics-handler-three-stage-full": (
            "fab149f13284d5428702d3f6ca72e618fe1004bc0dab7ae2409fc6161dd2e6c3"
        ),
    }
    for case in suite.cases:
        case_path = MULTI_STAGE_SUITE_PATH.parent / case.manifest_path
        manifest = RunABEvalManifest.model_validate(
            yaml.safe_load(case_path.read_text(encoding="utf-8"))
        )
        case.check_manifest(manifest)
        assert manifest.sha256 == expected_digests[case.case_id]
        first, second, third = manifest.initial_plan.items
        assert second.dependencies == (first.work_item_id,)
        assert third.dependencies == (second.work_item_id,)
        assert {arm.restart_points for arm in manifest.arms} == {(3, 6)}
        assert (
            len(set(first.acceptance_ids) | set(second.acceptance_ids) | set(third.acceptance_ids))
            == 3
        )


def test_three_stage_regression_edit_is_line_ending_independent():
    suite = RunABSuiteManifest.model_validate(
        yaml.safe_load(MULTI_STAGE_SUITE_PATH.read_text(encoding="utf-8"))
    )
    case_path = MULTI_STAGE_SUITE_PATH.parent / suite.cases[0].manifest_path
    manifest = RunABEvalManifest.model_validate(
        yaml.safe_load(case_path.read_text(encoding="utf-8"))
    )
    edit = next(
        action
        for action in manifest.arms[0].actions
        if action.tool == "replace_text" and action.arguments["path"] == "test/test_utils.py"
    )
    lines = [
        "    def test_unescape_html(self):",
        "        self.assertEqual(unescapeHTML('&#2013266066;'), '&#2013266066;')",
        "        # HTML5 entities",
    ]
    for newline in ("\n", "\r\n"):
        source = newline.join(lines)
        assert source.count(edit.arguments["old"]) == 1
        repaired = source.replace(edit.arguments["old"], edit.arguments["new"])
        assert "self.assertEqual(unescapeHTML('&a&quot;'), '&a\"')" in repaired


def test_luigi_three_stage_regression_edit_is_line_ending_independent():
    manifest = RunABEvalManifest.model_validate(
        yaml.safe_load(LUIGI_MULTI_STAGE_MANIFEST_PATH.read_text(encoding="utf-8"))
    )
    edit = next(
        action
        for action in manifest.arms[1].actions
        if action.tool == "replace_text" and action.arguments["path"] == "test/server_test.py"
    )
    lines = [
        "            self.handler.get()",
        "            patched_write.assert_called_once_with(mock_metrics)",
        "            mock_metrics.configure_http_handler.assert_called_once_with(self.handler)",
        "",
        "    def test_get_no_metrics(self):",
    ]
    for newline in ("\n", "\r\n"):
        source = newline.join(lines)
        assert source.count(edit.arguments["old"]) == 1
        repaired = source.replace(edit.arguments["old"], edit.arguments["new"])
        assert (
            "self.mock_scheduler._state._metrics_collector.configure_http_handler"
            ".assert_called_once_with(\n                self.handler)"
        ) in repaired
