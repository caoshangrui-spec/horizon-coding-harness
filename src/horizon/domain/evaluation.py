from __future__ import annotations

import fnmatch
from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, field_validator, model_validator

from horizon.domain.common import Contract, digest
from horizon.domain.task import Identifier, Text, relative_pattern

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Ratio = Annotated[float, Field(ge=0.0, le=1.0)]
Count = Annotated[StrictInt, Field(ge=0)]


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
