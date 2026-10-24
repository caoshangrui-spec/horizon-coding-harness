from __future__ import annotations

import fnmatch
from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, field_validator, model_validator

from horizon.domain.common import Contract, digest
from horizon.domain.task import Identifier, Text, relative_pattern

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
GitCommit = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
GitHubRepositoryUrl = Annotated[
    str,
    Field(
        min_length=20,
        max_length=240,
        pattern=r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?$",
    ),
]
Ratio = Annotated[float, Field(ge=0.0, le=1.0)]
Count = Annotated[StrictInt, Field(ge=0)]
Milliseconds = Annotated[float, Field(ge=0.0)]


def _literal_path(value: str) -> str:
    relative_pattern(value)
    if any(character in value for character in "*?[]"):
        raise ValueError("Evaluation targets must be literal paths, not globs")
    return value


class RetrievalEvalCase(Contract):
    case_id: Identifier
    query: Annotated[str, Field(min_length=1, max_length=200)]
    expected_paths: Annotated[tuple[Text, ...], Field(min_length=1, max_length=20)]
    forbidden_paths: Annotated[tuple[Text, ...], Field(max_length=20)] = ()
    max_chunks: Annotated[StrictInt, Field(ge=1, le=8)] = 5

    @field_validator("expected_paths", "forbidden_paths")
    @classmethod
    def validate_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _literal_path(value)
        if len({value.casefold() for value in values}) != len(values):
            raise ValueError("Evaluation paths must be unique and case-insensitive")
        return values

    @model_validator(mode="after")
    def disjoint_paths(self) -> Self:
        expected = {value.casefold() for value in self.expected_paths}
        forbidden = {value.casefold() for value in self.forbidden_paths}
        if expected & forbidden:
            raise ValueError("Expected and forbidden paths must be disjoint")
        return self


class RetrievalEvalManifest(Contract):
    schema_version: Literal[1] = 1
    benchmark_id: Identifier
    allowed_paths: Annotated[tuple[Text, ...], Field(min_length=1)] = ("src/**",)
    denied_paths: tuple[Text, ...] = (".git/**", ".env", ".env.*", "secrets/**")
    cases: Annotated[tuple[RetrievalEvalCase, ...], Field(min_length=1, max_length=100)]

    @field_validator("allowed_paths", "denied_paths")
    @classmethod
    def validate_patterns(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            relative_pattern(value)
        if len(values) != len(set(values)):
            raise ValueError("Evaluation path patterns must be unique")
        return values

    @model_validator(mode="after")
    def validate_cases(self) -> Self:
        ids = [case.case_id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("Evaluation case IDs must be unique")
        for case in self.cases:
            for path in case.expected_paths:
                if not any(fnmatch.fnmatchcase(path, pattern) for pattern in self.allowed_paths):
                    raise ValueError(f"Expected path is outside allowed scope: {path}")
                if any(fnmatch.fnmatchcase(path, pattern) for pattern in self.denied_paths):
                    raise ValueError(f"Expected path is denied by evaluation scope: {path}")
        return self

    @property
    def sha256(self) -> str:
        return digest(self)


class RetrievalEvalCaseResult(Contract):
    case_id: Identifier
    query: str
    max_chunks: Annotated[StrictInt, Field(ge=1, le=8)]
    expected_paths: tuple[Text, ...]
    returned_paths: tuple[Text, ...]
    hit_at_k: bool
    expected_path_count: Count
    relevant_path_count: Count
    path_recall_at_k: Ratio
    first_relevant_rank: Annotated[StrictInt, Field(gt=0, le=8)] | None = None
    reciprocal_rank: Ratio
    leaked_paths: tuple[Text, ...] = ()
    retrieval_status: Literal["ok", "empty", "degraded"]
    backend: Literal["sqlite_fts5", "lexical_scan"]
    degradation_reasons: tuple[Text, ...] = ()


class RetrievalEvalReport(Contract):
    schema_version: Literal[1] = 1
    benchmark_id: Identifier
    manifest_digest: Sha256
    workspace_revision: Sha256
    source_manifest_ref: Sha256
    scope_hash: Sha256
    index_key: Sha256
    case_count: Count
    hit_count: Count
    hit_rate_at_case_k: Ratio
    expected_path_count: Count
    relevant_path_count: Count
    micro_path_recall_at_case_k: Ratio
    mean_reciprocal_rank: Ratio
    leakage_count: Count
    empty_count: Count
    degraded_count: Count
    paid_model_called: Literal[False] = False
    network_called: Literal[False] = False
    cases: tuple[RetrievalEvalCaseResult, ...]

    @model_validator(mode="after")
    def validate_totals(self) -> Self:
        if self.case_count != len(self.cases):
            raise ValueError("Evaluation case count does not match its results")
        if self.hit_count != sum(case.hit_at_k for case in self.cases):
            raise ValueError("Evaluation hit count does not match its results")
        if self.expected_path_count != sum(case.expected_path_count for case in self.cases):
            raise ValueError("Evaluation expected-path count does not match its results")
        if self.relevant_path_count != sum(case.relevant_path_count for case in self.cases):
            raise ValueError("Evaluation relevant-path count does not match its results")
        if self.leakage_count != sum(len(case.leaked_paths) for case in self.cases):
            raise ValueError("Evaluation leakage count does not match its results")
        if self.empty_count != sum(case.retrieval_status == "empty" for case in self.cases):
            raise ValueError("Evaluation empty count does not match its results")
        if self.degraded_count != sum(case.retrieval_status == "degraded" for case in self.cases):
            raise ValueError("Evaluation degraded count does not match its results")
        return self


class RetrievalEvalSuiteCaseSpec(Contract):
    case_id: Identifier
    manifest_path: Text
    source_subdir: Text
    buggy_commit: GitCommit

    @field_validator("manifest_path", "source_subdir")
    @classmethod
    def validate_literal_paths(cls, value: str) -> str:
        return _literal_path(value)


class RetrievalEvalSuiteManifest(Contract):
    schema_version: Literal[1] = 1
    suite_id: Identifier
    selection_commit: GitCommit
    partition: Literal["development", "holdout"]
    cutoffs: Annotated[tuple[StrictInt, ...], Field(min_length=1, max_length=8)] = (1, 3, 5, 8)
    cases: Annotated[tuple[RetrievalEvalSuiteCaseSpec, ...], Field(min_length=1, max_length=100)]

    @field_validator("cutoffs")
    @classmethod
    def validate_cutoffs(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if any(value < 1 or value > 8 for value in values):
            raise ValueError("Retrieval suite cutoffs must be between 1 and 8")
        if tuple(sorted(set(values))) != values:
            raise ValueError("Retrieval suite cutoffs must be sorted and unique")
        return values

    @model_validator(mode="after")
    def validate_cases(self) -> Self:
        case_ids = [case.case_id for case in self.cases]
        manifest_paths = [case.manifest_path.casefold() for case in self.cases]
        source_subdirs = [case.source_subdir.casefold() for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Retrieval suite case IDs must be unique")
        if len(manifest_paths) != len(set(manifest_paths)):
            raise ValueError("Retrieval suite manifest paths must be unique")
        if len(source_subdirs) != len(set(source_subdirs)):
            raise ValueError("Retrieval suite source directories must be unique")
        return self

    @property
    def sha256(self) -> str:
        return digest(self)


class RetrievalSourceCaseSpec(Contract):
    case_id: Identifier
    repository_url: GitHubRepositoryUrl
    buggy_commit: GitCommit
    archive_paths: Annotated[tuple[Text, ...], Field(min_length=1, max_length=10)]
    expected_file_count: Annotated[StrictInt, Field(gt=0)]
    expected_byte_count: Annotated[StrictInt, Field(gt=0)]
    expected_tree_digest: Sha256

    @field_validator("archive_paths")
    @classmethod
    def validate_archive_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            _literal_path(value)
        folded = tuple(value.casefold() for value in values)
        if len(folded) != len(set(folded)):
            raise ValueError("Source archive paths must be unique and case-insensitive")
        for index, path in enumerate(folded):
            for other in folded[index + 1 :]:
                if path.startswith(f"{other}/") or other.startswith(f"{path}/"):
                    raise ValueError("Source archive paths must not overlap")
        return values


class RetrievalSourceManifest(Contract):
    schema_version: Literal[1] = 1
    suite_id: Identifier
    evaluation_suite_path: Text
    partition: Literal["development", "holdout"]
    cases: Annotated[tuple[RetrievalSourceCaseSpec, ...], Field(min_length=1, max_length=100)]

    @field_validator("evaluation_suite_path")
    @classmethod
    def validate_suite_path(cls, value: str) -> str:
        return _literal_path(value)

    @model_validator(mode="after")
    def validate_cases(self) -> Self:
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Source materialization case IDs must be unique")
        return self

    @property
    def sha256(self) -> str:
        return digest(self)


class RetrievalSourceCaseReceipt(Contract):
    case_id: Identifier
    repository_url: GitHubRepositoryUrl
    buggy_commit: GitCommit
    archive_paths: tuple[Text, ...]
    file_count: Annotated[StrictInt, Field(gt=0)]
    byte_count: Annotated[StrictInt, Field(gt=0)]
    tree_digest: Sha256


class RetrievalSourceReceipt(Contract):
    schema_version: Literal[1] = 1
    suite_id: Identifier
    source_manifest_digest: Sha256
    evaluation_suite_digest: Sha256
    partition: Literal["development", "holdout"]
    case_count: Count
    network_called: bool
    repository_code_executed: Literal[False] = False
    cases: tuple[RetrievalSourceCaseReceipt, ...]

    @model_validator(mode="after")
    def validate_totals(self) -> Self:
        if self.case_count != len(self.cases):
            raise ValueError("Source receipt case count does not match its results")
        return self


class RetrievalEvalAtK(Contract):
    cutoff: Annotated[StrictInt, Field(ge=1, le=8)]
    hit_count: Count
    hit_rate: Ratio
    expected_path_count: Count
    relevant_path_count: Count
    micro_path_recall: Ratio
    mean_average_precision: Ratio
    mean_ndcg: Ratio


class RetrievalEvalSuiteCaseResult(Contract):
    case_id: Identifier
    buggy_commit: GitCommit
    report_ref: Sha256
    cold_duration_ms: Milliseconds
    warm_duration_ms: Milliseconds
    report: RetrievalEvalReport

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if self.report.case_count != 1 or len(self.report.cases) != 1:
            raise ValueError("Retrieval suite case reports must contain exactly one case")
        if self.report.cases[0].case_id != self.case_id:
            raise ValueError("Retrieval suite case ID does not match the nested report")
        return self


class RetrievalEvalSuiteReport(Contract):
    schema_version: Literal[1] = 1
    suite_id: Identifier
    suite_manifest_digest: Sha256
    selection_commit: GitCommit
    partition: Literal["development", "holdout"]
    case_count: Count
    metrics_at_k: Annotated[tuple[RetrievalEvalAtK, ...], Field(min_length=1, max_length=8)]
    mean_reciprocal_rank: Ratio
    leakage_count: Count
    empty_count: Count
    degraded_count: Count
    cold_duration_p50_ms: Milliseconds
    cold_duration_p95_ms: Milliseconds
    warm_duration_p50_ms: Milliseconds
    warm_duration_p95_ms: Milliseconds
    paid_model_called: Literal[False] = False
    network_called: Literal[False] = False
    repository_code_executed: Literal[False] = False
    cases: tuple[RetrievalEvalSuiteCaseResult, ...]

    @model_validator(mode="after")
    def validate_totals(self) -> Self:
        if self.case_count != len(self.cases):
            raise ValueError("Retrieval suite case count does not match its results")
        if self.leakage_count != sum(case.report.leakage_count for case in self.cases):
            raise ValueError("Retrieval suite leakage count does not match its results")
        if self.empty_count != sum(case.report.empty_count for case in self.cases):
            raise ValueError("Retrieval suite empty count does not match its results")
        if self.degraded_count != sum(case.report.degraded_count for case in self.cases):
            raise ValueError("Retrieval suite degraded count does not match its results")
        cutoffs = tuple(metric.cutoff for metric in self.metrics_at_k)
        if cutoffs != tuple(sorted(set(cutoffs))):
            raise ValueError("Retrieval suite report cutoffs must be sorted and unique")
        return self
