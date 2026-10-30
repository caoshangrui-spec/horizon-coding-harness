from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from horizon.domain.common import Contract
from horizon.domain.task import Identifier, PositiveInt, Text, relative_pattern

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

CONTEXT_CAPACITY_DEMO_BOUNDARIES = (
    "the_blocked_request_stops_before_any_model_call_reservation_or_provider_dispatch",
    "the_context_stop_and_both_agent_sessions_are_content_addressed_and_trace_bound",
    "the_resumed_agent_session_preserves_the_canonical_transcript_byte_for_byte",
    "a_new_lease_epoch_resumes_only_after_a_sufficient_local_capacity_decision",
    "the_offline_scripted_model_and_text_inspection_check_incur_no_external_model_cost",
    "semantic_summary_quality_and_provider_specific_context_windows_are_not_proved",
    "cross_host_recovery_and_automatic_capacity_selection_are_not_covered",
    "self_consistent_hashes_are_not_origin_authentication",
)


class ContextCapacityDemoEvidence(Contract):
    schema_version: Literal[1] = 1
    run_id: Identifier
    task_id: Identifier
    terminal_status: Literal["SUCCEEDED"] = "SUCCEEDED"
    request_id: Identifier
    stop_reason: Literal["protected_prefix_exceeds_limit"] = "protected_prefix_exceeds_limit"
    stop_artifact_ref: Sha256
    blocked_session_artifact_ref: Sha256
    resumed_session_artifact_ref: Sha256
    source_digest: Sha256
    configured_context_chars: PositiveInt
    required_context_chars: PositiveInt
    configured_input_tokens: PositiveInt
    required_input_tokens: PositiveInt
    blocked_message_count: PositiveInt
    resumed_message_count: PositiveInt
    blocked_session_event_seq: PositiveInt
    resumed_session_covered_event_seq: PositiveInt
    blocked_lease_epoch: PositiveInt
    recovery_lease_epoch: PositiveInt
    request_event_seq: PositiveInt
    waiting_state_event_seq: PositiveInt
    blocked_lease_release_event_seq: PositiveInt
    recovery_lease_event_seq: PositiveInt
    decision_event_seq: PositiveInt
    running_state_event_seq: PositiveInt
    session_resumed_event_seq: PositiveInt
    first_model_reservation_event_seq: PositiveInt
    event_count: PositiveInt
    event_hash: Sha256
    projection_hash: Sha256
    final_model_call_count: Literal[2] = 2
    final_tool_call_count: Literal[3] = 3
    provider_dispatch_before_resume: Literal[False] = False
    transcript_preserved: Literal[True] = True
    new_lease_epoch_used: Literal[True] = True
    required_validation_passed: Literal[True] = True
    trace_replay_verified: Literal[True] = True
    no_unknown_effects: Literal[True] = True
    no_open_effects: Literal[True] = True
    paid_model_called: Literal[False] = False
    network_called: Literal[False] = False
    docker_called: Literal[False] = False
    repository_code_executed: Literal[False] = False
    external_cost_cny: Literal[0] = 0
    verification_mode: Literal["offline_recursive_trace_and_artifact_replay"] = (
        "offline_recursive_trace_and_artifact_replay"
    )
    claim_scope: Literal["single_host_context_capacity_pause_and_resume"] = (
        "single_host_context_capacity_pause_and_resume"
    )
    boundaries: tuple[Text, ...] = CONTEXT_CAPACITY_DEMO_BOUNDARIES

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if self.required_context_chars <= self.configured_context_chars:
            raise ValueError("Context-capacity demo requires an exceeded character limit")
        if self.blocked_message_count != self.resumed_message_count:
            raise ValueError("Context-capacity demo must preserve the transcript message count")
        if self.recovery_lease_epoch != self.blocked_lease_epoch + 1:
            raise ValueError("Context-capacity demo recovery must use the next lease epoch")
        if not (
            self.blocked_session_event_seq
            < self.request_event_seq
            < self.waiting_state_event_seq
            < self.blocked_lease_release_event_seq
            < self.recovery_lease_event_seq
            < self.decision_event_seq
            < self.running_state_event_seq
            < self.session_resumed_event_seq
            < self.first_model_reservation_event_seq
            <= self.event_count
        ):
            raise ValueError("Context-capacity evidence requires the exact recovery ordering")
        if self.resumed_session_covered_event_seq != self.session_resumed_event_seq - 1:
            raise ValueError("Resumed session must bind the event boundary before publication")
        if tuple(self.boundaries) != CONTEXT_CAPACITY_DEMO_BOUNDARIES:
            raise ValueError("Context-capacity claim boundaries must remain explicit")
        return self


class ContextCapacityEvidenceFile(Contract):
    role: Literal[
        "context_stop",
        "blocked_session",
        "resumed_session",
        "terminal_pack",
        "summary",
    ]
    path: Text
    sha256: Sha256
    bytes: PositiveInt

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        relative_pattern(self.path)
        if any(character in self.path for character in "*?[]"):
            raise ValueError("Context-capacity evidence paths must be literal")
        return self


class ContextCapacityEvidencePack(Contract):
    schema_version: Literal[1] = 1
    pack_type: Literal["horizon.context-capacity-recovery"] = "horizon.context-capacity-recovery"
    evidence: ContextCapacityDemoEvidence
    files: Annotated[tuple[ContextCapacityEvidenceFile, ...], Field(min_length=5, max_length=5)]

    @model_validator(mode="after")
    def validate_files(self) -> Self:
        roles = [item.role for item in self.files]
        paths = [item.path.casefold() for item in self.files]
        expected = {
            "context_stop",
            "blocked_session",
            "resumed_session",
            "terminal_pack",
            "summary",
        }
        if set(roles) != expected or len(roles) != len(expected):
            raise ValueError("Context-capacity EvidencePack requires all five evidence roles")
        if len(paths) != len(set(paths)):
            raise ValueError("Context-capacity EvidencePack paths must be unique")
        return self
