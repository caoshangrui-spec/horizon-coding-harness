from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, field_validator, model_validator

from horizon.domain.common import Contract, digest
from horizon.domain.task import Identifier, Text, relative_pattern

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
GitCommit = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
GitHubRepositoryUrl = Annotated[
    str,
    Field(pattern=r"^https://github\.com/[^/]+/[^/]+/?$"),
]
GitHubRunUrl = Annotated[
    str,
    Field(pattern=r"^https://github\.com/[^/]+/[^/]+/actions/runs/[0-9]+$"),
]
Count = Annotated[StrictInt, Field(ge=0)]


def _literal_path(value: str) -> str:
    relative_pattern(value)
    if any(character in value for character in "*?[]"):
        raise ValueError("Task-registry evidence paths must be literal")
    return value


class RegistrySelection(Contract):
    manifest_path: Text
    manifest_digest: Sha256
    included_rule: Annotated[str, Field(min_length=1, max_length=1000)]
    exclusion_rule: Annotated[str, Field(min_length=1, max_length=1000)]
    selected_task_count: Annotated[StrictInt, Field(ge=20, le=30)]

    @field_validator("manifest_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _literal_path(value)


class RegistryLicense(Contract):
    license_id: Identifier
    project: Identifier
    repository_url: GitHubRepositoryUrl
    spdx: Identifier
    license_path: Text

    @field_validator("license_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _literal_path(value)

    def url_for(self, commit: str) -> str:
        return f"{self.repository_url.rstrip('/')}/blob/{commit}/{self.license_path}"


class RegistryTaskEvidence(Contract):
    source_recipe_manifest: Text | None = None
    execution_suite_manifest: Text | None = None
    execution_case_id: Identifier | None = None
    historical_run_url: GitHubRunUrl | None = None

    @field_validator("source_recipe_manifest", "execution_suite_manifest")
    @classmethod
    def validate_optional_path(cls, value: str | None) -> str | None:
        return None if value is None else _literal_path(value)

    @model_validator(mode="after")
    def validate_execution_evidence(self) -> Self:
        pair = (self.execution_suite_manifest, self.execution_case_id)
        if (pair[0] is None) != (pair[1] is None):
            raise ValueError("Execution suite and case evidence must be declared together")
        if self.historical_run_url is not None and self.execution_suite_manifest is None:
            raise ValueError("Historical Run evidence requires an execution manifest")
        return self


class IndependentTaskCase(Contract):
    task_id: Identifier
    partition: Literal["development", "holdout"]
    project: Identifier
    bug_id: Identifier
    repository_url: GitHubRepositoryUrl
    buggy_commit: GitCommit
    fixed_commit: GitCommit
    failing_tests: Annotated[tuple[Text, ...], Field(min_length=1, max_length=10)]
    license_id: Identifier
    selection_reason: Annotated[str, Field(min_length=1, max_length=1000)]
    evidence: RegistryTaskEvidence = RegistryTaskEvidence()

    @model_validator(mode="after")
    def validate_case(self) -> Self:
        if self.buggy_commit == self.fixed_commit:
            raise ValueError("Registry buggy and fixed commits must differ")
        if len(self.failing_tests) != len(set(self.failing_tests)):
            raise ValueError("Registry failing tests must be unique")
        return self


class IndependentTaskRegistry(Contract):
    schema_version: Literal[1] = 1
    registry_id: Identifier
    frozen_at: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
    source_dataset: Identifier
    source_dataset_url: GitHubRepositoryUrl
    selection: RegistrySelection
    patch_content_included: Literal[False] = False
    gold_paths_included: Literal[False] = False
    licenses: Annotated[tuple[RegistryLicense, ...], Field(min_length=1, max_length=30)]
    tasks: Annotated[tuple[IndependentTaskCase, ...], Field(min_length=20, max_length=30)]

    @model_validator(mode="after")
    def validate_registry(self) -> Self:
        if len(self.tasks) != self.selection.selected_task_count:
            raise ValueError("Registry task count must match the frozen selection count")
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("Registry task IDs must be unique")
        source_ids = [
            (task.repository_url.rstrip("/").casefold(), task.bug_id.casefold())
            for task in self.tasks
        ]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("Registry source bug identities must be unique")
        if {task.partition for task in self.tasks} != {"development", "holdout"}:
            raise ValueError("Registry must preserve both development and holdout partitions")

        license_ids = [item.license_id for item in self.licenses]
        if len(license_ids) != len(set(license_ids)):
            raise ValueError("Registry license IDs must be unique")
        licenses = {item.license_id: item for item in self.licenses}
        for task in self.tasks:
            license_record = licenses.get(task.license_id)
            if license_record is None:
                raise ValueError(f"Unknown registry license ID: {task.license_id}")
            if license_record.project != task.project:
                raise ValueError("Registry task project does not match its license record")
            if license_record.repository_url.rstrip("/") != task.repository_url.rstrip("/"):
                raise ValueError("Registry task repository does not match its license record")
        return self

    @property
    def sha256(self) -> str:
        return digest(self)


class IndependentTaskPreflightCase(Contract):
    task_id: Identifier
    partition: Literal["development", "holdout"]
    project: Identifier
    bug_id: Identifier
    repository_url: GitHubRepositoryUrl
    buggy_commit: GitCommit
    fixed_commit: GitCommit
    license_spdx: Identifier
    license_url: Annotated[str, Field(pattern=r"^https://github\.com/.+/blob/[a-f0-9]{40}/.+$")]
    source_readiness: Literal["frozen_materialization_recipe", "metadata_only"]
    initial_failure_evidence: Literal[
        "public_historical_run_declared",
        "benchmark_declaration_only",
    ]
    execution_readiness: Literal[
        "full_checkout_manifest",
        "dependency_reduced_manifest",
        "not_prepared",
    ]
    evidence_refs: tuple[Text, ...]


class IndependentTaskRegistryPreflightReport(Contract):
    schema_version: Literal[1] = 1
    registry_id: Identifier
    manifest_digest: Sha256
    selection_manifest_digest: Sha256
    task_count: Count
    unique_source_task_count: Count
    development_task_count: Count
    holdout_task_count: Count
    frozen_source_recipe_count: Count
    metadata_only_source_count: Count
    public_historical_initial_failure_count: Count
    benchmark_declared_initial_failure_count: Count
    execution_manifest_count: Count
    full_checkout_manifest_count: Count
    dependency_reduced_manifest_count: Count
    not_prepared_execution_count: Count
    selection_binding_passed: Literal[True] = True
    identity_uniqueness_passed: Literal[True] = True
    license_metadata_binding_passed: Literal[True] = True
    evidence_binding_passed: Literal[True] = True
    solution_content_absent: Literal[True] = True
    historical_run_status_reverified_online: Literal[False] = False
    paid_model_called: Literal[False] = False
    network_called: Literal[False] = False
    repository_code_executed: Literal[False] = False
    preflight_passed: Literal[True] = True
    claim_scope: Text
    excluded_claims: Annotated[tuple[Text, ...], Field(min_length=1)]
    tasks: tuple[IndependentTaskPreflightCase, ...]

    @model_validator(mode="after")
    def validate_totals(self) -> Self:
        if self.task_count != len(self.tasks):
            raise ValueError("Registry report task count does not match task results")
        unique_sources = {
            (task.repository_url.rstrip("/").casefold(), task.bug_id.casefold())
            for task in self.tasks
        }
        if self.unique_source_task_count != len(unique_sources):
            raise ValueError("Registry report unique-source count is inconsistent")
        expected = {
            "development_task_count": sum(
                task.partition == "development" for task in self.tasks
            ),
            "holdout_task_count": sum(task.partition == "holdout" for task in self.tasks),
            "frozen_source_recipe_count": sum(
                task.source_readiness == "frozen_materialization_recipe" for task in self.tasks
            ),
            "metadata_only_source_count": sum(
                task.source_readiness == "metadata_only" for task in self.tasks
            ),
            "public_historical_initial_failure_count": sum(
                task.initial_failure_evidence == "public_historical_run_declared"
                for task in self.tasks
            ),
            "benchmark_declared_initial_failure_count": sum(
                task.initial_failure_evidence == "benchmark_declaration_only"
                for task in self.tasks
            ),
            "execution_manifest_count": sum(
                task.execution_readiness != "not_prepared" for task in self.tasks
            ),
            "full_checkout_manifest_count": sum(
                task.execution_readiness == "full_checkout_manifest" for task in self.tasks
            ),
            "dependency_reduced_manifest_count": sum(
                task.execution_readiness == "dependency_reduced_manifest" for task in self.tasks
            ),
            "not_prepared_execution_count": sum(
                task.execution_readiness == "not_prepared" for task in self.tasks
            ),
        }
        for field, value in expected.items():
            if getattr(self, field) != value:
                raise ValueError(f"Registry report {field} is inconsistent")
        return self
