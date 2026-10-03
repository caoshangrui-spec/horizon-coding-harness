from pathlib import Path

import pytest

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.application.memory import RunMemoryProjector
from horizon.domain.common import digest
from horizon.domain.errors import IntegrityError
from horizon.domain.tools import ToolCallRecord, ToolCallReservation

REVISION_A = "a" * 64
REVISION_B = "b" * 64


def settle_tool(
    service,
    run_id,
    token,
    artifacts: ArtifactStore,
    *,
    call_id: str,
    name: str,
    status: str,
    content: str,
    revision: str = REVISION_A,
    output_hash: str | None = None,
):
    arguments_hash = digest({"case": call_id})
    service.reserve_tool_call(
        run_id,
        ToolCallReservation(
            call_id=call_id,
            name=name,
            arguments_hash=arguments_hash,
            workspace_revision=revision,
        ),
        token,
        f"reserve-{call_id}",
    )
    artifact_ref = artifacts.put(content.encode("utf-8"))
    return service.settle_tool_call(
        run_id,
        ToolCallRecord(
            call_id=call_id,
            name=name,
            arguments_hash=arguments_hash,
            status=status,
            output_hash=output_hash or artifact_ref,
            workspace_revision_before=revision,
            workspace_revision_after=revision,
            artifact_ref=artifact_ref,
        ),
        token,
        f"settle-{call_id}",
    )


def test_run_memory_preserves_observed_success_failure_and_staleness(
    tmp_path: Path,
    running,
    service,
):
    run, token = running
    artifacts = ArtifactStore(tmp_path / "memory-artifacts")
    run = settle_tool(
        service,
        run.run_id,
        token,
        artifacts,
        call_id="read-1",
        name="read_file",
        status="success",
        content="def parse(value): return value",
    )
    run = settle_tool(
        service,
        run.run_id,
        token,
        artifacts,
        call_id="check-1",
        name="run_check",
        status="error",
        content="check_id=unit\npassed=false\n1 failed",
    )
    projector = RunMemoryProjector(artifacts, max_entries=8, excerpt_chars=80)

    active = projector.project(
        run,
        service.store.events(run.run_id),
        current_revision=REVISION_A,
        active_work_item_id=run.plan.items[0].work_item_id,
    )
    stale = projector.project(
        run,
        service.store.events(run.run_id),
        current_revision=REVISION_B,
        active_work_item_id=run.plan.items[0].work_item_id,
    )

    assert active.total_entry_count == active.included_entry_count == 2
    assert active.active_count == 2
    assert active.stale_count == 0
    assert [entry.outcome for entry in active.entries] == ["success", "error"]
    assert active.entries[1].kind == "validation"
    assert "1 failed" in active.entries[1].excerpt
    assert stale.active_count == 0
    assert stale.stale_count == 2
    assert stale.sha256 != active.sha256
    assert all(entry.confidence == "observed" for entry in stale.entries)


def test_run_memory_excludes_model_submit_claims_and_bounds_projection(
    tmp_path: Path,
    running,
    service,
):
    run, token = running
    artifacts = ArtifactStore(tmp_path / "memory-artifacts")
    run = settle_tool(
        service,
        run.run_id,
        token,
        artifacts,
        call_id="submit-claim",
        name="submit",
        status="success",
        content="Everything is fixed because the model says so.",
    )
    for index in range(4):
        run = settle_tool(
            service,
            run.run_id,
            token,
            artifacts,
            call_id=f"read-{index}",
            name="read_file",
            status="success",
            content=f"observed-{index}",
        )

    snapshot = RunMemoryProjector(
        artifacts,
        max_entries=2,
        excerpt_chars=20,
    ).project(
        run,
        service.store.events(run.run_id),
        current_revision=REVISION_A,
        active_work_item_id=run.plan.items[0].work_item_id,
    )

    assert snapshot.total_entry_count == 4
    assert snapshot.included_entry_count == 2
    assert snapshot.omitted_entry_count == 2
    assert [entry.excerpt for entry in snapshot.entries] == ["observed-2", "observed-3"]
    assert all("Everything is fixed" not in entry.excerpt for entry in snapshot.entries)


def test_run_memory_keeps_unknown_effect_unresolved_without_inventing_evidence(
    tmp_path: Path,
    running,
    service,
):
    run, token = running
    artifacts = ArtifactStore(tmp_path / "memory-artifacts")
    reservation = ToolCallReservation(
        call_id="unknown-read",
        name="read_file",
        arguments_hash=digest({"path": "src/parser.py"}),
        workspace_revision=REVISION_A,
    )
    service.reserve_tool_call(run.run_id, reservation, token, "reserve-unknown")
    run = service.mark_tool_call_unknown(
        run.run_id,
        reservation.call_id,
        token,
        "mark-unknown",
    )

    snapshot = RunMemoryProjector(artifacts).project(
        run,
        service.store.events(run.run_id),
        current_revision=REVISION_A,
        active_work_item_id=run.plan.items[0].work_item_id,
    )

    assert snapshot.unresolved_count == 1
    entry = snapshot.entries[0]
    assert entry.outcome == "unknown"
    assert entry.status == "unresolved"
    assert entry.evidence_ref is None
    assert entry.excerpt == ""
    assert "no success or failure fact" in entry.statement


def test_run_memory_rejects_tool_output_without_matching_artifact_hash(
    tmp_path: Path,
    running,
    service,
):
    run, token = running
    artifacts = ArtifactStore(tmp_path / "memory-artifacts")
    run = settle_tool(
        service,
        run.run_id,
        token,
        artifacts,
        call_id="bad-evidence",
        name="read_file",
        status="success",
        content="real evidence",
        output_hash="f" * 64,
    )

    with pytest.raises(IntegrityError, match="content-addressed"):
        RunMemoryProjector(artifacts).project(
            run,
            service.store.events(run.run_id),
            current_revision=REVISION_A,
            active_work_item_id=run.plan.items[0].work_item_id,
        )
