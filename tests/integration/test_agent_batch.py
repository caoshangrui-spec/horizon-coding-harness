import hashlib
import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import horizon.interfaces.cli.app as cli_app_module
from horizon.adapters.model.config import load_provider_config
from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.application.agent_batch import (
    SequentialAgentBatchRunner,
    write_agent_run_launch_receipt,
)
from horizon.application.services import HarnessService
from horizon.domain.agent_batch import (
    AgentBatchCase,
    AgentBatchManifest,
    AgentRunLaunchReceipt,
)
from horizon.domain.common import canonical_json
from horizon.domain.model import (
    FunctionCall,
    ModelMessage,
    ModelResponse,
    ModelUsage,
    ToolCall,
)
from horizon.domain.plan import Plan, WorkItem
from horizon.domain.states import RunStatus
from horizon.domain.task import TaskSpec
from horizon.domain.tools import AcceptanceResult
from horizon.interfaces.cli.app import app

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROVIDER = PROJECT_ROOT / "config" / "providers" / "siliconflow.yaml"


class ManualFixModel:
    def __init__(self):
        self.index = 0

    def generate(self, request, trace_id):
        actions = (
            (
                "replace_text",
                {
                    "path": "src/parser.py",
                    "old": "return [value]",
                    "new": "return [] if value == '' else [value]",
                },
            ),
            ("submit", {"summary": "Parser fix is ready."}),
        )
        name, arguments = actions[self.index]
        self.index += 1
        return ModelResponse(
            response_id=f"batch-response-{self.index}",
            model=request.model,
            message=ModelMessage(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        id=f"batch-tool-{self.index}",
                        function=FunctionCall(name=name, arguments=arguments),
                    ),
                ),
            ),
            finish_reason="tool_calls",
            usage=ModelUsage(input_tokens=100, output_tokens=20),
            provider_trace_id=f"batch-trace-{self.index}",
        )


class LocalBatchAcceptance:
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


def _write_inputs(tmp_path, task, plan, case_ids=("one", "two", "three")):
    task_path = tmp_path / "task.yaml"
    plan_path = tmp_path / "plan.yaml"
    task_path.write_text(
        yaml.safe_dump(task.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    plan_path.write_text(
        yaml.safe_dump(plan.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    manifest = AgentBatchManifest(
        batch_id="sequential-agent-cases",
        cases=tuple(
            AgentBatchCase(
                case_id=case_id,
                task_path=task_path.name,
                plan_path=plan_path.name,
            )
            for case_id in case_ids
        ),
    )
    manifest_path = tmp_path / "batch.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    return manifest_path, manifest


class CancellingExecutor:
    def __init__(self, store):
        self.store = store
        self.service = HarnessService(store)
        self.calls = []

    def __call__(self, request):
        self.calls.append(request.case.spec.case_id)
        task = TaskSpec.model_validate(
            {
                **request.case.task.model_dump(mode="json"),
                "task_id": request.expected_task_id,
            }
        )
        run = self.store.create(task, f"fake-create-{request.invocation_id}")
        write_agent_run_launch_receipt(
            request.launch_receipt_path,
            AgentRunLaunchReceipt(
                invocation_id=request.invocation_id,
                run_id=run.run_id,
                task_id=task.task_id,
                task_spec_hash=task.sha256,
                source_revision="a" * 64,
                workspace_path_hash="b" * 64,
            ),
        )
        self.service.cancel(run.run_id, f"fake-cancel-{request.invocation_id}")


def test_agent_batch_checkpoints_resumes_and_exports_terminal_suite(
    tmp_path,
    task,
    plan,
):
    manifest_path, manifest = _write_inputs(tmp_path, task, plan)
    store = SQLiteEventStore(tmp_path / "control.sqlite3")
    executor = CancellingExecutor(store)
    runner = SequentialAgentBatchRunner(store, executor)
    output = tmp_path / "batch-output"

    first = runner.run(
        manifest_path,
        manifest,
        output,
        max_cases_this_invocation=1,
    )

    assert first.state.complete is False
    assert first.launched_case_count == 1
    assert [case.state for case in first.state.cases] == ["terminal", "pending", "pending"]
    assert executor.calls == ["one"]

    completed = runner.run(
        manifest_path,
        manifest,
        output,
        resume=True,
        max_cases_this_invocation=2,
    )

    assert completed.state.complete is True
    assert completed.state.terminal_suite_path == "terminal-suite/suite-pack.json"
    assert [case.run_status for case in completed.state.cases] == [
        RunStatus.CANCELLED,
        RunStatus.CANCELLED,
        RunStatus.CANCELLED,
    ]
    assert executor.calls == ["one", "two", "three"]
    report = completed.terminal_suite.suite_pack.report
    assert report.case_count == 3
    assert report.cancelled_run_count == 3
    assert report.task_success_rate == 0.0

    repeated = runner.run(manifest_path, manifest, output, resume=True)
    assert repeated.state == completed.state
    assert repeated.launched_case_count == 0
    assert executor.calls == ["one", "two", "three"]


def test_agent_batch_blocks_on_nonterminal_case_then_reconciles_external_terminal_state(
    tmp_path,
    task,
    plan,
):
    manifest_path, manifest = _write_inputs(tmp_path, task, plan, case_ids=("blocked", "later"))
    store = SQLiteEventStore(tmp_path / "control.sqlite3")
    service = HarnessService(store)
    calls = []

    def executor(request):
        calls.append(request.case.spec.case_id)
        task_instance = TaskSpec.model_validate(
            {
                **request.case.task.model_dump(mode="json"),
                "task_id": request.expected_task_id,
            }
        )
        run = store.create(task_instance, f"create-{request.invocation_id}")
        write_agent_run_launch_receipt(
            request.launch_receipt_path,
            AgentRunLaunchReceipt(
                invocation_id=request.invocation_id,
                run_id=run.run_id,
                task_id=task_instance.task_id,
                task_spec_hash=task_instance.sha256,
                source_revision="c" * 64,
                workspace_path_hash="d" * 64,
            ),
        )
        if request.case.spec.case_id == "later":
            service.cancel(run.run_id, f"cancel-{request.invocation_id}")

    runner = SequentialAgentBatchRunner(store, executor)
    output = tmp_path / "attention-output"

    blocked = runner.run(manifest_path, manifest, output)

    assert calls == ["blocked"]
    assert [case.state for case in blocked.state.cases] == ["attention", "pending"]
    blocked_run_id = blocked.state.cases[0].run_id
    assert blocked_run_id is not None
    service.cancel(blocked_run_id, "operator-cancel-blocked-case")

    resumed = runner.run(manifest_path, manifest, output, resume=True)

    assert resumed.state.complete is True
    assert calls == ["blocked", "later"]
    assert all(case.run_status == RunStatus.CANCELLED for case in resumed.state.cases)


def test_agent_batch_retries_only_when_failed_attempt_created_no_run(
    tmp_path,
    task,
    plan,
):
    manifest_path, manifest = _write_inputs(tmp_path, task, plan, case_ids=("retry",))
    store = SQLiteEventStore(tmp_path / "control.sqlite3")
    delegate = CancellingExecutor(store)
    attempts = []

    def flaky(request):
        attempts.append(request.invocation_id)
        if len(attempts) == 1:
            raise RuntimeError("failed before durable Run creation")
        delegate(request)

    runner = SequentialAgentBatchRunner(store, flaky)
    output = tmp_path / "retry-output"

    failed = runner.run(manifest_path, manifest, output)

    record = failed.state.cases[0]
    assert record.state == "pending"
    assert record.attempt_count == 1
    assert record.last_error_type == "RuntimeError"
    assert store.list_runs() == []

    completed = runner.run(manifest_path, manifest, output, resume=True)

    assert completed.state.complete is True
    assert completed.state.cases[0].attempt_count == 2
    assert attempts[0] != attempts[1]


def test_agent_batch_cli_uses_checkpoint_runner_without_calling_a_real_model(
    tmp_path,
    monkeypatch,
    task_dict,
    plan,
):
    monkeypatch.chdir(tmp_path)
    provider = load_provider_config(PROVIDER)
    source = tmp_path / "source"
    source.mkdir()
    task = TaskSpec.model_validate(
        {
            **task_dict,
            "repository": {
                "source": "local",
                "path": str(source),
                "base_commit": "a" * 40,
            },
            "model_policy_id": provider.policy_id,
        }
    )
    manifest_path, _ = _write_inputs(tmp_path, task, plan, case_ids=("cli-case",))
    dotenv = tmp_path / ".env"
    dotenv.write_text("SILICONFLOW_API_KEY=fake-offline-key\n", encoding="utf-8")

    def fake_cli_executor(ctx, request, **kwargs):
        store = SQLiteEventStore(ctx.obj["db"])
        task_instance = TaskSpec.model_validate(
            {
                **request.case.task.model_dump(mode="json"),
                "task_id": request.expected_task_id,
            }
        )
        run = store.create(task_instance, f"cli-create-{request.invocation_id}")
        write_agent_run_launch_receipt(
            request.launch_receipt_path,
            AgentRunLaunchReceipt(
                invocation_id=request.invocation_id,
                run_id=run.run_id,
                task_id=task_instance.task_id,
                task_spec_hash=task_instance.sha256,
                source_revision="e" * 64,
                workspace_path_hash="f" * 64,
            ),
        )
        HarnessService(store).cancel(run.run_id, f"cli-cancel-{request.invocation_id}")

    monkeypatch.setattr(cli_app_module, "_execute_agent_batch_case", fake_cli_executor)
    result = CliRunner().invoke(
        app,
        [
            "agent",
            "batch",
            str(manifest_path),
            "--image",
            "offline-fixture",
            "--output",
            str(tmp_path / "cli-output"),
            "--config",
            str(PROVIDER),
            "--dotenv",
            str(dotenv),
            "--confirm-paid",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["complete"] is True
    assert payload["case_count"] == 1
    assert payload["terminal_case_count"] == 1
    assert payload["launched_case_count"] == 1
    assert payload["paid_execution_authorized"] is True
    state = json.loads((tmp_path / "cli-output/batch-state.json").read_text(encoding="utf-8"))
    assert state["complete"] is True
    assert "fake-offline-key" not in canonical_json(state)


def test_agent_batch_cli_requires_paid_confirmation_before_creating_checkpoint(
    tmp_path,
    monkeypatch,
    task,
    plan,
):
    monkeypatch.chdir(tmp_path)
    manifest_path, _ = _write_inputs(tmp_path, task, plan, case_ids=("not-started",))
    output = tmp_path / "must-not-exist"

    result = CliRunner().invoke(
        app,
        [
            "agent",
            "batch",
            str(manifest_path),
            "--image",
            "offline-fixture",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 2
    assert "--confirm-paid" in result.output
    assert not output.exists()
    assert not (tmp_path / ".horizon").exists()


def test_agent_batch_cli_executes_two_isolated_fake_agent_runs_end_to_end(
    tmp_path,
    monkeypatch,
    task_dict,
):
    monkeypatch.chdir(tmp_path)
    provider = load_provider_config(PROVIDER)
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "tests").mkdir()
    original = "def parse(value):\n    return [value]\n"
    (source / "src/parser.py").write_text(original, encoding="utf-8")
    (source / "tests/test_parser.py").write_text("# protected\n", encoding="utf-8")
    task = TaskSpec.model_validate(
        {
            **task_dict,
            "repository": {
                "source": "local",
                "path": str(source),
                "base_commit": "a" * 40,
            },
            "model_policy_id": provider.policy_id,
        }
    )
    writable_plan = Plan(
        items=(
            WorkItem(
                work_item_id="fix",
                title="Fix parser",
                objective="Modify and verify the parser",
                expected_artifacts=("src/parser.py",),
                acceptance_ids=("unit",),
                allowed_tools=("read_file", "replace_text", "run_check"),
            ),
        )
    )
    manifest_path, _ = _write_inputs(
        tmp_path,
        task,
        writable_plan,
        case_ids=("first", "second"),
    )
    dotenv = tmp_path / ".env"
    dotenv.write_text("SILICONFLOW_API_KEY=fake-offline-key\n", encoding="utf-8")
    monkeypatch.setattr(
        cli_app_module,
        "OpenAICompatibleModelGateway",
        lambda *args, **kwargs: ManualFixModel(),
    )
    monkeypatch.setattr(cli_app_module, "DockerSandbox", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli_app_module, "DockerAcceptanceExecutor", LocalBatchAcceptance)

    result = CliRunner().invoke(
        app,
        [
            "agent",
            "batch",
            str(manifest_path),
            "--image",
            "offline-fixture",
            "--output",
            str(tmp_path / "e2e-output"),
            "--config",
            str(PROVIDER),
            "--dotenv",
            str(dotenv),
            "--confirm-paid",
            "--in-process",
            "--slice-iterations",
            "8",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["complete"] is True
    assert payload["terminal_case_count"] == 2
    assert (source / "src/parser.py").read_text(encoding="utf-8") == original
    suite = json.loads(
        (tmp_path / "e2e-output/terminal-suite/suite-report.json").read_text(encoding="utf-8")
    )
    assert suite["succeeded_run_count"] == 2
    assert suite["task_success_rate"] == 1.0
    state = json.loads((tmp_path / "e2e-output/batch-state.json").read_text(encoding="utf-8"))
    assert len({case["run_id"] for case in state["cases"]}) == 2
    assert all(case["attempt_count"] == 1 for case in state["cases"])
    assert all(
        (tmp_path / f"e2e-output/cases/{index:03d}-{case_id}/run-receipt.json").is_file()
        for index, case_id in enumerate(("first", "second"), start=1)
    )


def test_agent_batch_manifest_rejects_ambiguous_plan_sources():
    with pytest.raises(ValueError, match="exactly one Plan source"):
        AgentBatchCase(
            case_id="ambiguous",
            task_path="task.yaml",
            plan_path="plan.yaml",
            auto_plan=True,
        )
