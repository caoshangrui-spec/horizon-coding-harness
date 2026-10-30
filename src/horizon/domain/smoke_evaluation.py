from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, field_validator, model_validator

from horizon.domain.common import Contract, digest
from horizon.domain.task import Identifier, PositiveInt, Text, relative_pattern
from horizon.domain.task_registry import Sha256

SmokeTool = Literal["read_file", "retrieve_code", "replace_text", "run_check"]
SuccessCriterion = Literal[
    "initial_failure_observed",
    "workspace_changed",
    "required_checks_passed",
    "source_unchanged",
    "trace_replay_verified",
    "no_open_effects",
]
SmokeMetric = Literal[
    "task_success",
    "correct_location_reached",
    "valid_edit_produced",
    "protected_validation_passed",
    "actual_cost_cny",
    "model_calls",
    "stop_category",
]
FailureCategory = Literal[
    "model_planning",
    "retrieval",
    "tool_protocol",
    "context_capacity",
    "budget_gate",
    "validation_failure",
    "harness_recovery",
    "unknown_effect",
]

REQUIRED_SUCCESS_CRITERIA = frozenset(
    {
        "initial_failure_observed",
        "workspace_changed",
        "required_checks_passed",
        "source_unchanged",
        "trace_replay_verified",
        "no_open_effects",
    }
)
REQUIRED_SMOKE_METRICS = frozenset(
    {
        "task_success",
        "correct_location_reached",
        "valid_edit_produced",
        "protected_validation_passed",
        "actual_cost_cny",
        "model_calls",
        "stop_category",
    }
)
REQUIRED_FAILURE_TAXONOMY = frozenset(
    {
        "model_planning",
        "retrieval",
        "tool_protocol",
        "context_capacity",
        "budget_gate",
        "validation_failure",
        "harness_recovery",
        "unknown_effect",
    }
)


def _literal_relative_path(value: str) -> str:
    relative_pattern(value)
    if any(character in value for character in "*?[]"):
        raise ValueError("Smoke-suite manifest references must be literal paths")
    return value


class SmokeSelectionPolicy(Contract):
    required_partition: Literal["development"] = "development"
    required_source_readiness: Literal["frozen_materialization_recipe"] = (
        "frozen_materialization_recipe"
    )
    required_execution_readiness: Literal["full_checkout_manifest"] = "full_checkout_manifest"
    require_public_historical_initial_failure: Literal[True] = True
    required_unique_projects: Literal[5] = 5
    author_selected_after_scripted_repairs_known: Literal[True] = True
    excluded_task_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]

    @field_validator("excluded_task_ids")
    @classmethod
    def validate_exclusions(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Smoke-suite exclusions must be unique")
        return values


class SmokeExecutionPolicy(Contract):
    paid_execution_authorized: Literal[False] = False
    provider_policy_bound: Literal[False] = False
    model_id_bound: Literal[False] = False
    cost_cap_bound: Literal[False] = False
    fallback_enabled: Literal[False] = False
    implicit_provider_retry_enabled: Literal[False] = False
    max_steps: Literal[24] = 24
    max_model_calls: Literal[14] = 14
    max_tool_calls: Literal[24] = 24
    max_wall_time_seconds: Literal[900] = 900
    max_repair_cycles: Literal[2] = 2
    max_execution_replans: Literal[1] = 1


class SmokeTaskContract(Contract):
    task_id: Identifier
    difficulty_band: Literal["easy", "medium"]
    allowed_path_patterns: Annotated[tuple[Text, ...], Field(min_length=1, max_length=3)]
    allowed_tools: Annotated[tuple[SmokeTool, ...], Field(min_length=1)]
    selection_reason: Annotated[str, Field(min_length=1, max_length=1000)]

    @field_validator("allowed_path_patterns")
    @classmethod
    def validate_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Smoke task path patterns must be unique")
        for value in values:
            relative_pattern(value)
        return values

    @field_validator("allowed_tools")
    @classmethod
    def validate_tools(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("Smoke task tools must be unique")
        required = {"read_file", "retrieve_code", "replace_text", "run_check"}
        if set(values) != required:
            raise ValueError("Smoke tasks must expose the frozen minimal repair tool set")
        return values


class RealModelSmokeSuite(Contract):
    schema_version: Literal[1] = 1
    suite_id: Identifier
    frozen_at: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
    registry_path: Text
    registry_digest: Sha256
    selection_scope: Literal["known_easy_development_smoke"] = "known_easy_development_smoke"
    patch_content_included: Literal[False] = False
    gold_paths_included: Literal[False] = False
    evaluator_manifests_model_visible: Literal[False] = False
    selection_policy: SmokeSelectionPolicy
    execution_policy: SmokeExecutionPolicy
    success_criteria: Annotated[tuple[SuccessCriterion, ...], Field(min_length=1)]
    required_metrics: Annotated[tuple[SmokeMetric, ...], Field(min_length=1)]
    failure_taxonomy: Annotated[tuple[FailureCategory, ...], Field(min_length=1)]
    tasks: Annotated[tuple[SmokeTaskContract, ...], Field(min_length=5, max_length=5)]

    @field_validator("registry_path")
    @classmethod
    def validate_registry_path(cls, value: str) -> str:
        return _literal_relative_path(value)

    @model_validator(mode="after")
    def validate_suite(self) -> Self:
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("Smoke-suite task IDs must be unique")
        if set(task_ids) & set(self.selection_policy.excluded_task_ids):
            raise ValueError("Smoke-suite tasks overlap the explicit exclusion list")
        frozen_sets = (
            (self.success_criteria, REQUIRED_SUCCESS_CRITERIA, "success criteria"),
            (self.required_metrics, REQUIRED_SMOKE_METRICS, "required metrics"),
            (self.failure_taxonomy, REQUIRED_FAILURE_TAXONOMY, "failure taxonomy"),
        )
        for values, required, label in frozen_sets:
            if len(values) != len(set(values)) or set(values) != required:
                raise ValueError(f"Smoke-suite {label} must preserve the complete frozen set")
        return self

    @property
    def sha256(self) -> str:
        return digest(self)


class SmokePreflightTask(Contract):
    task_id: Identifier
    project: Identifier
    partition: Literal["development"] = "development"
    difficulty_band: Literal["easy", "medium"]
    source_readiness: Literal["frozen_materialization_recipe"] = "frozen_materialization_recipe"
    execution_readiness: Literal["full_checkout_manifest"] = "full_checkout_manifest"
    initial_failure_evidence: Literal["public_historical_run_declared"] = (
        "public_historical_run_declared"
    )
    allowed_path_patterns: tuple[Text, ...]
    allowed_tools: tuple[SmokeTool, ...]


class RealModelSmokePreflightReport(Contract):
    schema_version: Literal[1] = 1
    suite_id: Identifier
    manifest_digest: Sha256
    registry_id: Identifier
    registry_digest: Sha256
    task_count: Literal[5] = 5
    unique_project_count: Literal[5] = 5
    frozen_source_recipe_count: Literal[5] = 5
    full_checkout_manifest_count: Literal[5] = 5
    public_historical_initial_failure_count: Literal[5] = 5
    success_criteria: tuple[SuccessCriterion, ...]
    required_metrics: tuple[SmokeMetric, ...]
    failure_taxonomy: tuple[FailureCategory, ...]
    max_steps: PositiveInt
    max_model_calls: PositiveInt
    max_tool_calls: PositiveInt
    max_wall_time_seconds: PositiveInt
    max_repair_cycles: Annotated[StrictInt, Field(ge=0)]
    max_execution_replans: Annotated[StrictInt, Field(ge=0)]
    provider_policy_bound: Literal[False] = False
    model_id_bound: Literal[False] = False
    cost_cap_bound: Literal[False] = False
    paid_launch_ready: Literal[False] = False
    launch_blockers: Annotated[tuple[Text, ...], Field(min_length=3)]
    structural_preflight_passed: Literal[True] = True
    paid_model_called: Literal[False] = False
    network_called: Literal[False] = False
    repository_code_executed: Literal[False] = False
    external_cost_cny: Literal[0] = 0
    claim_scope: Text
    excluded_claims: Annotated[tuple[Text, ...], Field(min_length=1)]
    tasks: Annotated[tuple[SmokePreflightTask, ...], Field(min_length=5, max_length=5)]

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        projects = {task.project.casefold() for task in self.tasks}
        if len(projects) != self.unique_project_count:
            raise ValueError("Smoke preflight unique-project count is inconsistent")
        if len(self.tasks) != self.task_count:
            raise ValueError("Smoke preflight task count is inconsistent")
        if self.full_checkout_manifest_count != sum(
            task.execution_readiness == "full_checkout_manifest" for task in self.tasks
        ):
            raise ValueError("Smoke preflight full-checkout count is inconsistent")
        if self.frozen_source_recipe_count != sum(
            task.source_readiness == "frozen_materialization_recipe" for task in self.tasks
        ):
            raise ValueError("Smoke preflight source-recipe count is inconsistent")
        if self.public_historical_initial_failure_count != sum(
            task.initial_failure_evidence == "public_historical_run_declared" for task in self.tasks
        ):
            raise ValueError("Smoke preflight historical-failure count is inconsistent")
        return self
