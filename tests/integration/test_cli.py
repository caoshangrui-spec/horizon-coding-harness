import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import horizon.interfaces.cli.app as cli_app_module
from horizon.adapters.model.config import load_provider_config
from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.sandbox.docker import CommandResult, SandboxAttemptStatus
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.agent_loop import AgentLoopConfig, CodingAgentRunner
from horizon.application.planning import (
    PlanGenerator,
    PlanGeneratorConfig,
    build_planning_context,
)
from horizon.application.recovery import RecoveryService
from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.agent import AgentSession, AgentSessionRecord
from horizon.domain.budget import Usage
from horizon.domain.common import canonical_json, digest
from horizon.domain.errors import BudgetStop, BudgetStopReason, Conflict
from horizon.domain.model import (
    FunctionCall,
    ModelCallRecord,
    ModelCallReservation,
    ModelMessage,
    ModelPolicyBinding,
    ModelResponse,
    ModelUsage,
    ToolCall,
)
from horizon.domain.promotion import WorkspaceOrigin
from horizon.domain.states import RunStatus
from horizon.domain.task import TaskSpec
from horizon.domain.tools import AcceptanceResult, ToolCallReservation
from horizon.interfaces.cli.app import app
from horizon.tools.gateway import WorkspaceToolGateway

runner = CliRunner()
EXAMPLE = Path(__file__).resolve().parents[2] / "examples/task.yaml"
PLAN = EXAMPLE.with_name("plan.yaml")
PROVIDER = EXAMPLE.parents[0].parent / "config/providers/siliconflow.yaml"
AGENT_TASK = EXAMPLE.with_name("agent-task.yaml")
AGENT_PLAN = EXAMPLE.with_name("agent-plan.yaml")


class OneToolModel:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments

    def generate(self, request, trace_id):
        return ModelResponse(
            response_id="cli-write-response",
            model=request.model,
            message=ModelMessage(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        id="cli-write-provider-call",
                        function=FunctionCall(name=self.name, arguments=self.arguments),
                    ),
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(input_tokens=100, output_tokens=20),
            provider_trace_id="cli-write-trace",
        )


class CrashAfterToolEffect:
    def __init__(self, delegate):
        self.delegate = delegate

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def dispatch_safe(self, name, arguments, attempt_id=None):
        self.delegate.dispatch_safe(name, arguments, attempt_id)
        raise RuntimeError("simulated crash after CLI write effect")


class AutoPlanAndFixModel:
    def __init__(self, *, invalid_plan: bool = False, repeat_read: bool = False):
        self.requests = []
        self.execution_index = 0
        self.invalid_plan = invalid_plan
        self.repeat_read = repeat_read

    def generate(self, request, trace_id):
        self.requests.append(request)
        if any(tool.name == "propose_plan" for tool in request.tools):
            name = "propose_plan"
            arguments = {
                "version": 1,
                "items": [
                    {
                        "work_item_id": "fix-parser",
                        "title": "Fix parser",
                        "objective": "Handle empty input and verify it",
                        "dependencies": [],
                        "expected_artifacts": ["src/parser.py"],
                        "acceptance_ids": ["unit"],
                        "allowed_tools": ["read_file", "replace_text", "run_check"],
                    }
                ],
            }
            if self.invalid_plan:
                arguments["items"][0]["allowed_tools"] = ["shell"]
        else:
            if self.repeat_read:
                name, arguments = "read_file", {"path": "src/parser.py"}
            else:
                actions = (
                    (
                        "replace_text",
                        {
                            "path": "src/parser.py",
                            "old": "return [value]",
                            "new": "return [] if value == '' else [value]",
                        },
                    ),
                    ("submit", {"summary": "Parser fix is ready for protected validation."}),
                )
                name, arguments = actions[self.execution_index]
                self.execution_index += 1
        index = len(self.requests)
        return ModelResponse(
            response_id=f"auto-plan-response-{index}",
            model=request.model,
            message=ModelMessage(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        id=f"auto-plan-tool-{index}",
                        function=FunctionCall(name=name, arguments=arguments),
                    ),
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(input_tokens=100, output_tokens=20),
            provider_trace_id=f"auto-plan-trace-{index}",
        )


class LocalParserAcceptance:
    def __init__(self, sandbox):
        self.sandbox = sandbox

    def execute(self, workspace, check):
        content = (workspace / "src/parser.py").read_text(encoding="utf-8")
        passed = "return [] if value == '' else [value]" in content
        output = "1 passed" if passed else "parser fix missing"
        return AcceptanceResult(
            check_id=check.id,
            passed=passed,
            exit_code=0 if passed else 1,
            timed_out=False,
            output=output,
            output_hash=hashlib.sha256(output.encode()).hexdigest(),
        )


def test_cli_prepare_plan_cancel_trace_and_replay(tmp_path):
    db = tmp_path / "db.sqlite3"
    prefix = ["--db", str(db)]
    result = runner.invoke(app, prefix + ["task", "validate", str(EXAMPLE)])
    assert result.exit_code == 0, result.output
    assert not db.exists()
    result = runner.invoke(app, prefix + ["run", str(EXAMPLE), "--prepare-only", "--key", "demo"])
    assert result.exit_code == 0, result.output
    run_id = json.loads(result.stdout)["run_id"]
    result = runner.invoke(app, prefix + ["plan", "set", run_id, str(PLAN)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "READY"
    result = runner.invoke(app, prefix + ["cancel", run_id])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "CANCELLED"
    exported = runner.invoke(app, prefix + ["trace", "export", run_id])
    trace = tmp_path / "trace.jsonl"
    trace.write_text(exported.stdout, encoding="utf-8")
    result = runner.invoke(app, ["trace", "replay", str(trace)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["state"]["status"] == "CANCELLED"
    historical = runner.invoke(app, prefix + ["replay", run_id, "--at", "1"])
    assert json.loads(historical.stdout)["state"]["status"] == "CREATED"


def test_cli_does_not_pretend_to_execute(tmp_path):
    db = tmp_path / "db.sqlite3"
    result = runner.invoke(app, ["--db", str(db), "run", str(EXAMPLE)])
    assert result.exit_code == 2
    assert "not connected" in result.output
    assert not db.exists()


def test_missing_status_does_not_create_database(tmp_path):
    db = tmp_path / "new" / "db.sqlite3"
    result = runner.invoke(app, ["--db", str(db), "status"])
    assert result.exit_code == 2
    assert not db.exists()


def test_doctor_checks_fts_without_model():
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["autonomous_execution"] is True
    assert data["execution_profile"] == "bounded_sequential_work_item_dag"
    assert data["multi_work_item_execution"] is True
    assert data["work_item_scheduler"] == "deterministic_dependency_ready_plan_order"
    assert data["atomic_work_item_handoff"] is True
    assert data["parallel_work_items"] is False
    assert data["automatic_plan_generation"] is True
    assert data["automatic_plan_profile"] == "one_shot_validated_work_item_dag"
    assert data["max_generated_work_items"] == 8
    assert data["planning_response_recovery"] is True
    assert data["planning_human_fallback"] is True
    assert data["planning_human_fallback_profile"] == "invalid_proposal_manual_plan"
    assert data["no_progress_human_guidance"] is True
    assert data["no_progress_human_guidance_profile"] == "bounded_local_session_guidance"
    assert data["general_hitl"] is False
    assert data["automatic_plan_retry"] is False
    assert data["dynamic_replanning"] is True
    assert data["dynamic_replanning_profile"] == "single_atomic_execution_evidence_revision"
    assert data["max_execution_replans"] == 1
    assert data["automatic_replan_trigger"] is False
    assert data["completed_work_items_immutable_during_replan"] is True
    assert data["no_progress_detection"] is True
    assert data["no_progress_profile"] == "exact_action_patterns_same_revision"
    assert data["no_progress_patterns"] == [
        "identical_action",
        "alternating_two_action_cycle",
    ]
    assert data["no_progress_repeat_limit"] == 2
    assert data["no_progress_alternating_cycle_limit"] == 2
    assert data["semantic_no_progress_detection"] is False
    assert data["controller_policy_evaluation"] is True
    assert data["controller_policy_evaluation_profile"] == ("frozen_offline_exact_policy_traces")
    assert data["controller_policy_evaluation_external_calls"] is False
    assert data["full_run_ab_evaluation"] is True
    assert data["full_run_ab_evaluation_profile"] == ("scripted_model_replayable_docker_validation")
    assert data["full_run_ab_paid_model_called"] is False
    assert data["full_run_ab_initial_failure_preflight"] is True
    assert data["source_bound_run_ab_suite"] is True
    assert data["source_bound_run_ab_suite_profile"] == ("external_reduced_and_clean_full_checkout")
    assert data["source_bound_run_ab_reduced_case_count"] == 5
    assert data["source_bound_run_ab_reduced_docker_verified_count"] == 5
    assert data["source_bound_run_ab_full_checkout_gate"] is True
    assert data["source_bound_run_ab_full_checkout_case_count"] == 3
    assert data["source_bound_run_ab_full_checkout_docker_verified_count"] == 3
    assert data["bounded_multi_file_patch"] is True
    assert data["max_patch_files"] == 8
    assert data["patch_recovery"] == "exact_pre_or_expected_effect"
    assert data["safe_turn_continuation"] is True
    assert data["response_receipt_continuation"] is True
    assert data["campaign_only_hold_recovery"] is True
    assert data["run_linked_call_reconciliation"] is True
    assert data["readonly_tool_retry_resolution"] is True
    assert data["replace_tool_accept_rollback"] is True
    assert data["write_tool_recovery"] is True
    assert data["run_check_attempt_identity"] is True
    assert data["run_check_attempt_status_query"] is True
    assert data["run_check_stopped_result_recovery"] is True
    assert data["run_check_stopped_result_recovery_profile"] == (
        "exact_natural_exit_bounded_complete_log"
    )
    assert data["run_check_receipt_before_cleanup"] is True
    assert data["portfolio_hard_crash_demo"] is True
    assert data["portfolio_hard_crash_demo_profile"] == (
        "replace_effect_before_receipt_exact_accept"
    )
    assert data["recovery_matrix_evaluation"] is True
    assert data["recovery_matrix_evaluation_profile"] == (
        "three_real_crashes_plus_exact_write_state_decisions"
    )
    assert data["recovery_matrix_case_count"] == 21
    assert data["complete_fault_injection_matrix"] is False
    assert data["run_check_running_attempt_stop_requires_explicit"] is True
    assert data["run_check_missing_attempt_proof"] is False
    assert data["run_check_signal_timeout_result_recovery"] is False
    assert data["arbitrary_crash_recovery"] is False
    assert data["promotion_enabled"] is True
    assert data["promotion_profile"] == "explicit_bounded_existing_files"
    assert data["max_promotion_files"] == 8
    assert data["partial_promotion_recovery"] is True
    assert data["git_head_binding"] is True
    assert data["deterministic_context_projection"] is True
    assert data["context_projection_artifacts"] is True
    assert data["canonical_transcript_retained"] is True
    assert data["mandatory_fact_ledger"] is True
    assert data["mandatory_fact_artifacts"] is True
    assert data["tool_schema_fact_binding"] is True
    assert data["token_aware_compaction"] is False
    assert data["semantic_compaction"] is False
    assert data["memory_enabled"] is True
    assert data["memory_profile"] == "evidence_backed_run_projection"
    assert data["run_memory_enabled"] is True
    assert data["project_memory_enabled"] is False
    assert data["model_claim_memory_promotion"] is False
    assert data["memory_revision_invalidation"] is True
    assert data["code_rag_enabled"] is True
    assert data["code_rag_profile"] == "revision_bound_lexical"
    assert data["retrieval_fts5_with_scan_fallback"] is True
    assert data["fixed_retrieval_diagnostic"] is True
    assert data["vector_retrieval"] is False
    assert isinstance(data["fts5"], bool)


def test_model_check_is_offline_and_redacts_credential(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("SILICONFLOW_API_KEY=cli-test-secret\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["model", "check", "--config", str(PROVIDER), "--dotenv", str(dotenv)],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["credential_present"] is True
    assert data["network_called"] is False
    assert data["campaign_max_cost"] == "3.00"
    assert data["per_run_max_cost"] == "1.00"
    assert data["max_context_chars"] == 60_000
    assert data["max_input_tokens"] == 120_000
    assert data["probe_input_token_estimate"]["token_ceiling"] < 120_000
    assert data["probe_input_token_estimate"]["estimator"] == (
        "openai_payload_utf8_bytes_x2_plus_1024_v2"
    )
    assert (
        data["probe_request_payload"]["payload_bytes"]
        == (data["probe_input_token_estimate"]["request_bytes"])
    )
    assert data["preserve_recent_context_units"] == 6
    assert 0 < float(data["probe_reserved_cost"]) < 1
    assert "cli-test-secret" not in result.output


def test_model_probe_requires_explicit_paid_acknowledgement():
    result = runner.invoke(app, ["model", "probe", "--config", str(PROVIDER)])
    assert result.exit_code == 2
    assert "--confirm-paid" in result.output


def test_missing_model_budget_is_read_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["model", "budget", "--config", str(PROVIDER)])
    assert result.exit_code == 2
    assert "No provider campaign ledger" in result.output
    assert not (tmp_path / ".horizon").exists()


def test_agent_run_requires_paid_ack_before_creating_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "agent",
            "run",
            str(AGENT_TASK),
            str(AGENT_PLAN),
            "--image",
            "redis:7-alpine",
        ],
    )
    assert result.exit_code == 2
    assert "--confirm-paid" in result.output
    assert not (tmp_path / ".horizon").exists()


def write_auto_plan_cli_fixture(tmp_path):
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "tests").mkdir()
    original = "def parse(value):\n    return [value]\n"
    (source / "src/parser.py").write_text(original, encoding="utf-8")
    (source / "tests/test_parser.py").write_text("# protected\n", encoding="utf-8")
    task_path = tmp_path / "task.yaml"
    task_path.write_text(
        json.dumps(
            {
                "task_id": "auto-plan-cli",
                "title": "Fix empty parser input",
                "objective": "Return an empty list for empty parser input",
                "repository": {
                    "source": "local",
                    "path": str(source),
                    "base_commit": "0" * 64,
                },
                "constraints": {
                    "allowed_paths": ["src/**", "tests/**"],
                    "denied_paths": [".git/**", ".env", ".env.*", "secrets/**"],
                    "network": "deny",
                },
                "acceptance": [
                    {
                        "id": "unit",
                        "command": "pytest -q",
                        "timeout_seconds": 30,
                        "required": True,
                    }
                ],
                "budgets": {
                    "max_steps": 10,
                    "max_model_calls": 8,
                    "max_tool_calls": 10,
                    "max_wall_time_seconds": 1800,
                    "max_cost_usd": "1.00",
                    "max_input_tokens": 100000,
                    "max_output_tokens": 8192,
                    "max_repair_cycles": 1,
                },
                "task_kind": "bugfix",
                "execution_mode": "workspace_write",
                "authority_scope": "workspace_write",
                "model_policy_id": "siliconflow-deepseek-v4-flash",
            }
        ),
        encoding="utf-8",
    )
    dotenv = tmp_path / ".env"
    dotenv.write_text("SILICONFLOW_API_KEY=fake-offline-key\n", encoding="utf-8")
    return source, original, task_path, dotenv


def test_agent_run_auto_plan_executes_validated_plan_without_touching_source(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    source, original, task_path, dotenv = write_auto_plan_cli_fixture(tmp_path)
    model = AutoPlanAndFixModel()
    monkeypatch.setattr(
        cli_app_module,
        "OpenAICompatibleModelGateway",
        lambda *args, **kwargs: model,
    )
    monkeypatch.setattr(cli_app_module, "DockerSandbox", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli_app_module, "DockerAcceptanceExecutor", LocalParserAcceptance)

    result = runner.invoke(
        app,
        [
            "agent",
            "run",
            str(task_path),
            "--auto-plan",
            "--image",
            "offline-fixture",
            "--config",
            str(PROVIDER),
            "--dotenv",
            str(dotenv),
            "--confirm-paid",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["status"] == "SUCCEEDED"
    assert data["plan_source"] == "model"
    assert data["plan_source_model_call_id"].startswith(f"model_{data['run_id']}_plan_")
    assert data["planning_response_reused"] is False
    assert data["model_call_records"] == 3
    assert data["source_workspace_unchanged"] is True
    assert (source / "src/parser.py").read_text(encoding="utf-8") == original
    staged = Path(data["workspace"])
    assert "return [] if" in (staged / "src/parser.py").read_text(encoding="utf-8")
    assert [request.tools[0].name for request in model.requests] == [
        "propose_plan",
        "read_file",
        "read_file",
    ]


def test_agent_run_invalid_auto_plan_persists_human_fallback_without_retry(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    source, original, task_path, dotenv = write_auto_plan_cli_fixture(tmp_path)
    model = AutoPlanAndFixModel(invalid_plan=True)
    monkeypatch.setattr(
        cli_app_module,
        "OpenAICompatibleModelGateway",
        lambda *args, **kwargs: model,
    )

    result = runner.invoke(
        app,
        [
            "agent",
            "run",
            str(task_path),
            "--auto-plan",
            "--image",
            "offline-fixture",
            "--config",
            str(PROVIDER),
            "--dotenv",
            str(dotenv),
            "--confirm-paid",
        ],
    )

    assert result.exit_code == 3, result.output
    data = json.loads(result.stdout)
    assert data["status"] == "WAITING_FOR_USER"
    assert data["human_request"]["kind"] == "replacement_plan_required"
    assert data["human_request"]["reason_code"] == "invalid_generated_plan"
    assert data["human_request"]["response_artifact_ref"]
    assert data["next_action"].startswith("horizon plan set ")
    assert data["model_call_records"] == 1
    assert data["campaign"]["reserved_cost"] == "0"
    assert data["campaign"]["unknown_cost"] == "0"
    assert len(model.requests) == 1
    assert (source / "src/parser.py").read_text(encoding="utf-8") == original

    persisted = SQLiteEventStore(tmp_path / ".horizon/control.sqlite3").get(data["run_id"])
    assert persisted.status == RunStatus.WAITING_FOR_USER
    assert persisted.resume_state == RunStatus.PLANNING
    assert persisted.pending_human_request is not None
    assert persisted.lease_id is None


def test_agent_run_reports_planning_budget_stop_before_model_dispatch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, _, task_path, dotenv = write_auto_plan_cli_fixture(tmp_path)
    provider_data = yaml.safe_load(PROVIDER.read_text(encoding="utf-8"))
    provider_data["run_budget"]["max_cost"] = "0.000001"
    provider_path = tmp_path / "provider.yaml"
    provider_path.write_text(yaml.safe_dump(provider_data), encoding="utf-8")
    model = AutoPlanAndFixModel()
    monkeypatch.setattr(
        cli_app_module,
        "OpenAICompatibleModelGateway",
        lambda *args, **kwargs: model,
    )

    result = runner.invoke(
        app,
        [
            "agent",
            "run",
            str(task_path),
            "--auto-plan",
            "--image",
            "offline-fixture",
            "--config",
            str(provider_path),
            "--dotenv",
            str(dotenv),
            "--confirm-paid",
        ],
    )

    assert result.exit_code == 3, result.output
    data = json.loads(result.stdout)
    assert data["status"] == "FAILED"
    assert data["failure_reason"] == "run_model_cost_limit"
    assert data["budget_stop"]["scope"] == "run"
    assert Decimal(data["budget_stop"]["required_cost"]) > Decimal(
        data["budget_stop"]["available_cost"]
    )
    assert data["model_request_budget"]["purpose"] == "planning"
    assert data["model_request_budget"]["input_token_budget"]["estimate"]["request_bytes"] > 0
    assert (
        data["model_request_budget"]["request_payload"]["payload_bytes"]
        == (data["model_request_budget"]["input_token_budget"]["estimate"]["request_bytes"])
    )
    assert data["model_request_budget"]["output_token_ceiling"] > 0
    assert data["campaign"]["reserved_cost"] == "0"
    assert data["campaign"]["unknown_cost"] == "0"
    assert len(model.requests) == 0
    persisted = SQLiteEventStore(tmp_path / ".horizon/control.sqlite3").get(data["run_id"])
    assert persisted.status == RunStatus.FAILED
    assert persisted.budget_stop is not None
    assert persisted.model_request_budget is not None
    assert persisted.model_request_budget.model_dump(mode="json") == data["model_request_budget"]
    assert persisted.lease_id is None
    assert persisted.reservations == {}


def test_agent_run_releases_quiescent_lease_after_pre_dispatch_failure(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    _, _, task_path, dotenv = write_auto_plan_cli_fixture(tmp_path)
    model = AutoPlanAndFixModel()
    monkeypatch.setattr(
        cli_app_module,
        "OpenAICompatibleModelGateway",
        lambda *args, **kwargs: model,
    )
    monkeypatch.setattr(cli_app_module, "DockerSandbox", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli_app_module, "DockerAcceptanceExecutor", LocalParserAcceptance)

    def fail_before_dispatch(*args, **kwargs):
        raise Conflict("simulated pre-dispatch projection failure")

    monkeypatch.setattr(CodingAgentRunner, "run", fail_before_dispatch)

    result = runner.invoke(
        app,
        [
            "agent",
            "run",
            str(task_path),
            "--auto-plan",
            "--image",
            "offline-fixture",
            "--config",
            str(PROVIDER),
            "--dotenv",
            str(dotenv),
            "--confirm-paid",
        ],
    )

    assert result.exit_code == 2, result.output
    assert "simulated pre-dispatch projection failure" in result.output
    store = SQLiteEventStore(tmp_path / ".horizon/control.sqlite3")
    rows = store.list_runs()
    assert len(rows) == 1
    persisted = store.get(rows[0]["run_id"])
    assert persisted.status == RunStatus.RUNNING
    assert persisted.lease_id is None
    assert persisted.reservations == {}


def test_agent_run_reports_terminal_budget_stop_as_structured_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, _, task_path, dotenv = write_auto_plan_cli_fixture(tmp_path)
    model = AutoPlanAndFixModel()
    monkeypatch.setattr(
        cli_app_module,
        "OpenAICompatibleModelGateway",
        lambda *args, **kwargs: model,
    )
    monkeypatch.setattr(cli_app_module, "DockerSandbox", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli_app_module, "DockerAcceptanceExecutor", LocalParserAcceptance)

    stop = BudgetStop(
        reason_code=BudgetStopReason.RUN_MODEL_COST_LIMIT,
        scope="run",
        currency="CNY",
        required_cost="0.067530",
        available_cost="0.059648",
    )

    def stop_before_dispatch(self, run_id, token, **kwargs):
        return self.service.fail_budget_stop(run_id, stop, token, "test-budget-stop")

    monkeypatch.setattr(CodingAgentRunner, "run", stop_before_dispatch)
    result = runner.invoke(
        app,
        [
            "agent",
            "run",
            str(task_path),
            "--auto-plan",
            "--image",
            "offline-fixture",
            "--config",
            str(PROVIDER),
            "--dotenv",
            str(dotenv),
            "--confirm-paid",
        ],
    )

    assert result.exit_code == 3, result.output
    data = json.loads(result.stdout)
    assert data["status"] == "FAILED"
    assert data["failure_reason"] == "run_model_cost_limit"
    assert data["budget_stop"] == stop.model_dump(mode="json")
    assert data["continuation_required"] is False
    persisted = SQLiteEventStore(tmp_path / ".horizon/control.sqlite3").get(data["run_id"])
    assert persisted.budget_stop == stop
    assert persisted.lease_id is None
    assert persisted.reservations == {}


def test_agent_cli_applies_guidance_and_resumes_after_no_progress(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source, original, task_path, dotenv = write_auto_plan_cli_fixture(tmp_path)
    stalled_model = AutoPlanAndFixModel(repeat_read=True)
    monkeypatch.setattr(
        cli_app_module,
        "OpenAICompatibleModelGateway",
        lambda *args, **kwargs: stalled_model,
    )
    monkeypatch.setattr(cli_app_module, "DockerSandbox", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli_app_module, "DockerAcceptanceExecutor", LocalParserAcceptance)

    stalled = runner.invoke(
        app,
        [
            "agent",
            "run",
            str(task_path),
            "--auto-plan",
            "--image",
            "offline-fixture",
            "--config",
            str(PROVIDER),
            "--dotenv",
            str(dotenv),
            "--confirm-paid",
        ],
    )
    assert stalled.exit_code == 3, stalled.output
    stalled_data = json.loads(stalled.stdout)
    assert stalled_data["status"] == "WAITING_FOR_USER"
    assert stalled_data["continuation_required"] is True
    assert stalled_data["human_request"]["kind"] == "operator_guidance_required"
    assert stalled_data["next_action"].startswith("horizon agent guide ")

    guidance_path = tmp_path / "guidance.txt"
    guidance_path.write_text(
        "Stop rereading the same file. Apply the exact empty-input branch and submit.",
        encoding="utf-8",
    )
    guided = runner.invoke(
        app,
        [
            "agent",
            "guide",
            stalled_data["run_id"],
            str(guidance_path),
        ],
    )
    assert guided.exit_code == 0, guided.output
    guided_data = json.loads(guided.stdout)
    assert guided_data["status"] == "RUNNING"
    assert guided_data["decision"]["kind"] == "operator_guidance_supplied"
    assert guided_data["paid_model_called"] is False

    resumed_model = AutoPlanAndFixModel()
    monkeypatch.setattr(
        cli_app_module,
        "OpenAICompatibleModelGateway",
        lambda *args, **kwargs: resumed_model,
    )
    resumed = runner.invoke(
        app,
        [
            "agent",
            "resume",
            stalled_data["run_id"],
            "--image",
            "offline-fixture",
            "--config",
            str(PROVIDER),
            "--dotenv",
            str(dotenv),
            "--confirm-paid",
        ],
    )
    assert resumed.exit_code == 0, resumed.output
    resumed_data = json.loads(resumed.stdout)
    assert resumed_data["status"] == "SUCCEEDED"
    assert resumed_data["model_call_records"] == 7
    assert any(
        "Trusted operator guidance" in (message.content or "")
        for message in resumed_model.requests[0].messages
    )
    assert (source / "src/parser.py").read_text(encoding="utf-8") == original


def test_agent_run_requires_exactly_one_plan_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "agent",
            "run",
            str(AGENT_TASK),
            str(AGENT_PLAN),
            "--auto-plan",
            "--image",
            "offline-fixture",
            "--confirm-paid",
        ],
    )
    assert result.exit_code == 2
    assert "exactly one Plan source" in result.output
    assert not (tmp_path / ".horizon").exists()


def test_agent_resume_finishes_planning_from_settled_response_without_second_plan_call(
    tmp_path,
    monkeypatch,
    task_dict,
):
    monkeypatch.chdir(tmp_path)
    provider = load_provider_config(PROVIDER)
    control_root = tmp_path / ".horizon"
    workspace = control_root / "staging" / "agent-planning-recovery"
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "src/parser.py").write_text(
        "def parse(value):\n    return [value]\n",
        encoding="utf-8",
    )
    (workspace / "tests/test_parser.py").write_text("# protected\n", encoding="utf-8")
    artifacts = ArtifactStore(control_root / "artifacts")
    snapshots = SnapshotManager(artifacts)
    snapshot, manifest_ref = snapshots.capture(workspace)
    task_data = {**task_dict, "model_policy_id": provider.policy_id}
    task_data["repository"] = {
        "source": "local",
        "path": str(workspace),
        "base_commit": snapshot.workspace_revision,
    }
    task = TaskSpec.model_validate(task_data)
    store = SQLiteEventStore(control_root / "control.sqlite3")
    service = HarnessService(store)
    run = store.create(task, "create-planning-recovery")
    run = service.bind_workspace_origin(
        run.run_id,
        WorkspaceOrigin(
            source_path_hash=digest(str(workspace.resolve())),
            source_revision=snapshot.workspace_revision,
            source_manifest_ref=manifest_ref,
        ),
        "origin-planning-recovery",
    )
    run = service.acquire_lease(
        run.run_id,
        "crashing-planner",
        "lease-planning-recovery",
        ttl_seconds=600,
    )
    token = LeaseToken.from_run(run)
    service.transition(run.run_id, RunStatus.PLANNING, token, "start-planning-recovery")
    context = build_planning_context(
        task,
        workspace_revision=snapshot.workspace_revision,
        source_manifest_ref=manifest_ref,
        repository_paths=(entry.path for entry in snapshot.files),
    )
    model = AutoPlanAndFixModel()
    ledger = CampaignBudgetLedger(Path(provider.ledger_path))
    generator = PlanGenerator(
        service,
        model,
        ledger,
        artifacts,
        provider_id=provider.provider_id,
        model_id=provider.model.id,
        pricing=provider.pricing,
        campaign=provider.campaign,
        config=PlanGeneratorConfig(
            max_output_tokens=min(1024, provider.request.max_output_tokens),
            max_run_cost=provider.run_budget.max_cost,
            enable_thinking=provider.request.enable_thinking,
        ),
    )
    real_set_plan = service.set_plan

    def crash_before_plan_event(*args, **kwargs):
        raise RuntimeError("simulated CLI planning crash")

    monkeypatch.setattr(service, "set_plan", crash_before_plan_event)
    with pytest.raises(RuntimeError, match="planning crash"):
        generator.generate_and_set(run.run_id, token, context)
    monkeypatch.setattr(service, "set_plan", real_set_plan)
    service.release_lease(run.run_id, token, "release-crashed-planner")

    dotenv = tmp_path / ".env"
    dotenv.write_text("SILICONFLOW_API_KEY=fake-offline-key\n", encoding="utf-8")
    monkeypatch.setattr(
        cli_app_module,
        "OpenAICompatibleModelGateway",
        lambda *args, **kwargs: model,
    )
    monkeypatch.setattr(cli_app_module, "DockerSandbox", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli_app_module, "DockerAcceptanceExecutor", LocalParserAcceptance)

    result = runner.invoke(
        app,
        [
            "agent",
            "resume",
            run.run_id,
            "--image",
            "offline-fixture",
            "--config",
            str(PROVIDER),
            "--dotenv",
            str(dotenv),
            "--confirm-paid",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["status"] == "SUCCEEDED"
    assert data["plan_source"] == "model"
    assert data["planning_response_reused"] is True
    assert data["model_call_records"] == 3
    assert (
        len([request for request in model.requests if request.tools[0].name == "propose_plan"]) == 1
    )


def test_agent_resume_requires_paid_ack_before_reading_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["agent", "resume", "run_missing", "--image", "redis:7-alpine"],
    )
    assert result.exit_code == 2
    assert "--confirm-paid" in result.output
    assert not (tmp_path / ".horizon").exists()


def test_agent_reconcile_is_offline_and_does_not_require_paid_ack(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "--db",
            str(tmp_path / "missing.sqlite3"),
            "agent",
            "reconcile",
            "run-missing",
            "--config",
            str(PROVIDER),
        ],
    )
    assert result.exit_code == 2
    assert "No control database" in result.output
    assert "--confirm-paid" not in result.output
    assert not (tmp_path / ".horizon").exists()


def test_agent_reconcile_cli_marks_pending_model_unknown_without_credential(
    tmp_path, monkeypatch, task_dict, plan
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    provider = load_provider_config(PROVIDER)
    task_data = {**task_dict, "model_policy_id": provider.policy_id}
    task = TaskSpec.model_validate(task_data)
    db = tmp_path / "control.sqlite3"
    past = datetime.now(UTC) - timedelta(seconds=5)
    store = SQLiteEventStore(db, clock=lambda: past)
    service = HarnessService(store)
    run = store.create(task, "create-recovery-cli")
    service.set_plan(run.run_id, plan, "plan-recovery-cli")
    leased = service.acquire_lease(
        run.run_id,
        "crashed-worker",
        "lease-recovery-cli",
        ttl_seconds=1,
    )
    token = LeaseToken.from_run(leased)
    service.transition(run.run_id, RunStatus.RUNNING, token, "start-recovery-cli")
    policy = ModelPolicyBinding(
        policy_id=provider.policy_id,
        provider_id=provider.provider_id,
        model=provider.model.id,
        campaign_id=provider.campaign.campaign_id,
        currency=provider.pricing.currency,
        max_run_cost=provider.run_budget.max_cost,
        price_card_hash=digest(provider.pricing),
    )
    service.bind_model_policy(run.run_id, policy, token, "bind-recovery-cli")
    ledger = CampaignBudgetLedger(Path(provider.ledger_path))
    ledger.initialize(
        provider.campaign,
        provider_id=provider.provider_id,
        model_id=provider.model.id,
    )
    call = ModelCallReservation(
        call_id="pending-model-call",
        request_hash="b" * 64,
        provider_id=provider.provider_id,
        model=provider.model.id,
        currency=provider.pricing.currency,
        reserved_cost=Decimal("0.20"),
    )
    ledger.reserve(provider.campaign, call.call_id, call.request_hash, call.reserved_cost)
    service.reserve_model_call(
        run.run_id,
        call,
        Usage(model_calls=1, input_tokens=100, output_tokens=50),
        token,
        "reserve-recovery-cli",
    )

    result = runner.invoke(
        app,
        [
            "--db",
            str(db),
            "agent",
            "reconcile",
            run.run_id,
            "--config",
            str(PROVIDER),
            "--confirm-old-worker-stopped",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["network_called"] is False
    assert data["safe_to_resume"] is False
    assert data["next_action"] == "manual_reconciliation"
    assert data["unknown_reservations"] == ["pending-model-call"]
    restored = SQLiteEventStore(db).get(run.run_id)
    assert restored.lease_id is None
    assert restored.unknown_model_calls == {"pending-model-call"}


@pytest.mark.parametrize(
    (
        "tool_name",
        "arguments",
        "decision_args",
        "expected_disposition",
        "expected_status",
        "sandbox_mode",
        "expected_sandbox_resolution",
    ),
    [
        (
            "read_file",
            {"path": "src/parser.py"},
            ["--retry-readonly"],
            "retry_readonly",
            "cancelled",
            None,
            None,
        ),
        (
            "run_check",
            {"check_id": "unit"},
            ["--discard-check", "--confirm-check-sandbox-stopped"],
            "discard_check",
            "cancelled",
            None,
            "operator_confirmed",
        ),
        (
            "run_check",
            {"check_id": "unit"},
            ["--discard-check", "--image", "local:test"],
            "discard_check",
            "cancelled",
            "stopped",
            "controller_verified_and_removed",
        ),
        (
            "run_check",
            {"check_id": "unit"},
            [
                "--discard-check",
                "--image",
                "local:test",
                "--stop-check-sandbox",
            ],
            "discard_check",
            "cancelled",
            "running",
            "controller_verified_and_removed",
        ),
        (
            "run_check",
            {"check_id": "unit"},
            ["--accept-check-result", "--image", "local:test"],
            "accept_check_result",
            "success",
            "stopped",
            "result_recorded_and_removed",
        ),
        (
            "run_check",
            {"check_id": "unit"},
            ["--accept-check-result", "--image", "local:test"],
            "accept_check_result",
            "success",
            "cleanup_error",
            "result_recorded_cleanup_required",
        ),
    ],
)
def test_agent_resolve_tool_cli_applies_explicit_offline_recovery_decision(
    tmp_path,
    monkeypatch,
    task_dict,
    plan,
    tool_name,
    arguments,
    decision_args,
    expected_disposition,
    expected_status,
    sandbox_mode,
    expected_sandbox_resolution,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    if tool_name == "run_check":
        plan = plan.model_copy(
            update={
                "items": (
                    plan.items[0].model_copy(
                        update={"allowed_tools": (*plan.items[0].allowed_tools, "run_check")}
                    ),
                )
            }
        )
    if sandbox_mode is not None:

        class FakeRecoverySandbox:
            def __init__(self, *_args, **_kwargs):
                self.cleanup_error = sandbox_mode == "cleanup_error"
                self.state = "stopped" if self.cleanup_error else sandbox_mode
                self.image_id = "sha256:" + "a" * 64

            def _status(self, attempt_id):
                return SandboxAttemptStatus(
                    attempt_id=attempt_id,
                    container_name="horizon-check-test",
                    state=self.state,
                    image_id=self.image_id,
                )

            def attempt_status(self, attempt_id):
                return self._status(attempt_id)

            def stop_attempt(self, attempt_id):
                assert self.state == "running"
                self.state = "stopped"
                return self._status(attempt_id)

            def remove_attempt(self, attempt_id):
                assert self.state == "stopped"
                if self.cleanup_error:
                    raise Conflict("simulated stopped-container cleanup failure")
                self.state = "missing"
                return self._status(attempt_id)

            def recover_stopped_attempt(self, workspace, request, attempt_id):
                assert self.state == "stopped"
                output = b"recovered check passed\n"
                return CommandResult(
                    exit_code=0,
                    timed_out=False,
                    output=output.decode(),
                    total_output_bytes=len(output),
                    output_sha256=hashlib.sha256(output).hexdigest(),
                    output_truncated=False,
                    image_id=self.image_id,
                    container_name="horizon-check-test",
                )

        monkeypatch.setattr(cli_app_module, "DockerSandbox", FakeRecoverySandbox)
    provider = load_provider_config(PROVIDER)
    control_root = tmp_path / ".horizon"
    workspace = control_root / "staging" / "agent-read-recovery"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src/parser.py").write_text(
        "def parse(value):\n    return [value]\n",
        encoding="utf-8",
    )
    task_data = {**task_dict, "model_policy_id": provider.policy_id}
    task_data["repository"] = {
        "source": "local",
        "path": str(workspace),
        "base_commit": "a" * 40,
    }
    task = TaskSpec.model_validate(task_data)
    db = tmp_path / "control.sqlite3"
    store = SQLiteEventStore(db)
    service = HarnessService(store)
    run = store.create(task, "create-resolve-tool-cli")
    run = service.set_plan(run.run_id, plan, "plan-resolve-tool-cli")
    run = service.acquire_lease(run.run_id, "crashed-worker", "lease-resolve-tool-cli")
    token = LeaseToken.from_run(run)
    run = service.transition(run.run_id, RunStatus.RUNNING, token, "start-resolve-tool-cli")
    policy = ModelPolicyBinding(
        policy_id=provider.policy_id,
        provider_id=provider.provider_id,
        model=provider.model.id,
        campaign_id=provider.campaign.campaign_id,
        currency=provider.pricing.currency,
        max_run_cost=provider.run_budget.max_cost,
        price_card_hash=digest(provider.pricing),
    )
    run = service.bind_model_policy(run.run_id, policy, token, "bind-resolve-tool-cli")

    artifacts = ArtifactStore(control_root / "artifacts")
    snapshot, _ = SnapshotManager(artifacts).capture(
        workspace,
        denied_paths=task.constraints.denied_paths,
    )
    session = AgentSession(
        run_id=run.run_id,
        task_spec_hash=task.sha256,
        plan_version=plan.version,
        work_item_id=plan.items[0].work_item_id,
        next_iteration=1,
        covered_event_seq=run.seq,
        workspace_revision=snapshot.workspace_revision,
        messages=(
            ModelMessage(role="system", content="bounded agent"),
            ModelMessage(role="user", content="read the parser"),
        ),
    )
    session_ref = artifacts.put(canonical_json(session.model_dump(mode="json")).encode("utf-8"))
    service.save_agent_session(
        run.run_id,
        AgentSessionRecord(
            artifact_ref=session_ref,
            task_spec_hash=session.task_spec_hash,
            plan_version=session.plan_version,
            work_item_id=session.work_item_id,
            next_iteration=session.next_iteration,
            covered_event_seq=session.covered_event_seq,
            workspace_revision=session.workspace_revision,
            message_count=len(session.messages),
        ),
        token,
        "session-resolve-tool-cli",
    )

    ledger = CampaignBudgetLedger(Path(provider.ledger_path))
    ledger.initialize(
        provider.campaign,
        provider_id=provider.provider_id,
        model_id=provider.model.id,
    )
    model_call = ModelCallReservation(
        call_id="model-resolve-tool-cli",
        request_hash="b" * 64,
        provider_id=provider.provider_id,
        model=provider.model.id,
        currency=provider.pricing.currency,
        reserved_cost=Decimal("0.01"),
    )
    ledger.reserve(
        provider.campaign,
        model_call.call_id,
        model_call.request_hash,
        model_call.reserved_cost,
    )
    service.reserve_model_call(
        run.run_id,
        model_call,
        Usage(model_calls=1, input_tokens=100, output_tokens=50),
        token,
        "reserve-model-resolve-tool-cli",
    )
    response = ModelResponse(
        response_id="response-resolve-tool-cli",
        model=provider.model.id,
        message=ModelMessage(
            role="assistant",
            tool_calls=(
                ToolCall(
                    id="provider-read-call",
                    function=FunctionCall(name=tool_name, arguments=arguments),
                ),
            ),
        ),
        finish_reason="tool_calls",
        usage=ModelUsage(input_tokens=90, output_tokens=10),
        provider_trace_id="trace-resolve-tool-cli",
    )
    response_ref = artifacts.put(canonical_json(response.model_dump(mode="json")).encode("utf-8"))
    actual_cost = provider.pricing.cost_for(response.usage)
    service.settle_model_call(
        run.run_id,
        ModelCallRecord(
            call_id=model_call.call_id,
            request_hash=model_call.request_hash,
            provider_id=provider.provider_id,
            model=provider.model.id,
            currency=provider.pricing.currency,
            estimated_cost=actual_cost,
            response_id=response.response_id,
            response_artifact_ref=response_ref,
            provider_trace_id=response.provider_trace_id,
            finish_reason=response.finish_reason,
            usage=response.usage,
        ),
        Usage(model_calls=1, input_tokens=90, output_tokens=10),
        token,
        "settle-model-resolve-tool-cli",
    )
    ledger.settle(
        provider.campaign.campaign_id,
        model_call.call_id,
        actual_cost,
        response.provider_trace_id,
    )
    tool_call = ToolCallReservation(
        call_id="tool-resolve-tool-cli",
        name=tool_name,
        arguments_hash=digest(arguments),
        workspace_revision=snapshot.workspace_revision,
    )
    service.reserve_tool_call(
        run.run_id,
        tool_call,
        token,
        "reserve-tool-resolve-tool-cli",
    )
    service.mark_tool_call_unknown(
        run.run_id,
        tool_call.call_id,
        token,
        "unknown-tool-resolve-tool-cli",
    )
    service.release_lease(run.run_id, token, "release-crashed-tool-worker")

    missing_decision = runner.invoke(
        app,
        [
            "--db",
            str(db),
            "agent",
            "resolve-tool",
            run.run_id,
            tool_call.call_id,
            "--config",
            str(PROVIDER),
        ],
    )
    assert missing_decision.exit_code == 2
    assert "Choose exactly one explicit decision" in missing_decision.output

    result = runner.invoke(
        app,
        [
            "--db",
            str(db),
            "agent",
            "resolve-tool",
            run.run_id,
            tool_call.call_id,
            *decision_args,
            "--config",
            str(PROVIDER),
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["decision"] == expected_disposition
    assert data["safe_to_resume"] is True
    assert data["network_called"] is False
    assert data["paid_model_called"] is False
    assert data["check_sandbox_resolution"] == expected_sandbox_resolution
    restored = SQLiteEventStore(db).get(run.run_id)
    assert restored.lease_id is None
    assert not restored.reservations
    assert restored.tool_calls[-1].status == expected_status
    assert restored.tool_calls[-1].recovery_disposition == expected_disposition
    if expected_disposition == "accept_check_result":
        assert data["recovered_check_result"]["passed"] is True
        assert data["recovered_check_result"]["output"] == "recovered check passed\n"
    if expected_sandbox_resolution == "result_recorded_cleanup_required":
        assert "cleanup failure" in data["check_sandbox_cleanup_error"]


@pytest.mark.parametrize(
    (
        "tool_name",
        "arguments",
        "decision_args",
        "expected_disposition",
        "expected_status",
    ),
    [
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            },
            ["--accept-replace"],
            "accept_replace",
            "success",
        ),
        (
            "create_file",
            {
                "path": "src/generated.py",
                "content": "def generated():\n    return '你好'\n",
            },
            ["--accept-write"],
            "accept_create",
            "success",
        ),
        (
            "create_file",
            {
                "path": "src/generated.py",
                "content": "def generated():\n    return '你好'\n",
            },
            ["--rollback-write"],
            "rollback_create",
            "cancelled",
        ),
    ],
)
def test_agent_resolve_tool_cli_resolves_exact_write_effect_offline(
    tmp_path,
    monkeypatch,
    task_dict,
    plan,
    tool_name,
    arguments,
    decision_args,
    expected_disposition,
    expected_status,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    provider = load_provider_config(PROVIDER)
    control_root = tmp_path / ".horizon"
    workspace = control_root / "staging" / "agent-write-recovery"
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "src/parser.py").write_text(
        "def parse(value):\n    return [value]\n",
        encoding="utf-8",
    )
    (workspace / "tests/test_parser.py").write_text(
        "# protected by controller\n",
        encoding="utf-8",
    )
    task_data = {**task_dict, "model_policy_id": provider.policy_id}
    task_data["repository"] = {
        "source": "local",
        "path": str(workspace),
        "base_commit": "a" * 40,
    }
    task = TaskSpec.model_validate(task_data)
    write_plan = plan.model_copy(
        update={
            "items": (
                plan.items[0].model_copy(
                    update={
                        "expected_artifacts": (arguments["path"],),
                        "allowed_tools": (
                            "search_repo",
                            "read_file",
                            tool_name,
                            "run_check",
                        ),
                    }
                ),
            )
        }
    )
    db = tmp_path / "control.sqlite3"
    store = SQLiteEventStore(db)
    service = HarnessService(store)
    run = store.create(task, "create-write-recovery-cli")
    service.set_plan(run.run_id, write_plan, "plan-write-recovery-cli")
    leased = service.acquire_lease(run.run_id, "crashed-writer", "lease-write-recovery-cli")
    token = LeaseToken.from_run(leased)
    service.transition(run.run_id, RunStatus.RUNNING, token, "start-write-recovery-cli")
    artifacts = ArtifactStore(control_root / "artifacts")
    snapshots = SnapshotManager(artifacts)
    real_tools = WorkspaceToolGateway(
        workspace,
        task,
        write_plan.items[0],
        snapshots,
        None,
    )
    ledger = CampaignBudgetLedger(Path(provider.ledger_path))
    agent = CodingAgentRunner(
        service,
        OneToolModel(tool_name, arguments),
        ledger,
        CrashAfterToolEffect(real_tools),
        artifacts,
        provider_id=provider.provider_id,
        model_id=provider.model.id,
        pricing=provider.pricing,
        campaign=provider.campaign,
        config=AgentLoopConfig(
            max_model_iterations=8,
            max_output_tokens=256,
            max_run_cost=provider.run_budget.max_cost,
        ),
    )
    with pytest.raises(RuntimeError, match="CLI write effect"):
        agent.run(run.run_id, token)
    report = RecoveryService(service, ledger, artifacts).reconcile(run.run_id, token)
    assert report.safe_to_resume is False
    pending = store.get(run.run_id)
    call_id = next(iter(pending.unknown_tool_calls))
    service.release_lease(run.run_id, token, "release-write-recovery-cli")

    result = runner.invoke(
        app,
        [
            "--db",
            str(db),
            "agent",
            "resolve-tool",
            run.run_id,
            call_id,
            *decision_args,
            "--config",
            str(PROVIDER),
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["decision"] == expected_disposition
    assert data["safe_to_resume"] is True
    assert data["network_called"] is False
    assert data["paid_model_called"] is False
    restored = SQLiteEventStore(db).get(run.run_id)
    assert restored.lease_id is None
    assert restored.agent_session is not None
    assert restored.agent_session.next_iteration == 2
    assert restored.tool_calls[-1].status == expected_status
    assert restored.tool_calls[-1].recovery_disposition == expected_disposition
    if tool_name == "replace_text":
        assert "return [] if" in (workspace / "src/parser.py").read_text(encoding="utf-8")
    elif expected_status == "success":
        assert (workspace / "src/generated.py").read_text(encoding="utf-8") == arguments["content"]
    else:
        assert not (workspace / "src/generated.py").exists()
