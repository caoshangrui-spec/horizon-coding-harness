from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from horizon.domain.common import Contract
from horizon.domain.states import TERMINAL, RunStatus
from horizon.domain.task import Identifier, PositiveInt, Text, relative_pattern

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

TERMINAL_EVIDENCE_BOUNDARIES = (
    "terminal_status_does_not_imply_external_effects_are_settled",
    "unknown_and_open_effects_remain_visible",
    "self_consistent_hashes_are_not_origin_authentication",
)


class TerminalRunEvidence(Contract):
    schema_version: Literal[1] = 1
    run_id: Identifier
    task_id: Identifier
    task_spec_hash: Sha256
    status: RunStatus
    task_succeeded: bool
    failure_reason: Text | None = None
    event_count: PositiveInt
    event_hash: Sha256
    projection_hash: Sha256
    unknown_reservation_ids: tuple[str, ...] = ()
    unknown_model_call_ids: tuple[str, ...] = ()
    unknown_tool_call_ids: tuple[str, ...] = ()
    open_reservation_ids: tuple[str, ...] = ()
    open_model_reservation_ids: tuple[str, ...] = ()
    open_tool_reservation_ids: tuple[str, ...] = ()
    promotion_pending: bool = False
    unknown_effects_present: bool
    open_effects_present: bool
    verification_mode: Literal["offline_trace_replay"] = "offline_trace_replay"
    claim_scope: Literal["terminal_control_state_and_trace_integrity"] = (
        "terminal_control_state_and_trace_integrity"
    )
    boundaries: tuple[Text, ...] = TERMINAL_EVIDENCE_BOUNDARIES

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status not in TERMINAL:
            raise ValueError("Terminal evidence requires a terminal Run status")
        if self.task_succeeded != (self.status == RunStatus.SUCCEEDED):
            raise ValueError("Task success must match the SUCCEEDED Run status")
        if (self.status == RunStatus.FAILED) != (self.failure_reason is not None):
            raise ValueError("Only a failed Run carries a failure reason")
        unknown = bool(
            self.unknown_reservation_ids
            or self.unknown_model_call_ids
            or self.unknown_tool_call_ids
        )
        if self.unknown_effects_present != unknown:
            raise ValueError("Unknown-effect summary does not match its identifiers")
        open_effects = bool(
            self.open_reservation_ids
            or self.open_model_reservation_ids
            or self.open_tool_reservation_ids
            or self.promotion_pending
        )
        if self.open_effects_present != open_effects:
            raise ValueError("Open-effect summary does not match its identifiers")
        if tuple(self.boundaries) != TERMINAL_EVIDENCE_BOUNDARIES:
            raise ValueError("Terminal evidence boundaries must remain explicit")
        return self


class TerminalEvidenceFile(Contract):
    role: Literal["trace", "final_state", "summary"]
    path: Text
    sha256: Sha256
    bytes: PositiveInt

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        relative_pattern(self.path)
        if any(character in self.path for character in "*?[]"):
            raise ValueError("Terminal evidence paths must be literal")
        return self


class TerminalEvidencePack(Contract):
    schema_version: Literal[1] = 1
    pack_type: Literal["horizon.terminal-run"] = "horizon.terminal-run"
    evidence: TerminalRunEvidence
    files: Annotated[tuple[TerminalEvidenceFile, ...], Field(min_length=3, max_length=3)]

    @model_validator(mode="after")
    def validate_files(self) -> Self:
        roles = [item.role for item in self.files]
        paths = [item.path.casefold() for item in self.files]
        if set(roles) != {"trace", "final_state", "summary"}:
            raise ValueError("Terminal EvidencePack requires all three evidence roles")
        if len(paths) != len(set(paths)):
            raise ValueError("Terminal EvidencePack paths must be unique")
        return self
