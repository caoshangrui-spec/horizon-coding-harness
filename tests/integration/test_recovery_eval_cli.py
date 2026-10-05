import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.application.recovery_eval import verify_recovery_matrix_report
from horizon.domain.recovery_evaluation import RecoveryMatrixManifest, RecoveryMatrixReport
from horizon.interfaces.cli.app import app

runner = CliRunner()
MANIFEST_PATH = (
    Path(__file__).resolve().parents[2]
    / "benchmarks"
    / "recovery"
    / "horizon-recovery-matrix-v2.yaml"
)


def test_recovery_eval_runs_real_crash_and_full_write_state_matrix(tmp_path):
    output = tmp_path / "recovery-evidence"

    result = runner.invoke(
        app,
        [
            "eval",
            "recovery",
            str(MANIFEST_PATH),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["case_count"] == 20
    assert payload["passed_case_count"] == 20
    assert payload["recovery_success_rate"] == 1.0
    assert payload["safe_block_rate"] == 1.0
    assert payload["unrecoverable_count"] == 0
    assert payload["incorrect_resume_count"] == 0
    assert payload["recovery_redispatch_count"] == 0
    assert payload["duplicate_side_effect_count"] == 0
    assert payload["actual_process_crash_case_count"] == 2
    assert payload["trace_replay_verified_count"] == 2
    assert payload["paid_model_called"] is False
    assert payload["network_called"] is False
    assert payload["repository_code_executed"] is False
    assert payload["external_cost_cny"] == "0"

    report_path = Path(payload["report"])
    report_content = report_path.read_bytes()
    assert ArtifactStore(output / "artifacts").read(payload["report_ref"]) == report_content
    report = RecoveryMatrixReport.model_validate_json(report_content)
    hard_crash = report.cases[0]
    assert hard_crash.kind == "hard_crash"
    assert hard_crash.actual_process_crash_observed is True
    assert hard_crash.trace_replay_verified is True
    assert hard_crash.observed_outcome == "auto_recovered"
    model_crash = report.cases[1]
    assert model_crash.kind == "model_response_hard_crash"
    assert model_crash.actual_process_crash_observed is True
    assert model_crash.trace_replay_verified is True
    assert model_crash.observed_outcome == "safely_blocked"
    assert model_crash.recovery_redispatch_count == 0
    assert all(
        case.observed_outcome == "safely_blocked"
        for case in report.cases
        if case.injected_state in {"pre_effect", "diverged"} and case.decision == "accept"
    )

    manifest = RecoveryMatrixManifest.model_validate(
        yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    )
    assert verify_recovery_matrix_report(report_path, manifest) == report

    marker = output / "cases" / "01" / "provider-returned.txt"
    original_marker = marker.read_text(encoding="utf-8")
    marker.write_text("tampered-client-trace", encoding="utf-8")
    with pytest.raises(ValueError, match="evidence file hash mismatch"):
        verify_recovery_matrix_report(report_path, manifest)
    marker.write_text(original_marker, encoding="utf-8")

    tampered = output / "cases" / "02" / "workspace" / "src" / "alpha.py"
    tampered.write_text("alpha = 'tampered'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="workspace no longer matches evidence"):
        verify_recovery_matrix_report(report_path, manifest)
