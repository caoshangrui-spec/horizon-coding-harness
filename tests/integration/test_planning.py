from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.model_probe import conservative_input_estimate
from horizon.application.planning import (
    PlanGenerator,
    PlanGeneratorConfig,
    build_plan_request,
    build_planning_context,
)
from horizon.application.recovery import RecoveryService
from horizon.application.services import HarnessService, LeaseToken
from horizon.domain.common import digest
from horizon.domain.errors import (
    BudgetExceeded,
    BudgetStopReason,
    PlanProposalError,
    PolicyDenied,
)
from horizon.domain.model import (
    CampaignBudget,
    FunctionCall,
    ModelCallReservation,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    PriceCard,
    ToolCall,
)
from horizon.domain.plan import Plan
from horizon.domain.planning import PlanningContext
from horizon.domain.promotion import WorkspaceOrigin
from horizon.domain.run import projection_hash
from horizon.domain.states import RunStatus
from horizon.domain.task import TaskSpec
from horizon.interfaces.cli.app import app


class ScriptedPlanner:
    def __init__(self, proposals):
        self.proposals = list(proposals)
        self.requests = []
        self.trace_ids = []

    def generate(self, request, trace_id):
        self.requests.append(request)
        self.trace_ids.append(trace_id)
        arguments = self.proposals.pop(0)
        index = len(self.requests)
        return ModelResponse(
            response_id=f"planner-response-{index}",
            model=request.model,
            message=ModelMessage(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        id=f"planner-tool-{index}",
                        function=FunctionCall(name="propose_plan", arguments=arguments),
                    ),
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(input_tokens=180, output_tokens=90),
            provider_trace_id=f"planner-trace-{index}",
        )


class RejectResponseArtifact:
    def __init__(self, delegate):
        self.delegate = delegate

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def put(self, content):
        if b'"response_id":' in content:
            raise OSError("simulated planning response artifact publication failure")
        return self.delegate.put(content)


def valid_proposal():
    tools = ["search_repo", "read_file", "apply_patch", "run_check"]
    return {
        "version": 1,
        "items": [
            {
                "work_item_id": "implement",
                "title": "Implement the bounded fix",
                "objective": "Inspect and update the parser implementation",
                "dependencies": [],
                "expected_artifacts": ["src/parser.py"],
                "acceptance_ids": ["unit"],
                "allowed_tools": tools,
            },
            {
                "work_item_id": "regression",
                "title": "Verify the regression",
                "objective": "Confirm the public behavior remains valid",
                "dependencies": ["implement"],
                "expected_artifacts": ["tests/test_parser.py"],
                "acceptance_ids": ["regression"],
                "allowed_tools": tools,
            },
        ],
    }


def test_planning_context_and_schema_honor_task_tool_allowlist(tmp_path):
    proposal = valid_proposal()
    generator, _, store, _, run_id, _, context, model, _ = setup_planner(
        tmp_path,
        [proposal],
    )
    task = store.get(run_id).task
    narrowed_task = task.model_copy(
        update={
            "constraints": task.constraints.model_copy(
                update={"allowed_tools": ("read_file", "replace_text")}
            )
        }
    )
    narrowed_context = build_planning_context(
        narrowed_task,
        workspace_revision=context.workspace_revision,
        source_manifest_ref=context.source_manifest_ref,
        repository_paths=context.repository_paths,
    )

    assert narrowed_context.permitted_tools == ("read_file", "replace_text")
    request = build_plan_request(
        generator.model_id,
        narrowed_context,
        max_output_tokens=generator.config.max_output_tokens,
        enable_thinking=generator.config.enable_thinking,
    )
    plan_tool = next(tool for tool in request.tools if tool.name == "propose_plan")
    item_schema = plan_tool.parameters["properties"]["items"]["items"]
    assert item_schema["properties"]["allowed_tools"]["items"]["enum"] == [
        "read_file",
        "replace_text",
    ]
    assert model.requests == []


def setup_planner(tmp_path: Path, proposals):
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "secrets").mkdir()
    (workspace / "src/parser.py").write_text("def parse(value):\n    return [value]\n")
    (workspace / "tests/test_parser.py").write_text("# tests\n")
    (workspace / "README.md").write_text("fixture\n")
    (workspace / "secrets/token.txt").write_text("not-for-planner\n")

    artifacts = ArtifactStore(tmp_path / "artifacts")
    snapshots = SnapshotManager(artifacts)
    snapshot, manifest_ref = snapshots.capture(
        workspace,
        denied_paths=(".git/**", ".env", ".env.*", "secrets/**"),
    )
    task = TaskSpec(
        task_id="generated-plan-test",
        title="Fix parser edge cases",
        objective="Handle empty parser input without changing its public API",
        repository={
            "source": "local",
            "path": str(workspace),
            "base_commit": snapshot.workspace_revision,
        },
        constraints={
            "allowed_paths": ("src/**", "tests/**"),
            "denied_paths": (".git/**", ".env", ".env.*", "secrets/**"),
            "requirements": ("Preserve the public parse signature",),
        },
        acceptance=(
            {"id": "unit", "command": "pytest unit", "required": True},
            {"id": "regression", "command": "pytest regression", "required": True},
        ),
        budgets={
            "max_steps": 20,
            "max_model_calls": 4,
            "max_tool_calls": 20,
            "max_wall_time_seconds": 3600,
            "max_cost_usd": "1.00",
            "max_input_tokens": 100_000,
            "max_output_tokens": 8_000,
        },
        task_kind="bugfix",
        execution_mode="workspace_write",
        authority_scope="workspace_write",
        model_policy_id="fake-policy",
    )
    store = SQLiteEventStore(tmp_path / "control.sqlite3")
    service = HarnessService(store)
    run = store.create(task, "create")
    run = service.bind_workspace_origin(
        run.run_id,
        WorkspaceOrigin(
            source_path_hash=digest(str(workspace.resolve())),
            source_revision=snapshot.workspace_revision,
            source_manifest_ref=manifest_ref,
        ),
        "origin",
    )
    run = service.acquire_lease(run.run_id, "planner", "lease", ttl_seconds=600)
    token = LeaseToken.from_run(run)
    run = service.transition(run.run_id, RunStatus.PLANNING, token, "planning")
    context = build_planning_context(
        task,
        workspace_revision=snapshot.workspace_revision,
        source_manifest_ref=manifest_ref,
        repository_paths=(
            *(entry.path for entry in snapshot.files),
            "secrets/token.txt",
            "outside.py",
        ),
    )
    model = ScriptedPlanner(proposals)
    campaign = CampaignBudget(
        campaign_id="planning-tests",
        currency="CNY",
        max_cost="3.00",
        max_cost_per_call="1.00",
    )
    pricing = PriceCard(
        currency="CNY",
        input_per_million="3.00",
        cached_input_per_million="0.30",
        output_per_million="9.00",
        version="test",
        source_url="https://example.test/pricing",
    )
    ledger = CampaignBudgetLedger(tmp_path / "campaign.sqlite3")
    generator = PlanGenerator(
        service,
        model,
        ledger,
        artifacts,
        provider_id="fake-provider",
        model_id="fake-model",
        pricing=pricing,
        campaign=campaign,
        config=PlanGeneratorConfig(max_output_tokens=512),
    )
    return generator, service, store, ledger, run.run_id, token, context, model, artifacts


def test_generated_plan_is_budgeted_validated_and_trace_linked(tmp_path):
    generator, service, store, ledger, run_id, token, context, model, artifacts = setup_planner(
        tmp_path,
        [valid_proposal()],
    )

    result = generator.generate_and_set(run_id, token, context)

    assert result.run.status == RunStatus.READY
    assert [item.work_item_id for item in result.plan.items] == ["implement", "regression"]
    assert result.run.plan_source_model_call_id == result.model_call_id
    assert result.run.usage.model_calls == 1
    assert result.run.model_calls[0].purpose == "planning"
    assert result.reused_response is False
    assert len(model.requests) == 1
    assert model.requests[0].tool_choice == "required"
    assert model.requests[0].tools[0].name == "propose_plan"
    assert "exactly one WorkItem" in model.requests[0].messages[0].content
    assert "never create standalone locate" in model.requests[0].messages[0].content
    assert "inventory proves only that a path exists" in model.requests[0].messages[0].content
    assert "Unless the immutable task explicitly names a path" in (
        model.requests[0].messages[0].content
    )
    assert "inventory membership alone is not evidence" in (model.requests[0].messages[0].content)
    assert context.repository_paths == ("src/parser.py", "tests/test_parser.py")
    assert "pytest unit" not in model.requests[0].messages[1].content
    assert (
        PlanningContext.model_validate_json(artifacts.read(result.planning_context_ref)) == context
    )
    assert ledger.summary("planning-tests").settled_cost == result.run.model_occupied_cost
    reservation_event = next(
        event for event in store.events(run_id) if event.event_type == "MODEL_CALL_RESERVED"
    )
    reservation = ModelCallReservation.model_validate(reservation_event.payload["reservation"])
    assert reservation.client_trace_id == model.trace_ids[0]
    assert reservation.input_token_budget is not None
    assert reservation.input_token_budget.max_input_tokens == generator.config.max_input_tokens
    assert reservation.input_token_budget.estimate == conservative_input_estimate(model.requests[0])
    assert reservation.request_payload == model.requests[0].openai_compatible_payload_evidence()
    assert (
        reservation.request_payload.payload_bytes
        == reservation.input_token_budget.estimate.request_bytes
    )
    assert result.run.usage.input_tokens == 180
    event = next(event for event in store.events(run_id) if event.event_type == "PLAN_CREATED")
    assert event.payload["source_model_call_id"] == result.model_call_id
    assert service.store.get(run_id).plan == result.plan
    replayed = SQLiteEventStore(store.path).get(run_id)
    assert replayed.plan == result.plan
    assert replayed.plan_source_model_call_id == result.model_call_id


def test_planning_response_artifact_failure_is_quarantined(tmp_path):
    generator, service, store, ledger, run_id, token, context, model, artifacts = setup_planner(
        tmp_path,
        [valid_proposal()],
    )
    generator.artifact_store = RejectResponseArtifact(artifacts)

    with pytest.raises(OSError, match="planning response artifact publication"):
        generator.generate_and_set(run_id, token, context)

    interrupted = store.get(run_id)
    assert interrupted.plan is None
    assert not interrupted.model_calls
    assert len(interrupted.model_reservations) == 1
    call_id, reservation = next(iter(interrupted.model_reservations.items()))
    assert reservation.purpose == "planning"
    assert reservation.client_trace_id == model.trace_ids[0]
    assert interrupted.unknown_model_calls == {call_id}
    assert interrupted.unknown_reservations == {call_id}
    attempt = ledger.attempt("planning-tests", call_id)
    assert attempt.status == "unknown"
    assert attempt.error_type == "PostResponseReceiptUnavailable"

    report = RecoveryService(service, ledger, artifacts).reconcile(run_id, token)
    assert report.safe_to_resume is False
    assert report.next_action == "manual_reconciliation"
    assert report.findings[0].classification == "model_effect_unknown"
    assert report.findings[0].client_trace_id == reservation.client_trace_id


def test_planning_input_token_budget_stops_before_dispatch(tmp_path):
    generator, _, store, ledger, run_id, token, context, model, _ = setup_planner(
        tmp_path,
        [valid_proposal()],
    )
    generator.config = generator.config.model_copy(update={"max_input_tokens": 2_000})

    with pytest.raises(PolicyDenied, match="input-token budget"):
        generator.generate_and_set(run_id, token, context)

    assert model.requests == []
    assert not any(event.event_type == "MODEL_CALL_RESERVED" for event in store.events(run_id))
    assert ledger.summary("planning-tests").occupied_cost == Decimal("0")


def test_planning_budget_stop_is_terminal_and_replayable_before_dispatch(tmp_path):
    generator, _, store, ledger, run_id, token, context, model, _ = setup_planner(
        tmp_path,
        [valid_proposal()],
    )
    generator.config = generator.config.model_copy(update={"max_run_cost": Decimal("0.000001")})

    with pytest.raises(BudgetExceeded, match="Per-run") as failure:
        generator.generate_and_set(run_id, token, context)

    assert failure.value.stop is not None
    assert failure.value.stop.reason_code == BudgetStopReason.RUN_MODEL_COST_LIMIT
    assert failure.value.model_request_budget is not None
    assert failure.value.model_request_budget.purpose == "planning"
    assert failure.value.model_request_budget.input_token_budget.estimate.request_bytes > 0
    assert failure.value.model_request_budget.request_payload is not None
    assert (
        failure.value.model_request_budget.request_payload.payload_bytes
        == failure.value.model_request_budget.input_token_budget.estimate.request_bytes
    )
    assert (
        failure.value.model_request_budget.output_token_ceiling
        == generator.config.max_output_tokens
    )
    stopped = store.get(run_id)
    assert stopped.status == RunStatus.FAILED
    assert stopped.failure_reason == BudgetStopReason.RUN_MODEL_COST_LIMIT.value
    assert stopped.budget_stop == failure.value.stop
    assert stopped.model_request_budget == failure.value.model_request_budget
    assert stopped.lease_id is None
    assert stopped.reservations == {}
    assert stopped.model_reservations == {}
    assert len(model.requests) == 0
    campaign = ledger.summary("planning-tests")
    assert campaign.reserved_cost == Decimal("0")
    assert campaign.unknown_cost == Decimal("0")
    assert campaign.settled_cost == Decimal("0")
    replayed = SQLiteEventStore.replay_jsonl(store.export_jsonl(run_id))
    assert replayed.model_request_budget == stopped.model_request_budget
    assert projection_hash(replayed) == projection_hash(stopped)


def test_invalid_planner_proposal_is_not_retried(tmp_path):
    proposal = valid_proposal()
    proposal["items"][0]["allowed_tools"] = ["shell"]
    generator, _, store, ledger, run_id, token, context, model, _ = setup_planner(
        tmp_path,
        [proposal],
    )

    with pytest.raises(PlanProposalError, match="outside controller policy"):
        generator.generate_and_set(run_id, token, context)
    with pytest.raises(PlanProposalError, match="outside controller policy"):
        generator.generate_and_set(run_id, token, context)

    run = store.get(run_id)
    assert run.status == RunStatus.PLANNING
    assert run.plan is None
    assert run.usage.model_calls == 1
    assert len(run.model_calls) == 1
    assert len(model.requests) == 1
    summary = ledger.summary("planning-tests")
    assert summary.reserved_cost == Decimal("0")
    assert summary.unknown_cost == Decimal("0")


def test_planner_rejects_path_absent_from_complete_inventory_without_retry(tmp_path):
    proposal = valid_proposal()
    proposal["items"][0]["expected_artifacts"] = [
        "Evidence-discovered change in src/missing.py (or the file where parse is defined)"
    ]
    generator, _, store, ledger, run_id, token, context, model, _ = setup_planner(
        tmp_path,
        [proposal],
    )

    with pytest.raises(PlanProposalError, match="absent from the complete repository inventory"):
        generator.generate_and_set(run_id, token, context)
    with pytest.raises(PlanProposalError, match="absent from the complete repository inventory"):
        generator.generate_and_set(run_id, token, context)

    run = store.get(run_id)
    assert run.status == RunStatus.PLANNING
    assert run.plan is None
    assert run.usage.model_calls == 1
    assert len(model.requests) == 1
    summary = ledger.summary("planning-tests")
    assert summary.settled_cost == run.model_occupied_cost
    assert summary.reserved_cost == Decimal("0")
    assert summary.unknown_cost == Decimal("0")


def test_planner_allows_scoped_new_path_only_for_create_file_work_item(tmp_path):
    proposal = valid_proposal()
    proposal["items"][0]["expected_artifacts"] = ["src/generated.py"]
    proposal["items"][0]["allowed_tools"].append("create_file")
    generator, _, _, _, run_id, token, context, model, _ = setup_planner(
        tmp_path,
        [proposal],
    )

    result = generator.generate_and_set(run_id, token, context)

    assert result.run.status == RunStatus.READY
    assert result.plan.items[0].expected_artifacts == ("src/generated.py",)
    assert "create_file" in result.plan.items[0].allowed_tools
    assert len(model.requests) == 1


@pytest.mark.parametrize("missing_path", ["secrets/generated.py", "src/../outside.py"])
def test_planner_rejects_out_of_scope_new_path_even_with_create_file(
    tmp_path,
    missing_path,
):
    proposal = valid_proposal()
    proposal["items"][0]["expected_artifacts"] = [missing_path]
    proposal["items"][0]["allowed_tools"].append("create_file")
    generator, _, _, _, run_id, token, context, _, _ = setup_planner(
        tmp_path,
        [proposal],
    )

    with pytest.raises(PlanProposalError, match="absent from the complete repository inventory"):
        generator.generate_and_set(run_id, token, context)


def test_planner_does_not_reject_unknown_path_from_truncated_inventory(tmp_path):
    generator, _, _, _, run_id, token, context, model, _ = setup_planner(
        tmp_path,
        [valid_proposal()],
    )
    truncated = context.model_copy(
        update={
            "repository_paths": ("src/parser.py",),
            "repository_path_count": 2,
            "repository_paths_truncated": True,
        }
    )

    result = generator.generate_and_set(run_id, token, truncated)

    assert result.run.status == RunStatus.READY
    assert result.plan == Plan.model_validate(valid_proposal())
    assert len(model.requests) == 1


def test_planner_rejects_duplicate_acceptance_ownership_without_retry(tmp_path):
    proposal = valid_proposal()
    proposal["items"][1]["acceptance_ids"] = ["unit", "regression"]
    generator, _, store, ledger, run_id, token, context, model, _ = setup_planner(
        tmp_path,
        [proposal],
    )

    with pytest.raises(PlanProposalError, match="exactly one WorkItem"):
        generator.generate_and_set(run_id, token, context)

    run = store.get(run_id)
    assert run.status == RunStatus.PLANNING
    assert run.plan is None
    assert run.usage.model_calls == 1
    assert len(model.requests) == 1
    summary = ledger.summary("planning-tests")
    assert summary.settled_cost == run.model_occupied_cost
    assert summary.reserved_cost == Decimal("0")
    assert summary.unknown_cost == Decimal("0")


def test_invalid_planner_proposal_waits_for_and_accepts_a_human_plan(tmp_path):
    proposal = valid_proposal()
    proposal["items"][0]["allowed_tools"] = ["shell"]
    generator, service, store, ledger, run_id, token, context, model, artifacts = setup_planner(
        tmp_path,
        [proposal],
    )

    with pytest.raises(PlanProposalError) as failure:
        generator.generate_and_set(run_id, token, context)
    planning_record = store.get(run_id).model_calls[0]
    waiting = service.request_replacement_plan(
        run_id,
        planning_record.call_id,
        str(failure.value),
        token,
        "request-human-plan",
    )

    assert waiting.status == RunStatus.WAITING_FOR_USER
    assert waiting.resume_state == RunStatus.PLANNING
    assert waiting.lease_id is None
    assert waiting.pending_human_request is not None
    assert waiting.pending_human_request.response_artifact_ref == (
        planning_record.response_artifact_ref
    )
    assert waiting.pending_human_request.task_spec_hash == waiting.task.sha256
    assert [event.event_type for event in store.events(run_id)[-3:]] == [
        "HUMAN_REQUEST_CREATED",
        "STATE_CHANGED",
        "LEASE_RELEASED",
    ]
    replayed = SQLiteEventStore(store.path).get(run_id)
    assert replayed.pending_human_request == waiting.pending_human_request

    recovery_lease = service.acquire_lease(run_id, "recovery", "recovery-lease")
    recovery_token = LeaseToken.from_run(recovery_lease)
    report = RecoveryService(service, ledger, artifacts).reconcile(run_id, recovery_token)
    assert report.safe_to_resume is False
    assert report.requires_human is True
    assert report.next_action == "provide_replacement_plan"
    assert not any(item.classification == "unsafe_agent_boundary" for item in report.findings)
    service.release_lease(run_id, recovery_token, "recovery-release")

    invalid_replacement = Plan.model_validate(
        {
            "version": 1,
            "items": [
                {
                    **valid_proposal()["items"][0],
                    "acceptance_ids": ["unit"],
                }
            ],
        }
    )
    with pytest.raises(PolicyDenied, match="cover every required check"):
        service.resolve_replacement_plan(
            run_id,
            invalid_replacement,
            "reject-invalid-human-plan",
        )
    unchanged = store.get(run_id)
    assert unchanged.status == RunStatus.WAITING_FOR_USER
    assert unchanged.pending_human_request is not None
    assert unchanged.human_decisions == []

    plan_path = tmp_path / "replacement-plan.yaml"
    plan_path.write_text(json.dumps(valid_proposal()), encoding="utf-8")
    result = CliRunner().invoke(
        app,
        ["--db", str(store.path), "plan", "set", run_id, str(plan_path)],
    )
    assert result.exit_code == 0, result.output
    output = json.loads(result.stdout)
    assert output["status"] == "READY"
    assert output["pending_human_request"] is None
    assert output["pending_human_plan_hash"] is None
    assert output["plan_source_model_call_id"] is None
    assert output["human_decisions"][0]["actor"] == "local_cli"

    resolved = SQLiteEventStore(store.path).get(run_id)
    assert resolved.status == RunStatus.READY
    assert resolved.plan == Plan.model_validate(valid_proposal())
    assert resolved.plan_source_model_call_id is None
    assert resolved.pending_human_request is None
    assert len(resolved.human_decisions) == 1
    assert len(model.requests) == 1
    assert ledger.summary("planning-tests").settled_cost == resolved.model_occupied_cost


def test_planning_resume_reuses_settled_response_after_plan_commit_crash(
    tmp_path,
    monkeypatch,
):
    generator, service, store, ledger, run_id, token, context, model, artifacts = setup_planner(
        tmp_path,
        [valid_proposal()],
    )
    real_set_plan = service.set_plan

    def crash_before_plan_event(*args, **kwargs):
        raise RuntimeError("simulated crash before plan event")

    monkeypatch.setattr(service, "set_plan", crash_before_plan_event)
    with pytest.raises(RuntimeError, match="before plan event"):
        generator.generate_and_set(run_id, token, context)
    monkeypatch.setattr(service, "set_plan", real_set_plan)
    recovery = RecoveryService(service, ledger, artifacts).reconcile(run_id, token)
    assert recovery.safe_to_resume is True
    assert recovery.next_action == "resume"

    resumed = PlanGenerator(
        service,
        ScriptedPlanner([]),
        ledger,
        artifacts,
        provider_id="fake-provider",
        model_id="fake-model",
        pricing=generator.pricing,
        campaign=generator.campaign,
        config=generator.config,
    ).generate_and_set(run_id, token, context)

    assert resumed.reused_response is True
    assert resumed.run.status == RunStatus.READY
    assert resumed.run.usage.model_calls == 1
    assert len(resumed.run.model_calls) == 1
    assert len(model.requests) == 1
    assert ledger.summary("planning-tests").reserved_cost == Decimal("0")


def test_planning_inventory_is_bounded_after_scope_filtering(tmp_path):
    generator, _, _, _, run_id, _, context, _, _ = setup_planner(
        tmp_path,
        [valid_proposal()],
    )
    task = generator.service.store.get(run_id).task
    bounded = build_planning_context(
        task,
        workspace_revision=context.workspace_revision,
        source_manifest_ref=context.source_manifest_ref,
        repository_paths=("src/c.py", "src/a.py", "src/b.py", "secrets/key", "README.md"),
        max_inventory_paths=2,
    )
    assert bounded.repository_paths == ("src/a.py", "src/b.py")
    assert bounded.repository_path_count == 3
    assert bounded.repository_paths_truncated is True

    default_bounded = build_planning_context(
        task,
        workspace_revision=context.workspace_revision,
        source_manifest_ref=context.source_manifest_ref,
        repository_paths=tuple(f"src/module_{index:03d}.py" for index in range(60)),
    )
    assert len(default_bounded.repository_paths) == 50
    assert default_bounded.repository_path_count == 60
    assert default_bounded.repository_paths_truncated is True
