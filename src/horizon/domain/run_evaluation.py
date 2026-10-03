from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, StrictInt, model_validator

from horizon.domain.budget import Usage
from horizon.domain.common import Contract, digest
from horizon.domain.human import NoProgressPattern
from horizon.domain.plan import Plan
from horizon.domain.states import RunStatus
from horizon.domain.task import (
    Identifier,
    NonNegativeInt,
    PositiveInt,
    TaskSpec,
    Text,
    relative_pattern,
)
from horizon.domain.tools import AcceptanceResult

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
GitCommit = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
NonNegativeMoney = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
SignedCount = StrictInt


class ScriptedModelAction(Contract):
    tool: Identifier
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    input_tokens: NonNegativeInt = 100
    output_tokens: NonNegativeInt = 20


class RunABArm(Contract):
    arm_id: Identifier
    role: Literal["baseline", "single_replan"]
    description: Annotated[str, Field(min_length=1, max_length=500)]
    actions: Annotated[tuple[ScriptedModelAction, ...], Field(min_length=1, max_length=50)]
    expected_status: RunStatus
    expected_plan_version: Annotated[StrictInt, Field(gt=0)]
    expected_execution_replans: NonNegativeInt
    expected_passed_items: tuple[Identifier, ...] = ()
    expected_human_request_pattern: NoProgressPattern | None = None
    expected_model_calls: NonNegativeInt
    expected_tool_calls: NonNegativeInt
    expected_steps: NonNegativeInt
    restart_after_model_calls: PositiveInt | None = None

    @model_validator(mode="after")
    def validate_expectation(self) -> Self:
        if len(self.expected_passed_items) != len(set(self.expected_passed_items)):
            raise ValueError("Expected passed WorkItems must be unique")
        if (
            self.expected_human_request_pattern is not None
            and self.expected_status != RunStatus.WAITING_FOR_USER
        ):
            raise ValueError("A human-request pattern requires WAITING_FOR_USER")
        if self.restart_after_model_calls is not None and self.restart_after_model_calls >= len(
            self.actions
        ):
            raise ValueError("A worker restart must leave at least one scripted action to resume")
        return self


class RunABEvalManifest(Contract):
    schema_version: Literal[1] = 1
    benchmark_id: Identifier
    fixture_path: Text
    task: TaskSpec
    initial_plan: Plan
    expected_initial_failed_checks: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    arms: Annotated[tuple[RunABArm, ...], Field(min_length=2, max_length=2)]

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        relative_pattern(self.fixture_path)
        if any(character in self.fixture_path for character in "*?[]"):
            raise ValueError("Run A/B fixture path must be literal")
        if {arm.role for arm in self.arms} != {"baseline", "single_replan"}:
            raise ValueError("Run A/B evaluation requires one baseline and one single_replan arm")
        if len({arm.arm_id for arm in self.arms}) != len(self.arms):
            raise ValueError("Run A/B arm IDs must be unique")
        expected_failures = set(self.expected_initial_failed_checks)
        if len(expected_failures) != len(self.expected_initial_failed_checks):
            raise ValueError("Expected initial failed checks must be unique")
        known_checks = {check.id for check in self.task.acceptance}
        if not expected_failures <= known_checks:
            raise ValueError("Expected initial failures must reference TaskSpec acceptance checks")
        self.initial_plan.check_task(self.task)
        return self

    @property
    def sha256(self) -> str:
        return digest(self)


class RunABArmResult(Contract):
    arm_id: Identifier
    role: Literal["baseline", "single_replan"]
    run_id: Identifier
    status: RunStatus
    plan_version: Annotated[StrictInt, Field(gt=0)]
    execution_replans: NonNegativeInt
    passed_items: tuple[Identifier, ...]
    human_request_pattern: NoProgressPattern | None = None
    usage: Usage
    model_cost: NonNegativeMoney
    model_currency: Literal["CNY", "USD"]
    event_count: NonNegativeInt
    worker_restarts: Literal[0, 1]
    final_lease_epoch: PositiveInt
    trace_ref: Sha256
    projection_hash: Sha256
    workspace_revision: Sha256
    workspace_manifest_ref: Sha256
    final_validation_passed: bool | None = None
    actions_consumed: bool
    trace_replay_verified: bool
    source_workspace_unchanged: bool
    unknown_model_calls: NonNegativeInt
    unknown_tool_calls: NonNegativeInt
    open_reservations: NonNegativeInt
    expectation_failures: tuple[Text, ...] = ()
    passed: bool

    @model_validator(mode="after")
    def validate_restart_evidence(self) -> Self:
        if self.final_lease_epoch != self.worker_restarts + 1:
            raise ValueError("Final lease epoch must account for every injected worker restart")
        return self


class RunABEvalReport(Contract):
    schema_version: Literal[1] = 1
    benchmark_id: Identifier
    evaluation_id: Identifier
    manifest_digest: Sha256
    fixture_revision: Sha256
    fixture_manifest_ref: Sha256
    validation_backend: Identifier
    validation_backend_ref: Text
    initial_validation: tuple[AcceptanceResult, ...]
    expected_initial_failed_checks: tuple[Identifier, ...]
    initial_failed_checks: tuple[Identifier, ...]
    initial_validation_matches_expectation: bool
    baseline: RunABArmResult
    single_replan: RunABArmResult
    all_expectations_met: bool
    treatment_recovered: bool
    success_delta: Annotated[SignedCount, Field(ge=-1, le=1)]
    model_call_delta: SignedCount
    tool_call_delta: SignedCount
    step_delta: SignedCount
    model_cost_delta: Decimal
    paid_model_called: Literal[False] = False
    network_called: Literal[False] = False
    repository_code_executed: bool

    @model_validator(mode="after")
    def validate_comparison(self) -> Self:
        if self.baseline.role != "baseline" or self.single_replan.role != "single_replan":
            raise ValueError("Run A/B report arms do not match their roles")
        observed_initial_failures = tuple(
            sorted(result.check_id for result in self.initial_validation if not result.passed)
        )
        if observed_initial_failures != tuple(sorted(self.initial_failed_checks)):
            raise ValueError(
                "Run A/B initial failed-check summary does not match validation receipts"
            )
        initial_matches = tuple(sorted(self.expected_initial_failed_checks)) == tuple(
            sorted(self.initial_failed_checks)
        )
        if self.initial_validation_matches_expectation != initial_matches:
            raise ValueError("Run A/B initial-validation verdict does not match failed checks")
        expected_all = (
            self.initial_validation_matches_expectation
            and self.baseline.passed
            and self.single_replan.passed
        )
        if self.all_expectations_met != expected_all:
            raise ValueError("Run A/B aggregate verdict does not match its arms")
        baseline_success = self.baseline.status == RunStatus.SUCCEEDED
        treatment_success = self.single_replan.status == RunStatus.SUCCEEDED
        if self.treatment_recovered != (not baseline_success and treatment_success):
            raise ValueError("Run A/B recovery verdict does not match arm outcomes")
        if self.success_delta != int(treatment_success) - int(baseline_success):
            raise ValueError("Run A/B success delta does not match arm outcomes")
        if self.model_call_delta != (
            self.single_replan.usage.model_calls - self.baseline.usage.model_calls
        ):
            raise ValueError("Run A/B model-call delta does not match arm usage")
        if self.tool_call_delta != (
            self.single_replan.usage.tool_calls - self.baseline.usage.tool_calls
        ):
            raise ValueError("Run A/B tool-call delta does not match arm usage")
        if self.step_delta != self.single_replan.usage.steps - self.baseline.usage.steps:
            raise ValueError("Run A/B step delta does not match arm usage")
        if self.model_cost_delta != self.single_replan.model_cost - self.baseline.model_cost:
            raise ValueError("Run A/B cost delta does not match arm costs")
        return self


class ExternalTaskSource(Contract):
    benchmark: Identifier
    project: Identifier
    bug_id: Identifier
    repository_url: Annotated[str, Field(pattern=r"^https://github\.com/[^/]+/[^/]+/?$")]
    buggy_commit: GitCommit
    fixed_commit: GitCommit
    fix_url: Annotated[str, Field(pattern=r"^https://github\.com/.+/commit/[a-f0-9]{40}$")]
    upstream_test: Text
    license_spdx: Identifier
    license_url: Annotated[str, Field(pattern=r"^https://github\.com/.+$")]
    reduction: Literal["dependency_reduced", "full_checkout"] = "dependency_reduced"
    reduction_note: Annotated[str, Field(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def validate_commit_links(self) -> Self:
        repository = self.repository_url.rstrip("/")
        if self.buggy_commit == self.fixed_commit:
            raise ValueError("External source buggy and fixed commits must differ")
        if self.fix_url != f"{repository}/commit/{self.fixed_commit}":
            raise ValueError("External source fix URL must identify its fixed commit")
        if not self.license_url.startswith(f"{repository}/"):
            raise ValueError("External source license URL must belong to its repository")
        return self


class RunABSuiteCase(Contract):
    case_id: Identifier
    manifest_path: Text
    source: ExternalTaskSource

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        relative_pattern(self.manifest_path)
        if any(character in self.manifest_path for character in "*?[]"):
            raise ValueError("Run A/B suite manifest paths must be literal")
        return self

    def check_manifest(self, manifest: RunABEvalManifest) -> None:
        if manifest.benchmark_id != self.case_id:
            raise ValueError("Run A/B suite case ID must match the case benchmark ID")
        if manifest.task.repository.base_commit != self.source.buggy_commit:
            raise ValueError("Run A/B suite source buggy commit must match the task base commit")


class RunABSuiteManifest(Contract):
    schema_version: Literal[1] = 1
    suite_id: Identifier
    cases: Annotated[tuple[RunABSuiteCase, ...], Field(min_length=1, max_length=20)]

    @model_validator(mode="after")
    def validate_cases(self) -> Self:
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Run A/B suite case IDs must be unique")
        paths = [case.manifest_path.casefold() for case in self.cases]
        if len(paths) != len(set(paths)):
            raise ValueError("Run A/B suite manifest paths must be unique")
        sources = [(case.source.project, case.source.bug_id) for case in self.cases]
        if len(sources) != len(set(sources)):
            raise ValueError("Run A/B suite source bugs must be unique")
        return self

    @property
    def sha256(self) -> str:
        return digest(self)


class RunABSuiteCaseResult(Contract):
    case_id: Identifier
    manifest_path: Text
    source: ExternalTaskSource
    manifest_digest: Sha256
    fixture_revision: Sha256
    report_ref: Sha256
    passed: bool
    initial_failure_confirmed: bool
    treatment_recovered: bool
    baseline_status: RunStatus
    single_replan_status: RunStatus
    model_call_delta: SignedCount
    tool_call_delta: SignedCount
    step_delta: SignedCount
    model_cost_delta: Decimal


class RunABSuiteReport(Contract):
    schema_version: Literal[1] = 1
    suite_id: Identifier
    suite_manifest_digest: Sha256
    validation_backend: Identifier
    validation_backend_ref: Text
    cases: Annotated[tuple[RunABSuiteCaseResult, ...], Field(min_length=1, max_length=20)]
    case_count: NonNegativeInt
    passed_case_count: NonNegativeInt
    initial_failure_confirmed_count: NonNegativeInt
    treatment_recovered_count: NonNegativeInt
    success_delta: SignedCount
    model_call_delta: SignedCount
    tool_call_delta: SignedCount
    step_delta: SignedCount
    model_cost_delta: Decimal
    all_expectations_met: bool
    paid_model_called: Literal[False] = False
    network_called: Literal[False] = False
    repository_code_executed: bool

    @model_validator(mode="after")
    def validate_aggregate(self) -> Self:
        expected = {
            "case_count": len(self.cases),
            "passed_case_count": sum(case.passed for case in self.cases),
            "initial_failure_confirmed_count": sum(
                case.initial_failure_confirmed for case in self.cases
            ),
            "treatment_recovered_count": sum(case.treatment_recovered for case in self.cases),
            "model_call_delta": sum(case.model_call_delta for case in self.cases),
            "tool_call_delta": sum(case.tool_call_delta for case in self.cases),
            "step_delta": sum(case.step_delta for case in self.cases),
            "model_cost_delta": sum(
                (case.model_cost_delta for case in self.cases), start=Decimal("0")
            ),
        }
        for field, value in expected.items():
            if getattr(self, field) != value:
                raise ValueError(f"Run A/B suite aggregate {field} does not match its cases")
        success_delta = sum(
            int(case.single_replan_status == RunStatus.SUCCEEDED)
            - int(case.baseline_status == RunStatus.SUCCEEDED)
            for case in self.cases
        )
        if self.success_delta != success_delta:
            raise ValueError("Run A/B suite success delta does not match its cases")
        if self.all_expectations_met != all(case.passed for case in self.cases):
            raise ValueError("Run A/B suite verdict does not match its cases")
        return self
