from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, StrictInt, model_validator

from horizon.domain.common import Contract, digest
from horizon.domain.human import NoProgressPattern
from horizon.domain.plan import Plan, WorkItem
from horizon.domain.task import Identifier, TaskSpec

ReliabilityDecision = Literal["allow", "soft_block", "hard_stop"]
ReplanDecision = Literal["accept", "reject"]
Ratio = Annotated[float, Field(ge=0.0, le=1.0)]
Count = Annotated[StrictInt, Field(ge=0)]
PositiveIndex = Annotated[StrictInt, Field(gt=0)]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class NoProgressEvalAction(Contract):
    tool: Identifier
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    workspace_revision_before: Identifier = "revision-a"
    workspace_revision_after: Identifier | None = None
    reset_before: bool = False
    expected_decision: ReliabilityDecision
    expected_pattern: NoProgressPattern | None = None

    @model_validator(mode="after")
    def validate_expectation(self) -> Self:
        if (self.expected_decision == "allow") != (self.expected_pattern is None):
            raise ValueError("Allowed actions must omit a pattern; blocked actions must name one")
        return self

    @property
    def settled_revision(self) -> str:
        return self.workspace_revision_after or self.workspace_revision_before


class NoProgressEvalCase(Contract):
    kind: Literal["no_progress"] = "no_progress"
    case_id: Identifier
    description: Annotated[str, Field(min_length=1, max_length=500)]
    actions: Annotated[tuple[NoProgressEvalAction, ...], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def hard_stop_is_final_ground_truth(self) -> Self:
        hard_stops = [
            index
            for index, action in enumerate(self.actions)
            if action.expected_decision == "hard_stop"
        ]
        if hard_stops and hard_stops != [len(self.actions) - 1]:
            raise ValueError("A no-progress ground-truth hard stop must be the final action")
        return self


class ExecutionReplanEvalCase(Contract):
    kind: Literal["execution_replan"] = "execution_replan"
    case_id: Identifier
    description: Annotated[str, Field(min_length=1, max_length=500)]
    passed_items: tuple[Identifier, ...] = ()
    candidate_items: Annotated[tuple[WorkItem, ...], Field(min_length=1, max_length=20)]
    expected_decision: ReplanDecision

    @model_validator(mode="after")
    def passed_items_are_unique(self) -> Self:
        if len(self.passed_items) != len(set(self.passed_items)):
            raise ValueError("Passed work-item IDs must be unique")
        return self


ReliabilityEvalCase = Annotated[
    NoProgressEvalCase | ExecutionReplanEvalCase,
    Field(discriminator="kind"),
]


class ReliabilityEvalManifest(Contract):
    schema_version: Literal[1] = 1
    benchmark_id: Identifier
    max_identical_actions: Annotated[StrictInt, Field(ge=1, le=10)] = 2
    task: TaskSpec
    base_plan: Plan
    cases: Annotated[tuple[ReliabilityEvalCase, ...], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Reliability evaluation case IDs must be unique")
        self.base_plan.check_task(self.task)
        return self

    @property
    def sha256(self) -> str:
        return digest(self)


class NoProgressStepResult(Contract):
    index: PositiveIndex
    tool: Identifier
    expected_decision: ReliabilityDecision
    observed_decision: ReliabilityDecision
    expected_pattern: NoProgressPattern | None = None
    observed_pattern: NoProgressPattern | None = None
    correct: bool


class NoProgressEvalCaseResult(Contract):
    kind: Literal["no_progress"] = "no_progress"
    case_id: Identifier
    passed: bool
    decision_count: Count
    correct_decision_count: Count
    false_positive_count: Count
    false_negative_count: Count
    steps: tuple[NoProgressStepResult, ...]


class ExecutionReplanEvalCaseResult(Contract):
    kind: Literal["execution_replan"] = "execution_replan"
    case_id: Identifier
    passed: bool
    expected_decision: ReplanDecision
    observed_decision: ReplanDecision
    rejection_reason: str | None = None


ReliabilityEvalCaseResult = Annotated[
    NoProgressEvalCaseResult | ExecutionReplanEvalCaseResult,
    Field(discriminator="kind"),
]


class ReliabilityEvalReport(Contract):
    schema_version: Literal[1] = 1
    benchmark_id: Identifier
    manifest_digest: Sha256
    case_count: Count
    passed_case_count: Count
    case_pass_rate: Ratio
    no_progress_case_count: Count
    execution_replan_case_count: Count
    policy_decision_count: Count
    correct_policy_decision_count: Count
    policy_decision_accuracy: Ratio
    false_positive_count: Count
    false_negative_count: Count
    paid_model_called: Literal[False] = False
    network_called: Literal[False] = False
    repository_code_executed: Literal[False] = False
    cases: tuple[ReliabilityEvalCaseResult, ...]

    @model_validator(mode="after")
    def validate_totals(self) -> Self:
        no_progress = [case for case in self.cases if isinstance(case, NoProgressEvalCaseResult)]
        replans = [case for case in self.cases if isinstance(case, ExecutionReplanEvalCaseResult)]
        if self.case_count != len(self.cases):
            raise ValueError("Reliability evaluation case count does not match results")
        if self.passed_case_count != sum(case.passed for case in self.cases):
            raise ValueError("Reliability evaluation passed-case count does not match results")
        if self.no_progress_case_count != len(no_progress):
            raise ValueError("No-progress case count does not match results")
        if self.execution_replan_case_count != len(replans):
            raise ValueError("Execution-replan case count does not match results")
        expected_decisions = sum(case.decision_count for case in no_progress) + len(replans)
        correct_decisions = sum(case.correct_decision_count for case in no_progress) + sum(
            case.passed for case in replans
        )
        if self.policy_decision_count != expected_decisions:
            raise ValueError("Reliability policy-decision count does not match results")
        if self.correct_policy_decision_count != correct_decisions:
            raise ValueError("Correct reliability policy-decision count does not match results")
        if self.false_positive_count != sum(case.false_positive_count for case in no_progress):
            raise ValueError("No-progress false-positive count does not match results")
        if self.false_negative_count != sum(case.false_negative_count for case in no_progress):
            raise ValueError("No-progress false-negative count does not match results")
        return self
