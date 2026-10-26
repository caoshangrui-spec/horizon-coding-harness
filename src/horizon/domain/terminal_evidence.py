from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from horizon.domain.common import Contract
from horizon.domain.run_reliability import RunReliabilitySummary
from horizon.domain.states import TERMINAL, RunStatus
from horizon.domain.task import Identifier, PositiveInt, Text, relative_pattern

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

TERMINAL_EVIDENCE_BOUNDARIES_V1 = (
    "terminal_status_does_not_imply_external_effects_are_settled",
    "unknown_and_open_effects_remain_visible",
    "self_consistent_hashes_are_not_origin_authentication",
)
TERMINAL_EVIDENCE_BOUNDARIES = (
    *TERMINAL_EVIDENCE_BOUNDARIES_V1,
    "fallback_and_degradation_history_contains_only_trace_bound_records",
)


class TerminalRunEvidence(Contract):
    schema_version: Literal[1, 2] = 2
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
    active_process_launch_id: Identifier | None = None
    active_process_id: PositiveInt | None = None
    unknown_effects_present: bool
    open_effects_present: bool
    reliability_summary: RunReliabilitySummary | None = None
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
            or self.active_process_launch_id is not None
        )
        if (self.active_process_launch_id is None) != (self.active_process_id is None):
            raise ValueError("Active Process Worker evidence requires launch ID and PID together")
        if self.open_effects_present != open_effects:
            raise ValueError("Open-effect summary does not match its identifiers")
        expected_boundaries = (
            TERMINAL_EVIDENCE_BOUNDARIES_V1
            if self.schema_version == 1
            else TERMINAL_EVIDENCE_BOUNDARIES
        )
        if tuple(self.boundaries) != expected_boundaries:
            raise ValueError("Terminal evidence boundaries must remain explicit")
        if self.schema_version == 1:
            if self.reliability_summary is not None:
                raise ValueError("Terminal evidence schema v1 cannot contain a reliability summary")
            return self
        summary = self.reliability_summary
        if summary is None:
            raise ValueError("Terminal evidence schema v2 requires a reliability summary")
        expected_status = {
            RunStatus.SUCCEEDED: "passed",
            RunStatus.FAILED: "failed",
            RunStatus.CANCELLED: "blocked",
        }[self.status]
        if summary.status != expected_status or summary.task_spec_hash != self.task_spec_hash:
            raise ValueError("Reliability summary does not match the terminal Run")
        flags = set(summary.quality_flags)
        if ("unknown_effects_present" in flags) != self.unknown_effects_present:
            raise ValueError("Reliability summary does not match unknown external effects")
        if ("open_effects_present" in flags) != self.open_effects_present:
            raise ValueError("Reliability summary does not match open external effects")
        if ("acceptance_evidence_unknown" in flags) != summary.unknown_acceptance_evidence:
            raise ValueError("Reliability summary does not match unknown acceptance evidence")
        required_checks_failed = (
            not summary.unknown_acceptance_evidence and not summary.required_checks_passed
        )
        if ("required_checks_failed" in flags) != required_checks_failed:
            raise ValueError("Reliability summary does not match required-check evidence")
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
    schema_version: Literal[1, 2] = 2
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
        if self.schema_version != self.evidence.schema_version:
            raise ValueError("Terminal EvidencePack and evidence schema versions must match")
        return self
