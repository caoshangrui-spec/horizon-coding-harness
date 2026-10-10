import json
from pathlib import Path

from typer.testing import CliRunner

from horizon.interfaces.cli.app import app

runner = CliRunner()
MANIFEST_PATH = (
    Path(__file__).resolve().parents[2]
    / "benchmarks"
    / "reliability"
    / "horizon-controller-v1.yaml"
)


def test_reliability_eval_cli_is_offline_and_persists_immutable_report(tmp_path):
    state = tmp_path / "state"

    result = runner.invoke(
        app,
        [
            "eval",
            "reliability",
            str(MANIFEST_PATH),
            "--state-dir",
            str(state),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    report = payload["report"]
    assert report["case_count"] == 14
    assert report["passed_case_count"] == 14
    assert report["policy_decision_count"] == 62
    assert report["false_positive_count"] == 0
    assert report["false_negative_count"] == 0
    assert report["paid_model_called"] is False
    assert report["network_called"] is False
    assert report["repository_code_executed"] is False
    report_ref = payload["report_ref"]
    report_path = state / "artifacts" / report_ref[:2] / report_ref
    assert report_path.is_file()
    assert json.loads(report_path.read_text(encoding="utf-8"))["benchmark_id"] == (
        "horizon-controller-v1"
    )
