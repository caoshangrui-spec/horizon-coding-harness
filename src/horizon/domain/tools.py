from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from horizon.domain.common import Contract
from horizon.domain.task import Identifier, Text

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class ToolCallReservation(Contract):
    call_id: Identifier
    name: Identifier
    arguments_hash: Digest
    workspace_revision: str | None = None
    workspace_manifest_ref: Digest | None = None


class ToolCallRecord(Contract):
    call_id: Identifier
    name: Identifier
    arguments_hash: Digest
    status: Literal["success", "error", "unknown", "cancelled"]
    output_hash: Digest
    workspace_revision_before: str | None = None
    workspace_revision_after: str | None = None
    artifact_ref: str | None = None
    workspace_manifest_ref: str | None = None
    recovery_disposition: (
        Literal[
            "retry_readonly",
            "accept_replace",
            "rollback_replace",
            "accept_patch",
            "rollback_patch",
            "discard_check",
        ]
        | None
    ) = None


class ToolOutcome(Contract):
    status: Literal["success", "error", "unknown", "cancelled"]
    content: Text
    output_hash: Digest
    workspace_revision_before: str | None = None
    workspace_revision_after: str | None = None
    artifact_ref: str | None = None
    workspace_manifest_ref: str | None = None


class AcceptanceResult(Contract):
    check_id: Identifier
    passed: bool
    exit_code: int
    timed_out: bool
    output: str
    output_hash: Digest
    output_truncated: bool = False


class ReplaceRecoveryAssessment(Contract):
    state: Literal["pre_effect", "expected_effect", "diverged"]
    path: str
    pre_revision: Digest
    expected_revision: Digest
    current_revision: Digest
    current_manifest_ref: Digest


class PatchRecoveryAssessment(Contract):
    state: Literal["pre_effect", "expected_effect", "diverged"]
    paths: tuple[str, ...]
    pre_revision: Digest
    expected_revision: Digest
    current_revision: Digest
    current_manifest_ref: Digest
