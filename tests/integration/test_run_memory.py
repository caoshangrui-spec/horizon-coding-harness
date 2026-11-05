import json
from copy import deepcopy
from pathlib import Path

import pytest

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.application.memory import TASK_RELEVANT_CODE_PREFIX, RunMemoryProjector
from horizon.application.retrieval_control import (
    EVIDENCE_TRANSITION_POLICY,
    RETRIEVAL_QUERY_POLICY,
)
from horizon.application.services import LeaseToken
from horizon.domain.common import canonical_json, digest
from horizon.domain.errors import IntegrityError
from horizon.domain.plan import Plan, WorkItem
from horizon.domain.retrieval import EvidenceChunk, EvidencePack
from horizon.domain.states import RunStatus
from horizon.domain.task import TaskSpec
from horizon.domain.tools import TOOL_ARGUMENT_POLICY, ToolCallRecord, ToolCallReservation

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


def test_run_memory_pins_cookiecutter_target_from_a_long_full_file_read(
    tmp_path: Path,
    task_dict,
    store,
    service,
):
    data = deepcopy(task_dict)
    data["title"] = "Prevent duplicate Cookiecutter choice rendering"
    data["objective"] = "Keep the numbered prompt without Click appending its choice list"
    data["acceptance"][0]["id"] = "cookiecutter-read-user-choice-first-closure-v1"
    task = TaskSpec.model_validate(data)
    plan = Plan(
        items=(
            WorkItem(
                work_item_id="fix-choice",
                title="Fix read_user_choice",
                objective="Preserve the numbered prompt and selected return value",
                expected_artifacts=(
                    "Evidence from cookiecutter/prompt.py showing the numbered prompt "
                    "rendering and the Click choice-list rendering path",
                    "A source change in cookiecutter/prompt.py that preserves the numbered "
                    "prompt text, default selection, and selected return value while preventing "
                    "Click from appending its own choice list",
                ),
                acceptance_ids=("cookiecutter-read-user-choice-first-closure-v1",),
            ),
        )
    )
    run = store.create(task, "create-cookiecutter-memory")
    service.set_plan(run.run_id, plan, "plan-cookiecutter-memory")
    leased = service.acquire_lease(run.run_id, "worker-cookiecutter-memory", "lease-memory")
    token = LeaseToken.from_run(leased)
    run = service.transition(run.run_id, RunStatus.RUNNING, token, "start-memory")
    artifacts = ArtifactStore(tmp_path / "cookiecutter-memory-artifacts")
    content = "\n".join(
        [
            "READ_RANGE path=cookiecutter/prompt.py lines=1-100 total_lines=242",
            *(f"# unrelated header line {index}" for index in range(55)),
            "def read_user_choice(var_name, options):",
            "    prompt = build_numbered_prompt(options)",
            "    user_choice = click.prompt(prompt, type=click.Choice(options))",
            "    return options[user_choice]",
            *(f"# unrelated tail line {index}" for index in range(55)),
            "    return read_user_choice(key, rendered_options)",
        ]
    )
    run = settle_tool(
        service,
        run.run_id,
        token,
        artifacts,
        call_id="read-cookiecutter-prompt",
        name="read_file",
        status="success",
        content=content,
    )

    snapshot = RunMemoryProjector(artifacts, excerpt_chars=240).project(
        run,
        service.store.events(run.run_id),
        current_revision=REVISION_A,
        active_work_item_id="fix-choice",
    )

    excerpt = snapshot.entries[0].excerpt
    assert excerpt.startswith(TASK_RELEVANT_CODE_PREFIX)
    assert "def read_user_choice" in excerpt
    assert "user_choice = click.prompt" in excerpt
    assert "READ_RANGE path=" not in excerpt
    assert "return read_user_choice(key" not in excerpt
    assert "unrelated header line 0" not in excerpt
    assert len(excerpt) <= 240


def test_run_memory_preserves_two_ranked_retrieval_chunks_for_replan_context(
    tmp_path: Path,
    running,
    service,
):
    run, token = running
    artifacts = ArtifactStore(tmp_path / "memory-artifacts")
    evidence = EvidencePack(
        query="parser empty value",
        normalized_terms=("parser", "empty", "value"),
        workspace_revision=REVISION_A,
        source_manifest_ref="b" * 64,
        scope_hash="c" * 64,
        index_key="d" * 64,
        backend="sqlite_fts5",
        status="ok",
        indexed_file_count=3,
        skipped_file_count=0,
        chunks=(
            EvidenceChunk(
                rank=1,
                path="src/request.py",
                start_line=1,
                end_line=2,
                content_hash="e" * 64,
                snippet="def request(value):\n    return value",
            ),
            EvidenceChunk(
                rank=2,
                path="src/parser.py",
                start_line=10,
                end_line=12,
                content_hash="f" * 64,
                snippet="def parse(value):\n    return value or '[]'",
            ),
            EvidenceChunk(
                rank=3,
                path="tests/test_parser.py",
                start_line=1,
                end_line=2,
                content_hash="1" * 64,
                snippet="def test_parse():\n    assert parse('') == '[]'",
            ),
        ),
    )
    run = settle_tool(
        service,
        run.run_id,
        token,
        artifacts,
        call_id="retrieve-1",
        name="retrieve_code",
        status="success",
        content=canonical_json(evidence),
    )
    run = settle_tool(
        service,
        run.run_id,
        token,
        artifacts,
        call_id="retrieve-reuse-1",
        name="retrieve_code",
        status="error",
        content=(f"{RETRIEVAL_QUERY_POLICY}: reuse src/request.py and inspect a bounded neighbor"),
    )
    run = settle_tool(
        service,
        run.run_id,
        token,
        artifacts,
        call_id="evidence-transition-1",
        name="retrieve_code",
        status="error",
        content=(
            f"{EVIDENCE_TRANSITION_POLICY}: reuse src/request.py and choose an allowed write tool"
        ),
    )

    snapshot = RunMemoryProjector(artifacts).project(
        run,
        service.store.events(run.run_id),
        current_revision=REVISION_A,
        active_work_item_id=run.plan.items[0].work_item_id,
    )

    assert len(snapshot.entries) == 1
    assert snapshot.total_entry_count == 1
    visible = json.loads(snapshot.entries[0].excerpt)
    assert [chunk["path"] for chunk in visible["chunks"]] == [
        "src/request.py",
        "src/parser.py",
    ]
    assert all(chunk["snippet"] for chunk in visible["chunks"])
    assert "tests/test_parser.py" not in snapshot.entries[0].excerpt
    assert len(snapshot.entries[0].excerpt) <= snapshot.excerpt_chars == 400


def test_run_memory_does_not_duplicate_tool_argument_policy(
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
        call_id="invalid-read-1",
        name="read_file",
        status="error",
        content=(
            f"{TOOL_ARGUMENT_POLICY}: tool=read_file; "
            '{"issues":[{"field":"$.start_line","type":"int_parsing"}]}'
        ),
    )

    snapshot = RunMemoryProjector(artifacts).project(
        run,
        service.store.events(run.run_id),
        current_revision=REVISION_A,
        active_work_item_id=run.plan.items[0].work_item_id,
    )

    assert snapshot.total_entry_count == 0
    assert snapshot.entries == ()


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
