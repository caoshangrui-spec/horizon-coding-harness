from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from horizon.domain.common import Contract, digest
from horizon.domain.task import Identifier, relative_pattern

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
GitCommit = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]


class WorkspaceOrigin(Contract):
    source_path_hash: Sha256
    source_revision: Sha256
    source_manifest_ref: Sha256
    git_head: GitCommit | None = None


class WorkspaceChange(Contract):
    path: str
    kind: Literal["modified"] = "modified"
    before_sha256: Sha256
    after_sha256: Sha256

    @model_validator(mode="after")
    def validate_change(self) -> Self:
        relative_pattern(self.path)
        if self.before_sha256 == self.after_sha256:
            raise ValueError("Promotion changes must alter file content")
        return self


class PromotionPlan(Contract):
    source_path_hash: Sha256
    source_revision_before: Sha256
    source_manifest_ref_before: Sha256
    candidate_revision: Sha256
    candidate_manifest_ref: Sha256
    diff_artifact_ref: Sha256
    git_head_before: GitCommit | None = None
    changes: Annotated[tuple[WorkspaceChange, ...], Field(min_length=1, max_length=8)]

    @property
    def sha256(self) -> str:
        return digest(self)


class PromotionIntent(Contract):
    promotion_id: Identifier
    plan: PromotionPlan


class PromotionReceipt(Contract):
    promotion_id: Identifier
    plan_hash: Sha256
    source_revision_after: Sha256
    source_manifest_ref_after: Sha256
    git_head_after: GitCommit | None = None
    recovered_after_crash: bool = False
