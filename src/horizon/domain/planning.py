from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from horizon.domain.common import Contract, digest
from horizon.domain.task import (
    Identifier,
    Mode,
    NonNegativeInt,
    PositiveInt,
    Text,
    relative_pattern,
)


class PlanningAcceptance(Contract):
    id: Identifier
    required: bool


class PlanningContext(Contract):
    """Immutable, bounded facts supplied to the model that proposes a Plan."""

    schema_version: Literal[1] = 1
    task_spec_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    task_title: Text
    objective: Text
    task_kind: Identifier
    execution_mode: Mode
    allowed_paths: tuple[str, ...]
    denied_paths: tuple[str, ...]
    requirements: tuple[Text, ...]
    acceptance: Annotated[tuple[PlanningAcceptance, ...], Field(min_length=1)]
    workspace_revision: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    source_manifest_ref: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    repository_paths: tuple[str, ...]
    repository_path_count: NonNegativeInt
    repository_paths_truncated: bool
    max_work_items: PositiveInt
    permitted_tools: Annotated[tuple[Identifier, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def check_inventory(self) -> Self:
        for path in self.repository_paths:
            relative_pattern(path)
        if tuple(sorted(self.repository_paths)) != self.repository_paths:
            raise ValueError("Planning repository paths must be sorted")
        if len({path.casefold() for path in self.repository_paths}) != len(self.repository_paths):
            raise ValueError("Planning repository paths must be unique")
        if self.repository_path_count < len(self.repository_paths):
            raise ValueError("Planning repository path count is smaller than its inventory")
        if self.repository_paths_truncated != (
            self.repository_path_count > len(self.repository_paths)
        ):
            raise ValueError("Planning inventory truncation metadata is inconsistent")
        return self

    @property
    def sha256(self) -> str:
        return digest(self)
