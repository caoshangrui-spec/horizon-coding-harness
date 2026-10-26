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
from horizon.application.run_ab_eval import (
    RunABEvaluator,
    build_run_ab_suite_report,
    store_run_ab_report,
)
from horizon.domain.events import Event
from horizon.domain.run_evaluation import (
    RunABEvalManifest,
    RunABSuiteManifest,
    RunABSuiteReport,
)
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
FULL_SUITE_V3_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-full-checkout-pilot-v3.yaml"
FULL_SUITE_V4_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-full-checkout-pilot-v4.yaml"
FULL_SUITE_V5_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-full-checkout-pilot-v5.yaml"
FULL_SUITE_V6_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-full-checkout-pilot-v6.yaml"
FULL_SUITE_V7_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-full-checkout-pilot-v7.yaml"
LUIGI_FULL_MANIFEST_PATH = (
    ROOT / "benchmarks" / "run_ab" / "full" / "luigi-1-metrics-handler" / "manifest.yaml"
)
NEW_FULL_MANIFEST_PATHS = {
    "fastapi-5-nested-field-clone-full": (
        ROOT / "benchmarks" / "run_ab" / "full" / "fastapi-5-nested-field-clone" / "manifest.yaml"
    ),
    "tornado-1-websocket-nodelay-full": (
        ROOT / "benchmarks" / "run_ab" / "full" / "tornado-1-websocket-nodelay" / "manifest.yaml"
    ),
}
HTTPIE_FULL_MANIFEST_PATH = (
    ROOT / "benchmarks" / "run_ab" / "full" / "httpie-2-max-redirects" / "manifest.yaml"
)
THEFUCK_FULL_MANIFEST_PATH = (
    ROOT / "benchmarks" / "run_ab" / "full" / "thefuck-1-pip-unknown-command" / "manifest.yaml"
)
SANIC_FULL_MANIFEST_PATH = (
    ROOT / "benchmarks" / "run_ab" / "full" / "sanic-1-blueprint-middleware-order" / "manifest.yaml"
)
PYSNOOPER_FULL_MANIFEST_PATH = (
    ROOT / "benchmarks" / "run_ab" / "full" / "pysnooper-2-custom-repr" / "manifest.yaml"
)
COOKIECUTTER_FULL_MANIFEST_PATH = (
    ROOT / "benchmarks" / "run_ab" / "full" / "cookiecutter-1-utf8-context" / "manifest.yaml"
)
HTTPIE_HOST_FULL_MANIFEST_PATH = (
    ROOT / "benchmarks" / "run_ab" / "full" / "httpie-4-host-header-overwrite" / "manifest.yaml"
)
LUIGI_TUPLE_FULL_MANIFEST_PATH = (
    ROOT / "benchmarks" / "run_ab" / "full" / "luigi-3-serialize-tuple-parameter" / "manifest.yaml"
)
BLIND_RETRIEVAL_SELECTION_PATH = (
    ROOT / "benchmarks" / "retrieval" / "external-blind-v1" / "selection.yaml"
)
HOLDOUT_RETRIEVAL_SELECTION_PATH = (
    ROOT / "benchmarks" / "retrieval" / "external-holdout-v2" / "selection.yaml"
)
DEVELOPMENT_RETRIEVAL_SELECTION_PATH = (
    ROOT / "benchmarks" / "retrieval" / "external-development-v3" / "selection.yaml"
)
MULTI_STAGE_V1_SUITE_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-multi-stage-pilot-v1.yaml"
MULTI_STAGE_V2_SUITE_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-multi-stage-pilot-v2.yaml"
MULTI_STAGE_V3_SUITE_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-multi-stage-pilot-v3.yaml"
MULTI_STAGE_SUITE_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-multi-stage-pilot-v4.yaml"
LUIGI_MULTI_STAGE_MANIFEST_PATH = (
    ROOT / "benchmarks" / "run_ab" / "full" / "luigi-1-metrics-handler" / "multi-stage-v3.yaml"
)
FASTAPI_MULTI_STAGE_MANIFEST_PATH = (
    ROOT / "benchmarks" / "run_ab" / "full" / "fastapi-5-nested-field-clone" / "multi-stage-v4.yaml"
)
HARD_CRASH_SUITE_PATH = ROOT / "benchmarks" / "run_ab" / "bugsinpy-multi-stage-hard-crash-v1.yaml"
LUIGI_HARD_CRASH_MANIFEST_PATH = (
    ROOT
    / "benchmarks"
    / "run_ab"
    / "full"
    / "luigi-1-metrics-handler"
    / "multi-stage-hard-crash-v1.yaml"
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
    assert report.duration_ms is not None
    assert report.duration_ms >= 0
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


def test_run_ab_recovers_real_write_hard_crash_without_redispatch(tmp_path):
    data = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    data["schema_version"] = 2
    data["benchmark_id"] = "write-hard-crash-recovery-v1"
    data["task"]["acceptance"] = [
        {
            "id": "empty-contract",
            "command": "unused-empty-check",
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
                "allowed_tools": ["read_file", "replace_text"],
            },
            {
                "work_item_id": "wrap-nonempty",
                "title": "Wrap non-empty input",
                "objective": "Preserve the empty guard and wrap non-empty input",
                "dependencies": ["guard-empty"],
                "expected_artifacts": ["combined-contract receipt"],
                "acceptance_ids": ["combined-contract"],
                "allowed_tools": ["replace_text"],
            },
        ],
    }
    data["expected_initial_failed_checks"] = ["empty-contract", "combined-contract"]
    actions = [
        {
            "tool": "read_file",
            "arguments": {"path": "src/parser.sh", "start_line": 1, "end_line": 3},
        },
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
        {
            "tool": "submit",
            "arguments": {"summary": "Preserved empty and wrapped non-empty input."},
        },
    ]
    data["arms"] = [
        {
            "arm_id": arm_id,
            "role": role,
            "description": "Recover one real write hard crash, then finish both WorkItems.",
            "expected_status": "SUCCEEDED",
            "expected_plan_version": 1,
            "expected_execution_replans": 0,
            "expected_passed_items": ["guard-empty", "wrap-nonempty"],
            "expected_model_calls": 5,
            "expected_tool_calls": 8,
            "expected_steps": 8,
            "restart_after_model_calls": 3,
            "hard_crash_after_tool_effect_at_model_call": 2,
            "actions": actions,
        }
        for arm_id, role in (
            ("baseline-hard-crash", "baseline"),
            ("treatment-hard-crash", "single_replan"),
        )
    ]
    manifest = RunABEvalManifest.model_validate(data)
    fixture = MANIFEST_PATH.parent / manifest.fixture_path

    report = RunABEvaluator(
        MultiStageAcceptance(),
        validation_backend="fixture-content",
        validation_backend_ref="test-only-hard-crash-contract",
        repository_code_executed=False,
    ).evaluate(manifest, fixture_source=fixture, state_dir=tmp_path / "state")

    assert report.schema_version == 2
    assert report.all_expectations_met is True
    assert report.treatment_recovered is False
    assert report.duration_ms is not None
    artifacts = ArtifactStore(tmp_path / "state" / "artifacts")
    for arm in (report.baseline, report.single_replan):
        assert arm.status == RunStatus.SUCCEEDED
        assert arm.worker_restarts == 2
        assert arm.final_lease_epoch == 3
        assert arm.usage.model_calls == 5
        assert arm.usage.tool_calls == 8
        assert arm.actions_consumed is True
        assert arm.hard_crash is not None
        assert arm.hard_crash.model_call_index == 2
        assert arm.hard_crash.tool == "replace_text"
        assert arm.hard_crash.crashed_worker_epoch == 1
        assert arm.hard_crash.recovery_worker_epoch == 2
        assert arm.hard_crash.recovery_disposition == "accept_replace"
        assert artifacts.read(arm.hard_crash.scripted_action_ref)
        assert artifacts.read(arm.hard_crash.crash_marker_ref)
        replayed = SQLiteEventStore.replay_jsonl(artifacts.read(arm.trace_ref).decode("utf-8"))
        recovered = [
            record
            for record in replayed.tool_calls
            if record.call_id == arm.hard_crash.pending_tool_call_id
        ]
        assert len(recovered) == 1
        assert recovered[0].recovery_disposition == "accept_replace"
        assert not replayed.unknown_tool_calls
        assert not replayed.reservations

    suite = RunABSuiteManifest.model_validate(
        {
            "suite_id": "write-hard-crash-suite-v1",
            "cases": [
                {
                    "case_id": manifest.benchmark_id,
                    "manifest_path": "write-hard-crash-recovery-v1.yaml",
                    "source": {
                        "benchmark": "fixture",
                        "project": "stalled-reader",
                        "bug_id": "1",
                        "repository_url": "https://github.com/example/stalled-reader",
                        "buggy_commit": "a" * 40,
                        "fixed_commit": "b" * 40,
                        "fix_url": "https://github.com/example/stalled-reader/commit/" + "b" * 40,
                        "upstream_test": "tests/test_parser.sh",
                        "license_spdx": "MIT",
                        "license_url": "https://github.com/example/stalled-reader/blob/main/LICENSE",
                        "reduction": "dependency_reduced",
                        "reduction_note": "Test-only source metadata for reliability aggregation.",
                    },
                }
            ],
        }
    )
    report_ref = store_run_ab_report(artifacts, report)
    suite_report = build_run_ab_suite_report(
        suite,
        {manifest.benchmark_id: (report, report_ref)},
        validation_backend="fixture-content",
        validation_backend_ref="test-only-hard-crash-contract",
        repository_code_executed=False,
    )
    reliability = suite_report.reliability
    assert reliability is not None
    assert reliability.scenario_count == 1
    assert reliability.arm_count == 2
    assert reliability.terminal_success_count == 2
    assert reliability.worker_restart_count == 4
    assert reliability.hard_crash_arm_count == 2
    assert reliability.recovered_hard_crash_arm_count == 2
    assert reliability.recovery_success_rate == 1.0
    assert reliability.recovery_redispatch_count == 0
    assert reliability.duplicate_side_effect_count == 0
    assert reliability.trace_replay_verified_count == 2
    assert reliability.trace_replay_rate == 1.0
    assert reliability.source_workspace_unchanged_count == 2
    assert reliability.source_workspace_unchanged_rate == 1.0
    assert reliability.final_open_effect_count == 0


def test_run_ab_hard_crash_contract_rejects_legacy_or_asymmetric_configuration():
    data = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    write = {
        "tool": "replace_text",
        "arguments": {"path": "src/parser.sh", "old": "before", "new": "after"},
    }
    for arm in data["arms"]:
        arm["actions"][1] = write
        arm["hard_crash_after_tool_effect_at_model_call"] = 2
    with pytest.raises(ValueError, match="schema version 2"):
        RunABEvalManifest.model_validate(data)

    data["schema_version"] = 2
    data["arms"][1].pop("hard_crash_after_tool_effect_at_model_call")
    with pytest.raises(ValueError, match="each arm"):
        RunABEvalManifest.model_validate(data)


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
    reliability = report["reliability"]
    assert reliability["scenario_count"] == 5
    assert reliability["timed_scenario_count"] == 5
    assert reliability["arm_count"] == 10
    assert reliability["terminal_success_count"] == 5
    assert reliability["worker_restart_count"] == 0
    assert reliability["hard_crash_arm_count"] == 0
    assert reliability["recovered_hard_crash_arm_count"] == 0
    assert reliability["recovery_success_rate"] == 0.0
    assert reliability["recovery_redispatch_count"] == 0
    assert reliability["duplicate_side_effect_count"] == 0
    assert reliability["trace_replay_verified_count"] == 10
    assert reliability["trace_replay_rate"] == 1.0
    assert reliability["source_workspace_unchanged_count"] == 10
    assert reliability["source_workspace_unchanged_rate"] == 1.0
    assert reliability["final_open_effect_count"] == 0
    assert reliability["p95_scenario_duration_ms"] >= 0
    assert {case["source"]["project"] for case in report["cases"]} == {
        "cookiecutter",
        "fastapi",
        "luigi",
        "tqdm",
        "tornado",
    }

    legacy = json.loads(json.dumps(report))
    legacy.pop("reliability")
    for case in legacy["cases"]:
        case.pop("reliability")
    assert RunABSuiteReport.model_validate(legacy).reliability is None

    tampered = json.loads(json.dumps(report))
    tampered["reliability"]["trace_replay_verified_count"] -= 1
    tampered["reliability"]["trace_replay_rate"] = 0.9
    with pytest.raises(ValueError, match="trace_replay_verified_count"):
        RunABSuiteReport.model_validate(tampered)

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


def test_full_checkout_v3_preserves_v2_and_binds_five_exact_case_manifests():
    v2 = RunABSuiteManifest.model_validate(
        yaml.safe_load(FULL_SUITE_PATH.read_text(encoding="utf-8"))
    )
    v3 = RunABSuiteManifest.model_validate(
        yaml.safe_load(FULL_SUITE_V3_PATH.read_text(encoding="utf-8"))
    )

    assert v2.sha256 == "be359684b9e3adfd676ed06c457197f7aff42bdcd1dbb3913321cebf5ef857b4"
    assert v3.sha256 == "d878964e246f8fabb6cf162f657791e959ae6c529d61eb1143cc98ea7d4edd8a"
    assert list(v3.cases[:3]) == list(v2.cases)
    assert {case.source.project for case in v3.cases} == {
        "fastapi",
        "luigi",
        "tornado",
        "tqdm",
        "youtube-dl",
    }
    assert all(case.source.reduction == "full_checkout" for case in v3.cases)

    expected_digests = {
        "fastapi-5-nested-field-clone-full": (
            "a3173ddf170b5e82d580b63a98db6f43e5ff164eccc928ee95cfe673babfdcb1"
        ),
        "tornado-1-websocket-nodelay-full": (
            "b94488b52b91dc0a8d7ba3b13060ee6526bd07ddaadff929b75626f09e4d6afc"
        ),
    }
    for case in v3.cases[3:]:
        case_path = FULL_SUITE_V3_PATH.parent / case.manifest_path
        manifest = RunABEvalManifest.model_validate(
            yaml.safe_load(case_path.read_text(encoding="utf-8"))
        )
        case.check_manifest(manifest)
        assert manifest.sha256 == expected_digests[case.case_id]
        assert manifest.task.acceptance[0].command.startswith('python -B -c "')
        assert manifest.expected_initial_failed_checks == (manifest.task.acceptance[0].id,)


def test_full_checkout_v3_repairs_match_the_upstream_change_shapes():
    samples = {
        "fastapi-5-nested-field-clone-full": (
            "        use_type.__fields__[f.name] = f\n",
            ("use_type.__fields__[f.name] = create_cloned_field(f)",),
        ),
        "tornado-1-websocket-nodelay-full": (
            "        assert self.stream is not None\n"
            "        self.stream.set_nodelay(value)\n"
            "\n"
            "    @abc.abstractmethod\n"
            "    async def _receive_frame_loop(self) -> None:\n"
            "        raise NotImplementedError()\n"
            "\n"
            "        self.last_ping = now\n",
            (
                "assert self.ws_connection is not None",
                "def set_nodelay(self, x: bool) -> None:",
                "self.stream.set_nodelay(x)",
            ),
        ),
    }

    for case_id, (source, expected_markers) in samples.items():
        manifest = RunABEvalManifest.model_validate(
            yaml.safe_load(NEW_FULL_MANIFEST_PATHS[case_id].read_text(encoding="utf-8"))
        )
        edits = [action for action in manifest.arms[1].actions if action.tool == "replace_text"]
        for edit in edits:
            expected_occurrences = edit.arguments.get("expected_occurrences", 1)
            assert source.count(edit.arguments["old"]) == expected_occurrences
            source = source.replace(edit.arguments["old"], edit.arguments["new"])
        assert all(marker in source for marker in expected_markers)


def test_full_checkout_v4_preserves_v3_and_binds_six_exact_case_manifests():
    v3 = RunABSuiteManifest.model_validate(
        yaml.safe_load(FULL_SUITE_V3_PATH.read_text(encoding="utf-8"))
    )
    v4 = RunABSuiteManifest.model_validate(
        yaml.safe_load(FULL_SUITE_V4_PATH.read_text(encoding="utf-8"))
    )

    assert v3.sha256 == "d878964e246f8fabb6cf162f657791e959ae6c529d61eb1143cc98ea7d4edd8a"
    assert v4.sha256 == "17d12ba657bde272251783d3364b4f809691fa71a5ae55c8d6dce98947b1d99c"
    assert list(v4.cases[:5]) == list(v3.cases)
    assert [case.case_id for case in v4.cases[5:]] == ["httpie-2-max-redirects-full"]
    assert {case.source.project for case in v4.cases} == {
        "fastapi",
        "httpie",
        "luigi",
        "tornado",
        "tqdm",
        "youtube-dl",
    }
    assert all(case.source.reduction == "full_checkout" for case in v4.cases)

    case = v4.cases[-1]
    manifest = RunABEvalManifest.model_validate(
        yaml.safe_load(HTTPIE_FULL_MANIFEST_PATH.read_text(encoding="utf-8"))
    )
    case.check_manifest(manifest)
    assert manifest.sha256 == "6f01c4403356930507d9a2188cf3c33c940bf6a9f0fe992d0a08fd5b80796920"
    assert manifest.task.acceptance[0].command.startswith('python -B -c "')
    assert manifest.expected_initial_failed_checks == (manifest.task.acceptance[0].id,)


def test_httpie_full_checkout_repair_matches_the_upstream_change_shape():
    manifest = RunABEvalManifest.model_validate(
        yaml.safe_load(HTTPIE_FULL_MANIFEST_PATH.read_text(encoding="utf-8"))
    )
    edits = [action for action in manifest.arms[1].actions if action.tool == "replace_text"]
    assert len(edits) == 1
    edit = edits[0]
    assert edit.arguments == {
        "path": "httpie/client.py",
        "old": "    requests_session = get_requests_session()",
        "new": (
            "    requests_session = get_requests_session()\n"
            "    requests_session.max_redirects = args.max_redirects"
        ),
    }

    lines = [
        "def get_response(args, base_dir):",
        "    requests_session = get_requests_session()",
        "",
        "    if not args.session and not args.session_read_only:",
        "        kwargs = get_requests_kwargs(args=args)",
    ]
    for newline in ("\n", "\r\n"):
        source = newline.join(lines)
        assert source.count(edit.arguments["old"]) == 1
        updated = source.replace(edit.arguments["old"], edit.arguments["new"])
        assert updated.count("requests_session.max_redirects = args.max_redirects") == 1


def test_full_checkout_v5_preserves_v4_and_binds_prefrozen_additions():
    v4 = RunABSuiteManifest.model_validate(
        yaml.safe_load(FULL_SUITE_V4_PATH.read_text(encoding="utf-8"))
    )
    v5 = RunABSuiteManifest.model_validate(
        yaml.safe_load(FULL_SUITE_V5_PATH.read_text(encoding="utf-8"))
    )

    assert v4.sha256 == "17d12ba657bde272251783d3364b4f809691fa71a5ae55c8d6dce98947b1d99c"
    assert v5.sha256 == "2dca6b3bcffcb88456ae0f05e833f11fa78a3ee1e0ca6dd081c5d0087c7ab12b"
    assert list(v5.cases[:6]) == list(v4.cases)
    assert [case.case_id for case in v5.cases[6:]] == [
        "thefuck-1-pip-unknown-command-full",
        "sanic-1-blueprint-middleware-order-full",
    ]
    assert {case.source.project for case in v5.cases} == {
        "fastapi",
        "httpie",
        "luigi",
        "sanic",
        "thefuck",
        "tornado",
        "tqdm",
        "youtube-dl",
    }
    assert all(case.source.reduction == "full_checkout" for case in v5.cases)

    expected = {
        "thefuck-1-pip-unknown-command-full": (
            THEFUCK_FULL_MANIFEST_PATH,
            "e481b6b3a1623fb4f6313474dc29df443bfb1de7f43a1a5042482ecdf2f14c12",
        ),
        "sanic-1-blueprint-middleware-order-full": (
            SANIC_FULL_MANIFEST_PATH,
            "cc9477a546a6360da2bc3b046fdac6cbea2ec2a41f5c00210a97e166909a20a1",
        ),
    }
    for case in v5.cases[6:]:
        path, digest = expected[case.case_id]
        manifest = RunABEvalManifest.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        )
        case.check_manifest(manifest)
        assert manifest.sha256 == digest
        assert manifest.task.acceptance[0].command.startswith('python -B -c "')
        assert manifest.expected_initial_failed_checks == (manifest.task.acceptance[0].id,)


def test_full_checkout_v5_additions_match_prefrozen_retrieval_sources():
    suite = RunABSuiteManifest.model_validate(
        yaml.safe_load(FULL_SUITE_V5_PATH.read_text(encoding="utf-8"))
    )
    blind = yaml.safe_load(BLIND_RETRIEVAL_SELECTION_PATH.read_text(encoding="utf-8"))
    holdout = yaml.safe_load(HOLDOUT_RETRIEVAL_SELECTION_PATH.read_text(encoding="utf-8"))

    thefuck = next(case for case in blind["cases"] if case["case_id"].startswith("thefuck-1"))
    sanic = next(case for case in holdout["cases"] if case["case_id"].startswith("sanic-1"))
    sources = {case.source.project: case.source for case in suite.cases}

    assert blind["frozen_at"] == "2026-10-04"
    assert blind["protocol"]["patch_reveal_status"] == "sealed"
    assert sources["thefuck"].buggy_commit == thefuck["buggy_commit"]
    assert sources["thefuck"].fixed_commit == thefuck["fixed_commit"]
    assert sources["thefuck"].repository_url == thefuck["repository_url"]

    assert holdout["frozen_at"] == "2026-10-04"
    assert holdout["protocol"]["patch_reveal_status"] == "sealed"
    assert sources["sanic"].buggy_commit == sanic["buggy_commit"]
    assert sources["sanic"].fixed_commit == sanic["fixed_commit"]
    assert sources["sanic"].repository_url == sanic["repository_url"]


def test_full_checkout_v5_repairs_match_upstream_and_both_line_endings():
    thefuck = RunABEvalManifest.model_validate(
        yaml.safe_load(THEFUCK_FULL_MANIFEST_PATH.read_text(encoding="utf-8"))
    )
    thefuck_edits = [action for action in thefuck.arms[1].actions if action.tool == "replace_text"]
    assert [edit.arguments for edit in thefuck_edits] == [
        {
            "path": "thefuck/rules/pip_unknown_command.py",
            "old": r"""    broken_cmd = re.findall(r'ERROR: unknown command \"([a-z]+)\"',""",
            "new": """    broken_cmd = re.findall(r'ERROR: unknown command "([^"]+)"',""",
        },
        {
            "path": "thefuck/rules/pip_unknown_command.py",
            "old": (
                r"""    new_cmd = re.findall(r'maybe you meant \"([a-z]+)\"', """
                "command.output)[0]"
            ),
            "new": (
                """    new_cmd = re.findall(r'maybe you meant "([^"]+)"', """
                "command.output)[0]"
            ),
        },
    ]

    thefuck_lines = [
        "def get_new_command(command):",
        thefuck_edits[0].arguments["old"],
        "                            command.output)[0]",
        thefuck_edits[1].arguments["old"],
        "    return replace_argument(command.script, broken_cmd, new_cmd)",
    ]
    for newline in ("\n", "\r\n"):
        source = newline.join(thefuck_lines)
        for edit in thefuck_edits:
            assert source.count(edit.arguments["old"]) == 1
            source = source.replace(edit.arguments["old"], edit.arguments["new"])
        assert 'unknown command "([^"]+)"' in source
        assert 'maybe you meant "([^"]+)"' in source

    sanic = RunABEvalManifest.model_validate(
        yaml.safe_load(SANIC_FULL_MANIFEST_PATH.read_text(encoding="utf-8"))
    )
    sanic_edits = [action for action in sanic.arms[1].actions if action.tool == "replace_text"]
    assert len(sanic_edits) == 1
    edit = sanic_edits[0]
    assert edit.arguments == {
        "path": "sanic/app.py",
        "old": "self.named_response_middleware[_rn].append(middleware)",
        "new": "self.named_response_middleware[_rn].appendleft(middleware)",
    }
    for newline in ("\n", "\r\n"):
        source = newline.join(
            [
                '        if attach_to == "response":',
                "            for _rn in route_names:",
                "                if middleware not in self.named_response_middleware[_rn]:",
                "                    self.named_response_middleware[_rn].append(middleware)",
            ]
        )
        assert source.count(edit.arguments["old"]) == 1
        updated = source.replace(edit.arguments["old"], edit.arguments["new"])
        assert updated.count(edit.arguments["new"]) == 1


def test_full_checkout_v6_preserves_v5_and_binds_prefrozen_two_file_case():
    v5 = RunABSuiteManifest.model_validate(
        yaml.safe_load(FULL_SUITE_V5_PATH.read_text(encoding="utf-8"))
    )
    v6 = RunABSuiteManifest.model_validate(
        yaml.safe_load(FULL_SUITE_V6_PATH.read_text(encoding="utf-8"))
    )

    assert v5.sha256 == "2dca6b3bcffcb88456ae0f05e833f11fa78a3ee1e0ca6dd081c5d0087c7ab12b"
    assert v6.sha256 == "52afa08518568e0c4e2ab19df6bf4cd267dcf260804152888b301e16f2940428"
    assert list(v6.cases[:8]) == list(v5.cases)
    assert [case.case_id for case in v6.cases[8:]] == ["pysnooper-2-custom-repr-full"]
    assert len(v6.cases) == 9
    assert len({case.source.project.casefold() for case in v6.cases}) == 9

    case = v6.cases[-1]
    manifest = RunABEvalManifest.model_validate(
        yaml.safe_load(PYSNOOPER_FULL_MANIFEST_PATH.read_text(encoding="utf-8"))
    )
    case.check_manifest(manifest)
    assert manifest.sha256 == "b7dbab0c33caefd4eb833d2c5da1d57c29a79815907a3ebb4ea5eb9ab1f5153b"
    assert manifest.expected_initial_failed_checks == (manifest.task.acceptance[0].id,)
    assert manifest.arms[1].expected_model_calls == 13
    assert manifest.arms[1].expected_tool_calls == 14


def test_full_checkout_v6_addition_matches_prefrozen_development_source():
    suite = RunABSuiteManifest.model_validate(
        yaml.safe_load(FULL_SUITE_V6_PATH.read_text(encoding="utf-8"))
    )
    selection = yaml.safe_load(DEVELOPMENT_RETRIEVAL_SELECTION_PATH.read_text(encoding="utf-8"))
    selected = next(
        case for case in selection["cases"] if case["case_id"] == "pysnooper-2-custom-repr"
    )
    source = suite.cases[-1].source

    assert selection["selected_at"] == "2026-10-24"
    assert selection["protocol"]["patch_reveal_status"] == "sealed"
    assert selected["partition"] == "development"
    assert source.project == selected["project"]
    assert source.bug_id == selected["bug_id"]
    assert source.repository_url == selected["repository_url"]
    assert source.buggy_commit == selected["buggy_commit"]
    assert source.fixed_commit == selected["fixed_commit"]


def test_full_checkout_v6_repair_is_two_file_and_line_ending_independent():
    manifest = RunABEvalManifest.model_validate(
        yaml.safe_load(PYSNOOPER_FULL_MANIFEST_PATH.read_text(encoding="utf-8"))
    )
    edits = [action for action in manifest.arms[1].actions if action.tool == "replace_text"]

    assert len(edits) == 8
    assert {edit.arguments["path"] for edit in edits} == {
        "pysnooper/tracer.py",
        "pysnooper/utils.py",
    }
    assert all("\n" not in edit.arguments["old"] for edit in edits)
    assert "custom_repr=(list" in manifest.task.acceptance[0].command
    assert "value = items=3" in manifest.task.acceptance[0].command
    assert "Return value:.. items=3" in manifest.task.acceptance[0].command

    edits_by_path: dict[str, list] = {}
    for edit in edits:
        edits_by_path.setdefault(edit.arguments["path"], []).append(edit)
    for newline in ("\n", "\r\n"):
        updated_sources = {}
        for path, path_edits in edits_by_path.items():
            source = newline.join(edit.arguments["old"] for edit in path_edits)
            for edit in path_edits:
                assert source.count(edit.arguments["old"]) == 1, path
                before = source
                source = source.replace(edit.arguments["old"], edit.arguments["new"])
                assert source != before
            assert all(edit.arguments["new"] in source for edit in path_edits)
            updated_sources[path] = source
        assert "def get_repr_function(item, custom_repr):" in updated_sources["pysnooper/utils.py"]
        assert "self.custom_repr = custom_repr" in updated_sources["pysnooper/tracer.py"]


def test_full_checkout_v7_preserves_v6_and_binds_three_development_cases():
    v6 = RunABSuiteManifest.model_validate(
        yaml.safe_load(FULL_SUITE_V6_PATH.read_text(encoding="utf-8"))
    )
    v7 = RunABSuiteManifest.model_validate(
        yaml.safe_load(FULL_SUITE_V7_PATH.read_text(encoding="utf-8"))
    )

    assert v7.sha256 == "4eb74bfbf8715a1ac5aaf14b5087a5c289cab1915cea3f46e79065b6835edb79"
    assert list(v7.cases[:9]) == list(v6.cases)
    assert [case.case_id for case in v7.cases[9:]] == [
        "cookiecutter-1-utf8-context-full",
        "httpie-4-host-header-overwrite-full",
        "luigi-3-serialize-tuple-parameter-full",
    ]
    assert len(v7.cases) == 12
    assert len({case.source.project.casefold() for case in v7.cases}) == 10

    expected = {
        "cookiecutter-1-utf8-context-full": (
            COOKIECUTTER_FULL_MANIFEST_PATH,
            "718854e4db3eddcffb863ccbd2480a53d0f89d7ccd5cf20b6d88632494b2e061",
            6,
            7,
        ),
        "httpie-4-host-header-overwrite-full": (
            HTTPIE_HOST_FULL_MANIFEST_PATH,
            "c03af519f5bced1df1bbab64e426f80a658fd243198dd21d854a5205581caa46",
            6,
            7,
        ),
        "luigi-3-serialize-tuple-parameter-full": (
            LUIGI_TUPLE_FULL_MANIFEST_PATH,
            "af4c7dce31107ca88c83faddb6e6d4367f7f85290b5b584fffa5de7750c06129",
            7,
            8,
        ),
    }
    for case in v7.cases[9:]:
        manifest_path, digest, model_calls, tool_calls = expected[case.case_id]
        manifest = RunABEvalManifest.model_validate(
            yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        )
        case.check_manifest(manifest)
        assert manifest.sha256 == digest
        assert manifest.expected_initial_failed_checks == (manifest.task.acceptance[0].id,)
        assert manifest.arms[1].expected_model_calls == model_calls
        assert manifest.arms[1].expected_tool_calls == tool_calls


def test_full_checkout_v7_additions_match_prefrozen_development_sources():
    suite = RunABSuiteManifest.model_validate(
        yaml.safe_load(FULL_SUITE_V7_PATH.read_text(encoding="utf-8"))
    )
    selection = yaml.safe_load(DEVELOPMENT_RETRIEVAL_SELECTION_PATH.read_text(encoding="utf-8"))
    selected_by_id = {case["case_id"]: case for case in selection["cases"]}
    source_ids = [
        "cookiecutter-1-non-ascii-context",
        "httpie-4-host-header-overwrite",
        "luigi-3-serialize-tuple-parameter",
    ]

    assert selection["selected_at"] == "2026-10-24"
    assert selection["protocol"]["patch_reveal_status"] == "sealed"
    for case, source_id in zip(suite.cases[9:], source_ids, strict=True):
        selected = selected_by_id[source_id]
        assert selected["partition"] == "development"
        assert case.source.project == selected["project"]
        assert case.source.bug_id == selected["bug_id"]
        assert case.source.repository_url == selected["repository_url"]
        assert case.source.buggy_commit == selected["buggy_commit"]
        assert case.source.fixed_commit == selected["fixed_commit"]


def test_full_checkout_v7_repairs_are_bounded_and_line_ending_independent():
    manifests = [
        RunABEvalManifest.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        for path in (
            COOKIECUTTER_FULL_MANIFEST_PATH,
            HTTPIE_HOST_FULL_MANIFEST_PATH,
            LUIGI_TUPLE_FULL_MANIFEST_PATH,
        )
    ]
    edits = [
        action
        for manifest in manifests
        for action in manifest.arms[1].actions
        if action.tool == "replace_text"
    ]

    assert len(edits) == 4
    assert {edit.arguments["path"] for edit in edits} == {
        "cookiecutter/generate.py",
        "httpie/models.py",
        "luigi/parameter.py",
    }
    assert all("\n" not in edit.arguments["old"] for edit in edits)
    for newline in ("\n", "\r\n"):
        for edit in edits:
            source = f"prefix{newline}{edit.arguments['old']}{newline}suffix"
            assert source.count(edit.arguments["old"]) == 1
            updated = source.replace(edit.arguments["old"], edit.arguments["new"])
            assert updated.count(edit.arguments["new"]) == 1

    cookie_command = manifests[0].task.acceptance[0].command
    httpie_command = manifests[1].task.acceptance[0].command
    luigi_command = manifests[2].task.acceptance[0].command
    assert "encoding':'ascii" in cookie_command
    assert "count('host:')==1" in httpie_command
    assert "json.dumps((1,2,3))" in luigi_command


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


def test_three_stage_full_checkout_suite_preserves_v1_v2_v3_and_binds_dependency_order():
    v1 = RunABSuiteManifest.model_validate(
        yaml.safe_load(MULTI_STAGE_V1_SUITE_PATH.read_text(encoding="utf-8"))
    )
    v2 = RunABSuiteManifest.model_validate(
        yaml.safe_load(MULTI_STAGE_V2_SUITE_PATH.read_text(encoding="utf-8"))
    )
    v3 = RunABSuiteManifest.model_validate(
        yaml.safe_load(MULTI_STAGE_V3_SUITE_PATH.read_text(encoding="utf-8"))
    )
    suite = RunABSuiteManifest.model_validate(
        yaml.safe_load(MULTI_STAGE_SUITE_PATH.read_text(encoding="utf-8"))
    )
    assert v1.sha256 == "e4afeee636e571010c65557757770b8c1b489227e3c3809314371e35527b7569"
    assert v2.sha256 == "c57ae62f6a4bbed7a7ba22e4169822887564c41272f9e440ca0f1c2bd4d458d3"
    assert v3.sha256 == "4f1d9c5596b5acb06986f082a42fa328f25e8954d44f56a9d02ddb50149314dd"
    assert suite.sha256 == "7541ec23c1a1d4a0ab6e052ae84cd256c756ecff6b5bb108f9ae086fda43cf17"
    assert list(suite.cases[:2]) == list(v3.cases)
    assert {case.source.project for case in suite.cases} == {
        "luigi",
        "fastapi",
        "youtube-dl",
    }
    assert all(case.source.reduction == "full_checkout" for case in suite.cases)

    expected_digests = {
        "youtube-dl-3-unescape-html-three-stage-full": (
            "ae87c1d42c3a4464e6167ee72f885bac45997806a06f67c98cd9942404448da4"
        ),
        "luigi-1-metrics-handler-three-stage-full": (
            "fab149f13284d5428702d3f6ca72e618fe1004bc0dab7ae2409fc6161dd2e6c3"
        ),
        "fastapi-5-nested-field-clone-three-stage-full": (
            "8ac330a5e7ec07053163695d05572e4c849155297743e742963750e666cb0fe1"
        ),
    }
    expected_restart_points = {
        "youtube-dl-3-unescape-html-three-stage-full": (3, 6),
        "luigi-1-metrics-handler-three-stage-full": (3, 6),
        "fastapi-5-nested-field-clone-three-stage-full": (3, 6),
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
        assert {arm.restart_points for arm in manifest.arms} == {
            expected_restart_points[case.case_id]
        }
        assert (
            len(set(first.acceptance_ids) | set(second.acceptance_ids) | set(third.acceptance_ids))
            == 3
        )


def test_fastapi_three_stage_preserves_exact_upstream_production_and_regression_edits():
    manifest = RunABEvalManifest.model_validate(
        yaml.safe_load(FASTAPI_MULTI_STAGE_MANIFEST_PATH.read_text(encoding="utf-8"))
    )
    write_tools = {"replace_text", "create_file"}
    baseline_writes = [action for action in manifest.arms[0].actions if action.tool in write_tools]
    treatment_writes = [action for action in manifest.arms[1].actions if action.tool in write_tools]
    assert treatment_writes == baseline_writes
    assert [action.tool for action in treatment_writes] == ["replace_text", "create_file"]

    production, regression = treatment_writes
    assert production.arguments == {
        "path": "fastapi/utils.py",
        "old": "use_type.__fields__[f.name] = f",
        "new": "use_type.__fields__[f.name] = create_cloned_field(f)",
    }
    assert regression.arguments["path"] == "tests/test_filter_pydantic_sub_model.py"
    content = regression.arguments["content"]
    assert len(content.splitlines()) == 91
    assert hashlib.sha256(content.encode("utf-8")).hexdigest() == (
        "f64fb11b78917f494f3101424f30219be3d8e761bff6da437f9ccd586b372c50"
    )


def test_source_bound_hard_crash_suite_binds_exact_recovery_window():
    suite = RunABSuiteManifest.model_validate(
        yaml.safe_load(HARD_CRASH_SUITE_PATH.read_text(encoding="utf-8"))
    )
    manifest = RunABEvalManifest.model_validate(
        yaml.safe_load(LUIGI_HARD_CRASH_MANIFEST_PATH.read_text(encoding="utf-8"))
    )

    assert suite.sha256 == "3b421aca80519b65e50d230c4cb3e397dc40851796f615c5ab778a83f1e64a02"
    assert manifest.sha256 == "72c612e86b0c2649ed70c251be48306916e11a2638313317cacd63421b53d6aa"
    assert len(suite.cases) == 1
    suite.cases[0].check_manifest(manifest)
    assert suite.cases[0].source.project == "luigi"
    assert suite.cases[0].source.reduction == "full_checkout"
    assert manifest.schema_version == 2
    assert {arm.restart_points for arm in manifest.arms} == {(3, 6)}
    assert {arm.hard_crash_after_tool_effect_at_model_call for arm in manifest.arms} == {2}
    crash_actions = [arm.actions[1] for arm in manifest.arms]
    assert crash_actions[0] == crash_actions[1]
    assert crash_actions[0].tool == "replace_text"
    assert crash_actions[0].arguments["path"] == "luigi/server.py"


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
