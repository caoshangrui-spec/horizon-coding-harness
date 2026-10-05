from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.workspace.promotion import WorkspacePromoter
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.checkpoints import commit_checkpoint
from horizon.application.portfolio_demo import (
    PortfolioDemoRunner,
    verify_portfolio_evidence_pack,
)
from horizon.application.promotion import PromotionService
from horizon.application.recovery import RecoveryService
from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.common import canonical_json
from horizon.domain.errors import HorizonError
from horizon.domain.events import Event
from horizon.domain.plan import Plan, WorkItem
from horizon.domain.promotion import PromotionIntent
from horizon.domain.recovery import write_recovery_verdict
from horizon.domain.recovery_evaluation import (
    MODEL_RESPONSE_CRASH_EXIT_CODE,
    MODEL_RESPONSE_CRASH_RESPONSE_ID,
    PROMOTION_CRASH_EXIT_CODE,
    HardCrashRecoveryEvalCase,
    ModelResponseHardCrashEvalCase,
    PromotionHardCrashEvalCase,
    RecoveryMatrixCaseResult,
    RecoveryMatrixManifest,
    RecoveryMatrixReport,
    WriteRecoveryEvalCase,
)
from horizon.domain.states import RunStatus
from horizon.domain.task import AcceptanceCheck, BudgetSpec, Constraints, Repository, TaskSpec
from horizon.tools.gateway import WorkspaceToolGateway

_DENIED_PATHS = (".git/**", ".env", ".env.*", "secrets/**")
_REPLACE_ARGUMENTS = {
    "path": "src/alpha.py",
    "old": "alpha = 1",
    "new": "alpha = 2",
}
_PATCH_ARGUMENTS = {
    "edits": [
        {"path": "src/alpha.py", "old": "alpha = 1", "new": "alpha = 2"},
        {"path": "src/beta.py", "old": "beta = 1", "new": "beta = 2"},
    ]
}
_CREATE_ARGUMENTS = {
    "path": "src/generated.py",
    "content": "generated = True\n",
}


@dataclass(frozen=True)
class RecoveryMatrixEvalResult:
    output_dir: Path
    report_path: Path
    report_ref: str
    report: RecoveryMatrixReport


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 6)


def _write_new(path: Path, content: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(content)


def _evidence_path(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"Recovery-matrix evidence has no {label} path")
    candidate = (root / value).resolve(strict=True)
    if not candidate.is_relative_to(root):
        raise ValueError(f"Recovery-matrix {label} path escapes its evidence directory")
    return candidate


def _fixture_task(workspace: Path) -> tuple[TaskSpec, WorkItem]:
    check = AcceptanceCheck(
        id="fixture",
        command="recovery-matrix:no-repository-execution",
        timeout_seconds=30,
        required=True,
    )
    task = TaskSpec(
        task_id="recovery-matrix-fixture",
        title="Exercise deterministic write recovery states",
        objective="Classify or reverse a bounded write without redispatching its side effect.",
        repository=Repository(
            source="local",
            path=str(workspace),
            base_commit="0" * 40,
        ),
        constraints=Constraints(
            allowed_paths=("src/**",),
            denied_paths=_DENIED_PATHS,
            network="deny",
            requirements=("Never redispatch an uncertain write during recovery.",),
        ),
        acceptance=(check,),
        budgets=BudgetSpec(
            max_steps=4,
            max_model_calls=1,
            max_tool_calls=2,
            max_wall_time_seconds=3600,
            max_cost_usd="0.01",
        ),
        task_kind="tests",
        execution_mode="workspace_write",
        authority_scope="workspace_write",
        model_policy_id="offline-scripted-recovery-matrix",
    )
    work_item = WorkItem(
        work_item_id="recover-write",
        title="Recover one bounded write",
        objective="Use exact pre/current/expected revisions to choose a safe disposition.",
        expected_artifacts=("content-addressed recovery evidence",),
        acceptance_ids=("fixture",),
        allowed_tools=("read_file", "replace_text", "apply_patch", "create_file"),
    )
    return task, work_item


def _promotion_fixture_task(candidate: Path, base_revision: str) -> tuple[TaskSpec, WorkItem]:
    task = TaskSpec(
        task_id="recovery-matrix-promotion-fixture",
        title="Exercise crash-recoverable source promotion",
        objective="Promote one validated staging change without replaying an applied effect.",
        repository=Repository(
            source="local",
            path=str(candidate),
            base_commit=base_revision,
        ),
        constraints=Constraints(
            allowed_paths=("src/**",),
            denied_paths=_DENIED_PATHS,
            network="deny",
            requirements=("Do not rewrite an already-applied promotion effect during recovery.",),
        ),
        acceptance=(
            AcceptanceCheck(
                id="fixture",
                command="recovery-matrix:no-repository-execution",
                timeout_seconds=30,
                required=True,
            ),
        ),
        budgets=BudgetSpec(
            max_steps=4,
            max_model_calls=1,
            max_tool_calls=2,
            max_wall_time_seconds=3600,
            max_cost_usd="0.01",
        ),
        task_kind="tests",
        execution_mode="workspace_write",
        authority_scope="workspace_write",
        model_policy_id="offline-scripted-recovery-matrix",
    )
    work_item = WorkItem(
        work_item_id="promote-change",
        title="Promote one validated change",
        objective="Copy the exact validated parser change back to its bound source.",
        expected_artifacts=("src/parser.py",),
        acceptance_ids=("fixture",),
        allowed_tools=("read_file", "replace_text", "run_check"),
    )
    return task, work_item


def _arguments(tool: str) -> dict:
    if tool == "replace_text":
        return _REPLACE_ARGUMENTS
    if tool == "apply_patch":
        return _PATCH_ARGUMENTS
    return _CREATE_ARGUMENTS


def _assess(gateway: WorkspaceToolGateway, tool: str, arguments: dict, manifest_ref: str):
    if tool == "replace_text":
        return gateway.assess_replace_recovery(arguments, manifest_ref)
    if tool == "apply_patch":
        return gateway.assess_patch_recovery(arguments, manifest_ref)
    return gateway.assess_create_recovery(arguments, manifest_ref)


def _rollback(
    gateway: WorkspaceToolGateway,
    tool: str,
    arguments: dict,
    manifest_ref: str,
    current_revision: str,
) -> tuple[str, str]:
    if tool == "replace_text":
        return gateway.rollback_replace_recovery(
            arguments,
            manifest_ref,
            current_revision,
        )
    if tool == "apply_patch":
        return gateway.rollback_patch_recovery(
            arguments,
            manifest_ref,
            current_revision,
        )
    return gateway.rollback_create_recovery(
        arguments,
        manifest_ref,
        current_revision,
    )


def _inject_divergence(workspace: Path, tool: str) -> None:
    if tool == "replace_text":
        (workspace / "src" / "alpha.py").write_text("alpha = 99\n", encoding="utf-8")
    elif tool == "apply_patch":
        # A realistic partial multi-file effect: one target changed, one remained at its pre-state.
        (workspace / "src" / "alpha.py").write_text("alpha = 2\n", encoding="utf-8")
    else:
        (workspace / "src" / "generated.py").write_text(
            "generated = 'unexpected'\n",
            encoding="utf-8",
        )


def _current_snapshot(
    workspace: Path,
    snapshots: SnapshotManager,
):
    return snapshots.capture(workspace, denied_paths=_DENIED_PATHS)


class RecoveryMatrixEvaluator:
    """Execute a bounded offline crash/state matrix against production recovery code."""

    def evaluate(
        self,
        manifest: RecoveryMatrixManifest,
        output_dir: Path,
    ) -> RecoveryMatrixEvalResult:
        output_dir = output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=False)
        artifacts = ArtifactStore(output_dir / "artifacts")
        cases_root = output_dir / "cases"
        cases_root.mkdir()
        results: list[RecoveryMatrixCaseResult] = []
        for index, case in enumerate(manifest.cases):
            case_root = cases_root / f"{index:02d}"
            if isinstance(case, HardCrashRecoveryEvalCase):
                results.append(self._hard_crash_case(case, case_root, output_dir, artifacts))
            elif isinstance(case, ModelResponseHardCrashEvalCase):
                results.append(
                    self._model_response_hard_crash_case(
                        case,
                        case_root,
                        output_dir,
                        artifacts,
                    )
                )
            elif isinstance(case, PromotionHardCrashEvalCase):
                results.append(
                    self._promotion_hard_crash_case(
                        case,
                        case_root,
                        output_dir,
                        artifacts,
                    )
                )
            else:
                results.append(self._write_state_case(case, case_root, output_dir, artifacts))

        expected_auto = [case for case in results if case.expected_outcome == "auto_recovered"]
        expected_block = [case for case in results if case.expected_outcome == "safely_blocked"]
        passed_count = sum(case.passed for case in results)
        report = RecoveryMatrixReport(
            schema_version=manifest.schema_version,
            benchmark_id=manifest.benchmark_id,
            manifest_digest=manifest.sha256,
            case_count=len(results),
            passed_case_count=passed_count,
            case_pass_rate=_ratio(passed_count, len(results)),
            expected_auto_recovery_count=len(expected_auto),
            auto_recovered_count=sum(case.observed_outcome == "auto_recovered" for case in results),
            recovery_success_rate=_ratio(
                sum(case.observed_outcome == "auto_recovered" for case in expected_auto),
                len(expected_auto),
            ),
            expected_safe_block_count=len(expected_block),
            safely_blocked_count=sum(case.observed_outcome == "safely_blocked" for case in results),
            safe_block_rate=_ratio(
                sum(case.observed_outcome == "safely_blocked" for case in expected_block),
                len(expected_block),
            ),
            unrecoverable_count=sum(case.observed_outcome == "unrecoverable" for case in results),
            incorrect_resume_count=sum(
                case.observed_outcome == "incorrect_resume" for case in results
            ),
            recovery_redispatch_count=sum(case.recovery_redispatch_count for case in results),
            duplicate_side_effect_count=sum(case.duplicate_side_effect_count for case in results),
            actual_process_crash_case_count=sum(
                case.actual_process_crash_observed for case in results
            ),
            trace_replay_verified_count=sum(case.trace_replay_verified is True for case in results),
            cases=tuple(results),
        )
        report_content = (canonical_json(report) + "\n").encode("utf-8")
        report_path = output_dir / "report.json"
        _write_new(report_path, report_content)
        report_ref = artifacts.put(report_content)
        verified = verify_recovery_matrix_report(report_path, manifest)
        if verified != report:
            raise ValueError("Recovery-matrix verification changed the report projection")
        return RecoveryMatrixEvalResult(
            output_dir=output_dir,
            report_path=report_path,
            report_ref=report_ref,
            report=report,
        )

    @staticmethod
    def _hard_crash_case(
        case: HardCrashRecoveryEvalCase,
        case_root: Path,
        output_dir: Path,
        artifacts: ArtifactStore,
    ) -> RecoveryMatrixCaseResult:
        started = time.perf_counter_ns()
        demo = PortfolioDemoRunner().run(case_root)
        verified = verify_portfolio_evidence_pack(demo.evidence_pack_path)
        crash = demo.report.crash_recovery
        actual_crash = crash is not None and crash.observed_exit_code == crash.expected_exit_code
        trace_verified = (
            verified.all_checks_passed
            and demo.report.verification.trace_replay_verified
            and demo.report.verification.hard_crash_recovery_verified is True
        )
        observed_outcome = (
            "auto_recovered"
            if actual_crash and trace_verified and demo.report.verification.all_checks_passed
            else "unrecoverable"
        )
        relative_pack = demo.evidence_pack_path.relative_to(output_dir).as_posix()
        pack_content = demo.evidence_pack_path.read_bytes()
        evidence = {
            "schema_version": 1,
            "case_id": case.case_id,
            "kind": case.kind,
            "observed_outcome": observed_outcome,
            "evidence_pack_path": relative_pack,
            "evidence_pack_sha256": _sha256(pack_content),
            "portfolio_report_sha256": _sha256(demo.report_path.read_bytes()),
            "trace_sha256": _sha256(demo.trace_path.read_bytes()),
            "crashed_worker_exit_code": crash.observed_exit_code if crash is not None else None,
            "pending_tool_call_id": crash.pending_tool_call_id if crash is not None else None,
            "recovery_disposition": crash.recovery_disposition if crash is not None else None,
            "trace_replay_verified": trace_verified,
            "recovery_redispatch_count": 0,
            "duplicate_side_effect_count": 0,
        }
        evidence_ref = artifacts.put(canonical_json(evidence).encode("utf-8"))
        duration_ms = (time.perf_counter_ns() - started) // 1_000_000
        return RecoveryMatrixCaseResult(
            case_id=case.case_id,
            kind=case.kind,
            expected_outcome=case.expected_outcome,
            observed_outcome=observed_outcome,
            passed=observed_outcome == case.expected_outcome,
            duration_ms=duration_ms,
            recovery_redispatch_count=0,
            duplicate_side_effect_count=0,
            evidence_ref=evidence_ref,
            evidence_path=relative_pack,
            actual_process_crash_observed=actual_crash,
            trace_replay_verified=trace_verified,
            detail=(
                "A child exited after the exact replace effect and before its receipt; the "
                "Trace proves unknown classification, one accepted receipt, and resumed validation."
            ),
        )

    @staticmethod
    def _model_response_hard_crash_case(
        case: ModelResponseHardCrashEvalCase,
        case_root: Path,
        output_dir: Path,
        artifacts: ArtifactStore,
    ) -> RecoveryMatrixCaseResult:
        started = time.perf_counter_ns()
        workspace = case_root / "workspace"
        (workspace / "src").mkdir(parents=True)
        (workspace / "src" / "parser.py").write_text(
            "def parse(value):\n    return [value]\n",
            encoding="utf-8",
        )
        task, work_item = _fixture_task(workspace)
        plan = Plan(items=(work_item,))
        plan.check_task(task)

        store_path = case_root / "control.sqlite3"
        ledger_path = case_root / "campaign.sqlite3"
        artifact_path = case_root / "model-artifacts"
        marker_path = case_root / "provider-returned.txt"
        old_now = datetime.now(UTC) - timedelta(minutes=5)
        store = SQLiteEventStore(store_path, clock=lambda: old_now)
        service = HarnessService(store)
        run = store.create(task, "create-model-response-crash-eval")
        service.set_plan(run.run_id, plan, "plan-model-response-crash-eval")
        leased = service.acquire_lease(
            run.run_id,
            "model-response-crash-worker",
            "lease-model-response-crash-eval",
            ttl_seconds=30,
        )
        crashed_token = LeaseToken.from_run(leased)
        service.transition(
            run.run_id,
            RunStatus.RUNNING,
            crashed_token,
            "start-model-response-crash-eval",
        )

        crashed_process = subprocess.run(
            [
                sys.executable,
                "-m",
                "horizon.application._model_response_crash_worker",
                str(store_path),
                run.run_id,
                crashed_token.lease_id,
                crashed_token.worker_id,
                str(crashed_token.epoch),
                str(artifact_path),
                old_now.isoformat(),
                str(workspace),
                str(ledger_path),
                str(marker_path),
            ],
            capture_output=True,
            timeout=30,
        )
        if crashed_process.returncode != MODEL_RESPONSE_CRASH_EXIT_CODE:
            stderr = crashed_process.stderr.decode(errors="replace")[-2_000:]
            raise ValueError(
                "Model-response child did not stop at the expected pre-Artifact boundary: "
                f"exit={crashed_process.returncode}, stderr={stderr!r}"
            )

        client_trace_id = marker_path.read_text(encoding="utf-8")
        interrupted_store = SQLiteEventStore(store_path)
        interrupted = interrupted_store.get(run.run_id)
        if len(interrupted.model_reservations) != 1:
            raise ValueError("Model-response crash did not leave exactly one durable intent")
        call_id, reservation = next(iter(interrupted.model_reservations.items()))
        ledger = CampaignBudgetLedger(ledger_path)
        attempt_before = ledger.attempt("hard-exit-agent", call_id)
        response_artifact_absent = all(
            MODEL_RESPONSE_CRASH_RESPONSE_ID.encode() not in path.read_bytes()
            for path in artifact_path.glob("*/*")
            if path.is_file()
        )

        recovery_service = HarnessService(interrupted_store)
        recovery_run = recovery_service.acquire_lease(
            run.run_id,
            "model-response-recovery-worker",
            "takeover-model-response-crash-eval",
            prior_worker_stopped=True,
        )
        recovery_token = LeaseToken.from_run(recovery_run)
        recovery = RecoveryService(
            recovery_service,
            ledger,
            ArtifactStore(artifact_path),
        ).reconcile(run.run_id, recovery_token)
        recovery_service.release_lease(
            run.run_id,
            recovery_token,
            "release-blocked-model-response-crash-eval",
        )
        restored = interrupted_store.get(run.run_id)
        attempt_after = ledger.attempt("hard-exit-agent", call_id)
        matching_findings = [
            finding
            for finding in recovery.findings
            if finding.operation_id == call_id
            and finding.classification == "model_effect_unknown"
            and finding.client_trace_id == client_trace_id
        ]

        trace_content = interrupted_store.export_jsonl(run.run_id).encode("utf-8")
        replayed = SQLiteEventStore.replay_jsonl(trace_content.decode("utf-8"))
        trace_replay_verified = replayed.as_dict() == restored.as_dict()
        events = tuple(
            Event.model_validate_json(line)
            for line in trace_content.decode("utf-8").splitlines()
            if line
        )
        reserved_events = [
            event
            for event in events
            if event.event_type == "MODEL_CALL_RESERVED"
            and event.payload.get("reservation", {}).get("call_id") == call_id
        ]
        unknown_events = [
            event
            for event in events
            if event.event_type == "MODEL_CALL_UNKNOWN" and event.payload.get("call_id") == call_id
        ]
        recovery_redispatch_count = max(0, len(reserved_events) - 1)
        event_order_verified = (
            len(reserved_events) == 1
            and len(unknown_events) == 1
            and reserved_events[0].seq < unknown_events[0].seq
        )
        parser_path = workspace / "src" / "parser.py"
        parser_sha256 = _sha256(parser_path.read_bytes())
        checks = (
            reservation.client_trace_id == client_trace_id,
            not interrupted.model_calls,
            not interrupted.unknown_model_calls,
            attempt_before.status == "reserved",
            response_artifact_absent,
            not recovery.safe_to_resume,
            recovery.next_action == "manual_reconciliation",
            len(matching_findings) == 1,
            restored.unknown_model_calls == {call_id},
            restored.unknown_reservations == {call_id},
            not restored.model_calls,
            attempt_after.status == "unknown",
            attempt_after.error_type == "RecoveryUncertainDispatch",
            event_order_verified,
            recovery_redispatch_count == 0,
            trace_replay_verified,
            restored.lease_id is None,
        )
        unsafe_resume = (
            recovery.safe_to_resume or bool(restored.model_calls) or recovery_redispatch_count > 0
        )
        observed_outcome = (
            "safely_blocked"
            if all(checks)
            else "incorrect_resume"
            if unsafe_resume
            else "unrecoverable"
        )

        trace_path = case_root / "trace.jsonl"
        final_state_path = case_root / "final-run.json"
        final_state_content = (canonical_json(restored.as_dict()) + "\n").encode("utf-8")
        _write_new(trace_path, trace_content)
        _write_new(final_state_path, final_state_content)
        relative_trace = trace_path.relative_to(output_dir).as_posix()
        evidence = {
            "schema_version": 2,
            "case_id": case.case_id,
            "kind": case.kind,
            "observed_outcome": observed_outcome,
            "observed_exit_code": crashed_process.returncode,
            "expected_exit_code": MODEL_RESPONSE_CRASH_EXIT_CODE,
            "model_call_id": call_id,
            "client_trace_id": client_trace_id,
            "response_artifact_absent": response_artifact_absent,
            "model_receipt_absent": not restored.model_calls,
            "recovery_classification": (
                matching_findings[0].classification if len(matching_findings) == 1 else None
            ),
            "safe_to_resume": recovery.safe_to_resume,
            "next_action": recovery.next_action,
            "campaign_status": attempt_after.status,
            "campaign_error_type": attempt_after.error_type,
            "crashed_worker_epoch": crashed_token.epoch,
            "recovery_worker_epoch": recovery_token.epoch,
            "lease_released": restored.lease_id is None,
            "reserved_event_count": len(reserved_events),
            "unknown_event_count": len(unknown_events),
            "trace_replay_verified": trace_replay_verified,
            "trace_path": relative_trace,
            "trace_sha256": _sha256(trace_content),
            "final_state_path": final_state_path.relative_to(output_dir).as_posix(),
            "final_state_sha256": _sha256(final_state_content),
            "marker_path": marker_path.relative_to(output_dir).as_posix(),
            "marker_sha256": _sha256(marker_path.read_bytes()),
            "campaign_ledger_path": ledger_path.relative_to(output_dir).as_posix(),
            "model_artifact_root_path": artifact_path.relative_to(output_dir).as_posix(),
            "workspace_file_path": parser_path.relative_to(output_dir).as_posix(),
            "workspace_file_sha256": parser_sha256,
            "recovery_redispatch_count": recovery_redispatch_count,
            "duplicate_side_effect_count": 0,
        }
        evidence_ref = artifacts.put(canonical_json(evidence).encode("utf-8"))
        duration_ms = (time.perf_counter_ns() - started) // 1_000_000
        return RecoveryMatrixCaseResult(
            case_id=case.case_id,
            kind=case.kind,
            expected_outcome=case.expected_outcome,
            observed_outcome=observed_outcome,
            passed=observed_outcome == case.expected_outcome,
            duration_ms=duration_ms,
            recovery_redispatch_count=recovery_redispatch_count,
            duplicate_side_effect_count=0,
            evidence_ref=evidence_ref,
            evidence_path=relative_trace,
            actual_process_crash_observed=(
                crashed_process.returncode == MODEL_RESPONSE_CRASH_EXIT_CODE
            ),
            trace_replay_verified=trace_replay_verified,
            detail=(
                "A child returned a model response and exited before publishing its Artifact; "
                "recovery preserved the client Trace ID, marked both ledgers unknown, and did "
                "not redispatch the model call."
            ),
        )

    @staticmethod
    def _promotion_hard_crash_case(
        case: PromotionHardCrashEvalCase,
        case_root: Path,
        output_dir: Path,
        artifacts: ArtifactStore,
    ) -> RecoveryMatrixCaseResult:
        started = time.perf_counter_ns()
        source = case_root / "source"
        (source / "src").mkdir(parents=True)
        source_file = source / "src" / "parser.py"
        source_file.write_text("def parse(value):\n    return [value]\n", encoding="utf-8")

        promotion_artifacts = ArtifactStore(case_root / "promotion-artifacts")
        snapshots = SnapshotManager(promotion_artifacts)
        promoter = WorkspacePromoter(snapshots)
        constraints = Constraints(
            allowed_paths=("src/**",),
            denied_paths=_DENIED_PATHS,
            network="deny",
        )
        origin = promoter.bind_origin(source, constraints)
        candidate = case_root / "staging"
        snapshots.restore(origin.source_manifest_ref, candidate)
        candidate_file = candidate / "src" / "parser.py"
        candidate_file.write_text(
            "def parse(value):\n    return [] if value == '' else [value]\n",
            encoding="utf-8",
        )
        task, work_item = _promotion_fixture_task(candidate, origin.source_revision)
        plan = Plan(items=(work_item,))
        plan.check_task(task)

        store_path = case_root / "control.sqlite3"
        store = SQLiteEventStore(store_path)
        service = HarnessService(store)
        run = store.create(task, "create-promotion-crash-eval")
        run = service.bind_workspace_origin(
            run.run_id,
            origin,
            "bind-promotion-crash-origin",
        )
        run = service.set_plan(run.run_id, plan, "plan-promotion-crash-eval")
        run = service.acquire_lease(
            run.run_id,
            "promotion-crash-fixture-worker",
            "lease-promotion-crash-eval",
        )
        token = LeaseToken.from_run(run)
        run = service.transition(
            run.run_id,
            RunStatus.RUNNING,
            token,
            "start-promotion-crash-eval",
        )
        candidate_snapshot, candidate_manifest = snapshots.capture(
            candidate,
            denied_paths=task.constraints.denied_paths,
        )
        run = commit_checkpoint(
            service,
            run.run_id,
            token,
            "checkpoint-promotion-crash-eval",
            candidate_manifest,
            candidate_snapshot.workspace_revision,
            run.seq,
            snapshots.verify,
        )
        run = service.transition(
            run.run_id,
            RunStatus.VALIDATING,
            token,
            "validate-promotion-crash-eval",
        )
        validation_ref = promotion_artifacts.put(b"offline promotion fixture validated")
        run = service.record_validation(
            run.run_id,
            ("fixture",),
            validation_ref,
            token,
            "record-promotion-crash-validation",
        )
        run = service.pass_work_item(
            run.run_id,
            work_item.work_item_id,
            token,
            "pass-promotion-crash-work-item",
        )
        run = service.transition(
            run.run_id,
            RunStatus.SUCCEEDED,
            token,
            "complete-promotion-crash-eval",
        )

        promotion = PromotionService(service, promoter)
        promotion_plan = promotion.plan(run.run_id, source)
        intent = PromotionIntent(
            promotion_id="recovery-matrix-promotion-hard-crash",
            plan=promotion_plan,
        )
        service.reserve_promotion(run.run_id, intent, "reserve-promotion-crash-eval")
        marker_path = case_root / "promotion-effect.json"
        crashed_process = subprocess.run(
            [
                sys.executable,
                "-m",
                "horizon.application._promotion_crash_worker",
                str(store_path),
                run.run_id,
                str(promotion_artifacts.root),
                str(source),
                str(candidate),
                str(marker_path),
            ],
            capture_output=True,
            timeout=30,
        )
        if crashed_process.returncode != PROMOTION_CRASH_EXIT_CODE:
            stderr = crashed_process.stderr.decode(errors="replace")[-2_000:]
            raise ValueError(
                "Promotion child did not stop at the expected effect-before-receipt boundary: "
                f"exit={crashed_process.returncode}, stderr={stderr!r}"
            )

        marker_content = marker_path.read_bytes()
        marker = json.loads(marker_content)
        interrupted_store = SQLiteEventStore(store_path)
        interrupted = interrupted_store.get(run.run_id)
        interrupted_snapshot, interrupted_manifest = snapshots.capture(
            source,
            denied_paths=task.constraints.denied_paths,
        )
        stat_before_recovery = source_file.stat(follow_symlinks=False)

        settled = PromotionService(HarnessService(interrupted_store), promoter).promote(
            run.run_id,
            source,
        )
        stat_after_recovery = source_file.stat(follow_symlinks=False)
        final_snapshot, final_manifest = snapshots.capture(
            source,
            denied_paths=task.constraints.denied_paths,
        )
        receipt = settled.promotion_receipt

        trace_content = interrupted_store.export_jsonl(run.run_id).encode("utf-8")
        replayed = SQLiteEventStore.replay_jsonl(trace_content.decode("utf-8"))
        trace_replay_verified = replayed.as_dict() == settled.as_dict()
        events = tuple(
            Event.model_validate_json(line)
            for line in trace_content.decode("utf-8").splitlines()
            if line
        )
        reserved_events = [
            event
            for event in events
            if event.event_type == "PROMOTION_RESERVED"
            and event.payload.get("intent", {}).get("promotion_id") == intent.promotion_id
        ]
        settled_events = [
            event
            for event in events
            if event.event_type == "PROMOTION_SETTLED"
            and event.payload.get("receipt", {}).get("promotion_id") == intent.promotion_id
        ]
        recovery_redispatch_count = max(0, len(reserved_events) - 1)
        event_order_verified = (
            len(reserved_events) == 1
            and len(settled_events) == 1
            and reserved_events[0].seq < settled_events[0].seq
        )
        target_marker = marker.get("targets", [{}])[0] if marker.get("targets") else {}
        target_unchanged_during_recovery = (
            stat_before_recovery.st_dev == stat_after_recovery.st_dev
            and stat_before_recovery.st_ino == stat_after_recovery.st_ino
            and stat_before_recovery.st_mtime_ns == stat_after_recovery.st_mtime_ns
            and target_marker.get("device") == stat_after_recovery.st_dev
            and target_marker.get("inode") == stat_after_recovery.st_ino
            and target_marker.get("mtime_ns") == stat_after_recovery.st_mtime_ns
        )
        duplicate_side_effect_count = 0 if target_unchanged_during_recovery else 1
        checks = (
            interrupted.promotion_intent == intent,
            interrupted.promotion_receipt is None,
            marker.get("promotion_id") == intent.promotion_id,
            marker.get("plan_hash") == promotion_plan.sha256,
            marker.get("source_revision_after") == promotion_plan.candidate_revision,
            marker.get("source_manifest_ref_after") == interrupted_manifest,
            marker.get("already_applied_before_worker") is False,
            target_marker.get("path") == "src/parser.py",
            target_marker.get("sha256") == promotion_plan.changes[0].after_sha256,
            interrupted_snapshot.workspace_revision == promotion_plan.candidate_revision,
            receipt is not None,
            receipt.recovered_after_crash if receipt is not None else False,
            receipt.source_revision_after == promotion_plan.candidate_revision
            if receipt is not None
            else False,
            receipt.source_manifest_ref_after == final_manifest if receipt is not None else False,
            final_snapshot.workspace_revision == promotion_plan.candidate_revision,
            target_unchanged_during_recovery,
            event_order_verified,
            recovery_redispatch_count == 0,
            duplicate_side_effect_count == 0,
            trace_replay_verified,
        )
        unsafe_recovery = recovery_redispatch_count > 0 or duplicate_side_effect_count > 0
        observed_outcome = (
            "auto_recovered"
            if all(checks)
            else "incorrect_resume"
            if unsafe_recovery
            else "unrecoverable"
        )

        trace_path = case_root / "trace.jsonl"
        final_state_path = case_root / "final-run.json"
        final_state_content = (canonical_json(settled.as_dict()) + "\n").encode("utf-8")
        _write_new(trace_path, trace_content)
        _write_new(final_state_path, final_state_content)
        relative_trace = trace_path.relative_to(output_dir).as_posix()
        evidence = {
            "schema_version": 3,
            "case_id": case.case_id,
            "kind": case.kind,
            "observed_outcome": observed_outcome,
            "observed_exit_code": crashed_process.returncode,
            "expected_exit_code": PROMOTION_CRASH_EXIT_CODE,
            "promotion_id": intent.promotion_id,
            "plan_hash": promotion_plan.sha256,
            "candidate_revision": promotion_plan.candidate_revision,
            "candidate_manifest_ref": promotion_plan.candidate_manifest_ref,
            "source_manifest_ref_after": final_manifest,
            "recovered_after_crash": (
                receipt.recovered_after_crash if receipt is not None else False
            ),
            "target_unchanged_during_recovery": target_unchanged_during_recovery,
            "reserved_event_count": len(reserved_events),
            "settled_event_count": len(settled_events),
            "trace_replay_verified": trace_replay_verified,
            "trace_path": relative_trace,
            "trace_sha256": _sha256(trace_content),
            "final_state_path": final_state_path.relative_to(output_dir).as_posix(),
            "final_state_sha256": _sha256(final_state_content),
            "marker_path": marker_path.relative_to(output_dir).as_posix(),
            "marker_sha256": _sha256(marker_content),
            "promotion_artifact_root_path": promotion_artifacts.root.relative_to(
                output_dir
            ).as_posix(),
            "source_path": source.relative_to(output_dir).as_posix(),
            "source_file_path": source_file.relative_to(output_dir).as_posix(),
            "source_file_sha256": _sha256(source_file.read_bytes()),
            "candidate_path": candidate.relative_to(output_dir).as_posix(),
            "candidate_file_path": candidate_file.relative_to(output_dir).as_posix(),
            "candidate_file_sha256": _sha256(candidate_file.read_bytes()),
            "recovery_redispatch_count": recovery_redispatch_count,
            "duplicate_side_effect_count": duplicate_side_effect_count,
        }
        evidence_ref = artifacts.put(canonical_json(evidence).encode("utf-8"))
        duration_ms = (time.perf_counter_ns() - started) // 1_000_000
        return RecoveryMatrixCaseResult(
            case_id=case.case_id,
            kind=case.kind,
            expected_outcome=case.expected_outcome,
            observed_outcome=observed_outcome,
            passed=observed_outcome == case.expected_outcome,
            duration_ms=duration_ms,
            recovery_redispatch_count=recovery_redispatch_count,
            duplicate_side_effect_count=duplicate_side_effect_count,
            evidence_ref=evidence_ref,
            evidence_path=relative_trace,
            actual_process_crash_observed=(crashed_process.returncode == PROMOTION_CRASH_EXIT_CODE),
            trace_replay_verified=trace_replay_verified,
            detail=(
                "A child exited after publishing the exact source effect and before its "
                "promotion receipt; recovery observed the effect, wrote one receipt, and did "
                "not rewrite the target."
            ),
        )

    @staticmethod
    def _write_state_case(
        case: WriteRecoveryEvalCase,
        case_root: Path,
        output_dir: Path,
        artifacts: ArtifactStore,
    ) -> RecoveryMatrixCaseResult:
        started = time.perf_counter_ns()
        workspace = case_root / "workspace"
        (workspace / "src").mkdir(parents=True)
        (workspace / "src" / "alpha.py").write_text("alpha = 1\n", encoding="utf-8")
        (workspace / "src" / "beta.py").write_text("beta = 1\n", encoding="utf-8")
        snapshots = SnapshotManager(artifacts)
        pre, pre_manifest_ref = _current_snapshot(workspace, snapshots)
        task, work_item = _fixture_task(workspace)
        gateway = WorkspaceToolGateway(
            workspace,
            task,
            work_item,
            snapshots,
            checks=None,
        )
        arguments = _arguments(case.tool)
        initial_dispatch_count = 0
        if case.injected_state == "expected_effect":
            outcome = gateway.dispatch_safe(
                case.tool,
                arguments,
                attempt_id=f"matrix-{case.case_id}",
            )
            if outcome.status != "success":
                raise ValueError(
                    f"Recovery matrix could not materialize expected effect: {outcome.content}"
                )
            initial_dispatch_count = 1
        elif case.injected_state == "diverged":
            _inject_divergence(workspace, case.tool)

        assessment = _assess(gateway, case.tool, arguments, pre_manifest_ref)
        verdict = write_recovery_verdict(assessment.state, case.decision)
        recovery_error = None
        try:
            if verdict == "rollback":
                final_revision, final_manifest_ref = _rollback(
                    gateway,
                    case.tool,
                    arguments,
                    pre_manifest_ref,
                    assessment.current_revision,
                )
            else:
                final, final_manifest_ref = _current_snapshot(workspace, snapshots)
                final_revision = final.workspace_revision
        except HorizonError as exc:
            recovery_error = str(exc)
            final, final_manifest_ref = _current_snapshot(workspace, snapshots)
            final_revision = final.workspace_revision

        if recovery_error is not None:
            observed_outcome = "unrecoverable"
        elif verdict == "block":
            if final_revision != assessment.current_revision:
                observed_outcome = "incorrect_resume"
            elif case.expected_outcome == "safely_blocked":
                observed_outcome = "safely_blocked"
            else:
                observed_outcome = "unrecoverable"
        else:
            expected_final = (
                assessment.current_revision if verdict == "accept" else pre.workspace_revision
            )
            if final_revision != expected_final or case.expected_outcome == "safely_blocked":
                observed_outcome = "incorrect_resume"
            else:
                observed_outcome = "auto_recovered"

        workspace_path = workspace.relative_to(output_dir).as_posix()
        evidence = {
            "schema_version": 1,
            "case_id": case.case_id,
            "kind": case.kind,
            "tool": case.tool,
            "arguments": arguments,
            "injected_state": case.injected_state,
            "observed_state": assessment.state,
            "decision": case.decision,
            "policy_verdict": verdict,
            "observed_outcome": observed_outcome,
            "workspace_path": workspace_path,
            "pre_revision": pre.workspace_revision,
            "pre_manifest_ref": pre_manifest_ref,
            "expected_revision": assessment.expected_revision,
            "observed_revision": assessment.current_revision,
            "observed_manifest_ref": assessment.current_manifest_ref,
            "final_revision": final_revision,
            "final_manifest_ref": final_manifest_ref,
            "initial_dispatch_count": initial_dispatch_count,
            "recovery_redispatch_count": 0,
            "duplicate_side_effect_count": 0,
            "recovery_error": recovery_error,
        }
        evidence_ref = artifacts.put(canonical_json(evidence).encode("utf-8"))
        duration_ms = (time.perf_counter_ns() - started) // 1_000_000
        return RecoveryMatrixCaseResult(
            case_id=case.case_id,
            kind=case.kind,
            expected_outcome=case.expected_outcome,
            observed_outcome=observed_outcome,
            passed=observed_outcome == case.expected_outcome,
            duration_ms=duration_ms,
            recovery_redispatch_count=0,
            duplicate_side_effect_count=0,
            evidence_ref=evidence_ref,
            tool=case.tool,
            injected_state=case.injected_state,
            observed_state=assessment.state,
            decision=case.decision,
            detail=(
                f"Observed {assessment.state}; production policy selected {verdict}; "
                f"final revision invariant yielded {observed_outcome}."
            ),
        )


def verify_recovery_matrix_report(
    report_path: Path,
    manifest: RecoveryMatrixManifest,
) -> RecoveryMatrixReport:
    """Verify content-addressed case evidence and replay the real crash Trace."""

    report_path = report_path.resolve(strict=True)
    root = report_path.parent
    report = RecoveryMatrixReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    if (
        report.schema_version != manifest.schema_version
        or report.benchmark_id != manifest.benchmark_id
        or report.manifest_digest != manifest.sha256
    ):
        raise ValueError("Recovery-matrix report does not match its frozen manifest")
    if [case.case_id for case in report.cases] != [case.case_id for case in manifest.cases]:
        raise ValueError("Recovery-matrix report case order does not match its manifest")

    artifacts = ArtifactStore(root / "artifacts")
    snapshots = SnapshotManager(artifacts)
    for case, result in zip(manifest.cases, report.cases, strict=True):
        evidence = json.loads(artifacts.read(result.evidence_ref))
        if (
            not isinstance(evidence, dict)
            or evidence.get("case_id") != case.case_id
            or evidence.get("kind") != case.kind
            or evidence.get("observed_outcome") != result.observed_outcome
            or evidence.get("recovery_redispatch_count") != result.recovery_redispatch_count
            or evidence.get("duplicate_side_effect_count") != result.duplicate_side_effect_count
        ):
            raise ValueError(f"Recovery-matrix evidence mismatch: {case.case_id}")

        if isinstance(case, HardCrashRecoveryEvalCase):
            relative = evidence.get("evidence_pack_path")
            if not isinstance(relative, str) or relative != result.evidence_path:
                raise ValueError("Hard-crash evidence path does not match its case result")
            pack_path = _evidence_path(root, relative, "hard-crash EvidencePack")
            pack_content = pack_path.read_bytes()
            if _sha256(pack_content) != evidence.get("evidence_pack_sha256"):
                raise ValueError("Hard-crash EvidencePack hash does not match matrix evidence")
            pack = verify_portfolio_evidence_pack(pack_path)
            if not pack.all_checks_passed or result.trace_replay_verified is not True:
                raise ValueError("Hard-crash EvidencePack does not prove a replayable recovery")
            continue

        if isinstance(case, ModelResponseHardCrashEvalCase):
            if evidence.get("trace_path") != result.evidence_path:
                raise ValueError("Model-response crash Trace path does not match its case result")
            trace_path = _evidence_path(root, evidence.get("trace_path"), "Trace")
            final_state_path = _evidence_path(
                root,
                evidence.get("final_state_path"),
                "final state",
            )
            marker_path = _evidence_path(root, evidence.get("marker_path"), "provider marker")
            ledger_path = _evidence_path(
                root,
                evidence.get("campaign_ledger_path"),
                "campaign ledger",
            )
            artifact_root = _evidence_path(
                root,
                evidence.get("model_artifact_root_path"),
                "model Artifact root",
            )
            workspace_file = _evidence_path(
                root,
                evidence.get("workspace_file_path"),
                "workspace file",
            )
            trace_content = trace_path.read_bytes()
            final_state_content = final_state_path.read_bytes()
            if (
                _sha256(trace_content) != evidence.get("trace_sha256")
                or _sha256(final_state_content) != evidence.get("final_state_sha256")
                or _sha256(marker_path.read_bytes()) != evidence.get("marker_sha256")
                or _sha256(workspace_file.read_bytes()) != evidence.get("workspace_file_sha256")
            ):
                raise ValueError("Model-response crash evidence file hash mismatch")
            replayed = SQLiteEventStore.replay_jsonl(trace_content.decode("utf-8"))
            expected_final = (canonical_json(replayed.as_dict()) + "\n").encode("utf-8")
            if final_state_content != expected_final:
                raise ValueError("Model-response crash final state does not match Trace replay")
            call_id = evidence.get("model_call_id")
            client_trace_id = evidence.get("client_trace_id")
            events = tuple(
                Event.model_validate_json(line)
                for line in trace_content.decode("utf-8").splitlines()
                if line
            )
            reserved_events = [
                event
                for event in events
                if event.event_type == "MODEL_CALL_RESERVED"
                and event.payload.get("reservation", {}).get("call_id") == call_id
            ]
            unknown_events = [
                event
                for event in events
                if event.event_type == "MODEL_CALL_UNKNOWN"
                and event.payload.get("call_id") == call_id
            ]
            attempt = CampaignBudgetLedger(ledger_path).attempt("hard-exit-agent", call_id)
            response_artifact_absent = all(
                MODEL_RESPONSE_CRASH_RESPONSE_ID.encode() not in path.read_bytes()
                for path in artifact_root.glob("*/*")
                if path.is_file()
            )
            if (
                evidence.get("observed_exit_code") != MODEL_RESPONSE_CRASH_EXIT_CODE
                or marker_path.read_text(encoding="utf-8") != client_trace_id
                or replayed.unknown_model_calls != {call_id}
                or replayed.unknown_reservations != {call_id}
                or replayed.model_calls
                or replayed.lease_id is not None
                or attempt.status != "unknown"
                or attempt.error_type != "RecoveryUncertainDispatch"
                or not response_artifact_absent
                or evidence.get("response_artifact_absent") is not True
                or len(reserved_events) != 1
                or len(unknown_events) != 1
                or not reserved_events[0].seq < unknown_events[0].seq
                or result.actual_process_crash_observed is not True
                or result.trace_replay_verified is not True
            ):
                raise ValueError(
                    "Model-response crash evidence does not prove conservative no-replay blocking"
                )
            continue

        if isinstance(case, PromotionHardCrashEvalCase):
            if evidence.get("trace_path") != result.evidence_path:
                raise ValueError("Promotion crash Trace path does not match its case result")
            trace_path = _evidence_path(root, evidence.get("trace_path"), "promotion Trace")
            final_state_path = _evidence_path(
                root,
                evidence.get("final_state_path"),
                "promotion final state",
            )
            marker_path = _evidence_path(
                root,
                evidence.get("marker_path"),
                "promotion effect marker",
            )
            artifact_root = _evidence_path(
                root,
                evidence.get("promotion_artifact_root_path"),
                "promotion Artifact root",
            )
            source = _evidence_path(root, evidence.get("source_path"), "promotion source")
            source_file = _evidence_path(
                root,
                evidence.get("source_file_path"),
                "promoted source file",
            )
            candidate = _evidence_path(
                root,
                evidence.get("candidate_path"),
                "promotion candidate",
            )
            candidate_file = _evidence_path(
                root,
                evidence.get("candidate_file_path"),
                "promotion candidate file",
            )
            trace_content = trace_path.read_bytes()
            final_state_content = final_state_path.read_bytes()
            marker_content = marker_path.read_bytes()
            if (
                _sha256(trace_content) != evidence.get("trace_sha256")
                or _sha256(final_state_content) != evidence.get("final_state_sha256")
                or _sha256(marker_content) != evidence.get("marker_sha256")
                or _sha256(source_file.read_bytes()) != evidence.get("source_file_sha256")
                or _sha256(candidate_file.read_bytes()) != evidence.get("candidate_file_sha256")
            ):
                raise ValueError("Promotion crash evidence file hash mismatch")
            replayed = SQLiteEventStore.replay_jsonl(trace_content.decode("utf-8"))
            expected_final = (canonical_json(replayed.as_dict()) + "\n").encode("utf-8")
            if final_state_content != expected_final:
                raise ValueError("Promotion crash final state does not match Trace replay")
            if replayed.promotion_intent is None or replayed.promotion_receipt is None:
                raise ValueError("Promotion crash Trace has no settled promotion")
            intent = replayed.promotion_intent
            receipt = replayed.promotion_receipt
            marker = json.loads(marker_content)
            events = tuple(
                Event.model_validate_json(line)
                for line in trace_content.decode("utf-8").splitlines()
                if line
            )
            reserved_events = [
                event
                for event in events
                if event.event_type == "PROMOTION_RESERVED"
                and event.payload.get("intent", {}).get("promotion_id") == intent.promotion_id
            ]
            settled_events = [
                event
                for event in events
                if event.event_type == "PROMOTION_SETTLED"
                and event.payload.get("receipt", {}).get("promotion_id") == intent.promotion_id
            ]
            promotion_snapshots = SnapshotManager(ArtifactStore(artifact_root))
            current_source, current_source_manifest = promotion_snapshots.capture(
                source,
                denied_paths=replayed.task.constraints.denied_paths,
            )
            current_candidate, current_candidate_manifest = promotion_snapshots.capture(
                candidate,
                denied_paths=replayed.task.constraints.denied_paths,
            )
            target_marker = marker.get("targets", [{}])[0] if marker.get("targets") else {}
            source_stat = source_file.stat(follow_symlinks=False)
            if (
                evidence.get("observed_exit_code") != PROMOTION_CRASH_EXIT_CODE
                or marker.get("promotion_id") != intent.promotion_id
                or marker.get("plan_hash") != intent.plan.sha256
                or marker.get("source_revision_after") != intent.plan.candidate_revision
                or marker.get("source_manifest_ref_after") != receipt.source_manifest_ref_after
                or marker.get("already_applied_before_worker") is not False
                or target_marker.get("path") != "src/parser.py"
                or target_marker.get("sha256") != intent.plan.changes[0].after_sha256
                or target_marker.get("device") != source_stat.st_dev
                or target_marker.get("inode") != source_stat.st_ino
                or target_marker.get("mtime_ns") != source_stat.st_mtime_ns
                or current_source.workspace_revision != intent.plan.candidate_revision
                or current_source_manifest != receipt.source_manifest_ref_after
                or current_candidate.workspace_revision != intent.plan.candidate_revision
                or current_candidate_manifest != intent.plan.candidate_manifest_ref
                or receipt.recovered_after_crash is not True
                or len(reserved_events) != 1
                or len(settled_events) != 1
                or not reserved_events[0].seq < settled_events[0].seq
                or evidence.get("target_unchanged_during_recovery") is not True
                or result.recovery_redispatch_count != 0
                or result.duplicate_side_effect_count != 0
                or result.actual_process_crash_observed is not True
                or result.trace_replay_verified is not True
            ):
                raise ValueError(
                    "Promotion crash evidence does not prove exact effect recovery without rewrite"
                )
            continue

        if (
            evidence.get("tool") != case.tool
            or evidence.get("injected_state") != case.injected_state
            or evidence.get("observed_state") != result.observed_state
            or evidence.get("decision") != case.decision
        ):
            raise ValueError(f"Write-state evidence input mismatch: {case.case_id}")
        for key in ("pre_manifest_ref", "observed_manifest_ref", "final_manifest_ref"):
            value = evidence.get(key)
            if not isinstance(value, str):
                raise ValueError(f"Write-state evidence is missing {key}: {case.case_id}")
            snapshots.verify(value)
        relative = evidence.get("workspace_path")
        if not isinstance(relative, str):
            raise ValueError(f"Write-state evidence has no workspace path: {case.case_id}")
        workspace = (root / relative).resolve(strict=True)
        if not workspace.is_relative_to(root):
            raise ValueError("Write-state workspace escapes the matrix directory")
        current, _ = _current_snapshot(workspace, snapshots)
        if current.workspace_revision != evidence.get("final_revision"):
            raise ValueError(f"Write-state workspace no longer matches evidence: {case.case_id}")
    return report
