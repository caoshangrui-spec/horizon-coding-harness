import json
from pathlib import Path

from typer.testing import CliRunner

from horizon.interfaces.cli.app import app

runner = CliRunner()
REGISTRY_PATH = Path(__file__).resolve().parents[2] / "benchmarks" / "independent-tasks-v1.yaml"


def test_task_registry_preflight_cli_is_zero_cost_and_persists_report(tmp_path):
    state = tmp_path / "state"

    result = runner.invoke(
        app,
        [
            "eval",
            "task-registry-preflight",
            str(REGISTRY_PATH),
            "--state-dir",
            str(state),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    report = payload["report"]
    assert report["task_count"] == 24
    assert report["unique_source_task_count"] == 24
    assert report["execution_manifest_count"] == 10
    assert report["not_prepared_execution_count"] == 14
    assert report["paid_model_called"] is False
    assert report["network_called"] is False
    assert report["repository_code_executed"] is False
    report_ref = payload["report_ref"]
    report_path = state / "artifacts" / report_ref[:2] / report_ref
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted["registry_id"] == "bugsinpy-independent-tasks-v1"
    assert persisted["preflight_passed"] is True
