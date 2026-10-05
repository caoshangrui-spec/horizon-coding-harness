from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from horizon.adapters.model.scripted import ScriptedModelGateway
from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.retrieval.sqlite_fts import SQLiteCodeRetriever
from horizon.adapters.workspace.promotion import workspace_path_hash
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.agent_loop import AgentLoopConfig, CodingAgentRunner
from horizon.application.recovery import RecoveryService
from horizon.application.services import HarnessService, LeaseToken
from horizon.application.tool_recovery import ToolRecoveryService
from horizon.domain.common import canonical_json, digest
from horizon.domain.context import ContextProjection
from horizon.domain.events import Event
from horizon.domain.model import CampaignBudget, ModelCallReservation, ModelResponse, PriceCard
from horizon.domain.plan import Plan, WorkItem
from horizon.domain.portfolio_demo import (
    PORTFOLIO_CRASH_EXIT_CODE,
    PortfolioCrashRecoveryEvidence,
    PortfolioDemoReport,
    PortfolioDemoVerification,
    PortfolioEvidenceFile,
    PortfolioEvidenceLineage,
    PortfolioEvidencePack,
)
from horizon.domain.promotion import WorkspaceOrigin
from horizon.domain.retrieval import EvidencePack
from horizon.domain.run import projection_hash
from horizon.domain.run_evaluation import ScriptedModelAction
from horizon.domain.states import RunStatus
from horizon.domain.task import (
    AcceptanceCheck,
    BudgetSpec,
    Constraints,
    Repository,
    TaskSpec,
)
from horizon.domain.tools import AcceptanceResult
from horizon.tools.gateway import WorkspaceToolGateway

_FIXTURE_CONTENT = "def parse(value):\n    return [value]\n"
_FIXED_CONTENT = "def parse(value):\n    return [] if value == '' else [value]\n"
_STRUCTURED_RANGE_ERROR = "start_line and end_line must be supplied together"
_WRITE_PATH = "src/parser.py"
_WRITE_PREIMAGE = "return [value]"
_OFFLINE_PRICE = PriceCard(
    currency="CNY",
    input_per_million="3.00",
    cached_input_per_million="0.30",
    output_per_million="9.00",
    version="portfolio-demo-simulated-v1",
    source_url="https://offline.invalid/horizon-portfolio-demo",
)


class _ParserAcceptanceExecutor:
    """Deterministic demo check; it inspects text and never executes repository code."""

    def execute(self, workspace: Path, check: AcceptanceCheck) -> AcceptanceResult:
        if check.id != "empty-input":
            raise ValueError(f"Unknown portfolio demo check: {check.id}")
        content = (workspace / "src" / "parser.py").read_text(encoding="utf-8")
        passed = content == _FIXED_CONTENT
        output = "empty input returns []" if passed else "empty input still returns ['']"
        return AcceptanceResult(
            check_id=check.id,
            passed=passed,
            exit_code=0 if passed else 1,
            timed_out=False,
            output=output,
            output_hash=_sha256(output.encode("utf-8")),
        )


@dataclass(frozen=True)
class PortfolioDemoResult:
    output_dir: Path
    evidence_pack_path: Path
    report_path: Path
    trace_path: Path
    final_run_path: Path
    summary_path: Path
    workspace: Path
    report: PortfolioDemoReport
    evidence_pack: PortfolioEvidencePack


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)


def _file_record(root: Path, role: str, path: Path) -> PortfolioEvidenceFile:
    content = path.read_bytes()
    return PortfolioEvidenceFile(
        role=role,
        path=path.relative_to(root).as_posix(),
        sha256=_sha256(content),
        bytes=len(content),
    )


def _read_crash_marker(path: Path) -> dict[str, object]:
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Portfolio hard-crash marker is missing or invalid") from exc
    if (
        not isinstance(marker, dict)
        or set(marker) != {"attempt_id", "exit_code", "pid", "tool"}
        or not isinstance(marker.get("attempt_id"), str)
        or marker.get("exit_code") != PORTFOLIO_CRASH_EXIT_CODE
        or not isinstance(marker.get("pid"), int)
        or marker["pid"] <= 0
        or marker.get("tool") != "replace_text"
    ):
        raise ValueError("Portfolio hard-crash marker does not match its strict contract")
    return marker


def _model_tool_for_record(run, artifacts: ArtifactStore, record):
    matches = []
    for model_call in run.model_calls:
        if model_call.response_artifact_ref is None:
            continue
        response = ModelResponse.model_validate_json(
            artifacts.read(model_call.response_artifact_ref)
        )
        for tool_call in response.message.tool_calls:
            if (
                tool_call.function.name == record.name
                and digest(tool_call.function.arguments) == record.arguments_hash
            ):
                matches.append((model_call, tool_call))
    if len(matches) != 1:
        raise ValueError(
            f"Portfolio demo expected one model response for {record.name}, found {len(matches)}"
        )
    return matches[0]


def _evidence_write_lineage(
    run,
    events: tuple[Event, ...],
    artifacts: ArtifactStore,
) -> PortfolioEvidenceLineage:
    retrieval_records = [
        record
        for record in run.tool_calls
        if record.name == "retrieve_code" and record.status == "success"
    ]
    write_records = [
        record
        for record in run.tool_calls
        if record.name == "replace_text" and record.status == "success"
    ]
    if len(retrieval_records) != 1 or len(write_records) != 1:
        raise ValueError("Portfolio demo requires one successful retrieval and exact write")
    retrieval_record = retrieval_records[0]
    write_record = write_records[0]
    if retrieval_record.artifact_ref is None:
        raise ValueError("Portfolio retrieval record has no evidence artifact")

    evidence_payload = artifacts.read(retrieval_record.artifact_ref)
    evidence = EvidencePack.model_validate_json(evidence_payload)
    _, retrieval_tool_call = _model_tool_for_record(
        run,
        artifacts,
        retrieval_record,
    )
    write_model_call, write_tool_call = _model_tool_for_record(run, artifacts, write_record)
    write_arguments = write_tool_call.function.arguments
    write_path = write_arguments.get("path")
    write_preimage = write_arguments.get("old")
    if not isinstance(write_path, str) or not isinstance(write_preimage, str):
        raise ValueError("Portfolio exact write response is missing path or old text")

    reservations = [
        ModelCallReservation.model_validate(event.payload["reservation"])
        for event in events
        if event.event_type == "MODEL_CALL_RESERVED"
    ]
    write_reservations = [
        reservation
        for reservation in reservations
        if reservation.call_id == write_model_call.call_id
    ]
    if len(write_reservations) != 1:
        raise ValueError("Portfolio write model call has no unique context reservation")
    write_reservation = write_reservations[0]
    if write_reservation.context_projection_ref is None:
        raise ValueError("Portfolio write model call has no context projection")
    projection = ContextProjection.model_validate_json(
        artifacts.read(write_reservation.context_projection_ref)
    )
    evidence_text = evidence_payload.decode("utf-8")
    context_contains_retrieval = any(
        message.role == "tool"
        and message.tool_call_id == retrieval_tool_call.id
        and message.content == evidence_text
        for message in projection.messages
    )

    path_chunks = [chunk for chunk in evidence.chunks if chunk.path == write_path]
    preimage_chunks = [chunk for chunk in path_chunks if write_preimage in chunk.snippet]
    matched_chunk = (
        preimage_chunks[0] if preimage_chunks else (path_chunks[0] if path_chunks else None)
    )
    revision_match = (
        retrieval_record.workspace_revision_before
        == retrieval_record.workspace_revision_after
        == evidence.workspace_revision
        == write_record.workspace_revision_before
    )
    checks = (
        context_contains_retrieval,
        bool(path_chunks),
        bool(preimage_chunks),
        revision_match,
    )
    return PortfolioEvidenceLineage(
        retrieval_call_id=retrieval_record.call_id,
        retrieval_artifact_ref=retrieval_record.artifact_ref,
        evidence_index_key=evidence.index_key,
        evidence_workspace_revision=evidence.workspace_revision,
        write_model_call_id=write_model_call.call_id,
        write_context_projection_ref=write_reservation.context_projection_ref,
        write_call_id=write_record.call_id,
        write_path=write_path,
        write_preimage_sha256=_sha256(write_preimage.encode("utf-8")),
        matched_chunk_content_hash=(
            matched_chunk.content_hash if matched_chunk is not None else None
        ),
        model_context_contains_retrieval=context_contains_retrieval,
        target_path_in_evidence=bool(path_chunks),
        preimage_in_evidence=bool(preimage_chunks),
        revision_match=revision_match,
        verified=all(checks),
    )


def verify_portfolio_evidence_pack(path: Path) -> PortfolioEvidencePack:
    """Verify exported files and replay the Trace without trusting the summary JSON."""

    path = path.resolve(strict=True)
    root = path.parent
    pack = PortfolioEvidencePack.model_validate_json(path.read_text(encoding="utf-8"))
    by_role = {item.role: item for item in pack.files}
    contents: dict[str, bytes] = {}
    for item in pack.files:
        candidate = (root / item.path).resolve(strict=True)
        if not candidate.is_relative_to(root):
            raise ValueError("Portfolio evidence file escapes its pack directory")
        content = candidate.read_bytes()
        if len(content) != item.bytes or _sha256(content) != item.sha256:
            raise ValueError(f"Portfolio evidence file failed integrity verification: {item.path}")
        contents[item.role] = content

    report = PortfolioDemoReport.model_validate_json(contents["report"])
    if (
        report.demo_id != pack.demo_id
        or report.run_id != pack.run_id
        or report.verification.all_checks_passed != pack.all_checks_passed
        or report.excluded_claims != pack.excluded_claims
    ):
        raise ValueError("Portfolio EvidencePack metadata does not match its report")
    if report.trace_sha256 != by_role["trace"].sha256:
        raise ValueError("Portfolio report Trace hash does not match its EvidencePack")
    if report.final_run_sha256 != by_role["final_state"].sha256:
        raise ValueError("Portfolio report final-state hash does not match its EvidencePack")

    trace_text = contents["trace"].decode("utf-8")
    replayed = SQLiteEventStore.replay_jsonl(trace_text)
    if projection_hash(replayed) != report.projection_hash:
        raise ValueError("Portfolio Trace replay projection does not match its report")
    events = tuple(Event.model_validate_json(line) for line in trace_text.splitlines() if line)
    if report.schema_version >= 2:
        lineage = _evidence_write_lineage(
            replayed,
            events,
            ArtifactStore(root / "artifacts"),
        )
        if lineage != report.evidence_lineage:
            raise ValueError("Portfolio evidence-write lineage does not match its Trace")
    if report.schema_version == 3:
        crash_recovery = report.crash_recovery
        if crash_recovery is None:
            raise ValueError("Portfolio schema v3 report has no hard-crash evidence")
        marker_path = (root / crash_recovery.crash_marker_path).resolve(strict=True)
        if not marker_path.is_relative_to(root):
            raise ValueError("Portfolio hard-crash marker escapes its evidence directory")
        marker_content = marker_path.read_bytes()
        marker = _read_crash_marker(marker_path)
        if (
            _sha256(marker_content) != crash_recovery.crash_marker_sha256
            or marker["attempt_id"] != crash_recovery.crash_marker_tool_call_id
            or marker["exit_code"] != crash_recovery.observed_exit_code
        ):
            raise ValueError("Portfolio hard-crash marker does not match its report")
        recovered_writes = [
            record
            for record in replayed.tool_calls
            if record.call_id == crash_recovery.pending_tool_call_id
            and record.name == "replace_text"
            and record.status == "success"
            and record.recovery_disposition == "accept_replace"
        ]
        if len(recovered_writes) != 1:
            raise ValueError("Portfolio Trace does not contain one exact recovered write")
        call_id = crash_recovery.pending_tool_call_id
        reserved_events = [
            event
            for event in events
            if event.event_type == "TOOL_CALL_RESERVED"
            and event.payload.get("reservation", {}).get("call_id") == call_id
        ]
        unknown_events = [
            event
            for event in events
            if event.event_type == "TOOL_CALL_UNKNOWN" and event.payload.get("call_id") == call_id
        ]
        settled_events = [
            event
            for event in events
            if event.event_type == "TOOL_CALL_SETTLED"
            and event.payload.get("record", {}).get("call_id") == call_id
        ]
        if (
            len(reserved_events) != 1
            or len(unknown_events) != 1
            or len(settled_events) != 1
            or not reserved_events[0].seq < unknown_events[0].seq < settled_events[0].seq
            or replayed.lease_epoch != crash_recovery.recovery_worker_epoch
            or report.final_lease_epoch != crash_recovery.recovery_worker_epoch
        ):
            raise ValueError(
                "Portfolio Trace does not prove intent-before-unknown-before-recovery ordering"
            )
    expected_final = (canonical_json(replayed.as_dict()) + "\n").encode("utf-8")
    if contents["final_state"] != expected_final:
        raise ValueError("Portfolio final-state export does not match Trace replay")
    return pack


class PortfolioDemoRunner:
    """Run one fixed, offline Harness story and export a self-verifying evidence directory."""

    def run(self, output_dir: Path) -> PortfolioDemoResult:
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=False)
        demo_id = f"demo-{uuid4().hex}"
        source = output_dir / "source"
        workspace = output_dir / "workspace"
        (source / "src").mkdir(parents=True)
        _write_new(source / "src" / "parser.py", _FIXTURE_CONTENT.encode("utf-8"))

        artifacts = ArtifactStore(output_dir / "artifacts")
        snapshots = SnapshotManager(artifacts)
        source_snapshot, source_manifest_ref = snapshots.capture(
            source,
            allowed_paths=("src/**",),
            denied_paths=(".git/**", ".env", ".env.*", "secrets/**"),
        )
        snapshots.restore(source_manifest_ref, workspace)

        check = AcceptanceCheck(
            id="empty-input",
            command="portfolio-demo:inspect-empty-input",
            timeout_seconds=30,
            required=True,
        )
        checker = _ParserAcceptanceExecutor()
        initial_validation = (checker.execute(source, check),)
        task = TaskSpec(
            task_id=f"horizon-portfolio-{demo_id[-12:]}",
            title="Recover from a bounded file-read error and repair an empty-input bug",
            objective=(
                "Use repository evidence, recover from an invalid one-sided line range, "
                "and make parse('') return an empty list."
            ),
            repository=Repository(
                source="local",
                path=str(workspace),
                base_commit=source_snapshot.workspace_revision,
            ),
            constraints=Constraints(
                allowed_paths=("src/**",),
                denied_paths=(".git/**", ".env", ".env.*", "secrets/**"),
                network="deny",
                requirements=(
                    "Use repository evidence before editing.",
                    "Do not expand the write scope beyond src/**.",
                ),
            ),
            acceptance=(check,),
            budgets=BudgetSpec(
                max_steps=12,
                max_model_calls=8,
                max_tool_calls=12,
                max_wall_time_seconds=120,
                max_cost_usd="1.00",
                max_input_tokens=50_000,
                max_output_tokens=2_048,
                max_repair_cycles=1,
            ),
            task_kind="bugfix",
            execution_mode="workspace_write",
            authority_scope="workspace_write",
            model_policy_id="offline-scripted-portfolio-demo",
            memory_scope=f"portfolio-{demo_id[-12:]}",
        )
        plan = Plan(
            items=(
                WorkItem(
                    work_item_id="repair-parser",
                    title="Repair empty-input parsing",
                    objective="Return [] for an empty input without changing the public API.",
                    expected_artifacts=("src/parser.py", "protected validation evidence"),
                    acceptance_ids=("empty-input",),
                    allowed_tools=(
                        "search_repo",
                        "read_file",
                        "retrieve_code",
                        "replace_text",
                        "run_check",
                    ),
                ),
            )
        )
        plan.check_task(task)

        actions = (
            ScriptedModelAction(tool="retrieve_code", arguments={"query": "parse"}),
            ScriptedModelAction(
                tool="read_file",
                arguments={"path": "src/parser.py", "start_line": 1},
            ),
            ScriptedModelAction(
                tool="read_file",
                arguments={"path": "src/parser.py", "start_line": 1, "end_line": 2},
            ),
            ScriptedModelAction(
                tool="replace_text",
                arguments={
                    "path": _WRITE_PATH,
                    "old": _WRITE_PREIMAGE,
                    "new": "return [] if value == '' else [value]",
                },
            ),
            ScriptedModelAction(tool="run_check", arguments={"check_id": "empty-input"}),
            ScriptedModelAction(
                tool="submit",
                arguments={"summary": "Recovered from the range error and verified the repair."},
            ),
        )
        store_path = output_dir / "control.sqlite3"
        campaign_path = output_dir / "campaign.sqlite3"
        retrieval_path = output_dir / "retrieval.sqlite3"
        store = SQLiteEventStore(store_path)
        service = HarnessService(store)
        run = store.create(task, "create")
        run = service.bind_workspace_origin(
            run.run_id,
            WorkspaceOrigin(
                source_path_hash=workspace_path_hash(source),
                source_revision=source_snapshot.workspace_revision,
                source_manifest_ref=source_manifest_ref,
            ),
            "workspace-origin",
        )
        run = service.set_plan(run.run_id, plan, "plan")
        run = service.acquire_lease(run.run_id, "portfolio-worker-1", "lease-1", ttl_seconds=120)
        token = LeaseToken.from_run(run)
        service.transition(run.run_id, RunStatus.RUNNING, token, "start")

        campaign = CampaignBudget(
            campaign_id=f"portfolio-{demo_id[-12:]}",
            currency="CNY",
            max_cost="10.00",
            max_cost_per_call="1.00",
        )
        config = AgentLoopConfig(
            max_model_iterations=len(actions),
            max_output_tokens=256,
            max_run_cost=Decimal("10.00"),
        )

        def build_runner(
            active_service: HarnessService,
            model: ScriptedModelGateway,
            active_artifacts: ArtifactStore,
            active_snapshots: SnapshotManager,
        ) -> CodingAgentRunner:
            tools = WorkspaceToolGateway(
                workspace,
                task,
                plan.items[0],
                active_snapshots,
                checker,
                SQLiteCodeRetriever(retrieval_path, active_snapshots),
            )
            return CodingAgentRunner(
                active_service,
                model,
                CampaignBudgetLedger(campaign_path),
                tools,
                active_artifacts,
                provider_id="offline-scripted",
                model_id="scripted-portfolio-v1",
                pricing=_OFFLINE_PRICE,
                campaign=campaign,
                config=config,
            )

        first_model = ScriptedModelGateway("portfolio", actions[:2])
        partial = build_runner(service, first_model, artifacts, snapshots).run(
            run.run_id,
            token,
            max_iterations_this_invocation=2,
        )
        if partial.status != RunStatus.RUNNING or partial.agent_session is None:
            raise ValueError("Portfolio demo did not reach its durable worker handoff boundary")
        service.release_lease(run.run_id, token, "worker-1-handoff")

        # Reopen every worker-owned adapter, then let a real child process die after the exact
        # replace effect but before its tool receipt is committed.
        store = SQLiteEventStore(store_path)
        service = HarnessService(store)
        artifacts = ArtifactStore(output_dir / "artifacts")
        snapshots = SnapshotManager(artifacts)
        crashed_lease = service.acquire_lease(
            run.run_id,
            "portfolio-worker-2",
            "lease-2",
            ttl_seconds=120,
        )
        crashed_token = LeaseToken.from_run(crashed_lease)
        crash_marker_path = output_dir / "hard-crash-marker.json"
        crashed_process = subprocess.run(
            [
                sys.executable,
                "-m",
                "horizon.application._portfolio_crash_worker",
                str(store_path),
                run.run_id,
                crashed_token.lease_id,
                crashed_token.worker_id,
                str(crashed_token.epoch),
                str(output_dir / "artifacts"),
                str(workspace),
                str(campaign_path),
                str(retrieval_path),
                str(crash_marker_path),
            ],
            capture_output=True,
            timeout=30,
        )
        if crashed_process.returncode != PORTFOLIO_CRASH_EXIT_CODE:
            stderr = crashed_process.stderr.decode(errors="replace")[-2_000:]
            raise ValueError(
                "Portfolio child did not stop at the expected hard-crash boundary: "
                f"exit={crashed_process.returncode}, stderr={stderr!r}"
            )
        marker = _read_crash_marker(crash_marker_path)
        marker_content = crash_marker_path.read_bytes()

        store = SQLiteEventStore(store_path)
        service = HarnessService(store)
        artifacts = ArtifactStore(output_dir / "artifacts")
        snapshots = SnapshotManager(artifacts)
        interrupted = store.get(run.run_id)
        pending_write_ids = [
            call_id
            for call_id, reservation in interrupted.tool_reservations.items()
            if reservation.name == "replace_text"
        ]
        if len(pending_write_ids) != 1:
            raise ValueError("Portfolio hard crash did not leave one pending replace intent")
        pending_write_id = pending_write_ids[0]
        reservation_without_receipt = not any(
            record.call_id == pending_write_id for record in interrupted.tool_calls
        )
        expected_effect_present = (workspace / _WRITE_PATH).read_text(encoding="utf-8") == (
            _FIXED_CONTENT
        )
        if marker["attempt_id"] != pending_write_id:
            raise ValueError("Portfolio crash marker does not identify the pending tool intent")

        ledger = CampaignBudgetLedger(campaign_path)
        blocked_report = RecoveryService(service, ledger, artifacts).reconcile(
            run.run_id,
            crashed_token,
        )
        matching_findings = [
            finding
            for finding in blocked_report.findings
            if finding.operation_id == pending_write_id
            and finding.classification == "tool_effect_unknown"
        ]
        conservative_unknown_recorded = (
            not blocked_report.safe_to_resume
            and len(matching_findings) == 1
            and pending_write_id in store.get(run.run_id).unknown_tool_calls
        )

        # The supervisor has synchronously reaped the child and conservatively classified its
        # open intent, so it can fence the exact lease token without waiting for TTL expiry. The
        # crashed worker itself never reaches either command.
        service.release_lease(
            run.run_id,
            crashed_token,
            "supervisor-fence-reaped-crash-worker",
        )
        recovered_lease = service.acquire_lease(
            run.run_id,
            "portfolio-worker-3",
            "lease-3-after-hard-crash",
            ttl_seconds=120,
        )
        token = LeaseToken.from_run(recovered_lease)
        recovery_gateway = WorkspaceToolGateway(
            workspace,
            task,
            plan.items[0],
            snapshots,
            checker,
            SQLiteCodeRetriever(retrieval_path, snapshots),
        )
        resolved = ToolRecoveryService(service, artifacts, recovery_gateway).resolve_write(
            run.run_id,
            pending_write_id,
            decision="accept",
            token=token,
        )
        safe_report = RecoveryService(service, ledger, artifacts).reconcile(run.run_id, token)
        if not safe_report.safe_to_resume:
            raise ValueError("Portfolio exact write recovery did not restore a safe Agent boundary")

        final_model = ScriptedModelGateway("portfolio", actions[4:], start_index=4)
        result = build_runner(service, final_model, artifacts, snapshots).run(run.run_id, token)

        trace_content = store.export_jsonl(run.run_id).encode("utf-8")
        replayed = SQLiteEventStore.replay_jsonl(trace_content.decode("utf-8"))
        trace_replay_verified = replayed.as_dict() == result.as_dict() and projection_hash(
            replayed
        ) == projection_hash(result)
        source_after, _ = snapshots.capture(
            source,
            allowed_paths=task.constraints.allowed_paths,
            denied_paths=task.constraints.denied_paths,
        )
        workspace_after, workspace_manifest_ref = snapshots.capture(
            workspace,
            allowed_paths=task.constraints.allowed_paths,
            denied_paths=task.constraints.denied_paths,
        )
        final_validation = tuple(checker.execute(workspace, item) for item in task.acceptance)
        lineage = _evidence_write_lineage(
            result,
            tuple(store.events(run.run_id)),
            artifacts,
        )

        error_records = [
            record
            for record in result.tool_calls
            if record.name == "read_file" and record.status == "error"
        ]
        if len(error_records) != 1 or error_records[0].artifact_ref is None:
            raise ValueError("Portfolio demo requires one content-addressed read error")
        error_content = artifacts.read(error_records[0].artifact_ref).decode("utf-8")
        resumed_context_contains_error = bool(final_model.requests) and any(
            message.role == "tool" and _STRUCTURED_RANGE_ERROR in (message.content or "")
            for message in final_model.requests[0].messages
        )
        resumed_context_contains_recovery = bool(final_model.requests) and any(
            message.role == "tool" and "accept_replace" in (message.content or "")
            for message in final_model.requests[0].messages
        )
        recovered_write_records = [
            record
            for record in result.tool_calls
            if record.call_id == pending_write_id
            and record.name == "replace_text"
            and record.status == "success"
            and record.recovery_disposition == "accept_replace"
        ]
        one_recovered_write_receipt = len(recovered_write_records) == 1
        resolved_write = next(
            (record for record in resolved.tool_calls if record.call_id == pending_write_id),
            None,
        )
        exact_effect_accepted = (
            resolved_write is not None and resolved_write.recovery_disposition == "accept_replace"
        )
        crash_checks = (
            crashed_process.returncode == PORTFOLIO_CRASH_EXIT_CODE,
            token.epoch == crashed_token.epoch + 1,
            marker["attempt_id"] == pending_write_id,
            reservation_without_receipt,
            expected_effect_present,
            conservative_unknown_recorded,
            exact_effect_accepted,
            resumed_context_contains_recovery,
            one_recovered_write_receipt,
        )
        crash_recovery = PortfolioCrashRecoveryEvidence(
            observed_exit_code=crashed_process.returncode,
            crashed_worker_epoch=crashed_token.epoch,
            recovery_worker_epoch=token.epoch,
            pending_tool_call_id=pending_write_id,
            crash_marker_tool_call_id=str(marker["attempt_id"]),
            crash_marker_sha256=_sha256(marker_content),
            reservation_without_receipt=reservation_without_receipt,
            expected_effect_present=expected_effect_present,
            conservative_unknown_recorded=conservative_unknown_recorded,
            exact_effect_accepted=exact_effect_accepted,
            resumed_context_contains_recovery=resumed_context_contains_recovery,
            one_recovered_write_receipt=one_recovered_write_receipt,
            verified=all(crash_checks),
        )
        required_ids = {item.id for item in task.acceptance if item.required}
        run_validation_passed = result.validation is not None and required_ids <= set(
            result.validation["passed_check_ids"]
        )
        verification_values = {
            "initial_failure_confirmed": all(not item.passed for item in initial_validation),
            "structured_tool_error_observed": _STRUCTURED_RANGE_ERROR in error_content,
            "resumed_context_contains_error": resumed_context_contains_error,
            "retrieval_evidence_in_model_context": (lineage.model_context_contains_retrieval),
            "evidence_backed_write": lineage.verified,
            "hard_crash_recovery_verified": crash_recovery.verified,
            "final_validation_passed": (
                run_validation_passed and all(item.passed for item in final_validation)
            ),
            "trace_replay_verified": trace_replay_verified,
            "source_workspace_unchanged": (
                source_after.workspace_revision == source_snapshot.workspace_revision
            ),
            "staging_workspace_changed": (
                workspace_after.workspace_revision != source_snapshot.workspace_revision
            ),
            "scripted_actions_consumed": (
                first_model.consumed
                and final_model.consumed
                and len(result.model_calls) == len(actions)
            ),
            "no_unknown_calls": (not result.unknown_model_calls and not result.unknown_tool_calls),
            "no_open_reservations": (
                not result.reservations
                and not result.model_reservations
                and not result.tool_reservations
            ),
        }
        verification = PortfolioDemoVerification(
            **verification_values,
            all_checks_passed=all(verification_values.values()),
        )

        trace_path = output_dir / "trace.jsonl"
        final_run_path = output_dir / "final-run.json"
        report_path = output_dir / "report.json"
        summary_path = output_dir / "SUMMARY.md"
        evidence_pack_path = output_dir / "evidence-pack.json"
        _write_new(trace_path, trace_content)
        final_run_content = (canonical_json(result.as_dict()) + "\n").encode("utf-8")
        _write_new(final_run_path, final_run_content)
        report = PortfolioDemoReport(
            demo_id=demo_id,
            run_id=result.run_id,
            status=result.status,
            final_lease_epoch=result.lease_epoch,
            event_count=result.seq,
            tool_sequence=tuple(record.name for record in result.tool_calls),
            tool_statuses=tuple(record.status for record in result.tool_calls),
            initial_validation=initial_validation,
            final_validation=final_validation,
            usage=result.usage,
            simulated_model_cost=result.model_occupied_cost,
            model_currency=_OFFLINE_PRICE.currency,
            projection_hash=projection_hash(result),
            trace_sha256=_sha256(trace_content),
            final_run_sha256=_sha256(final_run_content),
            source_revision=source_snapshot.workspace_revision,
            source_manifest_ref=source_manifest_ref,
            workspace_revision=workspace_after.workspace_revision,
            workspace_manifest_ref=workspace_manifest_ref,
            structured_error_artifact_ref=error_records[0].artifact_ref,
            evidence_lineage=lineage,
            crash_recovery=crash_recovery,
            verification=verification,
        )
        _write_new(report_path, (canonical_json(report) + "\n").encode("utf-8"))
        summary = self._summary(report)
        _write_new(summary_path, summary.encode("utf-8"))
        pack = PortfolioEvidencePack(
            demo_id=demo_id,
            run_id=result.run_id,
            files=(
                _file_record(output_dir, "report", report_path),
                _file_record(output_dir, "trace", trace_path),
                _file_record(output_dir, "final_state", final_run_path),
                _file_record(output_dir, "summary", summary_path),
            ),
            all_checks_passed=verification.all_checks_passed,
        )
        _write_new(evidence_pack_path, (canonical_json(pack) + "\n").encode("utf-8"))
        verified_pack = verify_portfolio_evidence_pack(evidence_pack_path)
        return PortfolioDemoResult(
            output_dir=output_dir,
            evidence_pack_path=evidence_pack_path,
            report_path=report_path,
            trace_path=trace_path,
            final_run_path=final_run_path,
            summary_path=summary_path,
            workspace=workspace,
            report=report,
            evidence_pack=verified_pack,
        )

    @staticmethod
    def _summary(report: PortfolioDemoReport) -> str:
        checks = report.verification
        crash = report.crash_recovery
        if crash is None:
            raise ValueError("Current portfolio summary requires hard-crash evidence")
        exclusions = "\n".join(f"- `{item}`" for item in report.excluded_claims)
        return (
            "# Horizon Portfolio Demo Evidence\n\n"
            f"- Outcome: `{report.status.value}`\n"
            f"- Run: `{report.run_id}`\n"
            f"- Durable worker handoffs: `{report.worker_handoffs}`\n"
            f"- Recovery mode: `{report.recovery_mode}`\n"
            f"- Crashed child exit code: `{crash.observed_exit_code}`\n"
            f"- Final lease epoch: `{report.final_lease_epoch}`\n"
            f"- Events: `{report.event_count}`\n"
            f"- Model/tool calls: `{report.usage.model_calls}` / "
            f"`{report.usage.tool_calls}`\n"
            f"- External model cost: `CNY {report.external_cost_cny}`\n"
            f"- Simulated accounting cost: `CNY {report.simulated_model_cost}`\n"
            f"- Trace projection: `{report.projection_hash}`\n\n"
            "## Verified path\n\n"
            f"- Initial failure confirmed: `{checks.initial_failure_confirmed}`\n"
            f"- Structured tool error observed: `{checks.structured_tool_error_observed}`\n"
            f"- Second worker received the persisted error: "
            f"`{checks.resumed_context_contains_error}`\n"
            f"- Retrieval evidence reached the write context: "
            f"`{checks.retrieval_evidence_in_model_context}`\n"
            f"- Exact write was backed by same-revision evidence: "
            f"`{checks.evidence_backed_write}`\n"
            f"- Pending write had no receipt after hard exit: "
            f"`{crash.reservation_without_receipt}`\n"
            f"- Unknown write was conservatively classified: "
            f"`{crash.conservative_unknown_recorded}`\n"
            f"- Exact existing effect was accepted once: "
            f"`{crash.exact_effect_accepted and crash.one_recovered_write_receipt}`\n"
            f"- Resumed context contains the recovery disposition: "
            f"`{crash.resumed_context_contains_recovery}`\n"
            f"- Hard-crash recovery verified: `{checks.hard_crash_recovery_verified}`\n"
            f"- Protected final validation passed: `{checks.final_validation_passed}`\n"
            f"- Trace replay matched final state: `{checks.trace_replay_verified}`\n"
            f"- Original source stayed unchanged: `{checks.source_workspace_unchanged}`\n"
            f"- All evidence checks passed: `{checks.all_checks_passed}`\n\n"
            "## Explicitly excluded claims\n\n"
            f"{exclusions}\n"
        )
