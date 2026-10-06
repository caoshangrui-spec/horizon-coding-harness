from __future__ import annotations

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
from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.common import canonical_json
from horizon.domain.human import HumanGuidanceRequest
from horizon.domain.model import CampaignBudget, PriceCard
from horizon.domain.ports import AcceptanceExecutorPort
from horizon.domain.promotion import WorkspaceOrigin
from horizon.domain.run import projection_hash
from horizon.domain.run_evaluation import (
    RunABArm,
    RunABArmResult,
    RunABEvalManifest,
    RunABEvalReport,
    RunABSuiteCaseResult,
    RunABSuiteManifest,
    RunABSuiteReport,
)
from horizon.domain.states import RunStatus
from horizon.domain.task import TaskSpec
from horizon.tools.gateway import WorkspaceToolGateway

_OFFLINE_PRICE = PriceCard(
    currency="CNY",
    input_per_million="3.00",
    cached_input_per_million="0.30",
    output_per_million="9.00",
    version="offline-scripted-v1",
    source_url="https://offline.invalid/horizon-scripted-price",
)


class RunABEvaluator:
    """Execute two frozen Agent Runs against isolated copies of one fixture repository."""

    def __init__(
        self,
        acceptance: AcceptanceExecutorPort,
        *,
        validation_backend: str,
        validation_backend_ref: str,
        repository_code_executed: bool,
    ):
        self.acceptance = acceptance
        self.validation_backend = validation_backend
        self.validation_backend_ref = validation_backend_ref
        self.repository_code_executed = repository_code_executed

    def evaluate(
        self,
        manifest: RunABEvalManifest,
        *,
        fixture_source: Path,
        state_dir: Path,
    ) -> RunABEvalReport:
        fixture_source = fixture_source.resolve(strict=True)
        if not fixture_source.is_dir():
            raise ValueError("Run A/B fixture must be a directory")
        state_dir = state_dir.resolve()
        if state_dir == fixture_source or state_dir.is_relative_to(fixture_source):
            raise ValueError("Run A/B state directory cannot live inside the fixture repository")
        state_dir.mkdir(parents=True, exist_ok=True)
        artifacts = ArtifactStore(state_dir / "artifacts")
        snapshots = SnapshotManager(artifacts)
        fixture_snapshot, fixture_manifest_ref = snapshots.capture(
            fixture_source,
            allowed_paths=manifest.task.constraints.allowed_paths,
            denied_paths=manifest.task.constraints.denied_paths,
        )
        evaluation_id = f"eval-{uuid4().hex}"
        evaluation_root = state_dir / "runs" / evaluation_id
        evaluation_root.mkdir(parents=True, exist_ok=False)
        staging_root = state_dir / "staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        initial_workspace = staging_root / f"{evaluation_id}-initial"
        snapshots.restore(fixture_manifest_ref, initial_workspace)
        initial_validation = tuple(
            self.acceptance.execute(initial_workspace, check) for check in manifest.task.acceptance
        )
        initial_failed_checks = tuple(
            sorted(result.check_id for result in initial_validation if not result.passed)
        )
        initial_matches = initial_failed_checks == tuple(
            sorted(manifest.expected_initial_failed_checks)
        )

        results: dict[str, RunABArmResult] = {}
        for arm in manifest.arms:
            results[arm.role] = self._run_arm(
                manifest,
                arm,
                evaluation_id=evaluation_id,
                evaluation_root=evaluation_root,
                staging_root=staging_root,
                fixture_source=fixture_source,
                fixture_revision=fixture_snapshot.workspace_revision,
                fixture_manifest_ref=fixture_manifest_ref,
                artifacts=artifacts,
                snapshots=snapshots,
            )

        baseline = results["baseline"]
        treatment = results["single_replan"]
        baseline_success = baseline.status == RunStatus.SUCCEEDED
        treatment_success = treatment.status == RunStatus.SUCCEEDED
        return RunABEvalReport(
            benchmark_id=manifest.benchmark_id,
            evaluation_id=evaluation_id,
            manifest_digest=manifest.sha256,
            fixture_revision=fixture_snapshot.workspace_revision,
            fixture_manifest_ref=fixture_manifest_ref,
            validation_backend=self.validation_backend,
            validation_backend_ref=self.validation_backend_ref,
            initial_validation=initial_validation,
            expected_initial_failed_checks=tuple(sorted(manifest.expected_initial_failed_checks)),
            initial_failed_checks=initial_failed_checks,
            initial_validation_matches_expectation=initial_matches,
            baseline=baseline,
            single_replan=treatment,
            all_expectations_met=initial_matches and baseline.passed and treatment.passed,
            treatment_recovered=not baseline_success and treatment_success,
            success_delta=int(treatment_success) - int(baseline_success),
            model_call_delta=treatment.usage.model_calls - baseline.usage.model_calls,
            tool_call_delta=treatment.usage.tool_calls - baseline.usage.tool_calls,
            step_delta=treatment.usage.steps - baseline.usage.steps,
            model_cost_delta=treatment.model_cost - baseline.model_cost,
            repository_code_executed=self.repository_code_executed,
        )

    def _run_arm(
        self,
        manifest: RunABEvalManifest,
        arm: RunABArm,
        *,
        evaluation_id: str,
        evaluation_root: Path,
        staging_root: Path,
        fixture_source: Path,
        fixture_revision: str,
        fixture_manifest_ref: str,
        artifacts: ArtifactStore,
        snapshots: SnapshotManager,
    ) -> RunABArmResult:
        workspace = staging_root / f"{evaluation_id}-{arm.arm_id}"
        snapshots.restore(fixture_manifest_ref, workspace)
        task_data = manifest.task.model_dump(mode="json")
        task_data["task_id"] = (
            f"{manifest.task.task_id[:60]}-{arm.arm_id[:30]}-{evaluation_id[-8:]}"
        )
        task_data["repository"] = {
            "source": "local",
            "path": str(workspace),
            "base_commit": fixture_revision,
        }
        task = TaskSpec.model_validate(task_data)
        manifest.initial_plan.check_task(task)

        arm_root = evaluation_root / arm.arm_id
        arm_root.mkdir(parents=True, exist_ok=False)
        store = SQLiteEventStore(arm_root / "control.sqlite3")
        service = HarnessService(store)
        run = store.create(task, "create")
        run = service.bind_workspace_origin(
            run.run_id,
            WorkspaceOrigin(
                source_path_hash=workspace_path_hash(fixture_source),
                source_revision=fixture_revision,
                source_manifest_ref=fixture_manifest_ref,
            ),
            "workspace-origin",
        )
        run = service.set_plan(run.run_id, manifest.initial_plan, "plan")
        run = service.acquire_lease(run.run_id, f"worker-{arm.arm_id}", "lease", ttl_seconds=600)
        token = LeaseToken.from_run(run)
        service.transition(run.run_id, RunStatus.RUNNING, token, "start")

        campaign = CampaignBudget(
            campaign_id=f"{manifest.benchmark_id[:50]}-{arm.arm_id[:30]}-{evaluation_id[-8:]}",
            currency="CNY",
            max_cost="10.00",
            max_cost_per_call="1.00",
        )
        runner_config = AgentLoopConfig(
            max_model_iterations=len(arm.actions),
            max_output_tokens=256,
            max_run_cost=Decimal("10.00"),
        )

        active_artifacts = artifacts
        active_snapshots = snapshots
        models: list[ScriptedModelGateway] = []

        def build_runner(
            active_service: HarnessService,
            active_model: ScriptedModelGateway,
            active_store: ArtifactStore,
            active_snapshot_manager: SnapshotManager,
        ) -> CodingAgentRunner:
            tools = WorkspaceToolGateway(
                workspace,
                task,
                manifest.initial_plan.ready_items(set())[0],
                active_snapshot_manager,
                self.acceptance,
                SQLiteCodeRetriever(
                    arm_root / "retrieval.sqlite3",
                    active_snapshot_manager,
                ),
            )
            return CodingAgentRunner(
                active_service,
                active_model,
                CampaignBudgetLedger(arm_root / "campaign.sqlite3"),
                tools,
                active_store,
                provider_id="offline-scripted",
                model_id="scripted-model-v1",
                pricing=_OFFLINE_PRICE,
                campaign=campaign,
                config=runner_config,
            )

        restart_points = arm.restart_points
        if not restart_points:
            model = ScriptedModelGateway(arm.arm_id, arm.actions)
            models.append(model)
            result = build_runner(service, model, active_artifacts, active_snapshots).run(
                run.run_id,
                token,
            )
            worker_restarts = 0
        else:
            segment_starts = (0, *restart_points)
            segment_ends = (*restart_points, len(arm.actions))
            result = None
            for segment_index, (start, end) in enumerate(
                zip(segment_starts, segment_ends, strict=True)
            ):
                model = ScriptedModelGateway(
                    arm.arm_id,
                    arm.actions[start:end],
                    start_index=start,
                )
                models.append(model)
                runner = build_runner(service, model, active_artifacts, active_snapshots)
                if end == len(arm.actions):
                    result = runner.run(run.run_id, token)
                    break

                partial = runner.run(
                    run.run_id,
                    token,
                    max_iterations_this_invocation=end - start,
                )
                if (
                    partial.status != RunStatus.RUNNING
                    or partial.agent_session is None
                    or not partial.passed_items
                    or partial.agent_session.work_item_id in partial.passed_items
                    or not set(
                        next(
                            item.dependencies
                            for item in partial.plan.items
                            if item.work_item_id == partial.agent_session.work_item_id
                        )
                    )
                    <= partial.passed_items
                ):
                    raise ValueError(
                        "Configured worker restart did not land on a persisted WorkItem boundary"
                    )

                restart_number = segment_index + 1
                release_key = "release-for-scripted-worker-restart"
                lease_key = "lease-after-scripted-worker-restart"
                if restart_number > 1:
                    release_key = f"{release_key}-{restart_number}"
                    lease_key = f"{lease_key}-{restart_number}"
                service.release_lease(run.run_id, token, release_key)

                # Reopen every worker-owned durable adapter. The Docker acceptance backend remains
                # external to the worker, just as a surviving sandbox would in production.
                store = SQLiteEventStore(arm_root / "control.sqlite3")
                service = HarnessService(store)
                leased = service.acquire_lease(
                    run.run_id,
                    f"worker-{arm.arm_id}-restart-{restart_number}",
                    lease_key,
                    ttl_seconds=600,
                )
                token = LeaseToken.from_run(leased)
                active_artifacts = ArtifactStore(artifacts.root)
                active_snapshots = SnapshotManager(active_artifacts)

            if result is None:
                raise ValueError("Worker restart evaluation did not execute its final segment")
            worker_restarts = len(restart_points)

        trace = store.export_jsonl(run.run_id)
        trace_ref = active_artifacts.put(trace.encode("utf-8"))
        replayed = SQLiteEventStore.replay_jsonl(trace)
        replay_verified = replayed.as_dict() == result.as_dict() and projection_hash(
            replayed
        ) == projection_hash(result)
        current_snapshot, current_manifest_ref = active_snapshots.capture(
            workspace,
            allowed_paths=task.constraints.allowed_paths,
            denied_paths=task.constraints.denied_paths,
        )
        source_after, _ = active_snapshots.capture(
            fixture_source,
            allowed_paths=task.constraints.allowed_paths,
            denied_paths=task.constraints.denied_paths,
        )
        source_unchanged = source_after.workspace_revision == fixture_revision
        human_pattern = (
            result.pending_human_request.pattern
            if isinstance(result.pending_human_request, HumanGuidanceRequest)
            else None
        )
        bound_workspace_revision = result.workspace_revision
        if bound_workspace_revision is None and result.agent_session is not None:
            bound_workspace_revision = result.agent_session.workspace_revision
        if bound_workspace_revision is None and isinstance(
            result.pending_human_request, HumanGuidanceRequest
        ):
            bound_workspace_revision = result.pending_human_request.workspace_revision
        required = {check.id for check in task.acceptance if check.required}
        final_validation_passed = (
            required <= set(result.validation["passed_check_ids"])
            if result.validation is not None
            else None
        )

        failures: list[str] = []
        expectations = (
            (result.status == arm.expected_status, "status"),
            (
                result.plan is not None and result.plan.version == arm.expected_plan_version,
                "plan_version",
            ),
            (len(result.execution_replans) == arm.expected_execution_replans, "replan_count"),
            (
                tuple(sorted(result.passed_items)) == tuple(sorted(arm.expected_passed_items)),
                "passed_items",
            ),
            (human_pattern == arm.expected_human_request_pattern, "human_request_pattern"),
            (result.usage.model_calls == arm.expected_model_calls, "model_calls"),
            (result.usage.tool_calls == arm.expected_tool_calls, "tool_calls"),
            (result.usage.steps == arm.expected_steps, "steps"),
            (all(model.consumed for model in models), "scripted_actions_consumed"),
            (
                result.lease_epoch == 1 + worker_restarts,
                "worker_restart_lease_epoch",
            ),
            (replay_verified, "trace_replay"),
            (source_unchanged, "source_workspace_unchanged"),
            (
                current_snapshot.workspace_revision == bound_workspace_revision,
                "workspace_revision",
            ),
            (not result.unknown_model_calls, "unknown_model_calls"),
            (not result.unknown_tool_calls, "unknown_tool_calls"),
            (not result.reservations, "open_reservations"),
        )
        failures.extend(name for passed, name in expectations if not passed)
        return RunABArmResult(
            arm_id=arm.arm_id,
            role=arm.role,
            run_id=result.run_id,
            status=result.status,
            plan_version=result.plan.version if result.plan is not None else 1,
            execution_replans=len(result.execution_replans),
            passed_items=tuple(sorted(result.passed_items)),
            human_request_pattern=human_pattern,
            usage=result.usage,
            model_cost=result.model_occupied_cost,
            model_currency=_OFFLINE_PRICE.currency,
            event_count=result.seq,
            worker_restarts=worker_restarts,
            final_lease_epoch=result.lease_epoch,
            trace_ref=trace_ref,
            projection_hash=projection_hash(result),
            workspace_revision=current_snapshot.workspace_revision,
            workspace_manifest_ref=current_manifest_ref,
            final_validation_passed=final_validation_passed,
            actions_consumed=all(model.consumed for model in models),
            trace_replay_verified=replay_verified,
            source_workspace_unchanged=source_unchanged,
            unknown_model_calls=len(result.unknown_model_calls),
            unknown_tool_calls=len(result.unknown_tool_calls),
            open_reservations=len(result.reservations),
            expectation_failures=tuple(failures),
            passed=not failures,
        )


def store_run_ab_report(artifacts: ArtifactStore, report: RunABEvalReport) -> str:
    return artifacts.put(canonical_json(report).encode("utf-8"))


def build_run_ab_suite_report(
    manifest: RunABSuiteManifest,
    reports: dict[str, tuple[RunABEvalReport, str]],
    *,
    validation_backend: str,
    validation_backend_ref: str,
    repository_code_executed: bool,
) -> RunABSuiteReport:
    expected_ids = {case.case_id for case in manifest.cases}
    if set(reports) != expected_ids:
        raise ValueError("Run A/B suite reports must match the manifest cases exactly")

    cases: list[RunABSuiteCaseResult] = []
    for case in manifest.cases:
        report, report_ref = reports[case.case_id]
        if report.paid_model_called or report.network_called:
            raise ValueError("Offline Run A/B suite cannot include external model or network calls")
        if report.validation_backend != validation_backend:
            raise ValueError("Run A/B suite cases must use one validation backend")
        if report.validation_backend_ref != validation_backend_ref:
            raise ValueError("Run A/B suite cases must use one validation backend reference")
        if report.repository_code_executed != repository_code_executed:
            raise ValueError(
                "Run A/B suite cases must agree on whether repository code was executed"
            )
        cases.append(
            RunABSuiteCaseResult(
                case_id=case.case_id,
                manifest_path=case.manifest_path,
                source=case.source,
                manifest_digest=report.manifest_digest,
                fixture_revision=report.fixture_revision,
                report_ref=report_ref,
                passed=report.all_expectations_met,
                initial_failure_confirmed=report.initial_validation_matches_expectation,
                treatment_recovered=report.treatment_recovered,
                baseline_status=report.baseline.status,
                single_replan_status=report.single_replan.status,
                model_call_delta=report.model_call_delta,
                tool_call_delta=report.tool_call_delta,
                step_delta=report.step_delta,
                model_cost_delta=report.model_cost_delta,
            )
        )

    case_results = tuple(cases)
    return RunABSuiteReport(
        suite_id=manifest.suite_id,
        suite_manifest_digest=manifest.sha256,
        validation_backend=validation_backend,
        validation_backend_ref=validation_backend_ref,
        cases=case_results,
        case_count=len(case_results),
        passed_case_count=sum(case.passed for case in case_results),
        initial_failure_confirmed_count=sum(
            case.initial_failure_confirmed for case in case_results
        ),
        treatment_recovered_count=sum(case.treatment_recovered for case in case_results),
        success_delta=sum(
            int(case.single_replan_status == RunStatus.SUCCEEDED)
            - int(case.baseline_status == RunStatus.SUCCEEDED)
            for case in case_results
        ),
        model_call_delta=sum(case.model_call_delta for case in case_results),
        tool_call_delta=sum(case.tool_call_delta for case in case_results),
        step_delta=sum(case.step_delta for case in case_results),
        model_cost_delta=sum((case.model_cost_delta for case in case_results), start=Decimal("0")),
        all_expectations_met=all(case.passed for case in case_results),
        repository_code_executed=repository_code_executed,
    )


def store_run_ab_suite_report(artifacts: ArtifactStore, report: RunABSuiteReport) -> str:
    return artifacts.put(canonical_json(report).encode("utf-8"))
