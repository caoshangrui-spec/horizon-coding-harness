import json
from pathlib import Path

from typer.testing import CliRunner

from horizon.interfaces.cli.app import app

runner = CliRunner()
SUITE_PATH = Path(__file__).resolve().parents[2] / "benchmarks" / "real-model-smoke-v1.yaml"


def test_smoke_preflight_cli_is_offline_and_persists_report(tmp_path):
    state = tmp_path / "state"

    result = runner.invoke(
        app,
        [
            "eval",
            "smoke-preflight",
            str(SUITE_PATH),
            "--state-dir",
            str(state),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    report = payload["report"]
    assert report["suite_id"] == "bugsinpy-real-model-smoke-v1"
    assert report["manifest_digest"] == (
        "2c8ca4da9b88ab047944cdbce98537c8ab017f4a5a6236da1067c53759453554"
    )
    assert report["task_count"] == 5
    assert report["unique_project_count"] == 5
    assert report["paid_launch_ready"] is False
    assert len(report["launch_blockers"]) == 3
    assert report["paid_model_called"] is False
    assert report["network_called"] is False
    assert report["repository_code_executed"] is False
    assert report["external_cost_cny"] == 0

    report_ref = payload["report_ref"]
    assert report_ref == "bc297aaaf85e8bd600198f333f6af23b03396f55dcbd5ee893cbd7c9133bc6e7"
    report_path = state / "artifacts" / report_ref[:2] / report_ref
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted == report
    assert persisted["structural_preflight_passed"] is True
