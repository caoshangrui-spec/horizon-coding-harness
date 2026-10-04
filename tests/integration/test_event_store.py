import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.application.services import LeaseToken
from horizon.domain.errors import (
    BudgetStop,
    BudgetStopReason,
    Conflict,
    IntegrityError,
    InvalidTransition,
    LeaseConflict,
)
from horizon.domain.events import NewEvent
from horizon.domain.model import InputTokenBudget, InputTokenEstimate, ModelRequestBudgetEvidence
from horizon.domain.run import projection_hash
from horizon.domain.states import RunStatus
from horizon.domain.task import TaskSpec


def test_idempotent_create(store, task):
    first = store.create(task, "same-key")
    second = store.create(task, "same-key")
    assert first.run_id == second.run_id
    assert len(store.events(first.run_id)) == 1
    with pytest.raises(Conflict):
        store.create(task.model_copy(update={"title": "Different"}), "same-key")


def test_immutable_contract_versions(store, service, task, plan):
    run = store.create(task, "create")
    service.set_plan(run.run_id, plan, "plan")
    data = task.model_dump(mode="json")
    data.update(spec_version=2, objective="Updated by user")
    amended = TaskSpec.model_validate(data)
    result = service.amend(run.run_id, amended, "cli:user", "amend")
    assert result.task.spec_version == 2
    assert result.plan is None
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM task_specs").fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE task_specs SET spec_json='{}'")


def test_replay_ignores_deleted_or_corrupt_view(store, running):
    run, _ = running
    expected = projection_hash(store.get(run.run_id))
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE run_views SET state_json='invalid' WHERE run_id=?", (run.run_id,))
    assert projection_hash(store.get(run.run_id)) == expected
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM run_views")
    reopened = SQLiteEventStore(store.path, clock=store.clock)
    assert projection_hash(reopened.rebuild_view(run.run_id)) == expected
    assert (
        projection_hash(SQLiteEventStore.replay_jsonl(store.export_jsonl(run.run_id))) == expected
    )


def test_transaction_rolls_back_all_events_and_receipt(store, task):
    run = store.create(task, "create")
    with pytest.raises(InvalidTransition):
        store.command(
            run.run_id,
            "bad",
            {"op": "bad"},
            lambda _: [
                NewEvent(event_type="STATE_CHANGED", payload={"from": "CREATED", "to": "PLANNING"}),
                NewEvent(
                    event_type="STATE_CHANGED", payload={"from": "PLANNING", "to": "SUCCEEDED"}
                ),
            ],
        )
    assert store.get(run.run_id).seq == 1
    with sqlite3.connect(store.path) as db:
        assert (
            db.execute("SELECT count(*) FROM commands WHERE command_key='bad'").fetchone()[0] == 0
        )


def test_append_only_sql_guards(store, task):
    store.create(task, "create")
    with sqlite3.connect(store.path) as db:
        for sql in ("DELETE FROM events", "UPDATE events SET seq=9"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(sql)


def test_trace_tampering_and_future_schema_rejected(store, task):
    run = store.create(task, "create")
    data = json.loads(store.export_jsonl(run.run_id))
    data["payload"]["task"]["title"] = "tampered"
    with pytest.raises(IntegrityError, match="hash"):
        SQLiteEventStore.replay_jsonl(json.dumps(data))
    data["schema_version"] = 2
    with pytest.raises(IntegrityError, match="Malformed"):
        SQLiteEventStore.replay_jsonl(json.dumps(data))


def test_budget_stop_request_evidence_must_match_replayed_run_phase(store, running):
    run, _ = running
    stop = BudgetStop(
        reason_code=BudgetStopReason.RUN_MODEL_COST_LIMIT,
        scope="run",
        currency="CNY",
        required_cost="0.02",
        available_cost="0.01",
    )
    evidence = ModelRequestBudgetEvidence(
        call_id="planner-call",
        purpose="planning",
        request_hash="a" * 64,
        input_token_budget=InputTokenBudget(
            max_input_tokens=4_000,
            estimate=InputTokenEstimate(request_bytes=1_000, token_ceiling=3_024),
        ),
        output_token_ceiling=512,
    )

    with pytest.raises(IntegrityError, match="Run phase"):
        store.command(
            run.run_id,
            "invalid-budget-evidence",
            {"operation": "invalid-budget-evidence"},
            lambda _: [
                NewEvent(
                    event_type="RUN_FAILED",
                    payload={
                        "reason": stop.reason_code.value,
                        "budget_stop": stop.model_dump(mode="json"),
                        "model_request_budget": evidence.model_dump(mode="json"),
                    },
                )
            ],
        )


def test_concurrent_writers_cannot_both_claim_lease(store, service, task):
    run = store.create(task, "create")

    def acquire(index):
        try:
            return service.acquire_lease(run.run_id, f"w{index}", f"lease{index}").worker_id
        except LeaseConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(acquire, range(8)))
    assert len([item for item in results if item is not None]) == 1
    assert store.get(run.run_id).lease_epoch == 1
    assert [event.seq for event in store.events(run.run_id)] == [1, 2]


def test_expiry_does_not_imply_process_stopped_and_old_worker_is_fenced(
    store,
    service,
    running,
    clock,
):
    run, old = running
    clock.advance(61)
    with pytest.raises(LeaseConflict, match="proof"):
        service.acquire_lease(run.run_id, "new", "takeover")
    takeover = service.acquire_lease(run.run_id, "new", "takeover", prior_worker_stopped=True)
    assert takeover.lease_epoch == old.epoch + 1
    with pytest.raises(LeaseConflict):
        service.transition(run.run_id, RunStatus.VALIDATING, old, "old-worker")
    service.transition(
        run.run_id, RunStatus.VALIDATING, LeaseToken.from_run(takeover), "new-worker"
    )


def test_cancel_prevents_subsequent_work(store, service, running):
    run, token = running
    cancelled = service.cancel(run.run_id, "cancel")
    assert cancelled.status == RunStatus.CANCELLED
    assert cancelled.lease_id is None
    with pytest.raises(InvalidTransition):
        service.transition(run.run_id, RunStatus.VALIDATING, token, "after-cancel")
    assert service.cancel(run.run_id, "cancel").seq == cancelled.seq


def test_expected_sequence_prevents_lost_updates(store, task):
    run = store.create(task, "create")
    with pytest.raises(Conflict):
        store.command(run.run_id, "stale", {}, lambda _: [], expected_seq=0)


def test_model_completion_cannot_mark_success(store, service, running):
    run, token = running
    service.transition(run.run_id, RunStatus.VALIDATING, token, "validating")
    with pytest.raises(InvalidTransition, match="Success"):
        service.transition(run.run_id, RunStatus.SUCCEEDED, token, "claim-done")


def test_retrying_old_lease_request_returns_original_receipt_not_new_owner(
    store,
    service,
    running,
    clock,
):
    run, old = running
    clock.advance(61)
    new = service.acquire_lease(run.run_id, "worker-2", "new-lease", prior_worker_stopped=True)
    old_receipt = service.acquire_lease(run.run_id, "worker-1", "lease")
    assert old_receipt.lease_id == old.lease_id
    assert old_receipt.lease_id != new.lease_id
    with pytest.raises(LeaseConflict):
        service.check_worker(store.get(run.run_id), LeaseToken.from_run(old_receipt))


def test_lease_renew_release_and_reacquire(store, service, running, clock):
    run, token = running
    clock.advance(10)
    renewed = service.renew_lease(run.run_id, token, "renew", ttl_seconds=100)
    assert renewed.lease_expires_at > run.lease_expires_at
    service.release_lease(run.run_id, token, "release")
    new = service.acquire_lease(run.run_id, "worker-2", "new")
    assert new.lease_epoch == token.epoch + 1
    with pytest.raises(LeaseConflict):
        service.renew_lease(run.run_id, token, "stale-renew")


def test_read_only_store_cannot_mutate(store, task):
    from horizon.domain.errors import PolicyDenied

    run = store.create(task, "create")
    readonly = SQLiteEventStore(store.path, read_only=True)
    assert readonly.get(run.run_id).run_id == run.run_id
    with pytest.raises(PolicyDenied):
        readonly.create(task, "another")


def test_future_database_version_is_not_silently_downgraded(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(IntegrityError, match="schema version"):
        SQLiteEventStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99


def test_plan_revision_preserves_both_versions(store, service, task, plan):
    run = store.create(task, "create")
    first = service.set_plan(run.run_id, plan, "first-plan")
    second = service.set_plan(run.run_id, plan.model_copy(update={"version": 2}), "second-plan")
    assert second.plan.version == 2
    assert store.get(run.run_id, first.seq).plan.version == 1
    assert store.events(run.run_id)[-1].event_type == "PLAN_REVISED"
