import json

import pytest
from typer.testing import CliRunner

from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.application.portfolio_demo import (
    PortfolioDemoRunner,
    verify_portfolio_evidence_pack,
)
from horizon.domain.portfolio_demo import PortfolioDemoReport, PortfolioEvidencePack
from horizon.domain.run import projection_hash
from horizon.domain.states import RunStatus
from horizon.interfaces.cli.app import app


def test_portfolio_demo_exports_replayable_recovery_evidence(tmp_path):
    output = tmp_path / "portfolio-evidence"

    result = PortfolioDemoRunner().run(output)

    assert result.report.status == RunStatus.SUCCEEDED
    assert result.report.recovery_mode == "durable_worker_handoff"
    assert result.report.final_lease_epoch == 2
    assert result.report.verification.all_checks_passed
    assert result.report.verification.structured_tool_error_observed
    assert result.report.verification.resumed_context_contains_error
    assert result.report.paid_model_called is False
    assert result.report.network_called is False
    assert result.report.repository_code_executed is False
    assert result.report.external_cost_cny == 0
    assert result.report.tool_sequence == (
        "retrieve_code",
        "read_file",
        "read_file",
        "replace_text",
        "run_check",
        "submit",
        "run_check",
    )
    assert result.report.tool_statuses == (
        "success",
        "error",
        "success",
        "success",
        "success",
        "success",
        "success",
    )
    assert (output / "source/src/parser.py").read_text(encoding="utf-8") == (
        "def parse(value):\n    return [value]\n"
    )
    assert (output / "workspace/src/parser.py").read_text(encoding="utf-8") == (
        "def parse(value):\n    return [] if value == '' else [value]\n"
    )

    pack = verify_portfolio_evidence_pack(result.evidence_pack_path)
    assert pack == result.evidence_pack
    assert {item.role for item in pack.files} == {
        "report",
        "trace",
        "final_state",
        "summary",
    }
    replayed = SQLiteEventStore.replay_jsonl(result.trace_path.read_text(encoding="utf-8"))
    assert projection_hash(replayed) == result.report.projection_hash
    assert json.loads(result.final_run_path.read_text(encoding="utf-8"))["status"] == "SUCCEEDED"
    assert (
        PortfolioDemoReport.model_validate_json(result.report_path.read_text(encoding="utf-8"))
        == result.report
    )
    assert (
        PortfolioEvidencePack.model_validate_json(
            result.evidence_pack_path.read_text(encoding="utf-8")
        )
        == result.evidence_pack
    )


def test_portfolio_evidence_pack_detects_file_tampering(tmp_path):
    result = PortfolioDemoRunner().run(tmp_path / "portfolio-evidence")
    result.summary_path.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(ValueError, match="integrity verification"):
        verify_portfolio_evidence_pack(result.evidence_pack_path)


def test_portfolio_demo_cli_is_one_command_and_never_overwrites(tmp_path):
    runner = CliRunner()
    output = tmp_path / "portfolio-evidence"

    completed = runner.invoke(app, ["demo", "run", "--output", str(output)])

    assert completed.exit_code == 0, completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["status"] == "SUCCEEDED"
    assert payload["all_checks_passed"] is True
    assert payload["paid_model_called"] is False
    assert payload["network_called"] is False
    assert payload["repository_code_executed"] is False
    assert payload["external_cost_cny"] == "0"
    assert payload["claim_scope"] == "offline_deterministic_harness_demo"
    pack_path = output / "evidence-pack.json"
    original = pack_path.read_bytes()
    assert verify_portfolio_evidence_pack(pack_path).all_checks_passed

    repeated = runner.invoke(app, ["demo", "run", "--output", str(output)])

    assert repeated.exit_code == 2
    assert "FileExistsError" in repeated.stderr
    assert pack_path.read_bytes() == original
