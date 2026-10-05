import hashlib
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from horizon.adapters.persistence.artifacts import ArtifactStore
from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.adapters.workspace.snapshot import SnapshotManager
from horizon.application.agent_loop import AgentLoopConfig, CodingAgentRunner
from horizon.application.recovery import RecoveryService
from horizon.application.services import HarnessService, LeaseToken
from horizon.application.tool_recovery import ToolRecoveryService
from horizon.domain.budget import Usage
from horizon.domain.common import digest
from horizon.domain.model import (
    CampaignBudget,
    FunctionCall,
    ModelCallReservation,
    ModelMessage,
    ModelPolicyBinding,
    ModelResponse,
    ModelUsage,
    PriceCard,
    ToolCall,
)
from horizon.domain.plan import Plan, WorkItem
from horizon.domain.states import RunStatus
from horizon.domain.task import TaskSpec
from horizon.domain.tools import AcceptanceResult, ToolCallReservation
from horizon.tools.gateway import WorkspaceToolGateway


def _campaign() -> CampaignBudget:
    return CampaignBudget(
        campaign_id="crash-campaign",
        currency="CNY",
        max_cost="3.00",
        max_cost_per_call="1.00",
    )


def _policy() -> ModelPolicyBinding:
    return ModelPolicyBinding(
        policy_id="unconfigured",
        provider_id="siliconflow",
        model="model-a",
        campaign_id="crash-campaign",
        currency="CNY",
        max_run_cost="1.00",
        price_card_hash="a" * 64,
    )


class _ScriptedModel:
    def __init__(self, actions):
        self.actions = list(actions)
        self.requests = []

    def generate(self, request, trace_id):
        self.requests.append(request)
        name, arguments = self.actions.pop(0)
        index = len(self.requests)
        return ModelResponse(
            response_id=f"resume-response-{index}",
            model=request.model,
            message=ModelMessage(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        id=f"resume-provider-call-{index}",
                        function=FunctionCall(name=name, arguments=arguments),
                    ),
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(input_tokens=101, output_tokens=20),
            provider_trace_id=f"resume-trace-{index}",
        )


class _ParserCheck:
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


def _agent_campaign() -> CampaignBudget:
    return CampaignBudget(
        campaign_id="hard-exit-agent",
        currency="CNY",
        max_cost="3.00",
        max_cost_per_call="1.00",
    )


def _price_card() -> PriceCard:
    return PriceCard(
        currency="CNY",
        input_per_million="3.00",
        cached_input_per_million="0.30",
        output_per_million="9.00",
        version="hard-exit-test",
        source_url="https://example.test/pricing",
    )


def _prepare_hard_exit_agent(tmp_path: Path, task_dict):
    workspace = tmp_path / "staging" / "workspace"
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
    task_data = {**task_dict, "model_policy_id": "fake-policy"}
    task_data["repository"] = {
        "source": "local",
        "path": str(workspace),
        "base_commit": "a" * 40,
    }
    task = TaskSpec.model_validate(task_data)
    plan = Plan(
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
                    "replace_text",
                    "apply_patch",
                    "create_file",
                    "run_check",
                ),
            ),
        )
    )
    store_path = tmp_path / "control.sqlite3"
    ledger_path = tmp_path / "campaign.sqlite3"
    artifact_path = tmp_path / "artifacts"
    old_now = datetime.now(UTC) - timedelta(minutes=5)
    store = SQLiteEventStore(store_path, clock=lambda: old_now)
    service = HarnessService(store)
    run = store.create(task, "create-hard-exit-agent")
    service.set_plan(run.run_id, plan, "plan-hard-exit-agent")
    leased = service.acquire_lease(
        run.run_id,
        "crashing-agent",
        "lease-hard-exit-agent",
        ttl_seconds=30,
    )
    token = LeaseToken.from_run(leased)
    service.transition(run.run_id, RunStatus.RUNNING, token, "start-hard-exit-agent")
    return (
        workspace,
        plan,
        store_path,
        ledger_path,
        artifact_path,
        old_now,
        run.run_id,
        token,
    )


def test_hard_exit_after_run_receipt_repairs_only_campaign(
    tmp_path, store, service, running, clock
):
    run, token = running
    service.bind_model_policy(run.run_id, _policy(), token, "bind-crash-model")
    ledger_path = tmp_path / "campaign.sqlite3"
    ledger = CampaignBudgetLedger(ledger_path, clock=clock)
    ledger.initialize(_campaign(), provider_id="siliconflow", model_id="model-a")
    reservation = ModelCallReservation(
        call_id="crash-model-call",
        request_hash="b" * 64,
        provider_id="siliconflow",
        model="model-a",
        currency="CNY",
        reserved_cost="0.20",
    )
    ledger.reserve(
        _campaign(),
        reservation.call_id,
        reservation.request_hash,
        reservation.reserved_cost,
    )
    service.reserve_model_call(
        run.run_id,
        reservation,
        Usage(model_calls=1, input_tokens=100, output_tokens=50),
        token,
        "reserve-crash-model",
    )
    script = textwrap.dedent(
        """
        import os, sys
        from datetime import datetime
        from horizon.adapters.persistence.sqlite import SQLiteEventStore
        from horizon.application.services import HarnessService, LeaseToken
        from horizon.domain.budget import Usage
        from horizon.domain.model import ModelCallRecord, ModelUsage

        store = SQLiteEventStore(sys.argv[1], clock=lambda: datetime.fromisoformat(sys.argv[6]))
        service = HarnessService(store)
        token = LeaseToken(lease_id=sys.argv[3], worker_id=sys.argv[4], epoch=int(sys.argv[5]))
        record = ModelCallRecord(
            call_id="crash-model-call",
            request_hash="b" * 64,
            provider_id="siliconflow",
            model="model-a",
            currency="CNY",
            estimated_cost="0.10",
            response_id="response-crash",
            provider_trace_id="trace-crash",
            finish_reason="tool_calls",
            usage=ModelUsage(input_tokens=90, output_tokens=10),
        )
        service.settle_model_call(
            sys.argv[2], record,
            Usage(model_calls=1, input_tokens=90, output_tokens=10),
            token, "settle-before-hard-exit",
        )
        os._exit(23)
        """
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(store.path),
            run.run_id,
            token.lease_id,
            token.worker_id,
            str(token.epoch),
            clock().isoformat(),
        ],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 23, result.stderr.decode(errors="replace")
    assert SQLiteEventStore(store.path, clock=clock).get(run.run_id).model_calls
    assert ledger.attempt("crash-campaign", "crash-model-call").status == "reserved"

    clock.advance(61)
    recovery_service = HarnessService(SQLiteEventStore(store.path, clock=clock))
    leased = recovery_service.acquire_lease(
        run.run_id,
        "recovery-worker",
        "takeover-after-hard-exit",
        prior_worker_stopped=True,
    )
    recovery_token = LeaseToken.from_run(leased)
    report = RecoveryService(recovery_service, ledger).reconcile(run.run_id, recovery_token)

    assert report.findings[0].classification == "campaign_settlement_repaired"
    assert ledger.attempt("crash-campaign", "crash-model-call").actual_cost == Decimal("0.10")
    assert not recovery_service.store.get(run.run_id).reservations


def test_hard_exit_after_tool_side_effect_is_classified_without_replay(
    tmp_path, store, service, running, clock
):
    run, token = running
    service.reserve_tool_call(
        run.run_id,
        ToolCallReservation(
            call_id="crash-tool-call",
            name="replace_text",
            arguments_hash="c" * 64,
            workspace_revision="before",
        ),
        token,
        "reserve-crash-tool",
    )
    marker = tmp_path / "side-effect.txt"
    script = (
        "from pathlib import Path; import os, sys; "
        "Path(sys.argv[1]).write_text('once'); os._exit(24)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(marker)],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 24, result.stderr.decode(errors="replace")

    clock.advance(61)
    recovery_service = HarnessService(SQLiteEventStore(store.path, clock=clock))
    leased = recovery_service.acquire_lease(
        run.run_id,
        "recovery-worker",
        "takeover-tool-hard-exit",
        prior_worker_stopped=True,
    )
    recovery_token = LeaseToken.from_run(leased)
    report = RecoveryService(
        recovery_service,
        CampaignBudgetLedger(tmp_path / "empty-campaign.sqlite3", clock=clock),
    ).reconcile(run.run_id, recovery_token)

    restored = recovery_service.store.get(run.run_id)
    assert marker.read_text() == "once"
    assert report.findings[0].classification == "tool_effect_unknown"
    assert restored.unknown_tool_calls == {"crash-tool-call"}
    assert not restored.tool_calls
    released = recovery_service.release_lease(run.run_id, recovery_token, "release-tool-recovery")
    assert released.lease_id is None


def test_hard_exit_after_model_return_before_response_artifact_is_quarantined(
    tmp_path: Path,
    task_dict,
):
    (
        workspace,
        _,
        store_path,
        ledger_path,
        artifact_path,
        old_now,
        run_id,
        token,
    ) = _prepare_hard_exit_agent(tmp_path, task_dict)
    marker = tmp_path / "provider-returned.txt"
    worker = Path(__file__).with_name("_crash_model_response_worker.py")

    result = subprocess.run(
        [
            sys.executable,
            str(worker),
            str(store_path),
            run_id,
            token.lease_id,
            token.worker_id,
            str(token.epoch),
            str(artifact_path),
            old_now.isoformat(),
            str(workspace),
            str(ledger_path),
            str(marker),
        ],
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 27, result.stderr.decode(errors="replace")
    client_trace_id = marker.read_text(encoding="utf-8")
    interrupted_store = SQLiteEventStore(store_path)
    interrupted = interrupted_store.get(run_id)
    assert not interrupted.model_calls
    assert len(interrupted.model_reservations) == 1
    call_id, reservation = next(iter(interrupted.model_reservations.items()))
    assert reservation.client_trace_id == client_trace_id
    assert not interrupted.unknown_model_calls
    assert CampaignBudgetLedger(ledger_path).attempt("hard-exit-agent", call_id).status == (
        "reserved"
    )
    assert all(
        b"hard-exit-preartifact-response" not in path.read_bytes()
        for path in artifact_path.glob("*/*")
        if path.is_file()
    )
    assert "return [value]" in (workspace / "src/parser.py").read_text(encoding="utf-8")

    recovery_service = HarnessService(interrupted_store)
    recovery_run = recovery_service.acquire_lease(
        run_id,
        "recovery-worker",
        "takeover-preartifact-hard-exit",
        prior_worker_stopped=True,
    )
    recovery_token = LeaseToken.from_run(recovery_run)
    ledger = CampaignBudgetLedger(ledger_path)
    report = RecoveryService(
        recovery_service,
        ledger,
        ArtifactStore(artifact_path),
    ).reconcile(run_id, recovery_token)

    restored = interrupted_store.get(run_id)
    assert report.safe_to_resume is False
    assert report.next_action == "manual_reconciliation"
    assert report.findings[0].classification == "model_effect_unknown"
    assert report.findings[0].client_trace_id == client_trace_id
    assert restored.unknown_model_calls == {call_id}
    assert restored.unknown_reservations == {call_id}
    assert not restored.model_calls
    attempt = ledger.attempt("hard-exit-agent", call_id)
    assert attempt.status == "unknown"
    assert attempt.error_type == "RecoveryUncertainDispatch"

    trace = interrupted_store.export_jsonl(run_id)
    replayed = SQLiteEventStore.replay_jsonl(trace)
    assert replayed.as_dict() == restored.as_dict()
    assert client_trace_id in trace
    assert trace.count('"event_type":"MODEL_CALL_RESERVED"') == 1
    assert trace.count('"event_type":"MODEL_CALL_UNKNOWN"') == 1


def test_hard_exit_after_response_artifact_recovers_without_model_replay(
    tmp_path: Path,
    task_dict,
):
    workspace = tmp_path / "staging" / "workspace"
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
    task_data = {**task_dict, "model_policy_id": "fake-policy"}
    task_data["repository"] = {
        "source": "local",
        "path": str(workspace),
        "base_commit": "a" * 40,
    }
    task = TaskSpec.model_validate(task_data)
    plan = Plan(
        items=(
            WorkItem(
                work_item_id="fix",
                title="Fix parser",
                objective="Return [] for empty input",
                expected_artifacts=("src/parser.py",),
                acceptance_ids=("unit",),
                allowed_tools=("search_repo", "read_file", "replace_text", "run_check"),
            ),
        )
    )
    store_path = tmp_path / "control.sqlite3"
    ledger_path = tmp_path / "campaign.sqlite3"
    artifact_path = tmp_path / "artifacts"
    old_now = datetime.now(UTC) - timedelta(minutes=5)
    old_clock = lambda: old_now  # noqa: E731 - explicit frozen crash-process clock
    store = SQLiteEventStore(store_path, clock=old_clock)
    service = HarnessService(store)
    run = store.create(task, "create-hard-exit-agent")
    service.set_plan(run.run_id, plan, "plan-hard-exit-agent")
    leased = service.acquire_lease(
        run.run_id,
        "crashing-agent",
        "lease-hard-exit-agent",
        ttl_seconds=30,
    )
    token = LeaseToken.from_run(leased)
    service.transition(run.run_id, RunStatus.RUNNING, token, "start-hard-exit-agent")

    script = textwrap.dedent(
        """
        import os
        import sys
        from datetime import datetime
        from pathlib import Path

        from horizon.adapters.persistence.artifacts import ArtifactStore
        from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
        from horizon.adapters.persistence.sqlite import SQLiteEventStore
        from horizon.adapters.workspace.snapshot import SnapshotManager
        from horizon.application.agent_loop import AgentLoopConfig, CodingAgentRunner
        from horizon.application.services import HarnessService, LeaseToken
        from horizon.domain.model import (
            CampaignBudget, FunctionCall, ModelMessage, ModelResponse, ModelUsage,
            PriceCard, ToolCall,
        )
        from horizon.domain.tools import AcceptanceResult
        from horizon.tools.gateway import WorkspaceToolGateway


        class OneResponseModel:
            def generate(self, request, trace_id):
                return ModelResponse(
                    response_id="hard-exit-response",
                    model=request.model,
                    message=ModelMessage(
                        role="assistant",
                        tool_calls=(ToolCall(
                            id="hard-exit-provider-call",
                            function=FunctionCall(
                                name="replace_text",
                                arguments={
                                    "path": "src/parser.py",
                                    "old": "return [value]",
                                    "new": "return [] if value == '' else [value]",
                                },
                            ),
                        ),),
                    ),
                    finish_reason="tool_calls",
                    usage=ModelUsage(input_tokens=101, output_tokens=20),
                    provider_trace_id="hard-exit-provider-trace",
                )


        class UnusedCheckRunner:
            def execute(self, workspace, check):
                raise AssertionError("validation must not run before the injected hard exit")


        class ExitBeforeCampaignSettlement:
            def __init__(self, delegate):
                self.delegate = delegate

            def __getattr__(self, name):
                return getattr(self.delegate, name)

            def settle(self, *args, **kwargs):
                os._exit(25)


        clock = lambda: datetime.fromisoformat(sys.argv[7])
        store = SQLiteEventStore(sys.argv[1], clock=clock)
        service = HarnessService(store)
        run = store.get(sys.argv[2])
        token = LeaseToken(
            lease_id=sys.argv[3],
            worker_id=sys.argv[4],
            epoch=int(sys.argv[5]),
        )
        artifacts = ArtifactStore(Path(sys.argv[6]))
        assert run.plan is not None
        tools = WorkspaceToolGateway(
            Path(sys.argv[8]),
            run.task,
            run.plan.items[0],
            SnapshotManager(artifacts),
            UnusedCheckRunner(),
        )
        ledger = CampaignBudgetLedger(Path(sys.argv[9]), clock=clock)
        runner = CodingAgentRunner(
            service,
            OneResponseModel(),
            ExitBeforeCampaignSettlement(ledger),
            tools,
            artifacts,
            provider_id="fake-provider",
            model_id="fake-model",
            pricing=PriceCard(
                currency="CNY",
                input_per_million="3.00",
                cached_input_per_million="0.30",
                output_per_million="9.00",
                version="hard-exit-test",
                source_url="https://example.test/pricing",
            ),
            campaign=CampaignBudget(
                campaign_id="hard-exit-agent",
                currency="CNY",
                max_cost="3.00",
                max_cost_per_call="1.00",
            ),
            config=AgentLoopConfig(max_model_iterations=8, max_output_tokens=256),
        )
        runner.run(run.run_id, token)
        """
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(store_path),
            run.run_id,
            token.lease_id,
            token.worker_id,
            str(token.epoch),
            str(artifact_path),
            old_now.isoformat(),
            str(workspace),
            str(ledger_path),
        ],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 25, result.stderr.decode(errors="replace")

    artifacts = ArtifactStore(artifact_path)
    interrupted = SQLiteEventStore(store_path).get(run.run_id)
    assert interrupted.agent_session is not None
    assert interrupted.agent_session.next_iteration == 1
    assert len(interrupted.model_calls) == 1
    record = interrupted.model_calls[0]
    assert record.response_artifact_ref is not None
    restored_response = ModelResponse.model_validate_json(
        artifacts.read(record.response_artifact_ref)
    )
    assert restored_response.response_id == "hard-exit-response"
    assert "return [value]" in (workspace / "src/parser.py").read_text(encoding="utf-8")

    ledger = CampaignBudgetLedger(ledger_path)
    assert ledger.attempt("hard-exit-agent", record.call_id).status == "reserved"
    recovery_service = HarnessService(SQLiteEventStore(store_path))
    recovery_run = recovery_service.acquire_lease(
        run.run_id,
        "recovery-worker",
        "takeover-response-hard-exit",
        prior_worker_stopped=True,
    )
    recovery_token = LeaseToken.from_run(recovery_run)
    report = RecoveryService(recovery_service, ledger, artifacts).reconcile(
        run.run_id,
        recovery_token,
    )
    assert report.safe_to_resume is True
    assert report.next_action == "resume"
    assert report.findings[0].classification == "campaign_settlement_repaired"
    settled = ledger.attempt("hard-exit-agent", record.call_id)
    assert settled.status == "settled"
    assert settled.provider_trace_id == "hard-exit-provider-trace"
    recovery_service.release_lease(
        run.run_id,
        recovery_token,
        "release-response-recovery",
    )

    resumed_service = HarnessService(SQLiteEventStore(store_path))
    resumed_run = resumed_service.acquire_lease(
        run.run_id,
        "resumed-agent",
        "lease-resumed-agent",
    )
    resumed_token = LeaseToken.from_run(resumed_run)
    resumed_model = _ScriptedModel(
        [("submit", {"summary": "Recovered response completed without replay."})]
    )
    resumed_tools = WorkspaceToolGateway(
        workspace,
        resumed_run.task,
        plan.items[0],
        SnapshotManager(artifacts),
        _ParserCheck(),
    )
    final = CodingAgentRunner(
        resumed_service,
        resumed_model,
        ledger,
        resumed_tools,
        artifacts,
        provider_id="fake-provider",
        model_id="fake-model",
        pricing=_price_card(),
        campaign=_agent_campaign(),
        config=AgentLoopConfig(max_model_iterations=8, max_output_tokens=256),
    ).run(run.run_id, resumed_token)

    assert final.status == RunStatus.SUCCEEDED
    assert len(resumed_model.requests) == 1
    assert len(final.model_calls) == 2
    assert "return [] if" in (workspace / "src/parser.py").read_text(encoding="utf-8")
    assert any(
        message.role == "tool" and "Replaced 1 occurrence" in (message.content or "")
        for message in resumed_model.requests[0].messages
    )


@pytest.mark.parametrize(
    ("tool_name", "disposition"),
    [
        ("replace_text", "accept_replace"),
        ("apply_patch", "accept_patch"),
    ],
)
def test_hard_exit_after_write_effect_can_be_exactly_accepted_and_resumed(
    tmp_path: Path,
    task_dict,
    tool_name,
    disposition,
):
    (
        workspace,
        plan,
        store_path,
        ledger_path,
        artifact_path,
        old_now,
        run_id,
        token,
    ) = _prepare_hard_exit_agent(tmp_path, task_dict)
    worker = Path(__file__).with_name("_crash_agent_worker.py")
    result = subprocess.run(
        [
            sys.executable,
            str(worker),
            str(store_path),
            run_id,
            token.lease_id,
            token.worker_id,
            str(token.epoch),
            str(artifact_path),
            old_now.isoformat(),
            str(workspace),
            str(ledger_path),
            tool_name,
        ],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 26, result.stderr.decode(errors="replace")

    interrupted = SQLiteEventStore(store_path).get(run_id)
    assert len(interrupted.model_calls) == 1
    assert len(interrupted.tool_reservations) == 1
    call_id = next(iter(interrupted.tool_reservations))
    reservation = interrupted.tool_reservations[call_id]
    assert reservation.name == tool_name
    assert reservation.workspace_manifest_ref is not None
    assert "return [] if" in (workspace / "src/parser.py").read_text(encoding="utf-8")
    ledger = CampaignBudgetLedger(ledger_path)
    assert ledger.attempt("hard-exit-agent", interrupted.model_calls[0].call_id).status == (
        "settled"
    )

    artifacts = ArtifactStore(artifact_path)
    recovery_service = HarnessService(SQLiteEventStore(store_path))
    recovery_run = recovery_service.acquire_lease(
        run_id,
        "recovery-worker",
        "takeover-replace-hard-exit",
        prior_worker_stopped=True,
    )
    recovery_token = LeaseToken.from_run(recovery_run)
    blocked = RecoveryService(recovery_service, ledger, artifacts).reconcile(
        run_id,
        recovery_token,
    )
    assert blocked.safe_to_resume is False
    assert blocked.findings[-1].classification == "tool_effect_unknown"
    tools = WorkspaceToolGateway(
        workspace,
        recovery_run.task,
        plan.items[0],
        SnapshotManager(artifacts),
        _ParserCheck(),
    )
    resolved = ToolRecoveryService(recovery_service, artifacts, tools).resolve_write(
        run_id,
        call_id,
        decision="accept",
        token=recovery_token,
    )
    assert resolved.tool_calls[-1].recovery_disposition == disposition
    assert resolved.agent_session is not None
    assert resolved.agent_session.next_iteration == 2
    safe = RecoveryService(recovery_service, ledger, artifacts).reconcile(
        run_id,
        recovery_token,
    )
    assert safe.safe_to_resume is True
    recovery_service.release_lease(
        run_id,
        recovery_token,
        "release-accepted-hard-exit",
    )

    resumed_service = HarnessService(SQLiteEventStore(store_path))
    resumed_run = resumed_service.acquire_lease(
        run_id,
        "resumed-agent",
        "lease-resumed-after-write-recovery",
    )
    resumed_token = LeaseToken.from_run(resumed_run)
    resumed_model = _ScriptedModel(
        [("submit", {"summary": "Accepted hard-exit replace completed."})]
    )
    final = CodingAgentRunner(
        resumed_service,
        resumed_model,
        ledger,
        tools,
        artifacts,
        provider_id="fake-provider",
        model_id="fake-model",
        pricing=_price_card(),
        campaign=_agent_campaign(),
        config=AgentLoopConfig(max_model_iterations=8, max_output_tokens=256),
    ).run(run_id, resumed_token)

    assert final.status == RunStatus.SUCCEEDED
    assert len(final.model_calls) == 2
    assert len(resumed_model.requests) == 1
    assert any(
        message.role == "tool" and disposition in (message.content or "")
        for message in resumed_model.requests[0].messages
    )


@pytest.mark.parametrize(
    ("decision", "disposition", "status", "file_exists"),
    [
        ("accept", "accept_create", "success", True),
        ("rollback", "rollback_create", "cancelled", False),
    ],
)
def test_hard_exit_after_create_file_requires_explicit_exact_resolution(
    tmp_path: Path,
    task_dict,
    decision,
    disposition,
    status,
    file_exists,
):
    (
        workspace,
        plan,
        store_path,
        ledger_path,
        artifact_path,
        old_now,
        run_id,
        token,
    ) = _prepare_hard_exit_agent(tmp_path, task_dict)
    worker = Path(__file__).with_name("_crash_agent_worker.py")
    result = subprocess.run(
        [
            sys.executable,
            str(worker),
            str(store_path),
            run_id,
            token.lease_id,
            token.worker_id,
            str(token.epoch),
            str(artifact_path),
            old_now.isoformat(),
            str(workspace),
            str(ledger_path),
            "create_file",
        ],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 26, result.stderr.decode(errors="replace")

    expected_arguments = {
        "path": "src/generated.py",
        "content": "def generated():\n    return '你好'\n",
    }
    expected_bytes = expected_arguments["content"].encode("utf-8")
    interrupted_store = SQLiteEventStore(store_path)
    interrupted = interrupted_store.get(run_id)
    assert (workspace / "src/generated.py").read_bytes() == expected_bytes
    assert len(interrupted.model_calls) == 1
    assert len(interrupted.tool_calls) == 0
    assert len(interrupted.tool_reservations) == 1
    call_id, reservation = next(iter(interrupted.tool_reservations.items()))
    assert reservation.name == "create_file"
    assert reservation.arguments_hash == digest(expected_arguments)
    assert reservation.workspace_manifest_ref is not None
    pre_manifest = ArtifactStore(artifact_path).read(reservation.workspace_manifest_ref)
    assert b"src/generated.py" not in pre_manifest

    artifacts = ArtifactStore(artifact_path)
    ledger = CampaignBudgetLedger(ledger_path)
    recovery_service = HarnessService(interrupted_store)
    recovery_run = recovery_service.acquire_lease(
        run_id,
        "recovery-worker",
        "takeover-create-hard-exit",
        prior_worker_stopped=True,
    )
    recovery_token = LeaseToken.from_run(recovery_run)
    report = RecoveryService(recovery_service, ledger, artifacts).reconcile(
        run_id,
        recovery_token,
    )

    assert report.safe_to_resume is False
    assert report.findings[-1].classification == "tool_effect_unknown"
    blocked = interrupted_store.get(run_id)
    assert blocked.unknown_tool_calls == {call_id}
    assert len(blocked.tool_calls) == 0
    assert (workspace / "src/generated.py").read_bytes() == expected_bytes

    tools = WorkspaceToolGateway(
        workspace,
        recovery_run.task,
        plan.items[0],
        SnapshotManager(artifacts),
        _ParserCheck(),
    )
    resolved = ToolRecoveryService(recovery_service, artifacts, tools).resolve_write(
        run_id,
        call_id,
        decision=decision,
        token=recovery_token,
    )

    record = resolved.tool_calls[-1]
    assert record.status == status
    assert record.recovery_disposition == disposition
    assert not resolved.reservations
    assert not resolved.unknown_tool_calls
    assert resolved.agent_session is not None
    assert resolved.agent_session.next_iteration == 2
    assert (workspace / "src/generated.py").exists() is file_exists
    if file_exists:
        assert (workspace / "src/generated.py").read_bytes() == expected_bytes
    safe = RecoveryService(recovery_service, ledger, artifacts).reconcile(
        run_id,
        recovery_token,
    )
    assert safe.safe_to_resume is True

    trace = interrupted_store.export_jsonl(run_id)
    replayed = SQLiteEventStore.replay_jsonl(trace)
    assert replayed.as_dict() == interrupted_store.get(run_id).as_dict()
