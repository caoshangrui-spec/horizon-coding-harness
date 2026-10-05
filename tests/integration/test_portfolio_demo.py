import json

import pytest
from typer.testing import CliRunner

from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.application.portfolio_demo import (
    PortfolioDemoRunner,
    verify_portfolio_evidence_pack,
)
from horizon.domain.errors import IntegrityError
from horizon.domain.portfolio_demo import (
    PORTFOLIO_DEMO_LEGACY_EXCLUDED_CLAIMS,
    PortfolioDemoReport,
    PortfolioEvidencePack,
)
from horizon.domain.run import projection_hash
from horizon.domain.states import RunStatus
from horizon.interfaces.cli.app import app


def test_portfolio_demo_exports_replayable_recovery_evidence(tmp_path):
    output = tmp_path / "portfolio-evidence"

    result = PortfolioDemoRunner().run(output)

    assert result.report.status == RunStatus.SUCCEEDED
    assert result.report.schema_version == 3
    assert result.evidence_pack.schema_version == 2
    assert result.report.recovery_mode == "durable_handoff_and_hard_crash"
    assert result.report.final_lease_epoch == 3
    assert result.report.verification.all_checks_passed
    assert result.report.verification.structured_tool_error_observed
    assert result.report.verification.resumed_context_contains_error
    assert result.report.verification.retrieval_evidence_in_model_context
    assert result.report.verification.evidence_backed_write
    assert result.report.verification.hard_crash_recovery_verified
    assert result.report.evidence_lineage is not None
    assert result.report.evidence_lineage.verified
    assert result.report.evidence_lineage.write_path == "src/parser.py"
    assert result.report.evidence_lineage.matched_chunk_content_hash is not None
    assert result.report.crash_recovery is not None
    assert result.report.crash_recovery.observed_exit_code == 86
    assert result.report.crash_recovery.crashed_worker_epoch == 2
    assert result.report.crash_recovery.recovery_worker_epoch == 3
    assert result.report.crash_recovery.reservation_without_receipt
    assert result.report.crash_recovery.expected_effect_present
    assert result.report.crash_recovery.conservative_unknown_recorded
    assert result.report.crash_recovery.exact_effect_accepted
    assert result.report.crash_recovery.resumed_context_contains_recovery
    assert result.report.crash_recovery.one_recovered_write_receipt
    assert result.report.crash_recovery.verified
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


def test_portfolio_report_still_reads_schema_v1_without_lineage(tmp_path):
    result = PortfolioDemoRunner().run(tmp_path / "portfolio-evidence")
    legacy = result.report.model_dump(mode="json")
    legacy["schema_version"] = 1
    legacy["recovery_mode"] = "durable_worker_handoff"
    legacy["worker_handoffs"] = 1
    legacy["excluded_claims"] = list(PORTFOLIO_DEMO_LEGACY_EXCLUDED_CLAIMS)
    legacy.pop("evidence_lineage")
    legacy.pop("crash_recovery")
    legacy["verification"].pop("retrieval_evidence_in_model_context")
    legacy["verification"].pop("evidence_backed_write")
    legacy["verification"].pop("hard_crash_recovery_verified")

    restored = PortfolioDemoReport.model_validate(legacy)

    assert restored.schema_version == 1
    assert restored.evidence_lineage is None
    assert restored.verification.all_checks_passed


def test_portfolio_report_still_reads_schema_v2_with_lineage(tmp_path):
    result = PortfolioDemoRunner().run(tmp_path / "portfolio-evidence")
    legacy = result.report.model_dump(mode="json")
    legacy["schema_version"] = 2
    legacy["recovery_mode"] = "durable_worker_handoff"
    legacy["worker_handoffs"] = 1
    legacy["excluded_claims"] = list(PORTFOLIO_DEMO_LEGACY_EXCLUDED_CLAIMS)
    legacy.pop("crash_recovery")
    legacy["verification"].pop("hard_crash_recovery_verified")

    restored = PortfolioDemoReport.model_validate(legacy)

    assert restored.schema_version == 2
    assert restored.evidence_lineage == result.report.evidence_lineage
    assert restored.crash_recovery is None
    assert restored.verification.all_checks_passed


def test_portfolio_evidence_pack_detects_file_tampering(tmp_path):
    result = PortfolioDemoRunner().run(tmp_path / "portfolio-evidence")
    result.summary_path.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(ValueError, match="integrity verification"):
        verify_portfolio_evidence_pack(result.evidence_pack_path)


def test_portfolio_lineage_detects_retrieval_artifact_tampering(tmp_path):
    result = PortfolioDemoRunner().run(tmp_path / "portfolio-evidence")
    assert result.report.evidence_lineage is not None
    reference = result.report.evidence_lineage.retrieval_artifact_ref
    artifact = result.output_dir / "artifacts" / reference[:2] / reference
    artifact.write_text("{}", encoding="utf-8")

    with pytest.raises(IntegrityError, match="digest mismatch"):
        verify_portfolio_evidence_pack(result.evidence_pack_path)


def test_portfolio_evidence_pack_detects_crash_marker_tampering(tmp_path):
    result = PortfolioDemoRunner().run(tmp_path / "portfolio-evidence")
    marker = result.output_dir / "hard-crash-marker.json"
    marker.write_text('{"tampered":true}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="hard-crash marker"):
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
    assert payload["recovery_mode"] == "durable_handoff_and_hard_crash"
    assert payload["worker_handoffs"] == 2
    assert payload["hard_crash_recovery_verified"] is True
    assert payload["crashed_worker_exit_code"] == 86
    assert payload["write_recovery_disposition"] == "accept_replace"
    pack_path = output / "evidence-pack.json"
    original = pack_path.read_bytes()
    assert verify_portfolio_evidence_pack(pack_path).all_checks_passed

    repeated = runner.invoke(app, ["demo", "run", "--output", str(output)])

    assert repeated.exit_code == 2
    assert "FileExistsError" in repeated.stderr
    assert pack_path.read_bytes() == original
