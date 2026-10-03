from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, model_validator

from horizon.domain.common import Contract
from horizon.domain.task import Text, relative_pattern

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
PositiveInt = Annotated[StrictInt, Field(gt=0)]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]


class EvidenceChunk(Contract):
    rank: PositiveInt
    path: Text
    start_line: PositiveInt
    end_line: PositiveInt
    content_hash: Digest
    snippet: str
    truncated: bool = False

    @model_validator(mode="after")
    def check_range(self) -> Self:
        relative_pattern(self.path)
        if self.end_line < self.start_line:
            raise ValueError("Evidence line range is reversed")
        if not self.snippet:
            raise ValueError("Evidence snippets cannot be empty")
        return self


class EvidencePack(Contract):
    schema_version: Literal[1] = 1
    query: Annotated[str, Field(min_length=1, max_length=200)]
    normalized_terms: Annotated[tuple[Text, ...], Field(max_length=20)]
    workspace_revision: Digest
    source_manifest_ref: Digest
    scope_hash: Digest
    index_key: Digest
    backend: Literal["sqlite_fts5", "lexical_scan"]
    status: Literal["ok", "empty", "degraded"]
    degradation_reasons: tuple[Text, ...] = ()
    indexed_file_count: NonNegativeInt
    skipped_file_count: NonNegativeInt
    chunks: Annotated[tuple[EvidenceChunk, ...], Field(max_length=8)] = ()

    @model_validator(mode="after")
    def check_status(self) -> Self:
        if self.status == "ok" and (not self.chunks or self.degradation_reasons):
            raise ValueError("An ok evidence pack requires results and no degradation")
        if self.status == "empty" and (self.chunks or self.degradation_reasons):
            raise ValueError("An empty evidence pack cannot contain results or degradation")
        if self.status == "degraded" and not self.degradation_reasons:
            raise ValueError("A degraded evidence pack requires an explicit reason")
        if tuple(chunk.rank for chunk in self.chunks) != tuple(range(1, len(self.chunks) + 1)):
            raise ValueError("Evidence ranks must be consecutive and one-based")
        return self
