import hashlib
import subprocess
from decimal import Decimal
from pathlib import Path

import yaml

from horizon.adapters.model.config import load_provider_config
from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.workspace.promotion import workspace_path_hash
from horizon.application.pilot import PilotPreflightService
from horizon.application.services import HarnessService, LeaseToken
from horizon.application.smoke_aggregate import aggregate_real_model_smoke_results
from horizon.application.smoke_result import evaluate_real_model_smoke_run
from horizon.application.terminal_evidence import export_terminal_evidence_pack
from horizon.domain.budget import Usage
from horizon.domain.common import canonical_json, digest
from horizon.domain.errors import BudgetStop, BudgetStopReason
from horizon.domain.model import (
    CampaignSummary,
    FunctionCall,
    ModelCallRecord,
    ModelCallReservation,
    ModelMessage,
    ModelPolicyBinding,
    ModelResponse,
    ModelUsage,
    ToolCall,
)
from horizon.domain.pilot import RealModelPilotManifest
from horizon.domain.plan import Plan, WorkItem
from horizon.domain.promotion import WorkspaceOrigin
from horizon.domain.retrieval import EvidenceChunk, EvidencePack
from horizon.domain.smoke_evaluation import (
    RealModelSmokeEvaluatorManifest,
    RealModelSmokeSuite,
)
from horizon.domain.states import RunStatus
from horizon.domain.task import TaskSpec
from horizon.domain.tools import AcceptanceResult, ToolCallRecord, ToolCallReservation

ROOT = Path(__file__).resolve().parents[2]
SUITE_PATH = ROOT / "benchmarks" / "real-model-smoke-v1.yaml"
PROVIDER_PATH = ROOT / "config" / "providers" / "siliconflow-pilot.yaml"


class _FailingAcceptance:
    def execute(self, workspace, check):
        output = "expected repaired behavior"
        return AcceptanceResult(
            check_id=check.id,
            passed=False,
            exit_code=1,
            timed_out=False,
            output=output,
            output_hash=hashlib.sha256(output.encode()).hexdigest(),
        )


def _git_source(path: Path) -> str:
    path.mkdir()
    (path / "module.py").write_text("VALUE = 'buggy'\n", encoding="utf-8")
    for arguments in (
        ("init",),
        ("config", "user.email", "smoke@example.invalid"),
        ("config", "user.name", "Horizon Smoke"),
        ("add", "module.py"),
        ("commit", "-m", "buggy fixture"),
    ):
        completed = subprocess.run(
            ["git", "-C", str(path), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _suite_and_evaluator():
    data = yaml.safe_load(SUITE_PATH.read_text(encoding="utf-8"))
    data["tasks"][0]["task_id"] = "fixture-smoke"
    data["tasks"][0]["allowed_path_patterns"] = ["module.py"]
    suite = RealModelSmokeSuite.model_validate(data)
    evaluator = RealModelSmokeEvaluatorManifest.model_validate(
        {
            "evaluator_id": "fixture-smoke-evaluator",
            "suite_id": suite.suite_id,
            "suite_digest": suite.sha256,
            "model_visible": False,
            "tasks": [
                {
                    "smoke_task_id": task.task_id,
                    "pilot_id": (
                        "fixture-pilot"
                        if task.task_id == "fixture-smoke"
                        else f"{task.task_id}-pilot"
                    ),
                    "target_locations": [
                        {
                            "location_id": f"{task.task_id}-target",
                            "path": (
                                "module.py"
                                if task.task_id == "fixture-smoke"
                                else task.allowed_path_patterns[0].replace("/**", "/target.py")
                            ),
                            "start_line": 1,
                            "end_line": 1,
                        }
                    ],
                }
                for task in suite.tasks
            ],
        }
    )
    return suite, evaluator


def test_smoke_result_derives_budget_stop_without_calling_a_model(tmp_path):
    source = tmp_path / "source"
    head = _git_source(source)
    provider = load_provider_config(PROVIDER_PATH)
    task = TaskSpec.model_validate(
        {
            "task_id": "fixture-prepared",
            "title": "Repair fixture behavior",
            "objective": "Change the buggy value without changing public behavior",
            "repository": {
                "source": "local",
                "path": "/pilot-source",
                "base_commit": head,
            },
            "constraints": {
                "allowed_paths": ["module.py"],
                "allowed_tools": [
                    "read_file",
                    "retrieve_code",
                    "replace_text",
                    "run_check",
                ],
                "network": "deny",
            },
            "acceptance": [
                {
                    "id": "behavior",
                    "command": "python -c pass",
                    "required": True,
                }
            ],
            "budgets": {
                "max_steps": 24,
                "max_model_calls": 14,
                "max_tool_calls": 24,
                "max_wall_time_seconds": 900,
                "max_cost_usd": "0.25",
                "max_input_tokens": 120000,
                "max_output_tokens": 4096,
                "max_repair_cycles": 2,
            },
            "task_kind": "bugfix",
            "execution_mode": "workspace_write",
            "authority_scope": "workspace_write",
            "model_policy_id": provider.policy_id,
        }
    )
    fixed = "b" * 40
    manifest = RealModelPilotManifest.model_validate(
        {
            "pilot_id": "fixture-pilot",
            "source_path": "source",
            "source": {
                "benchmark": "FixtureBench",
                "project": "fixture",
                "bug_id": "1",
                "repository_url": "https://github.com/example/fixture",
                "buggy_commit": head,
                "fixed_commit": fixed,
                "fix_url": f"https://github.com/example/fixture/commit/{fixed}",
                "upstream_test": "tests/test_behavior.py::test_behavior",
                "license_spdx": "MIT",
                "license_url": f"https://github.com/example/fixture/blob/{head}/LICENSE",
                "reduction": "full_checkout",
                "reduction_note": "A complete one-file smoke result fixture.",
            },
            "task": task.model_dump(mode="json"),
            "provider_policy_id": provider.policy_id,
            "currency": "CNY",
            "max_paid_cost": "0.25",
            "expected_initial_failed_checks": ["behavior"],
            "solution_isolation": {"forbidden_task_literals": ["VALUE = 'fixed'"]},
        }
    )
    campaign = CampaignSummary(
        campaign_id=provider.campaign.campaign_id,
        currency="CNY",
        max_cost="3.00",
        settled_cost="0",
        reserved_cost="0",
        unknown_cost="0",
        occupied_cost="0",
        remaining_cost="3.00",
    )
    preflight = PilotPreflightService(
        _FailingAcceptance(),
        validation_backend_ref=f"sha256:{'c' * 64}",
    ).evaluate(
        manifest,
        source=source,
        state_dir=tmp_path / "preflight",
        provider=provider,
        campaign=campaign,
    )
    assert preflight.report.ready is True
    prepared = TaskSpec.model_validate(
        yaml.safe_load(preflight.prepared_task_path.read_text(encoding="utf-8"))
    )

    runtime_data = prepared.model_dump(mode="json")
    runtime_data["task_id"] = f"{prepared.task_id}-deadbeef"
    runtime_data["repository"] = {
        "source": "local",
        "path": str((tmp_path / "staging").resolve()),
        "base_commit": preflight.report.source_snapshot_revision,
    }
    runtime_task = TaskSpec.model_validate(runtime_data)
    store = SQLiteEventStore(tmp_path / "control.sqlite3")
    service = HarnessService(store)
    run = store.create(runtime_task, "create-smoke-result")
    run = service.bind_workspace_origin(
        run.run_id,
        WorkspaceOrigin(
            source_path_hash=workspace_path_hash(source),
            source_revision=preflight.report.source_snapshot_revision,
            source_manifest_ref=preflight.report.source_manifest_ref,
            git_head=head,
        ),
        "bind-smoke-source",
    )
    run = service.set_plan(
        run.run_id,
        Plan(
            items=(
                WorkItem(
                    work_item_id="repair",
                    title="Repair fixture",
                    objective="Locate and repair the fixture value",
                    expected_artifacts=("patch",),
                    acceptance_ids=("behavior",),
                    allowed_tools=("retrieve_code",),
                ),
            )
        ),
        "plan-smoke-result",
    )
    leased = service.acquire_lease(run.run_id, "worker", "lease-smoke-result")
    token = LeaseToken.from_run(leased)
    service.transition(run.run_id, RunStatus.RUNNING, token, "start-smoke-result")
    service.bind_model_policy(
        run.run_id,
        ModelPolicyBinding(
            policy_id=provider.policy_id,
            provider_id=provider.provider_id,
            model=provider.model.id,
            campaign_id=provider.campaign.campaign_id,
            currency=provider.pricing.currency,
            max_run_cost=provider.run_budget.max_cost,
            price_card_hash=digest(provider.pricing),
        ),
        token,
        "bind-smoke-model",
    )
    run_artifacts = ArtifactStore(tmp_path / "run-artifacts")
    retrieval_arguments = {"query": "buggy value", "max_chunks": 1}
    model_usage = ModelUsage(input_tokens=10, output_tokens=5)
    model_response = ModelResponse(
        response_id="fixture-response",
        model=provider.model.id,
        message=ModelMessage(
            role="assistant",
            tool_calls=(
                ToolCall(
                    id="provider-retrieval-call",
                    function=FunctionCall(
                        name="retrieve_code",
                        arguments=retrieval_arguments,
                    ),
                ),
            ),
        ),
        finish_reason="tool_calls",
        usage=model_usage,
        provider_trace_id="fixture-provider-trace",
    )
    response_ref = run_artifacts.put(
        canonical_json(model_response.model_dump(mode="json")).encode("utf-8")
    )
    model_reservation = ModelCallReservation(
        call_id="fixture-model-call",
        request_hash="d" * 64,
        provider_id=provider.provider_id,
        model=provider.model.id,
        currency="CNY",
        reserved_cost=Decimal("0.01"),
    )
    service.reserve_model_call(
        run.run_id,
        model_reservation,
        Usage(model_calls=1, input_tokens=10, output_tokens=5),
        token,
        "reserve-smoke-model",
    )
    service.settle_model_call(
        run.run_id,
        ModelCallRecord(
            call_id=model_reservation.call_id,
            request_hash=model_reservation.request_hash,
            provider_id=provider.provider_id,
            model=provider.model.id,
            currency="CNY",
            estimated_cost=Decimal("0.001"),
            response_id=model_response.response_id,
            response_artifact_ref=response_ref,
            provider_trace_id=model_response.provider_trace_id,
            finish_reason=model_response.finish_reason,
            usage=model_usage,
        ),
        Usage(model_calls=1, input_tokens=10, output_tokens=5),
        token,
        "settle-smoke-model",
    )
    retrieval = EvidencePack(
        query="buggy value",
        normalized_terms=("buggy", "value"),
        workspace_revision=preflight.report.source_snapshot_revision,
        source_manifest_ref=preflight.report.source_manifest_ref,
        scope_hash="e" * 64,
        index_key="f" * 64,
        backend="sqlite_fts5",
        status="ok",
        indexed_file_count=1,
        skipped_file_count=0,
        chunks=(
            EvidenceChunk(
                rank=1,
                path="module.py",
                start_line=1,
                end_line=1,
                content_hash=hashlib.sha256(b"VALUE = 'buggy'\n").hexdigest(),
                snippet="VALUE = 'buggy'",
            ),
        ),
    )
    retrieval_ref = run_artifacts.put(
        canonical_json(retrieval.model_dump(mode="json")).encode("utf-8")
    )
    tool_reservation = ToolCallReservation(
        call_id="fixture-retrieval-call",
        name="retrieve_code",
        arguments_hash=digest(retrieval_arguments),
        workspace_revision=preflight.report.source_snapshot_revision,
    )
    service.reserve_tool_call(
        run.run_id,
        tool_reservation,
        token,
        "reserve-smoke-retrieval",
    )
    service.settle_tool_call(
        run.run_id,
        ToolCallRecord(
            call_id=tool_reservation.call_id,
            name=tool_reservation.name,
            arguments_hash=tool_reservation.arguments_hash,
            status="success",
            output_hash=retrieval_ref,
            artifact_ref=retrieval_ref,
            workspace_revision_before=preflight.report.source_snapshot_revision,
            workspace_revision_after=preflight.report.source_snapshot_revision,
        ),
        token,
        "settle-smoke-retrieval",
    )
    stopped = service.fail_budget_stop(
        run.run_id,
        BudgetStop(
            reason_code=BudgetStopReason.CAMPAIGN_CALL_COST_LIMIT,
            scope="campaign",
            currency="CNY",
            required_cost="0.30",
            available_cost="0.20",
        ),
        token,
        "stop-smoke-budget",
    )
    assert stopped.status == RunStatus.FAILED
    terminal = export_terminal_evidence_pack(
        store,
        run.run_id,
        tmp_path / "terminal-evidence",
    )
    suite, evaluator = _suite_and_evaluator()

    result = evaluate_real_model_smoke_run(
        suite,
        evaluator,
        "fixture-smoke",
        pilot_preflight_path=preflight.report_path,
        terminal_evidence_pack_path=terminal.evidence_pack_path,
        run_artifact_root=run_artifacts.root,
        output_dir=tmp_path / "smoke-result",
    )

    report = result.report
    assert report.task_success is False
    assert report.correct_location_reached is True
    assert report.valid_edit_produced is False
    assert report.protected_validation_passed is None
    assert report.actual_cost_cny == Decimal("0.001")
    assert report.model_calls == 1
    assert report.successful_retrieval_calls == 1
    assert report.stop_category == "budget_gate"
    assert report.supporting_artifacts_verified is True
    assert report.success_evidence.initial_failure_observed is True
    assert report.success_evidence.source_unchanged is True
    assert report.success_evidence.trace_replay_verified is True
    assert report.success_evidence.no_open_effects is True
    assert result.report_path.is_file()
    assert result.summary_path.is_file()
    assert "Task success: `false`" in result.summary_path.read_text(encoding="utf-8")

    aggregate = aggregate_real_model_smoke_results(
        suite,
        (result.report_path,),
        output_dir=tmp_path / "smoke-aggregate",
    )
    aggregate_report = aggregate.report
    assert aggregate_report.evaluated_task_count == 1
    assert aggregate_report.suite_coverage_rate == 0.2
    assert aggregate_report.task_success_count == 0
    assert aggregate_report.task_success_rate_evaluated == 0
    assert aggregate_report.correct_location.true_count == 1
    assert aggregate_report.valid_edit.false_count == 1
    assert aggregate_report.protected_validation.unknown_count == 1
    assert aggregate_report.total_actual_cost_cny == Decimal("0.001")
    assert aggregate_report.total_model_calls == 1
    assert len(aggregate_report.unevaluated_task_ids) == 4
    assert "Not yet evaluated" in aggregate.summary_path.read_text(encoding="utf-8")
