from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.portfolio_demo import (
    PortfolioDemoRunner,
    verify_portfolio_evidence_pack,
)
from horizon.domain.common import canonical_json
from horizon.domain.errors import HorizonError
from horizon.domain.plan import WorkItem
from horizon.domain.recovery import write_recovery_verdict
from horizon.domain.recovery_evaluation import (
    HardCrashRecoveryEvalCase,
    RecoveryMatrixCaseResult,
    RecoveryMatrixManifest,
    RecoveryMatrixReport,
    WriteRecoveryEvalCase,
)
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
            max_wall_time_seconds=60,
            max_cost_usd="0.01",
        ),
        task_kind="tests",
        execution_mode="workspace_write",
        authority_scope="workspace_write",
    )
    work_item = WorkItem(
        work_item_id="recover-write",
        title="Recover one bounded write",
        objective="Use exact pre/current/expected revisions to choose a safe disposition.",
        expected_artifacts=("content-addressed recovery evidence",),
        acceptance_ids=("fixture",),
        allowed_tools=("replace_text", "apply_patch", "create_file"),
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
            else:
                results.append(self._write_state_case(case, case_root, output_dir, artifacts))

        expected_auto = [case for case in results if case.expected_outcome == "auto_recovered"]
        expected_block = [case for case in results if case.expected_outcome == "safely_blocked"]
        passed_count = sum(case.passed for case in results)
        report = RecoveryMatrixReport(
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
    if report.benchmark_id != manifest.benchmark_id or report.manifest_digest != manifest.sha256:
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
            pack_path = (root / relative).resolve(strict=True)
            if not pack_path.is_relative_to(root):
                raise ValueError("Hard-crash EvidencePack escapes the matrix directory")
            pack_content = pack_path.read_bytes()
            if _sha256(pack_content) != evidence.get("evidence_pack_sha256"):
                raise ValueError("Hard-crash EvidencePack hash does not match matrix evidence")
            pack = verify_portfolio_evidence_pack(pack_path)
            if not pack.all_checks_passed or result.trace_replay_verified is not True:
                raise ValueError("Hard-crash EvidencePack does not prove a replayable recovery")
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
