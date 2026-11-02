from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.vcs.git import read_git_head
from horizon.adapters.workspace.promotion import workspace_path_hash
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.pilot import load_pilot_preflight_report
from horizon.application.terminal_evidence import verify_terminal_evidence_pack
from horizon.domain.common import canonical_json, digest
from horizon.domain.errors import IntegrityError
from horizon.domain.events import Event
from horizon.domain.model import ModelCallRecord, ModelResponse
from horizon.domain.pilot import RealModelPilotPreflightReport
from horizon.domain.retrieval import EvidencePack
from horizon.domain.run import Run
from horizon.domain.smoke_evaluation import (
    RealModelSmokeEvaluatorManifest,
    RealModelSmokeRunResult,
    RealModelSmokeSuite,
    SmokeEvaluatorTask,
    SmokeObservedLocation,
    SmokeStopCategory,
    SmokeSuccessEvidence,
    SmokeTargetLocation,
)
from horizon.domain.task import TaskSpec
from horizon.domain.tools import WRITE_TOOLS, ToolCallRecord, ToolCallReservation


@dataclass(frozen=True)
class SmokeRunEvaluationArtifacts:
    output_dir: Path
    report_path: Path
    summary_path: Path
    report_ref: str
    report: RealModelSmokeRunResult


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _target_for_task(
    suite: RealModelSmokeSuite,
    evaluator: RealModelSmokeEvaluatorManifest,
    smoke_task_id: str,
) -> SmokeEvaluatorTask:
    if evaluator.suite_id != suite.suite_id or evaluator.suite_digest != suite.sha256:
        raise IntegrityError("Smoke evaluator does not match the frozen suite")
    suite_tasks = {task.task_id: task for task in suite.tasks}
    evaluator_tasks = {task.smoke_task_id: task for task in evaluator.tasks}
    if set(evaluator_tasks) != set(suite_tasks):
        raise IntegrityError("Smoke evaluator task set does not match the frozen suite")
    try:
        return evaluator_tasks[smoke_task_id]
    except KeyError as exc:
        raise IntegrityError(f"Smoke evaluator does not contain task: {smoke_task_id}") from exc


def _load_prepared_task(
    report: RealModelPilotPreflightReport,
    artifact_store: ArtifactStore,
) -> TaskSpec:
    content = artifact_store.read(report.prepared_task_ref)
    try:
        payload = yaml.safe_load(content)
        task = TaskSpec.model_validate(payload)
    except (ValueError, yaml.YAMLError) as exc:
        raise IntegrityError("Prepared Pilot TaskSpec artifact is invalid") from exc
    if task.sha256 != report.prepared_task_digest:
        raise IntegrityError("Prepared Pilot TaskSpec does not match its preflight report")
    return task


def _bind_run(
    run: Run,
    prepared: TaskSpec,
    preflight: RealModelPilotPreflightReport,
    smoke_task,
) -> None:
    if not preflight.ready:
        raise IntegrityError("A paid smoke Run cannot bind to a non-ready Pilot preflight")
    if not run.task.task_id.startswith(f"{prepared.task_id}-"):
        raise IntegrityError("Terminal Run task ID is not derived from the prepared Pilot task")
    if run.workspace_origin is None:
        raise IntegrityError("Terminal smoke Run has no bound source workspace origin")
    if run.model_policy is None:
        raise IntegrityError("Terminal smoke Run has no bound model policy")
    expected = prepared.model_dump(mode="json")
    expected["task_id"] = run.task.task_id
    expected["repository"] = run.task.repository.model_dump(mode="json")
    if TaskSpec.model_validate(expected) != run.task:
        raise IntegrityError("Terminal Run TaskSpec differs from the prepared Pilot task")
    if (
        run.task.repository.base_commit != preflight.source_snapshot_revision
        or run.workspace_origin.source_revision != preflight.source_snapshot_revision
        or run.workspace_origin.source_manifest_ref != preflight.source_manifest_ref
        or run.workspace_origin.git_head != preflight.source_git_head
    ):
        raise IntegrityError("Terminal Run source binding differs from the Pilot preflight")
    policy = run.model_policy
    if (
        policy.policy_id != preflight.provider_policy_id
        or policy.provider_id != preflight.provider_id
        or policy.model != preflight.model_id
        or policy.currency != preflight.currency
        or policy.max_run_cost != preflight.run_cost_cap
    ):
        raise IntegrityError("Terminal Run model policy differs from the Pilot preflight")
    if run.task.budgets.max_model_calls != preflight.max_model_calls:
        raise IntegrityError("Terminal Run model-call cap differs from the Pilot preflight")
    if tuple(run.task.constraints.allowed_paths) != tuple(smoke_task.allowed_path_patterns) or set(
        run.task.constraints.allowed_tools or ()
    ) != set(smoke_task.allowed_tools):
        raise IntegrityError("Terminal Run authority differs from the frozen smoke task")


def _event_contracts(
    events: tuple[Event, ...],
) -> tuple[
    dict[str, tuple[ToolCallReservation, int]],
    dict[str, tuple[ToolCallRecord, int]],
    dict[str, ModelCallRecord],
]:
    reservations: dict[str, tuple[ToolCallReservation, int]] = {}
    records: dict[str, tuple[ToolCallRecord, int]] = {}
    models: dict[str, ModelCallRecord] = {}
    for event in events:
        if event.event_type == "TOOL_CALL_RESERVED":
            reservation = ToolCallReservation.model_validate(event.payload["reservation"])
            reservations[reservation.call_id] = (reservation, event.seq)
        elif event.event_type == "TOOL_CALL_SETTLED":
            record = ToolCallRecord.model_validate(event.payload["record"])
            records[record.call_id] = (record, event.seq)
        elif event.event_type == "MODEL_CALL_SETTLED":
            record = ModelCallRecord.model_validate(event.payload["record"])
            models[record.call_id] = record
    return reservations, records, models


def _model_tool_arguments(
    events: tuple[Event, ...],
    artifacts: ArtifactStore,
) -> tuple[dict[str, dict], set[str]]:
    """Bind successful durable tool reservations to exact provider arguments when available."""

    tool_events = [
        (event.seq, ToolCallReservation.model_validate(event.payload["reservation"]))
        for event in events
        if event.event_type == "TOOL_CALL_RESERVED"
    ]
    next_model_reservations = [
        event.seq for event in events if event.event_type == "MODEL_CALL_RESERVED"
    ]
    arguments: dict[str, dict] = {}
    unavailable_model_calls: set[str] = set()
    for event in events:
        if event.event_type != "MODEL_CALL_SETTLED":
            continue
        record = ModelCallRecord.model_validate(event.payload["record"])
        if record.response_artifact_ref is None:
            unavailable_model_calls.add(record.call_id)
            continue
        try:
            response = ModelResponse.model_validate_json(
                artifacts.read(record.response_artifact_ref)
            )
        except (IntegrityError, ValueError):
            unavailable_model_calls.add(record.call_id)
            continue
        upper = next((seq for seq in next_model_reservations if seq > event.seq), None)
        candidates = [
            reservation
            for seq, reservation in tool_events
            if seq > event.seq and (upper is None or seq < upper)
        ]
        cursor = 0
        for provider_call in response.message.tool_calls:
            expected_hash = digest(provider_call.function.arguments)
            match = next(
                (
                    index
                    for index in range(cursor, len(candidates))
                    if candidates[index].name == provider_call.function.name
                    and candidates[index].arguments_hash == expected_hash
                ),
                None,
            )
            if match is None:
                continue
            reservation = candidates[match]
            arguments[reservation.call_id] = provider_call.function.arguments
            cursor = match + 1
    return arguments, unavailable_model_calls


def _overlaps(location: SmokeTargetLocation, path: str, start: int, end: int) -> bool:
    return location.path == path and start <= location.end_line and end >= location.start_line


def _location_observations(
    task: SmokeEvaluatorTask,
    events: tuple[Event, ...],
    run: Run,
    artifacts: ArtifactStore,
) -> tuple[tuple[SmokeObservedLocation, ...], bool, int, int]:
    reservations, records, _models = _event_contracts(events)
    arguments, unavailable_models = _model_tool_arguments(events, artifacts)
    observations: list[SmokeObservedLocation] = []
    supporting_artifacts_verified = True
    successful_retrievals = 0
    successful_writes = 0

    def observe(channel: str, call_id: str, path: str, start: int, end: int) -> None:
        for location in task.target_locations:
            if _overlaps(location, path, start, end):
                observations.append(
                    SmokeObservedLocation(
                        location_id=location.location_id,
                        channel=channel,
                        source_call_id=call_id,
                        path=path,
                        start_line=start,
                        end_line=end,
                    )
                )

    snapshots = SnapshotManager(artifacts)
    for call_id, (record, _seq) in sorted(records.items(), key=lambda item: item[1][1]):
        if record.status != "success":
            continue
        if record.name == "retrieve_code":
            successful_retrievals += 1
            if call_id not in arguments:
                supporting_artifacts_verified = False
                continue
            if record.artifact_ref is None or record.artifact_ref != record.output_hash:
                supporting_artifacts_verified = False
                continue
            try:
                evidence = EvidencePack.model_validate_json(artifacts.read(record.artifact_ref))
            except (IntegrityError, ValueError):
                supporting_artifacts_verified = False
                continue
            if not (
                evidence.workspace_revision
                == record.workspace_revision_before
                == record.workspace_revision_after
            ):
                supporting_artifacts_verified = False
                continue
            for chunk in evidence.chunks:
                observe(
                    "retrieval_chunk",
                    call_id,
                    chunk.path,
                    chunk.start_line,
                    chunk.end_line,
                )
            continue

        if record.name == "read_file":
            call_arguments = arguments.get(call_id)
            if call_arguments is None:
                supporting_artifacts_verified = False
                continue
            path = call_arguments.get("path")
            if not isinstance(path, str):
                supporting_artifacts_verified = False
                continue
            start = call_arguments.get("start_line")
            end = call_arguments.get("end_line")
            if start is None and end is None:
                manifest_ref = record.workspace_manifest_ref
                try:
                    snapshot = snapshots.verify(manifest_ref) if manifest_ref else None
                    entry = next(item for item in snapshot.files if item.path == path)
                    line_count = max(1, len(artifacts.read(entry.sha256).splitlines()))
                except (IntegrityError, StopIteration):
                    supporting_artifacts_verified = False
                    continue
                start, end = 1, line_count
            if not isinstance(start, int) or not isinstance(end, int):
                supporting_artifacts_verified = False
                continue
            observe("read_range", call_id, path, start, end)
            continue

        if record.name not in WRITE_TOOLS:
            continue
        successful_writes += 1
        if call_id not in arguments:
            supporting_artifacts_verified = False
            continue
        reservation_entry = reservations.get(call_id)
        reservation = reservation_entry[0] if reservation_entry is not None else None
        precondition = reservation.write_precondition if reservation is not None else None
        if (
            precondition is None
            or record.workspace_manifest_ref is None
            or record.workspace_revision_before == record.workspace_revision_after
        ):
            supporting_artifacts_verified = False
            continue
        try:
            before = snapshots.verify(precondition.observed_workspace_manifest_ref)
            after = snapshots.verify(record.workspace_manifest_ref)
            before_entries = {entry.path: entry for entry in before.files}
            after_entries = {entry.path: entry for entry in after.files}
            for path in precondition.target_paths:
                before_entry = before_entries.get(path)
                after_entry = after_entries.get(path)
                if before_entry is None or after_entry is None:
                    raise IntegrityError("Smoke write target is absent from a workspace snapshot")
                before_lines = artifacts.read(before_entry.sha256).decode("utf-8").splitlines()
                after_lines = artifacts.read(after_entry.sha256).decode("utf-8").splitlines()
                matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
                for (
                    tag,
                    before_start,
                    before_end,
                    _after_start,
                    _after_end,
                ) in matcher.get_opcodes():
                    if tag == "equal":
                        continue
                    start = before_start + 1
                    end = max(start, before_end)
                    observe("write_preimage", call_id, path, start, end)
        except (IntegrityError, StopIteration, UnicodeDecodeError):
            supporting_artifacts_verified = False

    successful_reads = {
        call_id
        for call_id, (record, _seq) in records.items()
        if record.name == "read_file" and record.status == "success"
    }
    if successful_reads - set(arguments):
        supporting_artifacts_verified = False
    if unavailable_models and successful_reads:
        supporting_artifacts_verified = False
    if run.unknown_tool_calls:
        supporting_artifacts_verified = False

    unique = {
        (
            item.location_id,
            item.channel,
            item.source_call_id,
            item.path,
            item.start_line,
            item.end_line,
        ): item
        for item in observations
    }
    ordered = tuple(unique[key] for key in sorted(unique))
    return ordered, supporting_artifacts_verified, successful_retrievals, successful_writes


def _classify_stop(
    run: Run,
    *,
    task_success: bool,
    correct_location_reached: bool | None,
    protected_validation_passed: bool | None,
) -> SmokeStopCategory:
    if task_success:
        return "completed"
    if run.budget_stop is not None:
        return "budget_gate"
    if run.unknown_reservations or run.unknown_model_calls or run.unknown_tool_calls:
        return "unknown_effect"
    if (
        run.pending_human_request is not None
        and run.pending_human_request.kind == "context_capacity_required"
    ):
        return "context_capacity"
    if protected_validation_passed is False:
        return "validation_failure"
    if run.failure_reason in {"worker_timeout", "reconciliation_required"}:
        return "harness_recovery"
    if run.plan is None or not any(call.purpose == "execution" for call in run.model_calls):
        return "model_planning"
    if correct_location_reached is False and any(
        call.name in {"retrieve_code", "read_file"} for call in run.tool_calls
    ):
        return "retrieval"
    if any(call.status == "error" for call in run.tool_calls):
        return "tool_protocol"
    return "unknown"


def _summary(report: RealModelSmokeRunResult) -> str:
    criteria = report.success_evidence
    matched = (
        "\n".join(
            f"- `{item.location_id}` via `{item.channel}` at "
            f"`{item.path}:{item.start_line}-{item.end_line}`"
            for item in report.matched_locations
        )
        or "- none"
    )
    return (
        "# Real-model Smoke Run Result\n\n"
        f"- Suite task: `{report.smoke_task_id}`\n"
        f"- Pilot: `{report.pilot_id}`\n"
        f"- Run: `{report.run_id}`\n"
        f"- Terminal status: `{report.terminal_status.value}`\n"
        f"- Task success: `{str(report.task_success).lower()}`\n"
        f"- Stop category: `{report.stop_category}`\n"
        f"- Correct location reached: `{report.correct_location_reached}`\n"
        f"- Valid edit produced: `{report.valid_edit_produced}`\n"
        f"- Protected validation passed: `{report.protected_validation_passed}`\n"
        f"- Settled model calls: `{report.model_calls}`\n"
        f"- Settled CNY price-card cost: `{report.actual_cost_cny}`\n\n"
        "## Six success gates\n\n"
        f"- Initial failure observed: `{criteria.initial_failure_observed}`\n"
        f"- Workspace changed: `{criteria.workspace_changed}`\n"
        f"- Required checks passed: `{criteria.required_checks_passed}`\n"
        f"- Source unchanged: `{criteria.source_unchanged}`\n"
        f"- Trace replay verified: `{criteria.trace_replay_verified}`\n"
        f"- No open effects: `{criteria.no_open_effects}`\n\n"
        "## Matched evaluator-only locations\n\n"
        f"{matched}\n\n"
        "## Claim boundaries\n\n" + "\n".join(f"- {item}" for item in report.limitations) + "\n"
    )


def evaluate_real_model_smoke_run(
    suite: RealModelSmokeSuite,
    evaluator: RealModelSmokeEvaluatorManifest,
    smoke_task_id: str,
    *,
    pilot_preflight_path: Path,
    terminal_evidence_pack_path: Path,
    run_artifact_root: Path,
    output_dir: Path,
) -> SmokeRunEvaluationArtifacts:
    """Derive one smoke result without executing a model, provider request, or repository check."""

    evaluation_task = _target_for_task(suite, evaluator, smoke_task_id)
    smoke_task = next(task for task in suite.tasks if task.task_id == smoke_task_id)
    preflight, preflight_ref = load_pilot_preflight_report(pilot_preflight_path)
    if preflight.pilot_id != evaluation_task.pilot_id:
        raise IntegrityError("Pilot preflight does not match the evaluator task")
    preflight_artifacts = ArtifactStore(pilot_preflight_path.resolve(strict=True).parents[1])
    prepared = _load_prepared_task(preflight, preflight_artifacts)

    pack_path = terminal_evidence_pack_path.resolve(strict=True)
    pack = verify_terminal_evidence_pack(pack_path)
    trace_record = next(item for item in pack.files if item.role == "trace")
    trace_path = (pack_path.parent / trace_record.path).resolve(strict=True)
    if not trace_path.is_relative_to(pack_path.parent):
        raise IntegrityError("Terminal smoke Trace escapes its EvidencePack")
    trace_text = trace_path.read_text(encoding="utf-8")
    events = tuple(Event.model_validate_json(line) for line in trace_text.splitlines() if line)
    run = SQLiteEventStore.replay_jsonl(trace_text)
    _bind_run(run, prepared, preflight, smoke_task)

    destination = output_dir.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    source_artifacts = ArtifactStore(destination / "source-artifacts")
    source = Path(prepared.repository.path).resolve(strict=True)
    if workspace_path_hash(source) != run.workspace_origin.source_path_hash:
        raise IntegrityError("Current source path does not match the Run workspace origin")
    source_after, source_manifest_after = SnapshotManager(source_artifacts).capture(
        source,
        denied_paths=prepared.constraints.denied_paths,
    )
    source_unchanged = bool(
        source_after.workspace_revision == preflight.source_snapshot_revision
        and source_manifest_after == preflight.source_manifest_ref
        and read_git_head(source) == preflight.source_git_head
    )

    run_artifacts = ArtifactStore(run_artifact_root)
    matched, supporting_verified, retrieval_count, write_count = _location_observations(
        evaluation_task,
        events,
        run,
        run_artifacts,
    )
    correct_location = True if matched else False if supporting_verified else None
    changed_writes = [
        call
        for call in run.tool_calls
        if call.name in WRITE_TOOLS
        and call.status == "success"
        and call.workspace_revision_before is not None
        and call.workspace_revision_after is not None
        and call.workspace_revision_before != call.workspace_revision_after
    ]
    valid_edit = True if changed_writes else None if run.unknown_tool_calls else False
    reliability = pack.evidence.reliability_summary
    protected_validation = (
        None
        if reliability is None or reliability.unknown_acceptance_evidence
        else reliability.required_checks_passed
    )
    workspace_changed = bool(
        run.workspace_revision is not None
        and run.workspace_revision != run.workspace_origin.source_revision
    )
    no_open_effects = not (
        pack.evidence.unknown_effects_present or pack.evidence.open_effects_present
    )
    success_evidence = SmokeSuccessEvidence(
        initial_failure_observed=preflight.initial_failure_matches,
        workspace_changed=workspace_changed,
        required_checks_passed=protected_validation,
        source_unchanged=source_unchanged,
        no_open_effects=no_open_effects,
    )
    task_success = run.status.value == "SUCCEEDED" and success_evidence.task_success
    cost = sum((call.estimated_cost for call in run.model_calls), Decimal("0"))
    if any(call.currency != "CNY" for call in run.model_calls):
        raise IntegrityError("Smoke result requires all settled model calls to use CNY")
    stop_category = _classify_stop(
        run,
        task_success=task_success,
        correct_location_reached=correct_location,
        protected_validation_passed=protected_validation,
    )
    limitations = [
        "Evaluator target locations are controller-only, author-known development evidence; "
        "this is not a blind or holdout score.",
        "actual_cost_cny sums settled Trace price-card estimates and is not a provider invoice.",
        "source_unchanged is recaptured at post-Run evaluation time; it does not prove that no "
        "transient source mutation occurred and was later reverted.",
        "A verified terminal status alone does not establish semantic correctness; all six "
        "success gates remain separate.",
    ]
    if not supporting_verified:
        limitations.append(
            "One or more location-supporting artifacts or effects were unavailable; an absent "
            "match is therefore reported as unknown rather than false."
        )
    if run.unknown_model_calls:
        limitations.append(
            "The settled cost and model-call count exclude model calls whose external outcome "
            "remains unknown."
        )
    report = RealModelSmokeRunResult(
        evaluator_id=evaluator.evaluator_id,
        evaluator_digest=evaluator.sha256,
        suite_id=suite.suite_id,
        suite_digest=suite.sha256,
        smoke_task_id=smoke_task_id,
        pilot_id=evaluation_task.pilot_id,
        pilot_preflight_ref=preflight_ref,
        run_id=run.run_id,
        task_spec_hash=run.task.sha256,
        terminal_status=run.status,
        terminal_evidence_pack_sha256=_sha256(pack_path),
        trace_sha256=trace_record.sha256,
        provider_id=preflight.provider_id,
        model_id=preflight.model_id,
        success_evidence=success_evidence,
        task_success=task_success,
        correct_location_reached=correct_location,
        valid_edit_produced=valid_edit,
        protected_validation_passed=protected_validation,
        actual_cost_cny=cost,
        model_calls=len(run.model_calls),
        unknown_model_calls=len(run.unknown_model_calls),
        successful_retrieval_calls=retrieval_count,
        successful_write_calls=write_count,
        stop_category=stop_category,
        target_location_count=len(evaluation_task.target_locations),
        matched_locations=matched,
        supporting_artifacts_verified=supporting_verified,
        source_snapshot_revision_after=source_after.workspace_revision,
        source_manifest_ref_after=source_manifest_after,
        limitations=tuple(limitations),
    )
    report_content = (canonical_json(report) + "\n").encode("utf-8")
    summary_content = _summary(report).encode("utf-8")
    report_path = destination / "smoke-result.json"
    summary_path = destination / "SUMMARY.md"
    with report_path.open("xb") as stream:
        stream.write(report_content)
    with summary_path.open("xb") as stream:
        stream.write(summary_content)
    return SmokeRunEvaluationArtifacts(
        output_dir=destination,
        report_path=report_path,
        summary_path=summary_path,
        report_ref=hashlib.sha256(report_content).hexdigest(),
        report=report,
    )
