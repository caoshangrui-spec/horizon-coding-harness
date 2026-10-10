import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.retrieval.sqlite_fts import SQLiteCodeRetriever
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.agent_loop import AgentLoopConfig, CodingAgentRunner
from horizon.application.human import OperatorGuidanceService
from horizon.application.model_probe import conservative_input_estimate
from horizon.application.recovery import RecoveryService
from horizon.application.services import HarnessService, LeaseToken
from horizon.application.supervision import (
    ReapedWorkerBoundary,
    SequentialAgentSupervisor,
    SequentialSupervisorConfig,
)
from horizon.application.tool_recovery import ToolRecoveryService
from horizon.domain.agent import AgentSession
from horizon.domain.budget import Usage
from horizon.domain.common import canonical_json, digest
from horizon.domain.context import ContextProjection, MandatoryFactLedger
from horizon.domain.errors import BudgetStopReason, Conflict, ProviderConnectionError
from horizon.domain.human import HumanGuidanceRequest
from horizon.domain.memory import RunMemorySnapshot
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
from horizon.domain.plan import Plan, WorkItem
from horizon.domain.run import projection_hash
from horizon.domain.states import RunStatus
from horizon.domain.task import TaskSpec
from horizon.domain.tools import AcceptanceResult, ToolCallReservation
from horizon.tools.gateway import WorkspaceToolGateway


class ScriptedModel:
    def __init__(self, actions):
        self.actions = list(actions)
        self.requests = []
        self.trace_ids = []

    def generate(self, request, trace_id):
        self.requests.append(request)
        self.trace_ids.append(trace_id)
        name, arguments = self.actions.pop(0)
        index = len(self.requests)
        return ModelResponse(
            response_id=f"response-{index}",
            model=request.model,
            message=ModelMessage(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        id=f"provider-call-{index}",
                        function=FunctionCall(name=name, arguments=arguments),
                    ),
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(input_tokens=100 + index, output_tokens=20),
            provider_trace_id=f"trace-{index}",
        )


class ParserCheck:
    def execute(self, workspace, check):
        content = (workspace / "src/parser.py").read_text(encoding="utf-8")
        passed = "return [] if value == '' else [value]" in content
        output = "1 passed" if passed else "expected [] for empty input"
        return AcceptanceResult(
            check_id=check.id,
            passed=passed,
            exit_code=0 if passed else 1,
            timed_out=False,
            output=output,
            output_hash=hashlib.sha256(output.encode()).hexdigest(),
        )


class DurableAttemptCheck(ParserCheck):
    def __init__(self, *, fail_cleanup: bool = False):
        self.fail_cleanup = fail_cleanup
        self.events = []
        self.store = None
        self.run_id = None

    def execute_attempt(self, workspace, check, attempt_id):
        self.events.append(("execute", attempt_id))
        return self.execute(workspace, check)

    def cleanup_attempt(self, attempt_id):
        assert self.store is not None
        assert self.run_id is not None
        settled = self.store.get(self.run_id).tool_calls[-1]
        assert settled.call_id == attempt_id
        self.events.append(("cleanup_after_settlement", attempt_id))
        if self.fail_cleanup:
            raise Conflict("simulated post-receipt cleanup failure")


class RejectResponseArtifact:
    def __init__(self, delegate):
        self.delegate = delegate

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def put(self, content):
        if b'"response_id":' in content:
            raise OSError("simulated response artifact publication failure")
        return self.delegate.put(content)


class MultiStepCheck:
    def execute(self, workspace, check):
        content = (workspace / "src/parser.py").read_text(encoding="utf-8")
        markers = {
            "empty": "items = [] if value == '' else [value]",
            "none": "return items if value is not None else []",
        }
        passed = markers[check.id] in content
        output = "1 passed" if passed else f"missing marker for {check.id}"
        return AcceptanceResult(
            check_id=check.id,
            passed=passed,
            exit_code=0 if passed else 1,
            timed_out=False,
            output=output,
            output_hash=hashlib.sha256(output.encode()).hexdigest(),
        )


class MultiFileCheck:
    def execute(self, workspace, check):
        parser = (workspace / "src/parser.py").read_text(encoding="utf-8")
        formatter = (workspace / "src/formatter.py").read_text(encoding="utf-8")
        passed = "return [] if value == '' else [value]" in parser and "str(value)" in formatter
        output = "2 files verified" if passed else "multi-file patch is incomplete"
        return AcceptanceResult(
            check_id=check.id,
            passed=passed,
            exit_code=0 if passed else 1,
            timed_out=False,
            output=output,
            output_hash=hashlib.sha256(output.encode()).hexdigest(),
        )


class CreateFileCheck:
    def execute(self, workspace, check):
        target = workspace / "src/generated.py"
        expected = "def generated():\n    return '你好'\n"
        passed = target.is_file() and target.read_text(encoding="utf-8") == expected
        output = "created file verified" if passed else "created file is absent or changed"
        return AcceptanceResult(
            check_id=check.id,
            passed=passed,
            exit_code=0 if passed else 1,
            timed_out=False,
            output=output,
            output_hash=hashlib.sha256(output.encode()).hexdigest(),
        )


class InterruptBeforeCampaignSettlement:
    def __init__(self, delegate):
        self.delegate = delegate

    def initialize(self, *args, **kwargs):
        return self.delegate.initialize(*args, **kwargs)

    def reserve(self, *args, **kwargs):
        return self.delegate.reserve(*args, **kwargs)

    def settle(self, *args, **kwargs):
        raise RuntimeError("simulated exit before campaign settlement")

    def mark_unknown(self, *args, **kwargs):
        return self.delegate.mark_unknown(*args, **kwargs)

    def attempt(self, *args, **kwargs):
        return self.delegate.attempt(*args, **kwargs)

    def summary(self, *args, **kwargs):
        return self.delegate.summary(*args, **kwargs)


class InterruptAfterCampaignReservation:
    def __init__(self, delegate):
        self.delegate = delegate

    def initialize(self, *args, **kwargs):
        return self.delegate.initialize(*args, **kwargs)

    def reserve(self, *args, **kwargs):
        self.delegate.reserve(*args, **kwargs)
        raise RuntimeError("simulated exit after campaign reservation")

    def settle(self, *args, **kwargs):
        return self.delegate.settle(*args, **kwargs)

    def mark_unknown(self, *args, **kwargs):
        return self.delegate.mark_unknown(*args, **kwargs)

    def attempt(self, *args, **kwargs):
        return self.delegate.attempt(*args, **kwargs)

    def attempts(self, *args, **kwargs):
        return self.delegate.attempts(*args, **kwargs)

    def summary(self, *args, **kwargs):
        return self.delegate.summary(*args, **kwargs)


class InterruptToolDispatch:
    def __init__(self, delegate):
        self.delegate = delegate

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def dispatch_safe(self, name, arguments, attempt_id=None):
        raise RuntimeError("simulated process exit after durable tool intent")


class InterruptAfterToolDispatch:
    def __init__(self, delegate):
        self.delegate = delegate

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def dispatch_safe(self, name, arguments, attempt_id=None):
        self.delegate.dispatch_safe(name, arguments, attempt_id)
        raise RuntimeError("simulated process exit after tool side effect")


class ChangedToolSchema:
    def __init__(self, delegate):
        self.delegate = delegate

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    @property
    def definitions(self):
        definitions = self.delegate.definitions
        changed = definitions[0].model_copy(
            update={"description": definitions[0].description + " schema-drift"}
        )
        return (changed, *definitions[1:])


def setup_loop(tmp_path: Path, task_dict, actions, *, plan=None, checker=None):
    workspace = tmp_path / "staging" / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "tests").mkdir()
    (workspace / "src/parser.py").write_text(
        "def parse(value):\n    return [value]\n",
        encoding="utf-8",
    )
    (workspace / "src/formatter.py").write_text(
        "def format_value(value):\n    return value\n",
        encoding="utf-8",
    )
    (workspace / "tests/test_parser.py").write_text("# protected by controller\n")

    task_data = {**task_dict, "model_policy_id": "fake-policy"}
    task_data["repository"] = {
        "source": "local",
        "path": str(workspace),
        "base_commit": "a" * 40,
    }
    task = TaskSpec.model_validate(task_data)
    selected_plan = plan or Plan(
        items=(
            WorkItem(
                work_item_id="fix",
                title="Fix parser",
                objective="Return [] for empty input",
                expected_artifacts=("src/parser.py",),
                acceptance_ids=("unit",),
                allowed_tools=(
                    "search_repo",
                    "read_file",
                    "retrieve_code",
                    "replace_text",
                    "apply_patch",
                    "run_check",
                ),
            ),
        )
    )
    store = SQLiteEventStore(tmp_path / "control.sqlite3")
    service = HarnessService(store)
    run = store.create(task, "create")
    service.set_plan(run.run_id, selected_plan, "plan")
    leased = service.acquire_lease(run.run_id, "agent-worker", "lease", ttl_seconds=600)
    token = LeaseToken.from_run(leased)
    service.transition(run.run_id, RunStatus.RUNNING, token, "start")

    artifacts = ArtifactStore(tmp_path / "artifacts")
    snapshots = SnapshotManager(artifacts)
    tool_gateway = WorkspaceToolGateway(
        workspace,
        task,
        selected_plan.items[0],
        snapshots,
        checker or ParserCheck(),
        SQLiteCodeRetriever(tmp_path / "retrieval.sqlite3", snapshots),
    )
    model = ScriptedModel(actions)
    campaign = CampaignBudget(
        campaign_id="fake-agent-loop",
        currency="CNY",
        max_cost="3.00",
        max_cost_per_call="1.00",
    )
    pricing = PriceCard(
        currency="CNY",
        input_per_million="3.00",
        cached_input_per_million="0.30",
        output_per_million="9.00",
        version="test-price",
        source_url="https://example.test/pricing",
    )
    runner = CodingAgentRunner(
        service,
        model,
        CampaignBudgetLedger(tmp_path / "campaign.sqlite3"),
        tool_gateway,
        artifacts,
        provider_id="fake-provider",
        model_id="fake-model",
        pricing=pricing,
        campaign=campaign,
        config=AgentLoopConfig(max_model_iterations=8, max_output_tokens=256),
    )
    return runner, store, run.run_id, token, workspace, model


def multi_step_contract(task_dict):
    task = {
        **task_dict,
        "budgets": {**task_dict["budgets"], "max_steps": 20},
        "acceptance": [
            {"id": "empty", "command": "check empty", "required": True},
            {"id": "none", "command": "check none", "required": True},
        ],
    }
    plan = Plan(
        items=(
            WorkItem(
                work_item_id="guard-empty",
                title="Handle empty strings",
                objective="Create the empty-string branch",
                expected_artifacts=("src/parser.py",),
                acceptance_ids=("empty",),
                allowed_tools=("read_file", "replace_text", "run_check"),
            ),
            WorkItem(
                work_item_id="guard-none",
                title="Handle null values",
                objective="Create the null-value branch without regressing empty strings",
                dependencies=("guard-empty",),
                expected_artifacts=("src/parser.py",),
                acceptance_ids=("none",),
                allowed_tools=("read_file", "replace_text", "run_check"),
            ),
        )
    )
    first_actions = [
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "items = [] if value == '' else [value]\n    return items",
            },
        ),
        ("submit", {"summary": "Empty strings are handled."}),
    ]
    second_actions = [
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "return items",
                "new": "return items if value is not None else []",
            },
        ),
        ("submit", {"summary": "Null values are handled without regressing empty strings."}),
    ]
    return task, plan, first_actions, second_actions


def interrupt_after_replace_effect(tmp_path: Path, task_dict):
    arguments = {
        "path": "src/parser.py",
        "old": "return [value]",
        "new": "return [] if value == '' else [value]",
    }
    runner, store, run_id, token, workspace, model = setup_loop(
        tmp_path,
        task_dict,
        [("replace_text", arguments)],
    )
    real_tools = runner.tools
    runner.tools = InterruptAfterToolDispatch(real_tools)
    with pytest.raises(RuntimeError, match="tool side effect"):
        runner.run(run_id, token)
    assert "return [] if" in (workspace / "src/parser.py").read_text(encoding="utf-8")
    RecoveryService(runner.service, runner.campaign_ledger, runner.session_store).reconcile(
        run_id,
        token,
    )
    call_id = next(iter(store.get(run_id).unknown_tool_calls))
    return runner, store, run_id, token, workspace, model, real_tools, call_id


def test_explicit_multi_file_patch_acceptance_completes_turn_without_replay(
    tmp_path,
    task_dict,
):
    arguments = {
        "edits": [
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            },
            {
                "path": "src/formatter.py",
                "old": "return value",
                "new": "return str(value)",
            },
        ]
    }
    runner, store, run_id, first_token, workspace, first_model = setup_loop(
        tmp_path,
        task_dict,
        [("apply_patch", arguments)],
        checker=MultiFileCheck(),
    )
    real_tools = runner.tools
    runner.tools = InterruptAfterToolDispatch(real_tools)
    with pytest.raises(RuntimeError, match="tool side effect"):
        runner.run(run_id, first_token)
    RecoveryService(runner.service, runner.campaign_ledger, runner.session_store).reconcile(
        run_id,
        first_token,
    )
    call_id = next(iter(store.get(run_id).unknown_tool_calls))

    resolved = ToolRecoveryService(
        runner.service,
        runner.session_store,
        real_tools,
    ).resolve_write(
        run_id,
        call_id,
        decision="accept",
        token=first_token,
    )

    assert resolved.tool_calls[-1].status == "success"
    assert resolved.tool_calls[-1].recovery_disposition == "accept_patch"
    assert resolved.agent_session is not None
    assert resolved.agent_session.next_iteration == 2
    runner.service.release_lease(run_id, first_token, "release-accepted-patch")

    resumed_service = HarnessService(SQLiteEventStore(store.path))
    leased = resumed_service.acquire_lease(run_id, "agent-worker-2", "lease-accepted-patch")
    second_token = LeaseToken.from_run(leased)
    second_model = ScriptedModel([("submit", {"summary": "Accepted recovered patch effect."})])
    final = CodingAgentRunner(
        resumed_service,
        second_model,
        runner.campaign_ledger,
        real_tools,
        runner.session_store,
        provider_id=runner.provider_id,
        model_id=runner.model_id,
        pricing=runner.pricing,
        campaign=runner.campaign,
        config=runner.config,
    ).run(run_id, second_token)

    assert final.status == RunStatus.SUCCEEDED
    assert len(first_model.requests) == 1
    assert len(second_model.requests) == 1
    assert "return [] if" in (workspace / "src/parser.py").read_text(encoding="utf-8")
    assert "return str(value)" in (workspace / "src/formatter.py").read_text(encoding="utf-8")
    assert any(
        message.role == "tool" and "accept_patch" in (message.content or "")
        for message in second_model.requests[0].messages
    )


def test_multi_file_patch_resolution_rejects_partial_effect(tmp_path, task_dict):
    arguments = {
        "edits": [
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            },
            {
                "path": "src/formatter.py",
                "old": "return value",
                "new": "return str(value)",
            },
        ]
    }
    runner, store, run_id, token, workspace, _ = setup_loop(
        tmp_path,
        task_dict,
        [("apply_patch", arguments)],
        checker=MultiFileCheck(),
    )
    real_tools = runner.tools
    runner.tools = InterruptAfterToolDispatch(real_tools)
    with pytest.raises(RuntimeError, match="tool side effect"):
        runner.run(run_id, token)
    RecoveryService(runner.service, runner.campaign_ledger, runner.session_store).reconcile(
        run_id,
        token,
    )
    call_id = next(iter(store.get(run_id).unknown_tool_calls))
    (workspace / "src/formatter.py").write_text(
        "def format_value(value):\n    return value\n",
        encoding="utf-8",
    )

    with pytest.raises(Conflict, match="exact expected effect"):
        ToolRecoveryService(
            runner.service,
            runner.session_store,
            real_tools,
        ).resolve_write(
            run_id,
            call_id,
            decision="accept",
            token=token,
        )

    blocked = store.get(run_id)
    assert blocked.unknown_tool_calls == {call_id}
    assert call_id in blocked.reservations


def test_agent_loop_edits_runs_protected_validation_and_succeeds(tmp_path, task_dict):
    actions = [
        ("read_file", {"path": "src/parser.py"}),
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            },
        ),
        ("run_check", {"check_id": "unit"}),
        ("submit", {"summary": "Parser fixed and check passed."}),
    ]
    runner, store, run_id, token, workspace, model = setup_loop(tmp_path, task_dict, actions)
    result = runner.run(run_id, token)

    assert result.status == RunStatus.SUCCEEDED
    assert result.usage.model_calls == 4
    assert result.usage.tool_calls == 5  # four model tools plus protected final validation
    assert result.validation["passed_check_ids"] == ["unit"]
    assert result.passed_items == {"fix"}
    assert result.last_checkpoint is not None
    assert "return [] if" in (workspace / "src/parser.py").read_text()
    assert len(result.model_calls) == 4
    reservations = [
        ModelCallReservation.model_validate(event.payload["reservation"])
        for event in store.events(run_id)
        if event.event_type == "MODEL_CALL_RESERVED"
    ]
    assert [reservation.client_trace_id for reservation in reservations] == model.trace_ids
    for call in result.model_calls:
        assert call.response_artifact_ref is not None
        restored = ModelResponse.model_validate_json(
            runner.session_store.read(call.response_artifact_ref)
        )
        assert restored.response_id == call.response_id
        assert restored.usage == call.usage
    assert len(result.tool_calls) == 5
    assert not result.reservations
    assert not result.model_reservations
    assert not result.tool_reservations
    assert SQLiteEventStore(store.path).get(run_id).as_dict() == result.as_dict()
    assert len(model.requests) == 4
    assert "retrieve_code tool is preferred" in (model.requests[0].messages[0].content or "")
    assert "Do not repeat a saturated search" in (model.requests[0].messages[0].content or "")
    assert "follow the evidence" in (model.requests[0].messages[0].content or "")
    assert "never send only one bound" in (model.requests[0].messages[0].content or "")
    reservations = [
        ModelCallReservation.model_validate(event.payload["reservation"])
        for event in store.events(run_id)
        if event.event_type == "MODEL_CALL_RESERVED"
    ]
    ledgers = [
        MandatoryFactLedger.model_validate_json(
            runner.session_store.read(reservation.mandatory_facts_ref)
        )
        for reservation in reservations
        if reservation.mandatory_facts_ref is not None
    ]
    assert len(ledgers) == len(model.requests)
    assert all(ledger.required_acceptance_ids == ("unit",) for ledger in ledgers)
    assert all(ledger.task_spec_hash == result.task.sha256 for ledger in ledgers)
    assert all(
        reservation.mandatory_facts_ref == reservation.mandatory_facts_hash
        for reservation in reservations
    )
    assert all(
        reservation.run_memory_ref == reservation.run_memory_hash
        and reservation.run_memory_covered_event_seq >= 1
        for reservation in reservations
    )
    memories = [
        RunMemorySnapshot.model_validate_json(runner.session_store.read(reservation.run_memory_ref))
        for reservation in reservations
        if reservation.run_memory_ref is not None
    ]
    assert len(memories) == len(model.requests)
    assert memories[0].included_entry_count == 0
    assert [(entry.tool_name, entry.status) for entry in memories[1].entries] == [
        ("read_file", "active")
    ]
    assert [(entry.tool_name, entry.status) for entry in memories[2].entries] == [
        ("read_file", "stale"),
        ("replace_text", "active"),
    ]
    assert [entry.tool_name for entry in memories[3].entries] == [
        "read_file",
        "replace_text",
        "run_check",
    ]
    assert all(
        "Controller-derived Run memory" in (request.messages[0].content or "")
        for request in model.requests
    )
    assert "def parse(value)" in (model.requests[1].messages[0].content or "")
    assert "def parse(value)" not in (model.requests[2].messages[0].content or "")
    assert len({ledger.workspace_revision for ledger in ledgers}) == 2
    assert all(
        "Controller-owned mandatory facts" in (request.messages[0].content or "")
        for request in model.requests
    )


def test_agent_loop_create_file_is_bound_to_intent_receipt_memory_and_trace(
    tmp_path,
    task_dict,
):
    content = "def generated():\n    return '你好'\n"
    arguments = {"path": "src/generated.py", "content": content}
    create_plan = Plan(
        items=(
            WorkItem(
                work_item_id="create",
                title="Create generated module",
                objective="Add one bounded generated module",
                expected_artifacts=("src/generated.py",),
                acceptance_ids=("unit",),
                allowed_tools=("create_file", "run_check"),
            ),
        )
    )
    runner, store, run_id, token, workspace, _ = setup_loop(
        tmp_path,
        task_dict,
        [
            ("create_file", arguments),
            ("run_check", {"check_id": "unit"}),
            ("submit", {"summary": "Created and verified the generated module."}),
        ],
        plan=create_plan,
        checker=CreateFileCheck(),
    )

    result = runner.run(run_id, token)

    assert result.status == RunStatus.SUCCEEDED
    assert (workspace / "src/generated.py").read_bytes() == content.encode("utf-8")
    create_record = result.tool_calls[0]
    assert create_record.name == "create_file"
    assert create_record.arguments_hash == digest(arguments)
    assert create_record.workspace_revision_before != create_record.workspace_revision_after
    assert create_record.workspace_manifest_ref is not None
    manifest = runner.tools.verify_manifest(create_record.workspace_manifest_ref)
    assert any(entry.path == "src/generated.py" for entry in manifest.files)

    reservations = [
        ToolCallReservation.model_validate(event.payload["reservation"])
        for event in store.events(run_id)
        if event.event_type == "TOOL_CALL_RESERVED"
    ]
    assert reservations[0].name == "create_file"
    assert reservations[0].arguments_hash == digest(arguments)
    assert reservations[0].workspace_manifest_ref is not None
    first_response_ref = result.model_calls[0].response_artifact_ref
    assert first_response_ref is not None
    recorded = ModelResponse.model_validate_json(runner.session_store.read(first_response_ref))
    assert recorded.message.tool_calls[0].function.arguments == arguments

    second_model_reservation = next(
        ModelCallReservation.model_validate(event.payload["reservation"])
        for event in store.events(run_id)
        if event.event_type == "MODEL_CALL_RESERVED"
        and event.payload["reservation"]["call_id"] == result.model_calls[1].call_id
    )
    assert second_model_reservation.run_memory_ref is not None
    memory = RunMemorySnapshot.model_validate_json(
        runner.session_store.read(second_model_reservation.run_memory_ref)
    )
    assert memory.entries[-1].tool_name == "create_file"
    assert memory.entries[-1].kind == "workspace_change"

    trace = store.export_jsonl(run_id)
    replayed = SQLiteEventStore.replay_jsonl(trace)
    assert replayed.as_dict() == result.as_dict()


def test_agent_response_artifact_failure_is_quarantined_without_replay(tmp_path, task_dict):
    runner, store, run_id, token, workspace, model = setup_loop(
        tmp_path,
        task_dict,
        [("read_file", {"path": "src/parser.py"})],
    )
    runner.session_store = RejectResponseArtifact(runner.session_store)

    with pytest.raises(OSError, match="response artifact publication"):
        runner.run(run_id, token)

    interrupted = store.get(run_id)
    assert not interrupted.model_calls
    assert len(interrupted.model_reservations) == 1
    call_id, reservation = next(iter(interrupted.model_reservations.items()))
    assert reservation.client_trace_id == model.trace_ids[0]
    assert interrupted.unknown_model_calls == {call_id}
    assert interrupted.unknown_reservations == {call_id}
    attempt = runner.campaign_ledger.attempt(runner.campaign.campaign_id, call_id)
    assert attempt.status == "unknown"
    assert attempt.error_type == "PostResponseReceiptUnavailable"
    assert len(model.requests) == 1
    assert "return [value]" in (workspace / "src/parser.py").read_text(encoding="utf-8")

    report = RecoveryService(
        runner.service, runner.campaign_ledger, runner.session_store
    ).reconcile(
        run_id,
        token,
    )
    assert report.safe_to_resume is False
    assert report.next_action == "manual_reconciliation"
    assert report.findings[0].classification == "model_effect_unknown"
    assert report.findings[0].client_trace_id == reservation.client_trace_id


def test_agent_loop_recovers_from_one_sided_read_range_and_replays(tmp_path, task_dict):
    actions = [
        ("retrieve_code", {"query": "parse"}),
        ("read_file", {"path": "src/parser.py", "start_line": 1}),
        (
            "read_file",
            {"path": "src/parser.py", "start_line": 1, "end_line": 2},
        ),
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            },
        ),
        ("run_check", {"check_id": "unit"}),
        ("submit", {"summary": "Recovered from the invalid range and verified the fix."}),
    ]
    runner, store, run_id, token, workspace, model = setup_loop(tmp_path, task_dict, actions)

    result = runner.run(run_id, token)

    assert result.status == RunStatus.SUCCEEDED
    assert "return [] if" in (workspace / "src/parser.py").read_text(encoding="utf-8")
    reads = [record for record in result.tool_calls if record.name == "read_file"]
    assert [record.status for record in reads] == ["error", "success"]
    assert reads[0].artifact_ref is not None
    error_content = runner.session_store.read(reads[0].artifact_ref).decode("utf-8")
    assert "start_line and end_line must be supplied together" in error_content
    assert '"start_line":<start_line>,"end_line":<end_line>' in error_content
    assert any(
        message.role == "tool"
        and "start_line and end_line must be supplied together" in (message.content or "")
        for message in model.requests[2].messages
    )
    read_schema = next(tool for tool in model.requests[0].tools if tool.name == "read_file")
    assert read_schema.parameters["dependentRequired"] == {
        "start_line": ["end_line"],
        "end_line": ["start_line"],
    }
    replayed = SQLiteEventStore.replay_jsonl(store.export_jsonl(run_id))
    assert projection_hash(replayed) == projection_hash(result)


def test_agent_loop_exposes_only_task_and_work_item_allowed_tools(tmp_path, task_dict):
    task_dict["constraints"]["allowed_tools"] = ["read_file", "replace_text"]
    plan = Plan(
        items=(
            WorkItem(
                work_item_id="narrow-fix",
                title="Apply the bounded parser fix",
                objective="Use only the task-authorized read and exact replacement tools",
                expected_artifacts=("src/parser.py",),
                acceptance_ids=("unit",),
                allowed_tools=("read_file", "replace_text"),
            ),
        )
    )
    actions = [
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            },
        ),
        ("submit", {"summary": "Completed within the narrow task tool authority."}),
    ]
    runner, _, run_id, token, _, model = setup_loop(
        tmp_path,
        task_dict,
        actions,
        plan=plan,
    )

    result = runner.run(run_id, token)

    assert result.status == RunStatus.SUCCEEDED
    assert [tuple(tool.name for tool in request.tools) for request in model.requests] == [
        ("read_file", "replace_text", "submit"),
        ("read_file", "replace_text", "submit"),
    ]


def test_agent_loop_terminalizes_pre_dispatch_run_budget_stop(tmp_path, task_dict):
    runner, store, run_id, token, _, model = setup_loop(
        tmp_path,
        task_dict,
        [("read_file", {"path": "src/parser.py"})],
    )
    runner.config = runner.config.model_copy(update={"max_run_cost": Decimal("0.000001")})

    result = runner.run(run_id, token)

    assert result.status == RunStatus.FAILED
    assert result.failure_reason == BudgetStopReason.RUN_MODEL_COST_LIMIT.value
    assert result.budget_stop is not None
    assert result.budget_stop.reason_code == BudgetStopReason.RUN_MODEL_COST_LIMIT
    assert result.budget_stop.required_cost > result.budget_stop.available_cost
    assert result.model_request_budget is not None
    assert result.model_request_budget.purpose == "execution"
    assert result.model_request_budget.input_token_budget.max_input_tokens == (
        runner.config.max_input_tokens
    )
    assert result.model_request_budget.input_token_budget.estimate.request_bytes > 0
    assert result.model_request_budget.request_payload is not None
    assert (
        result.model_request_budget.request_payload.payload_bytes
        == result.model_request_budget.input_token_budget.estimate.request_bytes
    )
    assert result.model_request_budget.output_token_ceiling == runner.config.max_output_tokens
    assert result.lease_id is None
    assert result.reservations == {}
    assert result.model_reservations == {}
    assert len(model.requests) == 0
    campaign = runner.campaign_ledger.summary("fake-agent-loop")
    assert campaign.reserved_cost == Decimal("0")
    assert campaign.unknown_cost == Decimal("0")
    assert campaign.settled_cost == Decimal("0")
    replayed = SQLiteEventStore.replay_jsonl(store.export_jsonl(run_id))
    assert replayed.model_request_budget == result.model_request_budget
    assert projection_hash(replayed) == projection_hash(result)


def test_agent_loop_terminalizes_pre_dispatch_campaign_budget_stop(tmp_path, task_dict):
    runner, store, run_id, token, _, model = setup_loop(
        tmp_path,
        task_dict,
        [("read_file", {"path": "src/parser.py"})],
    )
    runner.campaign_ledger.initialize(
        runner.campaign,
        provider_id=runner.provider_id,
        model_id=runner.model_id,
    )
    for index, amount in enumerate((Decimal("1"), Decimal("1"), Decimal("0.99999"))):
        attempt_id = f"historical-{index}"
        runner.campaign_ledger.reserve(
            runner.campaign,
            attempt_id,
            f"historical-hash-{index}",
            amount,
        )
        runner.campaign_ledger.settle(
            runner.campaign.campaign_id,
            attempt_id,
            amount,
            f"historical-trace-{index}",
        )

    result = runner.run(run_id, token)

    assert result.status == RunStatus.FAILED
    assert result.failure_reason == BudgetStopReason.CAMPAIGN_COST_LIMIT.value
    assert result.budget_stop is not None
    assert result.budget_stop.reason_code == BudgetStopReason.CAMPAIGN_COST_LIMIT
    assert result.budget_stop.required_cost > result.budget_stop.available_cost
    assert result.model_request_budget is not None
    assert result.model_request_budget.purpose == "execution"
    assert result.model_request_budget.input_token_budget.estimate.request_bytes > 0
    assert result.model_request_budget.request_payload is not None
    assert (
        result.model_request_budget.request_payload.payload_bytes
        == result.model_request_budget.input_token_budget.estimate.request_bytes
    )
    assert result.model_request_budget.output_token_ceiling == runner.config.max_output_tokens
    assert result.lease_id is None
    assert result.reservations == {}
    assert len(model.requests) == 0
    campaign = runner.campaign_ledger.summary("fake-agent-loop")
    assert campaign.reserved_cost == Decimal("0")
    assert campaign.unknown_cost == Decimal("0")
    assert campaign.settled_cost == Decimal("2.99999")
    replayed = SQLiteEventStore.replay_jsonl(store.export_jsonl(run_id))
    assert replayed.model_request_budget == result.model_request_budget
    assert projection_hash(replayed) == projection_hash(result)


def test_agent_loop_executes_dependency_order_with_isolated_work_item_sessions(
    tmp_path,
    task_dict,
):
    multi_task, plan, first_actions, second_actions = multi_step_contract(task_dict)
    runner, store, run_id, token, workspace, model = setup_loop(
        tmp_path,
        multi_task,
        [*first_actions, *second_actions],
        plan=plan,
        checker=MultiStepCheck(),
    )

    result = runner.run(run_id, token)

    assert result.status == RunStatus.SUCCEEDED
    assert result.passed_items == {"guard-empty", "guard-none"}
    assert result.validation["passed_check_ids"] == ["empty", "none"]
    assert result.usage.model_calls == 4
    assert result.usage.tool_calls == 7
    content = (workspace / "src/parser.py").read_text(encoding="utf-8")
    assert "items = [] if value == '' else [value]" in content
    assert "return items if value is not None else []" in content

    reservations = [
        ModelCallReservation.model_validate(event.payload["reservation"])
        for event in store.events(run_id)
        if event.event_type == "MODEL_CALL_RESERVED"
    ]
    ledgers = [
        MandatoryFactLedger.model_validate_json(
            runner.session_store.read(reservation.mandatory_facts_ref)
        )
        for reservation in reservations
    ]
    assert [ledger.work_item_id for ledger in ledgers] == [
        "guard-empty",
        "guard-empty",
        "guard-none",
        "guard-none",
    ]
    assert ledgers[2].completed_work_item_ids == ("guard-empty",)
    assert ledgers[2].completed_work_items_hash == digest(("guard-empty",))
    assert '"completed_work_items":["guard-empty"]' in (model.requests[2].messages[1].content or "")

    third_memory = RunMemorySnapshot.model_validate_json(
        runner.session_store.read(reservations[2].run_memory_ref)
    )
    assert third_memory.work_item_id == "guard-none"
    assert {entry.work_item_id for entry in third_memory.entries} == {"guard-empty"}
    assert all(entry.status == "active" for entry in third_memory.entries)

    fourth_memory = RunMemorySnapshot.model_validate_json(
        runner.session_store.read(reservations[3].run_memory_ref)
    )
    prior_entries = [
        entry for entry in fourth_memory.entries if entry.work_item_id == "guard-empty"
    ]
    current_entries = [
        entry for entry in fourth_memory.entries if entry.work_item_id == "guard-none"
    ]
    assert prior_entries and all(entry.status == "stale" for entry in prior_entries)
    assert current_entries and all(entry.status == "active" for entry in current_entries)

    advanced = [event for event in store.events(run_id) if event.event_type == "STATE_CHANGED"]
    assert any(
        event.payload["from"] == RunStatus.VALIDATING and event.payload["to"] == RunStatus.RUNNING
        for event in advanced
    )


def test_multi_work_item_boundary_resumes_in_a_new_worker(tmp_path, task_dict):
    multi_task, plan, first_actions, second_actions = multi_step_contract(task_dict)
    runner, store, run_id, first_token, workspace, first_model = setup_loop(
        tmp_path,
        multi_task,
        first_actions,
        plan=plan,
        checker=MultiStepCheck(),
    )

    partial = runner.run(run_id, first_token, max_iterations_this_invocation=2)

    assert partial.status == RunStatus.RUNNING
    assert partial.passed_items == {"guard-empty"}
    assert partial.agent_session is not None
    assert partial.agent_session.work_item_id == "guard-none"
    assert partial.agent_session.next_iteration == 3
    runner.service.release_lease(run_id, first_token, "release-at-work-item-boundary")

    resumed_service = HarnessService(SQLiteEventStore(store.path))
    leased = resumed_service.acquire_lease(
        run_id,
        "agent-worker-2",
        "lease-at-work-item-boundary",
    )
    second_token = LeaseToken.from_run(leased)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    snapshots = SnapshotManager(artifacts)
    # Deliberately construct the adapter with the first item. The runner must restore the
    # persisted active item before exposing tools or validating the request hash.
    tools = WorkspaceToolGateway(
        workspace,
        leased.task,
        plan.items[0],
        snapshots,
        MultiStepCheck(),
        SQLiteCodeRetriever(tmp_path / "retrieval.sqlite3", snapshots),
    )
    second_model = ScriptedModel(second_actions)
    resumed = CodingAgentRunner(
        resumed_service,
        second_model,
        CampaignBudgetLedger(tmp_path / "campaign.sqlite3"),
        tools,
        artifacts,
        provider_id=runner.provider_id,
        model_id=runner.model_id,
        pricing=runner.pricing,
        campaign=runner.campaign,
        config=runner.config,
    ).run(run_id, second_token)

    assert resumed.status == RunStatus.SUCCEEDED
    assert resumed.lease_epoch == 2
    assert resumed.passed_items == {"guard-empty", "guard-none"}
    assert len(first_model.requests) == 2
    assert len(second_model.requests) == 2
    assert '"work_item_id":"guard-none"' in (second_model.requests[0].messages[1].content or "")
    assert SQLiteEventStore(store.path).get(run_id).as_dict() == resumed.as_dict()


def test_final_work_item_revalidates_prior_checks_and_repairs_regression(
    tmp_path,
    task_dict,
):
    multi_task, plan, first_actions, _ = multi_step_contract(task_dict)
    regressing_then_repairing = [
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "items = [] if value == '' else [value]\n    return items",
                "new": "items = [value]\n    return items if value is not None else []",
            },
        ),
        ("submit", {"summary": "Null values work, but this regressed empty strings."}),
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "items = [value]",
                "new": "items = [] if value == '' else [value]",
            },
        ),
        ("submit", {"summary": "The earlier empty-string invariant is restored."}),
    ]
    runner, _, run_id, token, workspace, model = setup_loop(
        tmp_path,
        multi_task,
        [*first_actions, *regressing_then_repairing],
        plan=plan,
        checker=MultiStepCheck(),
    )

    result = runner.run(run_id, token)

    assert result.status == RunStatus.SUCCEEDED
    assert result.passed_items == {"guard-empty", "guard-none"}
    assert result.usage.repair_cycles == 1
    assert len(model.requests) == 6
    feedback = model.requests[4].messages[-1].content or ""
    assert "Protected validation failed" in feedback
    assert '"check_id":"empty"' in feedback
    assert '"passed":false' in feedback
    content = (workspace / "src/parser.py").read_text(encoding="utf-8")
    assert "items = [] if value == '' else [value]" in content
    assert "return items if value is not None else []" in content


def test_agent_loop_can_use_revision_bound_code_evidence(tmp_path, task_dict):
    actions = [
        ("retrieve_code", {"query": "parse value empty", "max_chunks": 2}),
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            },
        ),
        ("submit", {"summary": "Fixed parser after revision-bound retrieval."}),
    ]
    runner, _, run_id, token, workspace, model = setup_loop(tmp_path, task_dict, actions)

    result = runner.run(run_id, token)

    assert result.status == RunStatus.SUCCEEDED
    assert result.usage.model_calls == 3
    assert result.usage.tool_calls == 4
    evidence_message = model.requests[1].messages[-1]
    assert evidence_message.role == "tool"
    evidence = json.loads(evidence_message.content)
    assert evidence["backend"] == "sqlite_fts5"
    assert evidence["workspace_revision"] == result.tool_calls[0].workspace_revision_before
    assert evidence["chunks"][0]["path"] == "src/parser.py"
    assert "return [] if" in (workspace / "src/parser.py").read_text(encoding="utf-8")


def test_no_progress_guard_blocks_third_identical_action_then_allows_progress(
    tmp_path,
    task_dict,
):
    actions = [
        ("read_file", {"path": "src/parser.py"}),
        ("read_file", {"path": "src/parser.py"}),
        ("read_file", {"path": "src/parser.py"}),
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            },
        ),
        ("submit", {"summary": "Progressed after controller feedback."}),
    ]
    runner, _, run_id, token, _, model = setup_loop(tmp_path, task_dict, actions)

    result = runner.run(run_id, token)

    assert result.status == RunStatus.SUCCEEDED
    repeated_reads = [record for record in result.tool_calls if record.name == "read_file"]
    assert [record.status for record in repeated_reads] == ["success", "success", "error"]
    denial = runner.session_store.read(repeated_reads[-1].artifact_ref).decode("utf-8")
    assert "NoProgressPolicy" in denial
    assert "already settled 2 consecutive times" in denial
    assert "NoProgressPolicy" in (model.requests[3].messages[0].content or "")
    assert result.failure_reason is None


def test_no_progress_guard_waits_on_exact_alternating_two_action_cycle(
    tmp_path,
    task_dict,
):
    actions = [
        ("read_file", {"path": "src/parser.py"}),
        ("search_repo", {"query": "return [value]"}),
    ] * 3
    runner, store, run_id, token, _, model = setup_loop(tmp_path, task_dict, actions)

    waiting = runner.run(run_id, token)

    assert waiting.status == RunStatus.WAITING_FOR_USER
    assert waiting.lease_id is None
    assert isinstance(waiting.pending_human_request, HumanGuidanceRequest)
    assert waiting.pending_human_request.pattern == "alternating_two_action_cycle"
    assert waiting.pending_human_request.source_tool_call_id == waiting.tool_calls[-1].call_id
    assert len(model.requests) == 6
    assert [(record.name, record.status) for record in waiting.tool_calls] == [
        ("read_file", "success"),
        ("search_repo", "success"),
        ("read_file", "success"),
        ("search_repo", "success"),
        ("read_file", "error"),
        ("search_repo", "error"),
    ]
    denial = runner.session_store.read(waiting.tool_calls[-1].artifact_ref).decode("utf-8")
    assert "exact alternating two-action cycle" in denial
    assert "6 consecutive unchanged-revision receipts" in denial
    replayed = SQLiteEventStore.replay_jsonl(store.export_jsonl(run_id))
    assert replayed.as_dict() == waiting.as_dict()
    assert projection_hash(replayed) == projection_hash(waiting)


def test_no_progress_guard_does_not_block_a_b_a_b_c_sequence(tmp_path, task_dict):
    actions = [
        ("read_file", {"path": "src/parser.py"}),
        ("search_repo", {"query": "return [value]"}),
        ("read_file", {"path": "src/parser.py"}),
        ("search_repo", {"query": "return [value]"}),
        ("read_file", {"path": "src/formatter.py"}),
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            },
        ),
        ("submit", {"summary": "Changed course before completing the cycle."}),
    ]
    runner, _, run_id, token, _, _ = setup_loop(tmp_path, task_dict, actions)

    result = runner.run(run_id, token)

    assert result.status == RunStatus.SUCCEEDED
    assert [record.status for record in result.tool_calls[:5]] == ["success"] * 5
    assert result.pending_human_request is None


def test_agent_replans_once_after_no_progress_evidence_and_finishes(tmp_path, task_dict):
    revised_item = WorkItem(
        work_item_id="fix-after-stall",
        title="Fix parser after repeated reads",
        objective="Use the collected evidence to implement the empty-input branch",
        expected_artifacts=("src/parser.py",),
        acceptance_ids=("unit",),
        allowed_tools=("read_file", "replace_text", "run_check"),
    )
    actions = [
        ("read_file", {"path": "src/parser.py"}),
        ("read_file", {"path": "src/parser.py"}),
        ("read_file", {"path": "src/parser.py"}),
        (
            "revise_plan",
            {
                "reason": "Repeated reads produced no new evidence; switch to one edit-ready item.",
                "items": [revised_item.model_dump(mode="json")],
            },
        ),
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            },
        ),
        ("submit", {"summary": "Completed the evidence-driven revised Plan."}),
    ]
    runner, store, run_id, token, _, model = setup_loop(tmp_path, task_dict, actions)

    result = runner.run(run_id, token)

    assert result.status == RunStatus.SUCCEEDED
    assert result.plan is not None
    assert result.plan.version == 2
    assert result.plan.items == (revised_item,)
    assert len(result.execution_replans) == 1
    replan = result.execution_replans[0]
    assert replan.old_plan_version == 1
    assert replan.new_plan_version == 2
    assert replan.preserved_work_item_ids == ()
    assert result.plan_source_model_call_id == replan.source_model_call_id
    assert [record.name for record in result.tool_calls[:4]] == [
        "read_file",
        "read_file",
        "read_file",
        "revise_plan",
    ]
    assert [record.status for record in result.tool_calls[:4]] == [
        "success",
        "success",
        "error",
        "success",
    ]
    assert "revise_plan" not in {tool.name for tool in model.requests[0].tools}
    assert "revise_plan" not in {tool.name for tool in model.requests[1].tools}
    assert "revise_plan" in {tool.name for tool in model.requests[2].tools}
    assert "revise_plan" in {tool.name for tool in model.requests[3].tools}
    assert "revise_plan" not in {tool.name for tool in model.requests[4].tools}
    assert '"work_item_id":"fix-after-stall"' in (model.requests[4].messages[1].content or "")
    replayed = SQLiteEventStore.replay_jsonl(store.export_jsonl(run_id))
    assert replayed.as_dict() == result.as_dict()
    assert projection_hash(replayed) == projection_hash(result)


def test_execution_replan_preserves_completed_work_item_and_replaces_remaining_plan(
    tmp_path,
    task_dict,
):
    multi_task, plan, first_actions, second_actions = multi_step_contract(task_dict)
    revised_second = plan.items[1].model_copy(
        update={
            "work_item_id": "guard-none-replanned",
            "title": "Handle null values from current evidence",
        }
    )
    replan_action = (
        "revise_plan",
        {
            "reason": "The completed empty-string item remains valid; replace only the remainder.",
            "items": [
                plan.items[0].model_dump(mode="json"),
                revised_second.model_dump(mode="json"),
            ],
        },
    )
    runner, _, run_id, token, _, model = setup_loop(
        tmp_path,
        multi_task,
        [*first_actions, replan_action, *second_actions],
        plan=plan,
        checker=MultiStepCheck(),
    )

    result = runner.run(run_id, token)

    assert result.status == RunStatus.SUCCEEDED
    assert result.plan is not None
    assert result.plan.version == 2
    assert result.plan.items[0] == plan.items[0]
    assert result.plan.items[1] == revised_second
    assert result.passed_items == {"guard-empty", "guard-none-replanned"}
    assert result.execution_replans[0].preserved_work_item_ids == ("guard-empty",)
    assert "revise_plan" not in {tool.name for tool in model.requests[0].tools}
    assert "revise_plan" not in {tool.name for tool in model.requests[1].tools}
    assert "revise_plan" in {tool.name for tool in model.requests[2].tools}
    assert '"completed_work_items":["guard-empty"]' in (model.requests[3].messages[1].content or "")
    assert '"work_item_id":"guard-none-replanned"' in (model.requests[3].messages[1].content or "")


def test_invalid_execution_replan_keeps_original_plan_and_returns_policy_evidence(
    tmp_path,
    task_dict,
):
    multi_task, plan, first_actions, second_actions = multi_step_contract(task_dict)
    changed_completed = plan.items[0].model_copy(update={"title": "Rewrite completed history"})
    invalid_replan = (
        "revise_plan",
        {
            "reason": "Attempt to rewrite an already validated item.",
            "items": [
                changed_completed.model_dump(mode="json"),
                plan.items[1].model_dump(mode="json"),
            ],
        },
    )
    runner, _, run_id, token, _, model = setup_loop(
        tmp_path,
        multi_task,
        [*first_actions, invalid_replan, *second_actions],
        plan=plan,
        checker=MultiStepCheck(),
    )

    result = runner.run(run_id, token)

    assert result.status == RunStatus.SUCCEEDED
    assert result.plan == plan
    assert result.execution_replans == []
    rejected = next(record for record in result.tool_calls if record.name == "revise_plan")
    assert rejected.status == "error"
    denial = runner.session_store.read(rejected.artifact_ref).decode("utf-8")
    assert "must preserve every passed WorkItem verbatim" in denial
    assert "ExecutionReplanPolicy" in (model.requests[3].messages[-1].content or "")


def test_no_progress_guard_waits_for_guidance_and_resumes_from_guided_session(
    tmp_path,
    task_dict,
):
    runner, store, run_id, token, _, model = setup_loop(
        tmp_path,
        task_dict,
        [("read_file", {"path": "src/parser.py"})] * 4,
    )

    waiting = runner.run(run_id, token)

    assert waiting.status == RunStatus.WAITING_FOR_USER
    assert waiting.failure_reason is None
    assert waiting.resume_state == RunStatus.RUNNING
    assert waiting.lease_id is None
    assert isinstance(waiting.pending_human_request, HumanGuidanceRequest)
    assert waiting.pending_human_request.reason_code == "repeated_action_no_progress"
    assert waiting.pending_human_request.pattern == "identical_action"
    assert waiting.pending_human_request.source_tool_call_id == waiting.tool_calls[-1].call_id
    assert len(model.requests) == 4
    assert len(waiting.tool_calls) == 4
    assert [record.status for record in waiting.tool_calls] == [
        "success",
        "success",
        "error",
        "error",
    ]
    assert waiting.agent_session is not None
    session = AgentSession.model_validate_json(
        runner.session_store.read(waiting.agent_session.artifact_ref)
    )
    assert session.next_iteration == 5
    assert session.messages[-1].role == "tool"
    assert "NoProgressPolicy" in (session.messages[-1].content or "")
    assert [event.event_type for event in store.events(run_id)[-3:]] == [
        "HUMAN_REQUEST_CREATED",
        "STATE_CHANGED",
        "LEASE_RELEASED",
    ]

    control = HarnessService(store)
    recovery_lease = control.acquire_lease(run_id, "recovery", "guidance-recovery-lease")
    recovery_token = LeaseToken.from_run(recovery_lease)
    report = RecoveryService(
        control,
        runner.campaign_ledger,
        runner.session_store,
    ).reconcile(run_id, recovery_token)
    assert report.safe_to_resume is False
    assert report.next_action == "provide_operator_guidance"
    assert not any(item.classification == "unsafe_agent_boundary" for item in report.findings)
    control.release_lease(run_id, recovery_token, "guidance-recovery-release")

    guidance_lease = control.acquire_lease(run_id, "operator", "guidance-lease")
    guided = (
        OperatorGuidanceService(control, runner.session_store)
        .apply(
            run_id,
            "Inspect the parser branch, make the smallest exact edit, then submit for validation.",
            LeaseToken.from_run(guidance_lease),
            "apply-guidance",
        )
        .run
    )
    assert guided.status == RunStatus.RUNNING
    assert guided.lease_id is None
    assert guided.pending_human_request is None
    assert guided.pending_human_session_ref is None
    assert guided.no_progress_reset_tool_count == 4
    assert guided.human_decisions[-1].kind == "operator_guidance_supplied"
    guided_session = AgentSession.model_validate_json(
        runner.session_store.read(guided.agent_session.artifact_ref)
    )
    assert guided_session.next_iteration == 5
    assert "Trusted operator guidance" in (guided_session.messages[-1].content or "")

    resumed_model = ScriptedModel(
        [
            ("read_file", {"path": "src/parser.py"}),
            (
                "replace_text",
                {
                    "path": "src/parser.py",
                    "old": "return [value]",
                    "new": "return [] if value == '' else [value]",
                },
            ),
            ("submit", {"summary": "Applied the operator's bounded guidance."}),
        ]
    )
    runner.model = resumed_model
    resumed_lease = control.acquire_lease(run_id, "agent-worker-2", "resume-after-guidance")
    result = runner.run(run_id, LeaseToken.from_run(resumed_lease))

    assert result.status == RunStatus.SUCCEEDED
    assert result.tool_calls[4].name == "read_file"
    assert result.tool_calls[4].status == "success"
    assert any(
        "Trusted operator guidance" in (message.content or "")
        for message in resumed_model.requests[0].messages
    )
    replayed = SQLiteEventStore.replay_jsonl(store.export_jsonl(run_id))
    assert replayed.as_dict() == result.as_dict()
    assert projection_hash(replayed) == projection_hash(result)


def test_agent_loop_reenters_running_after_failed_validation_and_repairs(tmp_path, task_dict):
    actions = [
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return ['wrong']",
            },
        ),
        ("submit", {"summary": "First attempt."}),
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "return ['wrong']",
                "new": "return [] if value == '' else [value]",
            },
        ),
        ("submit", {"summary": "Repaired after validation evidence."}),
    ]
    runner, _, run_id, token, workspace, model = setup_loop(tmp_path, task_dict, actions)
    result = runner.run(run_id, token)

    assert result.status == RunStatus.SUCCEEDED
    assert result.usage.repair_cycles == 1
    assert result.usage.model_calls == 4
    assert result.usage.tool_calls == 6  # four model tools plus two protected validations
    assert "return [] if" in (workspace / "src/parser.py").read_text()
    assert "Protected validation failed" in model.requests[2].messages[-1].content


def test_agent_loop_resumes_from_persisted_session_in_a_new_worker(tmp_path, task_dict):
    first_actions = [("read_file", {"path": "src/parser.py"})]
    runner, store, run_id, first_token, workspace, _ = setup_loop(
        tmp_path,
        task_dict,
        first_actions,
    )

    paused = runner.run(run_id, first_token, max_iterations_this_invocation=1)
    assert paused.status == RunStatus.RUNNING
    assert paused.agent_session is not None
    assert paused.agent_session.next_iteration == 2
    runner.service.release_lease(run_id, first_token, "release-first-worker")

    reopened = SQLiteEventStore(store.path)
    service = HarnessService(reopened)
    leased = service.acquire_lease(run_id, "agent-worker-2", "lease-second-worker")
    second_token = LeaseToken.from_run(leased)
    resumed_model = ScriptedModel(
        [
            (
                "replace_text",
                {
                    "path": "src/parser.py",
                    "old": "return [value]",
                    "new": "return [] if value == '' else [value]",
                },
            ),
            ("run_check", {"check_id": "unit"}),
            ("submit", {"summary": "Resumed worker completed the fix."}),
        ]
    )
    artifacts = ArtifactStore(tmp_path / "artifacts")
    plan = leased.plan
    assert plan is not None
    snapshots = SnapshotManager(artifacts)
    tools = WorkspaceToolGateway(
        workspace,
        leased.task,
        plan.items[0],
        snapshots,
        ParserCheck(),
        SQLiteCodeRetriever(tmp_path / "retrieval.sqlite3", snapshots),
    )
    resumed = CodingAgentRunner(
        service,
        resumed_model,
        CampaignBudgetLedger(tmp_path / "campaign.sqlite3"),
        tools,
        artifacts,
        provider_id=runner.provider_id,
        model_id=runner.model_id,
        pricing=runner.pricing,
        campaign=runner.campaign,
        config=runner.config,
    ).run(run_id, second_token)

    assert resumed.status == RunStatus.SUCCEEDED
    assert resumed.lease_epoch == 2
    assert resumed.usage.model_calls == 4
    assert resumed.usage.tool_calls == 5
    restored_messages = resumed_model.requests[0].messages
    assert any(
        message.role == "assistant"
        and message.tool_calls
        and message.tool_calls[0].function.name == "read_file"
        for message in restored_messages
    )
    assert any(
        message.role == "tool" and "def parse" in (message.content or "")
        for message in restored_messages
    )
    assert SQLiteEventStore(store.path).get(run_id).as_dict() == resumed.as_dict()


def test_agent_loop_resumes_persisted_model_response_without_rebilling(tmp_path, task_dict):
    runner, store, run_id, first_token, workspace, first_model = setup_loop(
        tmp_path,
        task_dict,
        [
            (
                "replace_text",
                {
                    "path": "src/parser.py",
                    "old": "return [value]",
                    "new": "return [] if value == '' else [value]",
                },
            )
        ],
    )
    ledger = runner.campaign_ledger
    runner.campaign_ledger = InterruptBeforeCampaignSettlement(ledger)

    with pytest.raises(RuntimeError, match="campaign settlement"):
        runner.run(run_id, first_token)

    interrupted = store.get(run_id)
    assert interrupted.agent_session is not None
    assert interrupted.agent_session.next_iteration == 1
    assert len(interrupted.model_calls) == 1
    assert interrupted.model_calls[0].response_artifact_ref is not None
    pending_reservation = ModelCallReservation.model_validate(
        [event for event in store.events(run_id) if event.event_type == "MODEL_CALL_RESERVED"][
            -1
        ].payload["reservation"]
    )
    assert pending_reservation.run_memory_ref is not None
    pending_memory = RunMemorySnapshot.model_validate_json(
        runner.session_store.read(pending_reservation.run_memory_ref)
    )
    assert pending_memory.covered_event_seq == pending_reservation.run_memory_covered_event_seq
    assert pending_memory.sha256 == pending_reservation.run_memory_hash
    assert "return [value]" in (workspace / "src/parser.py").read_text()
    report = RecoveryService(runner.service, ledger, runner.session_store).reconcile(
        run_id, first_token
    )
    assert report.safe_to_resume is True
    assert report.next_action == "resume"
    runner.service.release_lease(run_id, first_token, "release-interrupted-worker")

    service = HarnessService(SQLiteEventStore(store.path))
    leased = service.acquire_lease(run_id, "agent-worker-2", "lease-recovered-response")
    second_token = LeaseToken.from_run(leased)
    second_model = ScriptedModel([("submit", {"summary": "Recovered response completed."})])
    resumed = CodingAgentRunner(
        service,
        second_model,
        ledger,
        runner.tools,
        runner.session_store,
        provider_id=runner.provider_id,
        model_id=runner.model_id,
        pricing=runner.pricing,
        campaign=runner.campaign,
        config=runner.config,
    ).run(run_id, second_token)

    assert resumed.status == RunStatus.SUCCEEDED
    assert len(first_model.requests) == 1
    assert len(second_model.requests) == 1
    assert len(resumed.model_calls) == 2
    assert "return [] if" in (workspace / "src/parser.py").read_text()
    assert any(
        message.role == "tool" and "Replaced 1 occurrence" in (message.content or "")
        for message in second_model.requests[0].messages
    )


def test_pending_model_response_rejects_mandatory_fact_schema_drift(tmp_path, task_dict):
    runner, _, run_id, token, _, first_model = setup_loop(
        tmp_path,
        task_dict,
        [("read_file", {"path": "src/parser.py"})],
    )
    ledger = runner.campaign_ledger
    runner.campaign_ledger = InterruptBeforeCampaignSettlement(ledger)

    with pytest.raises(RuntimeError, match="campaign settlement"):
        runner.run(run_id, token)

    report = RecoveryService(runner.service, ledger, runner.session_store).reconcile(
        run_id,
        token,
    )
    assert report.safe_to_resume is True
    resumed_model = ScriptedModel([("submit", {"summary": "must not execute"})])
    resumed = CodingAgentRunner(
        runner.service,
        resumed_model,
        ledger,
        ChangedToolSchema(runner.tools),
        runner.session_store,
        provider_id=runner.provider_id,
        model_id=runner.model_id,
        pricing=runner.pricing,
        campaign=runner.campaign,
        config=runner.config,
    )

    with pytest.raises(Conflict, match="different mandatory fact ledger"):
        resumed.run(run_id, token)

    assert len(first_model.requests) == 1
    assert resumed_model.requests == []


@pytest.mark.parametrize(
    ("tool_name", "arguments", "output_marker"),
    [
        ("read_file", {"path": "src/parser.py"}, "def parse"),
        (
            "retrieve_code",
            {"query": "parse value", "max_chunks": 2},
            '"backend":"sqlite_fts5"',
        ),
    ],
)
def test_explicit_readonly_retry_resumes_without_replaying_model(
    tmp_path,
    task_dict,
    tool_name,
    arguments,
    output_marker,
):
    runner, store, run_id, first_token, workspace, first_model = setup_loop(
        tmp_path,
        task_dict,
        [(tool_name, arguments)],
    )
    real_tools = runner.tools
    runner.tools = InterruptToolDispatch(real_tools)

    with pytest.raises(RuntimeError, match="durable tool intent"):
        runner.run(run_id, first_token)

    interrupted = store.get(run_id)
    assert len(interrupted.model_calls) == 1
    assert len(interrupted.tool_reservations) == 1
    call_id = next(iter(interrupted.tool_reservations))
    report = RecoveryService(
        runner.service,
        runner.campaign_ledger,
        runner.session_store,
    ).reconcile(run_id, first_token)
    assert report.safe_to_resume is False
    assert report.findings[-1].classification == "tool_effect_unknown"

    revision, manifest_ref = real_tools.checkpoint()
    resolved = ToolRecoveryService(runner.service, runner.session_store).authorize_readonly_retry(
        run_id,
        call_id,
        current_workspace_revision=revision,
        current_workspace_manifest_ref=manifest_ref,
        token=first_token,
    )
    assert not resolved.reservations
    assert not resolved.unknown_tool_calls
    assert resolved.tool_calls[-1].status == "cancelled"
    assert resolved.tool_calls[-1].recovery_disposition == "retry_readonly"
    resume_report = RecoveryService(
        runner.service,
        runner.campaign_ledger,
        runner.session_store,
    ).reconcile(run_id, first_token)
    assert resume_report.safe_to_resume is True
    runner.service.release_lease(run_id, first_token, "release-readonly-recovery")

    resumed_service = HarnessService(SQLiteEventStore(store.path))
    leased = resumed_service.acquire_lease(run_id, "agent-worker-2", "lease-readonly-retry")
    second_token = LeaseToken.from_run(leased)
    second_model = ScriptedModel(
        [
            (
                "replace_text",
                {
                    "path": "src/parser.py",
                    "old": "return [value]",
                    "new": "return [] if value == '' else [value]",
                },
            ),
            ("submit", {"summary": "Completed after explicit read-only retry."}),
        ]
    )
    final = CodingAgentRunner(
        resumed_service,
        second_model,
        runner.campaign_ledger,
        real_tools,
        runner.session_store,
        provider_id=runner.provider_id,
        model_id=runner.model_id,
        pricing=runner.pricing,
        campaign=runner.campaign,
        config=runner.config,
    ).run(run_id, second_token)

    assert final.status == RunStatus.SUCCEEDED
    assert len(first_model.requests) == 1
    assert len(second_model.requests) == 2
    assert len(final.model_calls) == 3
    assert [call.status for call in final.tool_calls].count("cancelled") == 1
    assert any(
        message.role == "tool" and output_marker in (message.content or "")
        for message in second_model.requests[0].messages
    )


def test_uncertain_run_check_can_be_explicitly_discarded_without_replay(
    tmp_path,
    task_dict,
):
    runner, store, run_id, first_token, workspace, first_model = setup_loop(
        tmp_path,
        task_dict,
        [("run_check", {"check_id": "unit"})],
    )
    real_tools = runner.tools
    runner.tools = InterruptAfterToolDispatch(real_tools)

    with pytest.raises(RuntimeError, match="tool side effect"):
        runner.run(run_id, first_token)

    blocked = RecoveryService(
        runner.service,
        runner.campaign_ledger,
        runner.session_store,
    ).reconcile(run_id, first_token)
    assert blocked.safe_to_resume is False
    call_id = next(iter(store.get(run_id).unknown_tool_calls))
    revision, manifest_ref = real_tools.checkpoint()
    recovery = ToolRecoveryService(runner.service, runner.session_store)

    with pytest.raises(Conflict, match="sandbox stopped"):
        recovery.discard_check_result(
            run_id,
            call_id,
            current_workspace_revision=revision,
            current_workspace_manifest_ref=manifest_ref,
            sandbox_stopped=False,
            token=first_token,
        )

    unresolved = store.get(run_id)
    assert unresolved.unknown_tool_calls == {call_id}
    original = (workspace / "src/parser.py").read_text(encoding="utf-8")
    (workspace / "src/parser.py").write_text(
        "def parse(value):\n    return ['external drift']\n",
        encoding="utf-8",
    )
    drift_revision, drift_manifest = real_tools.checkpoint()
    with pytest.raises(Conflict, match="Workspace changed"):
        recovery.discard_check_result(
            run_id,
            call_id,
            current_workspace_revision=drift_revision,
            current_workspace_manifest_ref=drift_manifest,
            sandbox_stopped=True,
            token=first_token,
        )
    (workspace / "src/parser.py").write_text(original, encoding="utf-8")
    revision, manifest_ref = real_tools.checkpoint()
    resolved = recovery.discard_check_result(
        run_id,
        call_id,
        current_workspace_revision=revision,
        current_workspace_manifest_ref=manifest_ref,
        sandbox_stopped=True,
        token=first_token,
    )
    assert not resolved.reservations
    assert not resolved.unknown_tool_calls
    assert resolved.tool_calls[-1].status == "cancelled"
    assert resolved.tool_calls[-1].recovery_disposition == "discard_check"
    assert resolved.agent_session is not None
    assert resolved.agent_session.next_iteration == 2
    safe = RecoveryService(
        runner.service,
        runner.campaign_ledger,
        runner.session_store,
    ).reconcile(run_id, first_token)
    assert safe.safe_to_resume is True
    runner.service.release_lease(run_id, first_token, "release-discarded-check")

    resumed_service = HarnessService(SQLiteEventStore(store.path))
    leased = resumed_service.acquire_lease(
        run_id,
        "agent-worker-2",
        "lease-after-discarded-check",
        ttl_seconds=600,
    )
    second_token = LeaseToken.from_run(leased)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    snapshots = SnapshotManager(artifacts)
    resumed_tools = WorkspaceToolGateway(
        workspace,
        leased.task,
        leased.plan.items[0],
        snapshots,
        ParserCheck(),
        SQLiteCodeRetriever(tmp_path / "retrieval.sqlite3", snapshots),
    )
    second_model = ScriptedModel(
        [
            (
                "replace_text",
                {
                    "path": "src/parser.py",
                    "old": "return [value]",
                    "new": "return [] if value == '' else [value]",
                },
            ),
            ("submit", {"summary": "Repaired after discarding an uncertain check."}),
        ]
    )
    final = CodingAgentRunner(
        resumed_service,
        second_model,
        CampaignBudgetLedger(tmp_path / "campaign.sqlite3"),
        resumed_tools,
        artifacts,
        provider_id=runner.provider_id,
        model_id=runner.model_id,
        pricing=runner.pricing,
        campaign=runner.campaign,
        config=runner.config,
    ).run(run_id, second_token)

    assert final.status == RunStatus.SUCCEEDED
    assert len(first_model.requests) == 1
    assert len(second_model.requests) == 2
    assert len(final.model_calls) == 3
    assert final.tool_calls[0].recovery_disposition == "discard_check"
    restored_messages = second_model.requests[0].messages
    assert any(
        message.role == "tool" and "discard_check" in (message.content or "")
        for message in restored_messages
    )
    assert not any(
        "expected [] for empty input" in (message.content or "") for message in restored_messages
    )


def test_uncertain_run_check_can_accept_exact_stopped_attempt_result(
    tmp_path,
    task_dict,
):
    runner, store, run_id, first_token, workspace, first_model = setup_loop(
        tmp_path,
        task_dict,
        [("run_check", {"check_id": "unit"})],
    )
    real_tools = runner.tools
    runner.tools = InterruptAfterToolDispatch(real_tools)

    with pytest.raises(RuntimeError, match="tool side effect"):
        runner.run(run_id, first_token)

    blocked = RecoveryService(
        runner.service,
        runner.campaign_ledger,
        runner.session_store,
    ).reconcile(run_id, first_token)
    assert blocked.safe_to_resume is False
    call_id = next(iter(store.get(run_id).unknown_tool_calls))
    revision, manifest_ref = real_tools.checkpoint()
    check = next(item for item in store.get(run_id).task.acceptance if item.id == "unit")
    recovered_result = ParserCheck().execute(workspace, check)
    assert recovered_result.passed is False

    recovery = ToolRecoveryService(runner.service, runner.session_store)
    assert (
        recovery.validate_check_result_recovery(
            run_id,
            call_id,
            current_workspace_revision=revision,
            current_workspace_manifest_ref=manifest_ref,
            token=first_token,
        )
        == "unit"
    )
    resolved = recovery.accept_check_result(
        run_id,
        call_id,
        recovered_result,
        current_workspace_revision=revision,
        current_workspace_manifest_ref=manifest_ref,
        token=first_token,
    )

    assert not resolved.reservations
    assert not resolved.unknown_tool_calls
    assert resolved.tool_calls[-1].status == "error"
    assert resolved.tool_calls[-1].recovery_disposition == "accept_check_result"
    assert resolved.agent_session is not None
    restored_session = AgentSession.model_validate_json(
        runner.session_store.read(resolved.agent_session.artifact_ref)
    )
    recovered_message = restored_session.messages[-1]
    assert recovered_message.role == "tool"
    assert "recovery=controller_verified_stopped_attempt" in recovered_message.content
    assert "passed=false" in recovered_message.content
    assert "expected [] for empty input" in recovered_message.content
    assert len(first_model.requests) == 1
    assert (
        RecoveryService(
            runner.service,
            runner.campaign_ledger,
            runner.session_store,
        )
        .reconcile(run_id, first_token)
        .safe_to_resume
        is True
    )


@pytest.mark.parametrize("fail_cleanup", [False, True])
def test_durable_check_attempt_cleanup_happens_after_tool_receipt(
    tmp_path,
    task_dict,
    fail_cleanup,
):
    checker = DurableAttemptCheck(fail_cleanup=fail_cleanup)
    runner, store, run_id, token, _, _ = setup_loop(
        tmp_path,
        task_dict,
        [("run_check", {"check_id": "unit"})],
        checker=checker,
    )
    checker.store = store
    checker.run_id = run_id

    result = runner.run(run_id, token, max_iterations_this_invocation=1)

    assert result.status == RunStatus.RUNNING
    assert result.tool_calls[-1].status == "error"
    call_id = result.tool_calls[-1].call_id
    assert checker.events == [("execute", call_id), ("cleanup_after_settlement", call_id)]
    if fail_cleanup:
        assert runner.tools.check_cleanup_failures == {
            call_id: "simulated post-receipt cleanup failure"
        }
    else:
        assert runner.tools.check_cleanup_failures == {}


def test_check_result_recovery_rejects_inconsistent_result(
    tmp_path,
    task_dict,
):
    runner, store, run_id, token, workspace, _ = setup_loop(
        tmp_path,
        task_dict,
        [("run_check", {"check_id": "unit"})],
    )
    runner.tools = InterruptAfterToolDispatch(runner.tools)
    with pytest.raises(RuntimeError, match="tool side effect"):
        runner.run(run_id, token)
    blocked = RecoveryService(
        runner.service,
        runner.campaign_ledger,
        runner.session_store,
    ).reconcile(run_id, token)
    assert blocked.safe_to_resume is False
    call_id = next(iter(store.get(run_id).unknown_tool_calls))
    revision, manifest_ref = runner.tools.checkpoint()
    inconsistent = AcceptanceResult(
        check_id="unit",
        passed=True,
        exit_code=1,
        timed_out=False,
        output="failed",
        output_hash=hashlib.sha256(b"failed").hexdigest(),
    )

    with pytest.raises(Conflict, match="incomplete or inconsistent"):
        ToolRecoveryService(runner.service, runner.session_store).accept_check_result(
            run_id,
            call_id,
            inconsistent,
            current_workspace_revision=revision,
            current_workspace_manifest_ref=manifest_ref,
            token=token,
        )

    assert store.get(run_id).unknown_tool_calls == {call_id}


@pytest.mark.parametrize(
    ("tool_name", "arguments", "drift"),
    [
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            },
            False,
        ),
        ("read_file", {"path": "src/parser.py"}, True),
    ],
)
def test_readonly_retry_rejects_writes_and_workspace_drift(
    tmp_path,
    task_dict,
    tool_name,
    arguments,
    drift,
):
    runner, store, run_id, token, workspace, _ = setup_loop(
        tmp_path,
        task_dict,
        [(tool_name, arguments)],
    )
    real_tools = runner.tools
    runner.tools = InterruptToolDispatch(real_tools)
    with pytest.raises(RuntimeError, match="durable tool intent"):
        runner.run(run_id, token)
    RecoveryService(runner.service, runner.campaign_ledger, runner.session_store).reconcile(
        run_id,
        token,
    )
    call_id = next(iter(store.get(run_id).unknown_tool_calls))
    if drift:
        (workspace / "src/parser.py").write_text(
            "def parse(value):\n    return ['external change']\n",
            encoding="utf-8",
        )
    revision, manifest_ref = real_tools.checkpoint()

    with pytest.raises(Conflict, match="read-only tool|Workspace changed"):
        ToolRecoveryService(runner.service, runner.session_store).authorize_readonly_retry(
            run_id,
            call_id,
            current_workspace_revision=revision,
            current_workspace_manifest_ref=manifest_ref,
            token=token,
        )

    blocked = store.get(run_id)
    assert blocked.unknown_tool_calls == {call_id}
    assert call_id in blocked.reservations


def test_explicit_replace_acceptance_completes_turn_without_replaying_model(
    tmp_path,
    task_dict,
):
    (
        runner,
        store,
        run_id,
        first_token,
        workspace,
        first_model,
        real_tools,
        call_id,
    ) = interrupt_after_replace_effect(tmp_path, task_dict)

    resolved = ToolRecoveryService(
        runner.service,
        runner.session_store,
        real_tools,
    ).resolve_replace(
        run_id,
        call_id,
        decision="accept",
        token=first_token,
    )

    assert not resolved.reservations
    assert resolved.tool_calls[-1].status == "success"
    assert resolved.tool_calls[-1].recovery_disposition == "accept_replace"
    assert resolved.agent_session is not None
    assert resolved.agent_session.next_iteration == 2
    report = RecoveryService(
        runner.service,
        runner.campaign_ledger,
        runner.session_store,
    ).reconcile(run_id, first_token)
    assert report.safe_to_resume is True
    runner.service.release_lease(run_id, first_token, "release-accepted-replace")

    resumed_service = HarnessService(SQLiteEventStore(store.path))
    leased = resumed_service.acquire_lease(run_id, "agent-worker-2", "lease-accepted-replace")
    second_token = LeaseToken.from_run(leased)
    second_model = ScriptedModel([("submit", {"summary": "Accepted recovered replace effect."})])
    final = CodingAgentRunner(
        resumed_service,
        second_model,
        runner.campaign_ledger,
        real_tools,
        runner.session_store,
        provider_id=runner.provider_id,
        model_id=runner.model_id,
        pricing=runner.pricing,
        campaign=runner.campaign,
        config=runner.config,
    ).run(run_id, second_token)

    assert final.status == RunStatus.SUCCEEDED
    assert len(first_model.requests) == 1
    assert len(second_model.requests) == 1
    assert len(final.model_calls) == 2
    assert "return [] if" in (workspace / "src/parser.py").read_text(encoding="utf-8")
    assert any(
        message.role == "tool" and "accept_replace" in (message.content or "")
        for message in second_model.requests[0].messages
    )


def test_cancelled_unknown_write_stays_recoverable_then_terminalizes(
    tmp_path,
    task_dict,
):
    (
        runner,
        store,
        run_id,
        token,
        _,
        first_model,
        real_tools,
        call_id,
    ) = interrupt_after_replace_effect(tmp_path, task_dict)
    pending = runner.service.cancel(run_id, "cancel-unknown-write")
    assert pending.status == RunStatus.RUNNING
    assert pending.cancel_requested is True
    assert pending.unknown_tool_calls == {call_id}

    cancelled = ToolRecoveryService(
        runner.service,
        runner.session_store,
        real_tools,
    ).resolve_replace(
        run_id,
        call_id,
        decision="accept",
        token=token,
    )

    assert cancelled.status == RunStatus.CANCELLED
    assert cancelled.tool_calls[-1].recovery_disposition == "accept_replace"
    assert not cancelled.reservations
    assert len(first_model.requests) == 1
    report = RecoveryService(
        runner.service,
        runner.campaign_ledger,
        runner.session_store,
    ).reconcile(run_id, token)
    assert report.next_action == "cancelled"
    assert report.requires_human is False
    assert SQLiteEventStore.replay_jsonl(store.export_jsonl(run_id)).as_dict() == (
        cancelled.as_dict()
    )


def test_explicit_replace_rollback_restores_pre_effect_and_continues(
    tmp_path,
    task_dict,
):
    (
        runner,
        store,
        run_id,
        first_token,
        workspace,
        first_model,
        real_tools,
        call_id,
    ) = interrupt_after_replace_effect(tmp_path, task_dict)

    resolved = ToolRecoveryService(
        runner.service,
        runner.session_store,
        real_tools,
    ).resolve_replace(
        run_id,
        call_id,
        decision="rollback",
        token=first_token,
    )

    assert resolved.tool_calls[-1].status == "cancelled"
    assert resolved.tool_calls[-1].recovery_disposition == "rollback_replace"
    assert "return [value]" in (workspace / "src/parser.py").read_text(encoding="utf-8")
    runner.service.release_lease(run_id, first_token, "release-rolled-back-replace")

    resumed_service = HarnessService(SQLiteEventStore(store.path))
    leased = resumed_service.acquire_lease(run_id, "agent-worker-2", "lease-rollback-replace")
    second_token = LeaseToken.from_run(leased)
    second_model = ScriptedModel(
        [
            (
                "replace_text",
                {
                    "path": "src/parser.py",
                    "old": "return [value]",
                    "new": "return [] if value == '' else [value]",
                },
            ),
            ("submit", {"summary": "Reapplied after explicit rollback."}),
        ]
    )
    final = CodingAgentRunner(
        resumed_service,
        second_model,
        runner.campaign_ledger,
        real_tools,
        runner.session_store,
        provider_id=runner.provider_id,
        model_id=runner.model_id,
        pricing=runner.pricing,
        campaign=runner.campaign,
        config=runner.config,
    ).run(run_id, second_token)

    assert final.status == RunStatus.SUCCEEDED
    assert len(first_model.requests) == 1
    assert len(second_model.requests) == 2
    assert len(final.model_calls) == 3
    assert "return [] if" in (workspace / "src/parser.py").read_text(encoding="utf-8")
    assert any(
        message.role == "tool" and "rollback_replace" in (message.content or "")
        for message in second_model.requests[0].messages
    )


def test_replace_resolution_rejects_partial_or_external_workspace_drift(
    tmp_path,
    task_dict,
):
    runner, store, run_id, token, workspace, _, real_tools, call_id = (
        interrupt_after_replace_effect(tmp_path, task_dict)
    )
    (workspace / "tests/test_parser.py").write_text(
        "# external drift after uncertain replace\n",
        encoding="utf-8",
    )

    with pytest.raises(Conflict, match="exact expected effect|Diverged"):
        ToolRecoveryService(
            runner.service,
            runner.session_store,
            real_tools,
        ).resolve_replace(
            run_id,
            call_id,
            decision="accept",
            token=token,
        )

    blocked = store.get(run_id)
    assert blocked.unknown_tool_calls == {call_id}
    assert call_id in blocked.reservations


def test_agent_loop_releases_campaign_only_hold_before_retry(tmp_path, task_dict):
    actions = [
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            },
        ),
        ("submit", {"summary": "Completed after pre-dispatch recovery."}),
    ]
    runner, store, run_id, token, workspace, model = setup_loop(tmp_path, task_dict, actions)
    ledger = runner.campaign_ledger
    runner.campaign_ledger = InterruptAfterCampaignReservation(ledger)

    with pytest.raises(RuntimeError, match="campaign reservation"):
        runner.run(run_id, token)

    interrupted = store.get(run_id)
    assert interrupted.agent_session is not None
    assert not interrupted.model_calls
    assert not interrupted.reservations
    assert model.requests == []
    report = RecoveryService(runner.service, ledger, runner.session_store).reconcile(run_id, token)
    assert report.safe_to_resume is True
    assert report.findings[0].classification == "campaign_only_reservation_released"
    orphan = ledger.attempt(runner.campaign.campaign_id, report.findings[0].operation_id)
    assert orphan.status == "settled"
    assert orphan.actual_cost == 0

    runner.campaign_ledger = ledger
    result = runner.run(run_id, token)

    assert result.status == RunStatus.SUCCEEDED
    assert len(model.requests) == 2
    assert len(result.model_calls) == 2
    assert "return [] if" in (workspace / "src/parser.py").read_text()


def test_agent_loop_refuses_resume_after_workspace_drift(tmp_path, task_dict):
    runner, store, run_id, first_token, workspace, _ = setup_loop(
        tmp_path,
        task_dict,
        [("read_file", {"path": "src/parser.py"})],
    )
    runner.run(run_id, first_token, max_iterations_this_invocation=1)
    runner.service.release_lease(run_id, first_token, "release-before-drift")
    (workspace / "src/parser.py").write_text("unexpected external change\n", encoding="utf-8")

    reopened = SQLiteEventStore(store.path)
    service = HarnessService(reopened)
    leased = service.acquire_lease(run_id, "agent-worker-2", "lease-after-drift")
    token = LeaseToken.from_run(leased)
    plan = leased.plan
    assert plan is not None
    model = ScriptedModel([("submit", {"summary": "should not run"})])
    artifacts = ArtifactStore(tmp_path / "artifacts")
    snapshots = SnapshotManager(artifacts)
    tools = WorkspaceToolGateway(
        workspace,
        leased.task,
        plan.items[0],
        snapshots,
        ParserCheck(),
        SQLiteCodeRetriever(tmp_path / "retrieval.sqlite3", snapshots),
    )
    resumed = CodingAgentRunner(
        service,
        model,
        CampaignBudgetLedger(tmp_path / "campaign.sqlite3"),
        tools,
        artifacts,
        provider_id=runner.provider_id,
        model_id=runner.model_id,
        pricing=runner.pricing,
        campaign=runner.campaign,
        config=runner.config,
    )

    with pytest.raises(Conflict, match="Workspace changed"):
        resumed.run(run_id, token)
    assert model.requests == []


def test_model_projection_rejects_task_prefix_drift(tmp_path, task_dict):
    runner, store, run_id, token, _, _ = setup_loop(
        tmp_path,
        task_dict,
        [("read_file", {"path": "src/parser.py"})],
    )
    paused = runner.run(run_id, token, max_iterations_this_invocation=1)
    assert paused.agent_session is not None
    session = AgentSession.model_validate_json(
        runner.session_store.read(paused.agent_session.artifact_ref)
    )
    run = store.get(run_id)
    facts = runner._mandatory_facts(run)
    facts_ref = runner._store_mandatory_facts(facts)
    memory = runner._run_memory(run, workspace_revision=facts.workspace_revision)
    memory_ref = runner._store_run_memory(memory)
    messages = list(session.messages)
    messages[1] = ModelMessage(role="user", content="different task contract")

    with pytest.raises(Conflict, match="task prefix"):
        runner._project_context(run, messages, facts, facts_ref, memory, memory_ref)


def test_agent_loop_refuses_resume_if_run_advanced_after_safe_session(tmp_path, task_dict):
    runner, store, run_id, first_token, workspace, _ = setup_loop(
        tmp_path,
        task_dict,
        [("read_file", {"path": "src/parser.py"})],
    )
    runner.run(run_id, first_token, max_iterations_this_invocation=1)
    runner.service.reserve(
        run_id,
        "late-operation",
        Usage(steps=1),
        first_token,
        "late-reserve",
    )
    runner.service.settle(
        run_id,
        "late-operation",
        Usage(steps=1),
        first_token,
        "late-settle",
    )
    runner.service.release_lease(run_id, first_token, "release-after-late-operation")

    reopened = SQLiteEventStore(store.path)
    service = HarnessService(reopened)
    leased = service.acquire_lease(run_id, "agent-worker-2", "lease-after-late-operation")
    token = LeaseToken.from_run(leased)
    plan = leased.plan
    assert plan is not None
    model = ScriptedModel([("submit", {"summary": "should not run"})])
    artifacts = ArtifactStore(tmp_path / "artifacts")
    snapshots = SnapshotManager(artifacts)
    tools = WorkspaceToolGateway(
        workspace,
        leased.task,
        plan.items[0],
        snapshots,
        ParserCheck(),
        SQLiteCodeRetriever(tmp_path / "retrieval.sqlite3", snapshots),
    )
    resumed = CodingAgentRunner(
        service,
        model,
        CampaignBudgetLedger(tmp_path / "campaign.sqlite3"),
        tools,
        artifacts,
        provider_id=runner.provider_id,
        model_id=runner.model_id,
        pricing=runner.pricing,
        campaign=runner.campaign,
        config=runner.config,
    )

    with pytest.raises(Conflict, match="advanced beyond"):
        resumed.run(run_id, token)
    assert model.requests == []


def test_agent_loop_persists_bounded_context_projection_without_losing_transcript(
    tmp_path,
    task_dict,
):
    reads = [("read_file", {"path": "src/parser.py"})] * 4
    actions = [
        *reads,
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            },
        ),
        ("submit", {"summary": "Completed with bounded context."}),
    ]
    runner, store, run_id, token, workspace, model = setup_loop(tmp_path, task_dict, actions)
    (workspace / "src/parser.py").write_text(
        f"# {'context-evidence-' * 80}\ndef parse(value):\n    return [value]\n",
        encoding="utf-8",
    )
    runner.config = AgentLoopConfig(
        max_model_iterations=8,
        max_output_tokens=256,
        max_context_chars=6_000,
        preserve_recent_context_units=1,
        max_identical_no_progress_actions=10,
    )

    result = runner.run(run_id, token)

    assert result.status == RunStatus.SUCCEEDED
    compacted_requests = [
        request
        for request in model.requests
        if any(
            message.role == "user" and "Deterministic history projection" in (message.content or "")
            for message in request.messages
        )
    ]
    assert compacted_requests
    assert result.agent_session is not None
    session = AgentSession.model_validate_json(
        runner.session_store.read(result.agent_session.artifact_ref)
    )
    assert len(session.messages) > len(model.requests[-1].messages)

    reservation_events = [
        event for event in store.events(run_id) if event.event_type == "MODEL_CALL_RESERVED"
    ]
    assert len(reservation_events) == len(model.requests)
    last_reservation = ModelCallReservation.model_validate(
        reservation_events[-1].payload["reservation"]
    )
    assert last_reservation.context_projection_ref is not None
    assert last_reservation.mandatory_facts_ref is not None
    assert last_reservation.run_memory_ref is not None
    projection = ContextProjection.model_validate_json(
        runner.session_store.read(last_reservation.context_projection_ref)
    )
    assert projection.messages == model.requests[-1].messages
    assert projection.source_message_count == len(session.messages)
    assert projection.projected_message_count == len(model.requests[-1].messages)
    assert projection.compacted is True
    assert projection.input_token_budget == last_reservation.input_token_budget
    assert projection.input_token_budget.max_input_tokens == runner.config.max_input_tokens
    assert projection.input_token_budget.estimate == conservative_input_estimate(model.requests[-1])
    assert projection.mandatory_facts_ref == last_reservation.mandatory_facts_ref
    assert projection.run_memory_ref == last_reservation.run_memory_ref
    memory = RunMemorySnapshot.model_validate_json(
        runner.session_store.read(last_reservation.run_memory_ref)
    )
    assert memory.sha256 == last_reservation.run_memory_hash
    assert memory.included_entry_count == last_reservation.run_memory_entry_count
    assert "Controller-derived Run memory" in (projection.messages[0].content or "")
    facts = MandatoryFactLedger.model_validate_json(
        runner.session_store.read(last_reservation.mandatory_facts_ref)
    )
    assert facts.sha256 == last_reservation.mandatory_facts_hash
    assert facts.task_spec_hash in (projection.messages[0].content or "")


def test_agent_loop_compacts_history_to_fit_remaining_campaign_budget(tmp_path, task_dict):
    actions = [
        ("read_file", {"path": "src/parser.py"}),
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            },
        ),
        ("submit", {"summary": "Completed within the remaining Campaign budget."}),
    ]
    runner, store, run_id, token, workspace, model = setup_loop(
        tmp_path,
        task_dict,
        actions,
    )
    (workspace / "src/parser.py").write_text(
        f"# {'budget-context-' * 1_600}\ndef parse(value):\n    return [value]\n",
        encoding="utf-8",
    )
    runner.campaign = runner.campaign.model_copy(
        update={"max_cost": Decimal("0.08"), "max_cost_per_call": Decimal("0.08")}
    )
    runner.config = AgentLoopConfig(
        max_model_iterations=8,
        max_output_tokens=256,
        max_context_chars=100_000,
        preserve_recent_context_units=1,
        max_identical_no_progress_actions=10,
    )

    result = runner.run(run_id, token)

    assert result.status == RunStatus.SUCCEEDED
    assert "return [] if" in (workspace / "src/parser.py").read_text(encoding="utf-8")
    reservations = [
        ModelCallReservation.model_validate(event.payload["reservation"])
        for event in store.events(run_id)
        if event.event_type == "MODEL_CALL_RESERVED"
    ]
    projections = [
        ContextProjection.model_validate_json(
            runner.session_store.read(reservation.context_projection_ref)
        )
        for reservation in reservations
        if reservation.context_projection_ref is not None
    ]
    compacted = [projection for projection in projections if projection.compacted]
    assert compacted
    assert projections[0].input_token_budget.max_input_tokens == 25_898
    assert all(
        projection.input_token_budget.max_input_tokens < runner.config.max_input_tokens
        for projection in projections
    )
    assert all(
        projection.input_token_budget.estimate.token_ceiling
        <= projection.input_token_budget.max_input_tokens
        for projection in projections
    )
    assert any(
        message.role == "user" and "Deterministic history projection" in (message.content or "")
        for request in model.requests
        for message in request.messages
    )
    assert result.agent_session is not None
    session = AgentSession.model_validate_json(
        runner.session_store.read(result.agent_session.artifact_ref)
    )
    assert (
        len(canonical_json([message.model_dump(mode="json") for message in session.messages]))
        < runner.config.max_context_chars
    )
    campaign = runner.campaign_ledger.summary(runner.campaign.campaign_id)
    assert campaign.remaining_cost > 0


def test_compacted_pending_model_response_recovers_without_rebilling(tmp_path, task_dict):
    reads = [("read_file", {"path": "src/parser.py"})] * 4
    runner, store, run_id, first_token, workspace, first_model = setup_loop(
        tmp_path,
        task_dict,
        reads,
    )
    (workspace / "src/parser.py").write_text(
        f"# {'recovery-context-' * 1_600}\ndef parse(value):\n    return [value]\n",
        encoding="utf-8",
    )
    runner.campaign = runner.campaign.model_copy(
        update={"max_cost": Decimal("0.08"), "max_cost_per_call": Decimal("0.08")}
    )
    runner.config = AgentLoopConfig(
        max_model_iterations=8,
        max_output_tokens=256,
        max_context_chars=100_000,
        preserve_recent_context_units=1,
        max_identical_no_progress_actions=10,
    )
    paused = runner.run(run_id, first_token, max_iterations_this_invocation=3)
    assert paused.agent_session is not None
    assert paused.agent_session.next_iteration == 4

    ledger = runner.campaign_ledger
    runner.campaign_ledger = InterruptBeforeCampaignSettlement(ledger)
    with pytest.raises(RuntimeError, match="campaign settlement"):
        runner.run(run_id, first_token)

    interrupted = store.get(run_id)
    assert len(interrupted.model_calls) == 4
    pending_event = [
        event for event in store.events(run_id) if event.event_type == "MODEL_CALL_RESERVED"
    ][-1]
    pending_reservation = ModelCallReservation.model_validate(pending_event.payload["reservation"])
    assert pending_reservation.context_projection_ref is not None
    pending_projection = ContextProjection.model_validate_json(
        runner.session_store.read(pending_reservation.context_projection_ref)
    )
    assert pending_projection.compacted is True
    assert pending_projection.input_token_budget.max_input_tokens < runner.config.max_input_tokens
    assert len(first_model.requests) == 4

    report = RecoveryService(runner.service, ledger, runner.session_store).reconcile(
        run_id,
        first_token,
    )
    assert report.safe_to_resume is True
    runner.service.release_lease(run_id, first_token, "release-compacted-response")

    resumed_service = HarnessService(SQLiteEventStore(store.path))
    leased = resumed_service.acquire_lease(run_id, "agent-worker-2", "lease-compacted-response")
    second_token = LeaseToken.from_run(leased)
    second_model = ScriptedModel(
        [
            (
                "replace_text",
                {
                    "path": "src/parser.py",
                    "old": "return [value]",
                    "new": "return [] if value == '' else [value]",
                },
            ),
            ("submit", {"summary": "Recovered compacted response and completed."}),
        ]
    )
    final = CodingAgentRunner(
        resumed_service,
        second_model,
        ledger,
        runner.tools,
        runner.session_store,
        provider_id=runner.provider_id,
        model_id=runner.model_id,
        pricing=runner.pricing,
        campaign=runner.campaign,
        config=runner.config,
    ).run(run_id, second_token)

    assert final.status == RunStatus.SUCCEEDED
    assert len(first_model.requests) == 4
    assert len(second_model.requests) == 2
    assert len(final.model_calls) == 6
    assert "return [] if" in (workspace / "src/parser.py").read_text(encoding="utf-8")


def test_sequential_supervisor_reopens_workers_and_respects_slice_limit(tmp_path, task_dict):
    actions = [
        ("read_file", {"path": "src/parser.py"}),
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            },
        ),
        ("submit", {"summary": "Parser fix is ready."}),
    ]
    template, store, run_id, first_token, workspace, model = setup_loop(
        tmp_path,
        task_dict,
        actions,
    )
    reopened_services = []
    worker_gateways = []

    def service_factory():
        service = HarnessService(SQLiteEventStore(store.path))
        reopened_services.append(service)
        return service

    def runner_factory(service):
        current = service.store.get(run_id)
        assert current.plan is not None
        work_item_id = (
            current.agent_session.work_item_id
            if current.agent_session is not None
            else current.plan.ready_items(current.passed_items)[0].work_item_id
        )
        item = next(
            candidate for candidate in current.plan.items if candidate.work_item_id == work_item_id
        )
        artifacts = ArtifactStore(tmp_path / "artifacts")
        snapshots = SnapshotManager(artifacts)
        tools = WorkspaceToolGateway(
            workspace,
            current.task,
            item,
            snapshots,
            ParserCheck(),
            SQLiteCodeRetriever(tmp_path / "retrieval.sqlite3", snapshots),
        )
        worker_gateways.append(tools)
        return CodingAgentRunner(
            service,
            model,
            CampaignBudgetLedger(tmp_path / "campaign.sqlite3"),
            tools,
            artifacts,
            provider_id=template.provider_id,
            model_id=template.model_id,
            pricing=template.pricing,
            campaign=template.campaign,
            config=template.config,
        )

    first = SequentialAgentSupervisor(
        service_factory,
        runner_factory,
        config=SequentialSupervisorConfig(
            slice_iterations=1,
            max_worker_slices=2,
        ),
    ).run(run_id, template.service, first_token)

    assert first.run.status == RunStatus.RUNNING
    assert first.run.lease_id is None
    assert first.run.agent_session is not None
    assert first.run.agent_session.next_iteration == 3
    assert first.worker_slices == 2
    assert first.worker_handoffs == 1
    assert first.slice_limit_reached is True
    assert len(model.requests) == 2

    final_service = service_factory()
    leased = final_service.acquire_lease(run_id, "supervisor-resume", "supervisor-resume")
    final = SequentialAgentSupervisor(
        service_factory,
        runner_factory,
        config=SequentialSupervisorConfig(
            slice_iterations=1,
            max_worker_slices=8,
        ),
    ).run(run_id, final_service, LeaseToken.from_run(leased))

    assert final.run.status == RunStatus.SUCCEEDED
    assert final.run.lease_epoch == 3
    assert final.worker_slices == 1
    assert final.worker_handoffs == 0
    assert final.slice_limit_reached is False
    assert len(model.requests) == 3
    assert len(worker_gateways) == 3
    assert len(reopened_services) == 2
    event_types = [event.event_type for event in store.events(run_id)]
    assert event_types.count("LEASE_ACQUIRED") == 3
    assert event_types.count("LEASE_RELEASED") == 2
    assert "return [] if" in (workspace / "src/parser.py").read_text(encoding="utf-8")


def test_agent_slice_persists_deadline_after_late_model_receipt(tmp_path, task_dict):
    bounded_task = {
        **task_dict,
        "budgets": {**task_dict["budgets"], "max_wall_time_seconds": 1},
    }
    runner, store, run_id, token, _, _ = setup_loop(tmp_path, bounded_task, [])
    deadline = datetime.fromisoformat(store.get(run_id).deadline_at)

    class DeadlineCrossingModel:
        def __init__(self):
            self.requests = []

        def generate(self, request, trace_id):
            self.requests.append((request, trace_id))
            store.clock = lambda: deadline + timedelta(seconds=1)
            return ModelResponse(
                response_id="late-response",
                model=request.model,
                message=ModelMessage(role="assistant", content="finished without a tool"),
                finish_reason="stop",
                usage=ModelUsage(input_tokens=100, output_tokens=20),
                provider_trace_id="late-trace",
            )

    model = DeadlineCrossingModel()
    runner.model = model
    result = runner.run(run_id, token, max_iterations_this_invocation=1)

    assert result.status == RunStatus.FAILED
    assert result.failure_reason == "wall_clock_limit"
    assert len(model.requests) == 1
    assert len(result.model_calls) == 1
    assert not result.tool_calls
    assert not result.reservations
    assert store.events(run_id)[-1].event_type == "RUN_FAILED"


def test_agent_slice_settles_late_model_receipt_before_cancellation(tmp_path, task_dict):
    runner, store, run_id, token, _, _ = setup_loop(tmp_path, task_dict, [])

    class CancellingModel:
        def __init__(self):
            self.requests = []

        def generate(self, request, trace_id):
            self.requests.append((request, trace_id))
            pending = runner.service.cancel(run_id, "cancel-during-model-call")
            assert pending.cancel_requested is True
            return ModelResponse(
                response_id="cancelled-late-response",
                model=request.model,
                message=ModelMessage(
                    role="assistant",
                    tool_calls=(
                        ToolCall(
                            id="must-not-dispatch",
                            function=FunctionCall(
                                name="read_file",
                                arguments={"path": "src/parser.py"},
                            ),
                        ),
                    ),
                ),
                finish_reason="tool_calls",
                usage=ModelUsage(input_tokens=100, output_tokens=20),
                provider_trace_id="cancelled-late-trace",
            )

    model = CancellingModel()
    runner.model = model
    result = runner.run(run_id, token)

    assert result.status == RunStatus.CANCELLED
    assert len(model.requests) == 1
    assert len(result.model_calls) == 1
    assert not result.tool_calls
    assert not result.reservations
    event_types = [event.event_type for event in store.events(run_id)]
    assert event_types[-2:] == ["MODEL_CALL_SETTLED", "CANCEL_REQUESTED"]
    assert SQLiteEventStore.replay_jsonl(store.export_jsonl(run_id)).as_dict() == result.as_dict()


def test_agent_cancellation_classifies_unknown_model_before_terminalizing(tmp_path, task_dict):
    runner, store, run_id, token, _, _ = setup_loop(tmp_path, task_dict, [])

    class CancellingFailedModel:
        def generate(self, request, trace_id):
            pending = runner.service.cancel(run_id, "cancel-during-failed-model-call")
            assert pending.cancel_requested is True
            raise ProviderConnectionError("simulated uncertain provider disconnect")

    runner.model = CancellingFailedModel()
    result = runner.run(run_id, token)

    assert result.status == RunStatus.CANCELLED
    assert len(result.model_reservations) == 1
    call_id = next(iter(result.model_reservations))
    assert result.unknown_model_calls == {call_id}
    assert result.unknown_reservations == {call_id}
    assert runner.campaign_ledger.attempt(result.model_policy.campaign_id, call_id).status == (
        "unknown"
    )
    report = RecoveryService(
        runner.service,
        runner.campaign_ledger,
        runner.session_store,
    ).reconcile(run_id, token)
    assert report.next_action == "cancelled"
    assert report.requires_human is False
    assert report.unknown_reservations == (call_id,)
    assert SQLiteEventStore.replay_jsonl(store.export_jsonl(run_id)).as_dict() == result.as_dict()


def test_agent_cancellation_after_first_tool_blocks_remaining_dispatches(tmp_path, task_dict):
    runner, store, run_id, token, _, _ = setup_loop(tmp_path, task_dict, [])

    class TwoToolModel:
        def generate(self, request, trace_id):
            return ModelResponse(
                response_id="two-tool-response",
                model=request.model,
                message=ModelMessage(
                    role="assistant",
                    tool_calls=(
                        ToolCall(
                            id="first-read",
                            function=FunctionCall(
                                name="read_file",
                                arguments={"path": "src/parser.py"},
                            ),
                        ),
                        ToolCall(
                            id="second-read",
                            function=FunctionCall(
                                name="read_file",
                                arguments={"path": "src/formatter.py"},
                            ),
                        ),
                    ),
                ),
                finish_reason="tool_calls",
                usage=ModelUsage(input_tokens=100, output_tokens=20),
                provider_trace_id="two-tool-trace",
            )

    class CancelAfterFirstDispatch:
        def __init__(self, delegate):
            self.delegate = delegate
            self.dispatches = 0

        def __getattr__(self, name):
            return getattr(self.delegate, name)

        def dispatch_safe(self, name, arguments, attempt_id=None):
            self.dispatches += 1
            outcome = self.delegate.dispatch_safe(name, arguments, attempt_id)
            pending = runner.service.cancel(run_id, "cancel-during-first-tool")
            assert pending.cancel_requested is True
            return outcome

    tools = CancelAfterFirstDispatch(runner.tools)
    runner.model = TwoToolModel()
    runner.tools = tools
    result = runner.run(run_id, token)

    assert result.status == RunStatus.CANCELLED
    assert tools.dispatches == 1
    assert len(result.model_calls) == 1
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "read_file"
    assert not result.reservations
    assert SQLiteEventStore.replay_jsonl(store.export_jsonl(run_id)).as_dict() == result.as_dict()


@pytest.mark.parametrize(
    ("error", "lease_released"),
    [
        (Conflict("handled controller failure"), True),
        (RuntimeError("unexpected worker failure"), False),
    ],
)
def test_sequential_supervisor_only_releases_lease_for_handled_controller_errors(
    tmp_path,
    task_dict,
    error,
    lease_released,
):
    _, store, run_id, token, _, _ = setup_loop(
        tmp_path,
        task_dict,
        [("read_file", {"path": "src/parser.py"})],
    )

    class FailingRunner:
        def run(self, run_id, token, *, max_iterations_this_invocation=None):
            raise error

    supervisor = SequentialAgentSupervisor(
        lambda: HarnessService(SQLiteEventStore(store.path)),
        lambda service: FailingRunner(),
        config=SequentialSupervisorConfig(slice_iterations=1, max_worker_slices=2),
    )
    with pytest.raises(type(error), match=str(error)):
        supervisor.run(run_id, HarnessService(store), token)

    persisted = store.get(run_id)
    assert (persisted.lease_id is None) is lease_released
    assert persisted.lease_epoch == 1
    releases = [event for event in store.events(run_id) if event.event_type == "LEASE_RELEASED"]
    assert len(releases) == int(lease_released)


def _run_reaped_supervisor_worker(
    tmp_path,
    store,
    run_id,
    token,
    workspace,
    *,
    mode,
    expected_exit_code,
):
    worker = Path(__file__).parents[1] / "fault_injection" / "_reaped_supervisor_worker.py"
    process = subprocess.Popen(
        [
            sys.executable,
            str(worker),
            mode,
            str(store.path),
            run_id,
            token.lease_id,
            token.worker_id,
            str(token.epoch),
            str(tmp_path / "artifacts"),
            str(workspace),
            str(tmp_path / "campaign.sqlite3"),
            str(tmp_path / "retrieval.sqlite3"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _, stderr = process.communicate(timeout=20)
    assert process.returncode == expected_exit_code, stderr.decode(errors="replace")
    return process


@pytest.mark.parametrize("expire_original_lease", [False, True], ids=["live", "expired"])
def test_supervisor_continues_after_reaped_worker_at_safe_boundary(
    tmp_path,
    task_dict,
    expire_original_lease,
):
    remaining_actions = [
        (
            "replace_text",
            {
                "path": "src/parser.py",
                "old": "return [value]",
                "new": "return [] if value == '' else [value]",
            },
        ),
        ("submit", {"summary": "Parser fix survived the child Worker exit."}),
    ]
    template, store, run_id, token, workspace, model = setup_loop(
        tmp_path,
        task_dict,
        remaining_actions,
    )
    boundary = ReapedWorkerBoundary.capture(store.get(run_id), token)
    process = _run_reaped_supervisor_worker(
        tmp_path,
        store,
        run_id,
        token,
        workspace,
        mode="safe",
        expected_exit_code=37,
    )

    expired_now = None
    if expire_original_lease:
        lease_expires_at = store.get(run_id).lease_expires_at
        assert lease_expires_at is not None
        expired_now = datetime.fromisoformat(lease_expires_at) + timedelta(seconds=1)

    def reopened_store():
        if expired_now is None:
            return SQLiteEventStore(store.path)
        return SQLiteEventStore(store.path, clock=lambda: expired_now)

    reopened_services = []

    def service_factory():
        service = HarnessService(reopened_store())
        reopened_services.append(service)
        return service

    def runner_factory(service):
        current = service.store.get(run_id)
        assert current.plan is not None
        assert current.agent_session is not None
        item = next(
            candidate
            for candidate in current.plan.items
            if candidate.work_item_id == current.agent_session.work_item_id
        )
        artifacts = ArtifactStore(tmp_path / "artifacts")
        snapshots = SnapshotManager(artifacts)
        return CodingAgentRunner(
            service,
            model,
            CampaignBudgetLedger(tmp_path / "campaign.sqlite3"),
            WorkspaceToolGateway(
                workspace,
                current.task,
                item,
                snapshots,
                ParserCheck(),
                SQLiteCodeRetriever(tmp_path / "retrieval.sqlite3", snapshots),
            ),
            artifacts,
            provider_id=template.provider_id,
            model_id=template.model_id,
            pricing=template.pricing,
            campaign=template.campaign,
            config=template.config,
        )

    supervisor = SequentialAgentSupervisor(
        service_factory,
        runner_factory,
        config=SequentialSupervisorConfig(slice_iterations=1, max_worker_slices=4),
        recovery_factory=lambda service: RecoveryService(
            service,
            CampaignBudgetLedger(tmp_path / "campaign.sqlite3"),
            ArtifactStore(tmp_path / "artifacts"),
        ),
    )
    result = supervisor.continue_after_reaped_worker(
        boundary,
        HarnessService(reopened_store()),
        process_id=process.pid,
        exit_code=process.returncode,
    )

    assert result.disposition == "continued"
    assert result.run.status == RunStatus.SUCCEEDED
    assert result.run.lease_epoch == 3
    assert result.recovery_report is not None
    assert result.recovery_report.safe_to_resume is True
    assert result.supervision is not None
    assert result.supervision.worker_slices == 2
    assert result.supervision.worker_handoffs == 1
    assert result.lease_transition == (
        "expired_takeover" if expire_original_lease else "live_release"
    )
    assert len(reopened_services) == 2
    assert len(model.requests) == 2
    assert "return [] if" in (workspace / "src/parser.py").read_text(encoding="utf-8")

    events = store.events(run_id)
    if expire_original_lease:
        takeover = next(
            event
            for event in events
            if event.event_type == "LEASE_ACQUIRED"
            and event.payload.get("takeover_reason") == "confirmed_reaped_worker_expired"
        )
        assert takeover.payload["previous_lease_id"] == token.lease_id
        assert takeover.payload["previous_epoch"] == token.epoch
        assert takeover.payload["process_id"] == process.pid
        assert takeover.payload["exit_code"] == 37
        assert takeover.payload["launch_event_seq"] == boundary.launch_event_seq
        assert takeover.payload["observed_event_seq"] > boundary.launch_event_seq
    else:
        reaped_release = next(
            event
            for event in events
            if event.event_type == "LEASE_RELEASED"
            and event.payload.get("release_reason") == "confirmed_reaped_worker"
        )
        assert reaped_release.payload["process_id"] == process.pid
        assert reaped_release.payload["exit_code"] == 37
        assert reaped_release.payload["launch_event_seq"] == boundary.launch_event_seq
        assert reaped_release.payload["safe_event_seq"] > boundary.launch_event_seq
    replayed = SQLiteEventStore.replay_jsonl(store.export_jsonl(run_id))
    assert replayed.as_dict() == result.run.as_dict()


@pytest.mark.parametrize("expire_original_lease", [False, True], ids=["live", "expired"])
def test_supervisor_does_not_dispatch_reaped_worker_with_pending_intent(
    tmp_path,
    task_dict,
    expire_original_lease,
):
    template, store, run_id, token, workspace, model = setup_loop(
        tmp_path,
        task_dict,
        [("submit", {"summary": "must not be dispatched"})],
    )
    boundary = ReapedWorkerBoundary.capture(store.get(run_id), token)
    process = _run_reaped_supervisor_worker(
        tmp_path,
        store,
        run_id,
        token,
        workspace,
        mode="pending",
        expected_exit_code=38,
    )

    expired_now = None
    if expire_original_lease:
        lease_expires_at = store.get(run_id).lease_expires_at
        assert lease_expires_at is not None
        expired_now = datetime.fromisoformat(lease_expires_at) + timedelta(seconds=1)

    def reopened_store():
        if expired_now is None:
            return SQLiteEventStore(store.path)
        return SQLiteEventStore(store.path, clock=lambda: expired_now)

    recovery_called = False
    reopened_services = []

    def recovery_factory(service):
        nonlocal recovery_called
        recovery_called = True
        return RecoveryService(
            service,
            CampaignBudgetLedger(tmp_path / "campaign.sqlite3"),
            ArtifactStore(tmp_path / "artifacts"),
        )

    def service_factory():
        service = HarnessService(reopened_store())
        reopened_services.append(service)
        return service

    supervisor = SequentialAgentSupervisor(
        service_factory,
        lambda service: pytest.fail("blocked handoff must not construct a runner"),
        config=SequentialSupervisorConfig(slice_iterations=1, max_worker_slices=2),
        recovery_factory=recovery_factory,
    )
    result = supervisor.continue_after_reaped_worker(
        boundary,
        HarnessService(reopened_store()),
        process_id=process.pid,
        exit_code=process.returncode,
    )

    persisted = store.get(run_id)
    assert result.disposition == "reconciliation_required"
    assert result.pending_reservation_ids == ("reaped-pending-read",)
    assert result.recovery_report is None
    assert result.supervision is None
    assert recovery_called is False
    assert result.lease_transition == ("expired_takeover" if expire_original_lease else "none")
    assert len(reopened_services) == int(expire_original_lease)
    if expire_original_lease:
        assert persisted.lease_id != token.lease_id
        assert persisted.lease_epoch == token.epoch + 1
        takeover = next(
            event
            for event in store.events(run_id)
            if event.event_type == "LEASE_ACQUIRED"
            and event.payload.get("takeover_reason") == "confirmed_reaped_worker_expired"
        )
        assert takeover.payload["previous_lease_id"] == token.lease_id
        assert takeover.payload["process_id"] == process.pid
        assert result.run.lease_id == persisted.lease_id
    else:
        assert persisted.lease_id == token.lease_id
        assert persisted.lease_epoch == token.epoch
    assert set(persisted.reservations) == {"reaped-pending-read"}
    assert not model.requests
    assert not any(event.event_type == "LEASE_RELEASED" for event in store.events(run_id))
    assert template.service.store.get(run_id).lease_id == persisted.lease_id


@pytest.mark.parametrize("expire_original_lease", [False, True], ids=["live", "expired"])
def test_supervisor_requires_recovery_safe_boundary_after_reaped_worker(
    tmp_path,
    task_dict,
    expire_original_lease,
):
    _, store, run_id, token, workspace, model = setup_loop(
        tmp_path,
        task_dict,
        [("submit", {"summary": "must not be dispatched"})],
    )
    boundary = ReapedWorkerBoundary.capture(store.get(run_id), token)
    process = _run_reaped_supervisor_worker(
        tmp_path,
        store,
        run_id,
        token,
        workspace,
        mode="idle",
        expected_exit_code=39,
    )

    expired_now = None
    if expire_original_lease:
        lease_expires_at = store.get(run_id).lease_expires_at
        assert lease_expires_at is not None
        expired_now = datetime.fromisoformat(lease_expires_at) + timedelta(seconds=1)

    def reopened_store():
        if expired_now is None:
            return SQLiteEventStore(store.path)
        return SQLiteEventStore(store.path, clock=lambda: expired_now)

    reopened_services = []

    def service_factory():
        service = HarnessService(reopened_store())
        reopened_services.append(service)
        return service

    supervisor = SequentialAgentSupervisor(
        service_factory,
        lambda service: pytest.fail("unsafe boundary must not construct a runner"),
        config=SequentialSupervisorConfig(slice_iterations=1, max_worker_slices=2),
        recovery_factory=lambda service: RecoveryService(
            service,
            CampaignBudgetLedger(tmp_path / "campaign.sqlite3"),
            ArtifactStore(tmp_path / "artifacts"),
        ),
    )
    result = supervisor.continue_after_reaped_worker(
        boundary,
        HarnessService(reopened_store()),
        process_id=process.pid,
        exit_code=process.returncode,
    )

    persisted = store.get(run_id)
    assert result.disposition == "reconciliation_required"
    assert result.pending_reservation_ids == ()
    assert result.recovery_report is not None
    assert result.recovery_report.safe_to_resume is False
    assert result.recovery_report.findings[-1].classification == "unsafe_agent_boundary"
    assert result.supervision is None
    assert result.lease_transition == ("expired_takeover" if expire_original_lease else "none")
    assert len(reopened_services) == int(expire_original_lease)
    assert persisted.lease_epoch == token.epoch + int(expire_original_lease)
    assert not persisted.reservations
    assert not model.requests
