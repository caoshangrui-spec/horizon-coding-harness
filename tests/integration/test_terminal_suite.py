import hashlib
import json

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.application.services import HarnessService, LeaseToken
from horizon.application.terminal_evidence import _summary as terminal_evidence_summary
from horizon.application.terminal_suite import (
    _summary as terminal_suite_summary,
)
from horizon.application.terminal_suite import (
    export_terminal_suite,
    verify_terminal_suite_pack,
)
from horizon.domain.budget import Usage
from horizon.domain.common import canonical_json, digest
from horizon.domain.errors import BudgetStop, BudgetStopReason
from horizon.domain.events import NewEvent
from horizon.domain.run_reliability import DegradationRecord
from horizon.domain.states import RunStatus
from horizon.domain.terminal_evidence import (
    TERMINAL_EVIDENCE_BOUNDARIES_V1,
    TerminalRunEvidence,
)
from horizon.domain.terminal_suite import (
    TERMINAL_SUITE_BOUNDARIES_V1,
    TerminalSuiteCase,
    TerminalSuiteManifest,
    TerminalSuiteReport,
)
from horizon.domain.tools import ToolCallRecord, ToolCallReservation
from horizon.interfaces.cli.app import app


def _start_run(store, service, task, plan, prefix):
    run = store.create(task, f"{prefix}-create")
    service.set_plan(run.run_id, plan, f"{prefix}-plan")
    leased = service.acquire_lease(run.run_id, f"{prefix}-worker", f"{prefix}-lease")
    token = LeaseToken.from_run(leased)
    running = service.transition(run.run_id, RunStatus.RUNNING, token, f"{prefix}-start")
    return running, token


def _succeed_run(store, service, run_id, token, prefix):
    store.command(
        run_id,
        f"{prefix}-checkpoint",
        {"operation": f"{prefix}-checkpoint"},
        lambda current: [
            NewEvent(
                event_type="CHECKPOINT_COMMITTED",
                payload={
                    "checkpoint_id": f"chk_{prefix}",
                    "event_seq": current.seq,
                    "task_spec_hash": current.task.sha256,
                    "manifest_sha256": "a" * 64,
                    "workspace_revision": "b" * 64,
                },
            )
        ],
    )
    service.transition(run_id, RunStatus.VALIDATING, token, f"{prefix}-validating")
    service.record_validation(
        run_id,
        ("unit",),
        "c" * 64,
        token,
        f"{prefix}-validation",
    )
    return service.pass_work_item_and_succeed(run_id, "fix", token, f"{prefix}-success")


def test_terminal_suite_aggregates_outcomes_budget_stops_and_uncertainty(
    tmp_path,
    store,
    service,
    task,
    plan,
    clock,
):
    succeeded_run, succeeded_token = _start_run(store, service, task, plan, "suite-success")
    succeeded = _succeed_run(store, service, succeeded_run.run_id, succeeded_token, "suite-success")

    budget_run, budget_token = _start_run(store, service, task, plan, "suite-budget")
    budget_failed = service.fail_budget_stop(
        budget_run.run_id,
        BudgetStop(
            reason_code=BudgetStopReason.RUN_MODEL_COST_LIMIT,
            scope="run",
            currency="USD",
            required_cost="0.20",
            available_cost="0.10",
        ),
        budget_token,
        "suite-budget-stop",
    )

    cancelled_run = store.create(task, "suite-cancel-create")
    cancelled = service.cancel(cancelled_run.run_id, "suite-cancel")

    unknown_run, unknown_token = _start_run(store, service, task, plan, "suite-unknown")
    service.reserve(
        unknown_run.run_id,
        "suite-uncertain-call",
        Usage(model_calls=1, cost_usd="0.10"),
        unknown_token,
        "suite-unknown-reserve",
    )
    service.mark_usage_unknown(
        unknown_run.run_id,
        "suite-uncertain-call",
        unknown_token,
        "suite-unknown-mark",
    )
    clock.advance(3601)
    unknown_failed = service.expire(unknown_run.run_id, "suite-unknown-expire")

    known_failed_run, known_failed_token = _start_run(
        store, service, task, plan, "suite-known-failed"
    )
    store.command(
        known_failed_run.run_id,
        "suite-known-failed-checkpoint",
        {"operation": "suite-known-failed-checkpoint"},
        lambda current: [
            NewEvent(
                event_type="CHECKPOINT_COMMITTED",
                payload={
                    "checkpoint_id": "chk_suite_known_failed",
                    "event_seq": current.seq,
                    "task_spec_hash": current.task.sha256,
                    "manifest_sha256": "d" * 64,
                    "workspace_revision": "e" * 64,
                },
            )
        ],
    )
    service.transition(
        known_failed_run.run_id,
        RunStatus.VALIDATING,
        known_failed_token,
        "suite-known-failed-validating",
    )
    service.record_validation(
        known_failed_run.run_id,
        (),
        "f" * 64,
        known_failed_token,
        "suite-known-failed-validation",
    )
    known_failed = service.fail(
        known_failed_run.run_id,
        "required_validation_failed",
        known_failed_token,
        "suite-known-failed-terminal",
    )

    manifest = TerminalSuiteManifest(
        suite_id="mixed-terminal-runs",
        cases=(
            TerminalSuiteCase(case_id="success", run_id=succeeded.run_id),
            TerminalSuiteCase(case_id="budget-stop", run_id=budget_failed.run_id),
            TerminalSuiteCase(case_id="cancelled", run_id=cancelled.run_id),
            TerminalSuiteCase(case_id="unknown-effect", run_id=unknown_failed.run_id),
            TerminalSuiteCase(case_id="known-check-failure", run_id=known_failed.run_id),
        ),
    )
    result = export_terminal_suite(store, manifest, tmp_path / "terminal-suite")
    report = result.suite_pack.report

    assert result.suite_pack.schema_version == 2
    assert report.schema_version == 2
    assert report.case_count == 5
    assert report.succeeded_run_count == 1
    assert report.failed_run_count == 3
    assert report.cancelled_run_count == 1
    assert report.task_success_rate == 0.2
    assert report.terminal_capture_verified_count == 5
    assert report.unknown_effect_run_count == 1
    assert report.open_effect_run_count == 1
    assert report.reliability_summary_verified_count == 5
    assert report.unknown_acceptance_run_count == 3
    assert report.required_check_failure_run_count == 1
    assert report.quality_flagged_run_count == 4
    assert report.clean_success_run_count == 1
    assert report.quality_flagged_success_run_count == 0
    assert report.fallback_run_count == 0
    assert report.degradation_run_count == 0
    assert report.retrieval_degraded_run_count == 0
    assert [(item.reason, item.run_count) for item in report.quality_flag_counts] == [
        ("acceptance_evidence_unknown", 3),
        ("open_effects_present", 1),
        ("required_checks_failed", 1),
        ("unknown_effects_present", 1),
    ]
    assert [(item.reason, item.run_count) for item in report.failure_reason_counts] == [
        ("required_validation_failed", 1),
        ("run_model_cost_limit", 1),
        ("wall_clock_limit", 1),
    ]
    assert [(item.reason, item.run_count) for item in report.budget_stop_reason_counts] == [
        ("run_model_cost_limit", 1)
    ]
    assert [case.status for case in report.cases] == [
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.FAILED,
        RunStatus.FAILED,
    ]
    assert report.cases[1].budget_stop_reason == BudgetStopReason.RUN_MODEL_COST_LIMIT
    assert report.cases[3].unknown_effects_present is True
    assert report.cases[3].open_effects_present is True
    assert report.cases[0].required_checks_passed is True
    assert report.cases[0].acceptance_evidence_unknown is False
    assert report.cases[0].quality_flags == ()
    assert report.cases[4].required_checks_passed is False
    assert report.cases[4].acceptance_evidence_unknown is False
    assert report.cases[4].quality_flags == ("required_checks_failed",)
    assert report.runs_executed is False
    assert report.paid_model_called is False
    assert report.network_called is False
    assert report.tool_called is False
    assert report.repository_code_executed is False
    assert verify_terminal_suite_pack(result.suite_pack_path) == result.suite_pack
    summary = result.summary_path.read_text(encoding="utf-8")
    assert "Task success rate: `0.200000`" in summary
    assert "Clean / quality-flagged succeeded Runs: `1` / `0`" in summary
    assert "Runs with known required-check failure: `1`" in summary


def test_terminal_suite_cli_exports_and_verifies_without_database(tmp_path, task):
    db = tmp_path / "control.sqlite3"
    store = SQLiteEventStore(db)
    service = HarnessService(store)
    run = store.create(task, "cli-suite-create")
    cancelled = service.cancel(run.run_id, "cli-suite-cancel")
    manifest = TerminalSuiteManifest(
        suite_id="cli-terminal-suite",
        cases=(TerminalSuiteCase(case_id="cancelled", run_id=cancelled.run_id),),
    )
    manifest_path = tmp_path / "terminal-suite.json"
    manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8", newline="\n")
    output = tmp_path / "cli-suite-output"
    runner = CliRunner()

    completed = runner.invoke(
        app,
        [
            "--db",
            str(db),
            "eval",
            "terminal-suite",
            str(manifest_path),
            "--output",
            str(output),
        ],
    )

    assert completed.exit_code == 0, completed.output
    payload = json.loads(completed.stdout)
    assert payload["case_count"] == 1
    assert payload["schema_version"] == 2
    assert payload["cancelled_run_count"] == 1
    assert payload["task_success_rate"] == 0.0
    assert payload["reliability_summary_verified_count"] == 1
    assert payload["unknown_acceptance_run_count"] == 1
    assert payload["quality_flagged_run_count"] == 1
    assert payload["runs_executed"] is False

    verified = runner.invoke(
        app,
        ["eval", "verify-terminal-suite", str(output / "suite-pack.json")],
    )

    assert verified.exit_code == 0, verified.output
    verification = json.loads(verified.stdout)
    assert verification["verified"] is True
    assert verification["schema_version"] == 2
    assert verification["suite_id"] == "cli-terminal-suite"
    assert verification["terminal_capture_verified_count"] == 1
    assert verification["reliability_summary_verified_count"] == 1


def test_terminal_suite_preflights_all_runs_and_rejects_duplicate_ids(
    tmp_path,
    store,
    task,
):
    active = store.create(task, "nonterminal-suite-create")
    manifest = TerminalSuiteManifest(
        suite_id="nonterminal-suite",
        cases=(TerminalSuiteCase(case_id="active", run_id=active.run_id),),
    )
    output = tmp_path / "must-not-exist"

    with pytest.raises(ValueError, match="nonterminal Run"):
        export_terminal_suite(store, manifest, output)

    assert not output.exists()
    with pytest.raises(ValidationError, match="Run IDs must be unique"):
        TerminalSuiteManifest(
            suite_id="duplicate-suite",
            cases=(
                TerminalSuiteCase(case_id="one", run_id=active.run_id),
                TerminalSuiteCase(case_id="two", run_id=active.run_id),
            ),
        )


def test_terminal_suite_verifier_rejects_rehashed_false_summary(
    tmp_path,
    store,
    service,
    task,
):
    run = store.create(task, "tamper-suite-create")
    cancelled = service.cancel(run.run_id, "tamper-suite-cancel")
    manifest = TerminalSuiteManifest(
        suite_id="tampered-terminal-suite",
        cases=(TerminalSuiteCase(case_id="cancelled", run_id=cancelled.run_id),),
    )
    result = export_terminal_suite(store, manifest, tmp_path / "tampered-suite")
    false_summary = b"# Horizon Terminal Run Suite\n\n- Task success rate: `1.000000`\n"
    result.summary_path.write_bytes(false_summary)
    payload = json.loads(result.suite_pack_path.read_text(encoding="utf-8"))
    summary_record = next(item for item in payload["files"] if item["role"] == "summary")
    summary_record["bytes"] = len(false_summary)
    summary_record["sha256"] = hashlib.sha256(false_summary).hexdigest()
    result.suite_pack_path.write_text(
        canonical_json(payload) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ValueError, match="summary does not match"):
        verify_terminal_suite_pack(result.suite_pack_path)


def test_terminal_suite_aggregates_trace_bound_retrieval_degradation(
    tmp_path,
    store,
    service,
    task,
    plan,
):
    run, token = _start_run(store, service, task, plan, "suite-degraded")
    reservation = ToolCallReservation(
        call_id="suite-retrieval-call",
        name="retrieve_code",
        arguments_hash="a" * 64,
        workspace_revision="b" * 64,
        workspace_manifest_ref="c" * 64,
    )
    service.reserve_tool_call(run.run_id, reservation, token, "suite-retrieval-reserve")
    record = ToolCallRecord(
        call_id=reservation.call_id,
        name=reservation.name,
        arguments_hash=reservation.arguments_hash,
        status="success",
        output_hash="d" * 64,
        workspace_revision_before=reservation.workspace_revision,
        workspace_revision_after=reservation.workspace_revision,
        artifact_ref="d" * 64,
        workspace_manifest_ref=reservation.workspace_manifest_ref,
    )
    degradation = DegradationRecord(
        operation="retrieval",
        source_tool_call_id=record.call_id,
        original_error_type="fts5_unavailable",
        original_error_ref=record.artifact_ref,
        strategy="bounded_lexical_scan",
        quality_flag="retrieval_degraded",
        task_spec_hash=run.task.sha256,
        required_acceptance_hash=digest(("unit",)),
        effective_required_acceptance_hash=digest(("unit",)),
    )
    service.settle_tool_call(
        run.run_id,
        record,
        token,
        "suite-retrieval-settle",
        degradation=degradation,
    )
    succeeded = _succeed_run(store, service, run.run_id, token, "suite-degraded")
    manifest = TerminalSuiteManifest(
        suite_id="degraded-terminal-run",
        cases=(TerminalSuiteCase(case_id="degraded-success", run_id=succeeded.run_id),),
    )

    result = export_terminal_suite(store, manifest, tmp_path / "degraded-terminal-suite")
    report = result.suite_pack.report
    case = report.cases[0]

    assert report.succeeded_run_count == 1
    assert report.clean_success_run_count == 0
    assert report.quality_flagged_success_run_count == 1
    assert report.quality_flagged_run_count == 1
    assert report.degradation_run_count == 1
    assert report.retrieval_degraded_run_count == 1
    assert report.fallback_run_count == 0
    assert [(item.reason, item.run_count) for item in report.quality_flag_counts] == [
        ("retrieval_degraded", 1)
    ]
    assert case.required_checks_passed is True
    assert case.acceptance_evidence_unknown is False
    assert case.quality_flags == ("retrieval_degraded",)
    assert case.degradation_count == 1
    assert verify_terminal_suite_pack(result.suite_pack_path) == result.suite_pack


def test_terminal_suite_verifier_keeps_schema_v1_packs_readable(
    tmp_path,
    store,
    service,
    task,
):
    run = store.create(task, "legacy-suite-create")
    cancelled = service.cancel(run.run_id, "legacy-suite-cancel")
    manifest = TerminalSuiteManifest(
        suite_id="legacy-terminal-suite",
        cases=(TerminalSuiteCase(case_id="cancelled", run_id=cancelled.run_id),),
    )
    result = export_terminal_suite(store, manifest, tmp_path / "legacy-terminal-suite")
    suite_payload = json.loads(result.suite_pack_path.read_text(encoding="utf-8"))
    report_payload = suite_payload["report"]

    child_path = result.output_dir / report_payload["cases"][0]["evidence_pack_path"]
    child_payload = json.loads(child_path.read_text(encoding="utf-8"))
    child_evidence_payload = child_payload["evidence"]
    child_evidence_payload["schema_version"] = 1
    child_evidence_payload["boundaries"] = list(TERMINAL_EVIDENCE_BOUNDARIES_V1)
    child_evidence_payload.pop("reliability_summary")
    legacy_evidence = TerminalRunEvidence.model_validate(child_evidence_payload)
    child_summary = terminal_evidence_summary(legacy_evidence).encode("utf-8")
    child_summary_path = child_path.parent / "SUMMARY.md"
    child_summary_path.write_bytes(child_summary)
    child_summary_record = next(
        item for item in child_payload["files"] if item["role"] == "summary"
    )
    child_summary_record["bytes"] = len(child_summary)
    child_summary_record["sha256"] = hashlib.sha256(child_summary).hexdigest()
    child_payload["schema_version"] = 1
    child_path.write_text(canonical_json(child_payload) + "\n", encoding="utf-8", newline="\n")

    case_payload = report_payload["cases"][0]
    case_payload["evidence_pack_bytes"] = child_path.stat().st_size
    case_payload["evidence_pack_sha256"] = hashlib.sha256(child_path.read_bytes()).hexdigest()
    for field in (
        "reliability_summary_present",
        "required_checks_passed",
        "acceptance_evidence_unknown",
        "quality_flags",
        "fallback_count",
        "degradation_count",
    ):
        case_payload.pop(field)
    report_payload["schema_version"] = 1
    report_payload["boundaries"] = list(TERMINAL_SUITE_BOUNDARIES_V1)
    for field in (
        "reliability_summary_verified_count",
        "unknown_acceptance_run_count",
        "required_check_failure_run_count",
        "quality_flagged_run_count",
        "clean_success_run_count",
        "quality_flagged_success_run_count",
        "fallback_run_count",
        "degradation_run_count",
        "retrieval_degraded_run_count",
        "quality_flag_counts",
    ):
        report_payload.pop(field)
    legacy_report = TerminalSuiteReport.model_validate(report_payload)
    report_content = (canonical_json(report_payload) + "\n").encode("utf-8")
    summary_content = terminal_suite_summary(legacy_report).encode("utf-8")
    result.report_path.write_bytes(report_content)
    result.summary_path.write_bytes(summary_content)
    for role, content in (("report", report_content), ("summary", summary_content)):
        record_payload = next(item for item in suite_payload["files"] if item["role"] == role)
        record_payload["bytes"] = len(content)
        record_payload["sha256"] = hashlib.sha256(content).hexdigest()
    suite_payload["schema_version"] = 1
    result.suite_pack_path.write_text(
        canonical_json(suite_payload) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    verified = verify_terminal_suite_pack(result.suite_pack_path)

    assert verified.schema_version == 1
    assert verified.report.schema_version == 1
    assert verified.report.reliability_summary_verified_count == 0
    assert verified.report.quality_flag_counts == ()


def test_terminal_suite_verifier_rejects_rehashed_false_reliability_aggregate(
    tmp_path,
    store,
    service,
    task,
):
    run = store.create(task, "tamper-reliability-suite-create")
    cancelled = service.cancel(run.run_id, "tamper-reliability-suite-cancel")
    manifest = TerminalSuiteManifest(
        suite_id="tampered-reliability-terminal-suite",
        cases=(TerminalSuiteCase(case_id="cancelled", run_id=cancelled.run_id),),
    )
    result = export_terminal_suite(store, manifest, tmp_path / "tampered-reliability-suite")
    suite_payload = json.loads(result.suite_pack_path.read_text(encoding="utf-8"))
    suite_payload["report"]["unknown_acceptance_run_count"] = 0
    report_content = (canonical_json(suite_payload["report"]) + "\n").encode("utf-8")
    result.report_path.write_bytes(report_content)
    report_record = next(item for item in suite_payload["files"] if item["role"] == "report")
    report_record["bytes"] = len(report_content)
    report_record["sha256"] = hashlib.sha256(report_content).hexdigest()
    result.suite_pack_path.write_text(
        canonical_json(suite_payload) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ValueError, match="unknown_acceptance_run_count"):
        verify_terminal_suite_pack(result.suite_pack_path)
