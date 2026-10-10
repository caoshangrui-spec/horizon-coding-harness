import hashlib
import json

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.application.services import HarnessService, LeaseToken
from horizon.application.terminal_suite import (
    export_terminal_suite,
    verify_terminal_suite_pack,
)
from horizon.domain.budget import Usage
from horizon.domain.common import canonical_json
from horizon.domain.errors import BudgetStop, BudgetStopReason
from horizon.domain.events import NewEvent
from horizon.domain.states import RunStatus
from horizon.domain.terminal_suite import (
    TerminalSuiteCase,
    TerminalSuiteManifest,
)
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

    manifest = TerminalSuiteManifest(
        suite_id="mixed-terminal-runs",
        cases=(
            TerminalSuiteCase(case_id="success", run_id=succeeded.run_id),
            TerminalSuiteCase(case_id="budget-stop", run_id=budget_failed.run_id),
            TerminalSuiteCase(case_id="cancelled", run_id=cancelled.run_id),
            TerminalSuiteCase(case_id="unknown-effect", run_id=unknown_failed.run_id),
        ),
    )
    result = export_terminal_suite(store, manifest, tmp_path / "terminal-suite")
    report = result.suite_pack.report

    assert report.case_count == 4
    assert report.succeeded_run_count == 1
    assert report.failed_run_count == 2
    assert report.cancelled_run_count == 1
    assert report.task_success_rate == 0.25
    assert report.terminal_capture_verified_count == 4
    assert report.unknown_effect_run_count == 1
    assert report.open_effect_run_count == 1
    assert [(item.reason, item.run_count) for item in report.failure_reason_counts] == [
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
    ]
    assert report.cases[1].budget_stop_reason == BudgetStopReason.RUN_MODEL_COST_LIMIT
    assert report.cases[3].unknown_effects_present is True
    assert report.cases[3].open_effects_present is True
    assert report.runs_executed is False
    assert report.paid_model_called is False
    assert report.network_called is False
    assert report.tool_called is False
    assert report.repository_code_executed is False
    assert verify_terminal_suite_pack(result.suite_pack_path) == result.suite_pack
    assert "Task success rate: `0.250000`" in result.summary_path.read_text(encoding="utf-8")


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
    assert payload["cancelled_run_count"] == 1
    assert payload["task_success_rate"] == 0.0
    assert payload["runs_executed"] is False

    verified = runner.invoke(
        app,
        ["eval", "verify-terminal-suite", str(output / "suite-pack.json")],
    )

    assert verified.exit_code == 0, verified.output
    verification = json.loads(verified.stdout)
    assert verification["verified"] is True
    assert verification["suite_id"] == "cli-terminal-suite"
    assert verification["terminal_capture_verified_count"] == 1


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
