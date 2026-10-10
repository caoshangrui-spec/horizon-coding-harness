import hashlib
import json

import pytest
from typer.testing import CliRunner

from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.application.services import HarnessService
from horizon.application.terminal_evidence import (
    export_terminal_evidence_pack,
    verify_terminal_evidence_pack,
)
from horizon.domain.budget import Usage
from horizon.domain.common import canonical_json
from horizon.domain.events import NewEvent
from horizon.domain.run import projection_hash
from horizon.domain.states import RunStatus
from horizon.interfaces.cli.app import app


def test_failed_terminal_evidence_replays_and_preserves_unknown_effects(
    tmp_path,
    store,
    service,
    running,
    clock,
):
    run, token = running
    service.reserve(
        run.run_id,
        "uncertain-provider-call",
        Usage(model_calls=1, cost_usd="0.50"),
        token,
        "reserve-uncertain-provider-call",
    )
    service.mark_usage_unknown(
        run.run_id,
        "uncertain-provider-call",
        token,
        "classify-uncertain-provider-call",
    )
    clock.advance(3601)
    failed = service.expire(run.run_id, "expire-after-unknown-provider-call")
    assert failed.status == RunStatus.FAILED

    result = export_terminal_evidence_pack(
        store,
        run.run_id,
        tmp_path / "failed-evidence",
    )

    evidence = result.evidence_pack.evidence
    assert evidence.status == RunStatus.FAILED
    assert evidence.task_succeeded is False
    assert evidence.failure_reason == "wall_clock_limit"
    assert evidence.unknown_reservation_ids == ("uncertain-provider-call",)
    assert evidence.open_reservation_ids == ("uncertain-provider-call",)
    assert evidence.unknown_effects_present is True
    assert evidence.open_effects_present is True
    assert {item.role for item in result.evidence_pack.files} == {
        "trace",
        "final_state",
        "summary",
    }
    assert verify_terminal_evidence_pack(result.evidence_pack_path) == result.evidence_pack
    replayed = SQLiteEventStore.replay_jsonl(result.trace_path.read_text(encoding="utf-8"))
    assert projection_hash(replayed) == evidence.projection_hash
    assert json.loads(result.final_state_path.read_text(encoding="utf-8")) == failed.as_dict()
    summary = result.summary_path.read_text(encoding="utf-8")
    assert "Task succeeded: `false`" in summary
    assert 'Unknown reservations: ["uncertain-provider-call"]' in summary


def test_succeeded_terminal_evidence_claims_task_success_only_after_validation(
    tmp_path,
    store,
    service,
    running,
):
    run, token = running
    checkpointed = store.command(
        run.run_id,
        "terminal-evidence-checkpoint",
        {"operation": "terminal-evidence-checkpoint"},
        lambda current: [
            NewEvent(
                event_type="CHECKPOINT_COMMITTED",
                payload={
                    "checkpoint_id": "chk_terminal_evidence",
                    "event_seq": current.seq,
                    "task_spec_hash": current.task.sha256,
                    "manifest_sha256": "a" * 64,
                    "workspace_revision": "b" * 64,
                },
            )
        ],
    )
    service.transition(
        run.run_id,
        RunStatus.VALIDATING,
        token,
        "terminal-evidence-validating",
    )
    service.record_validation(
        run.run_id,
        ("unit",),
        "c" * 64,
        token,
        "terminal-evidence-validation",
    )
    succeeded = service.pass_work_item_and_succeed(
        run.run_id,
        "fix",
        token,
        "terminal-evidence-success",
    )
    assert checkpointed.workspace_revision == "b" * 64
    assert succeeded.status == RunStatus.SUCCEEDED

    result = export_terminal_evidence_pack(
        store,
        run.run_id,
        tmp_path / "succeeded-evidence",
    )

    evidence = result.evidence_pack.evidence
    assert evidence.status == RunStatus.SUCCEEDED
    assert evidence.task_succeeded is True
    assert evidence.failure_reason is None
    assert evidence.unknown_effects_present is False
    assert evidence.open_effects_present is False
    assert verify_terminal_evidence_pack(result.evidence_pack_path) == result.evidence_pack


def test_cancelled_terminal_evidence_cli_is_successful_but_does_not_claim_task_success(
    tmp_path,
    task,
):
    db = tmp_path / "control.sqlite3"
    store = SQLiteEventStore(db)
    service = HarnessService(store)
    run = store.create(task, "create-cancelled-terminal-evidence")
    cancelled = service.cancel(run.run_id, "cancel-terminal-evidence")
    assert cancelled.status == RunStatus.CANCELLED
    output = tmp_path / "cancelled-evidence"
    runner = CliRunner()

    completed = runner.invoke(
        app,
        [
            "--db",
            str(db),
            "trace",
            "bundle",
            run.run_id,
            "--output",
            str(output),
        ],
    )

    assert completed.exit_code == 0, completed.output
    payload = json.loads(completed.stdout)
    assert payload["status"] == "CANCELLED"
    assert payload["task_succeeded"] is False
    assert payload["failure_reason"] is None
    assert payload["unknown_effects_present"] is False
    assert payload["open_effects_present"] is False
    assert payload["verification_mode"] == "offline_trace_replay"
    assert payload["export_network_called"] is False
    pack_path = output / "evidence-pack.json"
    original = pack_path.read_bytes()
    assert verify_terminal_evidence_pack(pack_path).evidence.status == RunStatus.CANCELLED

    verified = runner.invoke(app, ["trace", "verify-bundle", str(pack_path)])

    assert verified.exit_code == 0, verified.output
    verification = json.loads(verified.stdout)
    assert verification["verified"] is True
    assert verification["status"] == "CANCELLED"
    assert verification["task_succeeded"] is False

    repeated = runner.invoke(
        app,
        [
            "--db",
            str(db),
            "trace",
            "bundle",
            run.run_id,
            "--output",
            str(output),
        ],
    )

    assert repeated.exit_code == 2
    assert "FileExistsError" in repeated.stderr
    assert pack_path.read_bytes() == original


def test_terminal_evidence_rejects_nonterminal_run_without_creating_output(
    tmp_path,
    store,
    task,
):
    run = store.create(task, "create-active-terminal-evidence")
    output = tmp_path / "must-not-exist"

    with pytest.raises(ValueError, match="terminal state"):
        export_terminal_evidence_pack(store, run.run_id, output)

    assert not output.exists()


def test_terminal_evidence_verifier_rejects_rehashed_false_summary(
    tmp_path,
    store,
    service,
    task,
):
    run = store.create(task, "create-tamper-terminal-evidence")
    service.cancel(run.run_id, "cancel-tamper-terminal-evidence")
    result = export_terminal_evidence_pack(
        store,
        run.run_id,
        tmp_path / "tampered-evidence",
    )
    false_summary = b"# Horizon Terminal Run Evidence\n\n- Task succeeded: `true`\n"
    result.summary_path.write_bytes(false_summary)
    payload = json.loads(result.evidence_pack_path.read_text(encoding="utf-8"))
    summary_record = next(item for item in payload["files"] if item["role"] == "summary")
    summary_record["bytes"] = len(false_summary)
    summary_record["sha256"] = hashlib.sha256(false_summary).hexdigest()
    result.evidence_pack_path.write_text(
        canonical_json(payload) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ValueError, match="summary does not match"):
        verify_terminal_evidence_pack(result.evidence_pack_path)
