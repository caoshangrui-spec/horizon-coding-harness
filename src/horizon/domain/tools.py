from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from horizon.domain.common import Contract
from horizon.domain.task import Identifier, Text

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
WRITE_TOOLS = frozenset({"replace_text", "apply_patch", "create_file"})


def write_target_paths(name: str, arguments: dict[str, Any]) -> tuple[str, ...]:
    """Best-effort target extraction for a controller-owned write precondition.

    The tool adapter remains responsible for authoritative argument and path validation.  An
    empty tuple therefore means that the candidate call was malformed, not that it may write an
    unbounded target.
    """

    if name in {"replace_text", "create_file"}:
        path = arguments.get("path")
        return (path,) if isinstance(path, str) and path else ()
    if name == "apply_patch":
        edits = arguments.get("edits")
        if not isinstance(edits, (list, tuple)):
            return ()
        paths = tuple(
            edit.get("path")
            for edit in edits
            if isinstance(edit, dict) and isinstance(edit.get("path"), str) and edit.get("path")
        )
        return paths if len(paths) == len(edits) else ()
    return ()


class WorkspaceWritePrecondition(Contract):
    """Trace-bound compare-and-swap guard for one model-proposed workspace write.

    ``expected_workspace_revision`` is the tree hash placed in the source model request.
    ``observed_workspace_manifest_ref`` addresses the immediately pre-dispatch manifest whose
    entries carry each target file's SHA-256.  Together they bind the model view, target paths,
    and live preimage without asking the model to echo controller-owned hashes.
    """

    schema_version: Literal[1] = 1
    source_model_call_id: Identifier
    expected_workspace_revision: Digest
    observed_workspace_revision: Digest
    observed_workspace_manifest_ref: Digest
    target_paths: Annotated[tuple[str, ...], Field(max_length=8)] = ()
    stale: bool

    @model_validator(mode="after")
    def check_cas_state(self) -> Self:
        if self.stale != (self.expected_workspace_revision != self.observed_workspace_revision):
            raise ValueError("Write precondition stale flag does not match its revisions")
        if len(self.target_paths) != len(set(self.target_paths)):
            raise ValueError("Write precondition target paths must be unique")
        return self


class ToolCallReservation(Contract):
    call_id: Identifier
    name: Identifier
    arguments_hash: Digest
    workspace_revision: str | None = None
    workspace_manifest_ref: Digest | None = None
    write_precondition: WorkspaceWritePrecondition | None = None

    @model_validator(mode="after")
    def check_write_precondition(self) -> Self:
        precondition = self.write_precondition
        if precondition is None:
            return self
        if self.name not in WRITE_TOOLS:
            raise ValueError("Only workspace write tools may carry a write precondition")
        if (
            self.workspace_revision != precondition.observed_workspace_revision
            or self.workspace_manifest_ref != precondition.observed_workspace_manifest_ref
        ):
            raise ValueError("Write precondition must match the reservation checkpoint")
        return self

    def as_dict(self) -> dict[str, Any]:
        """Preserve historical event/projection hashes when the additive guard is absent."""

        result = self.model_dump(mode="json")
        if self.write_precondition is None:
            result.pop("write_precondition")
        return result


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
            "accept_create",
            "rollback_create",
            "discard_check",
            "accept_check_result",
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


class CreateRecoveryAssessment(Contract):
    state: Literal["pre_effect", "expected_effect", "diverged"]
    path: str
    content_sha256: Digest
    pre_revision: Digest
    expected_revision: Digest
    current_revision: Digest
    current_manifest_ref: Digest
