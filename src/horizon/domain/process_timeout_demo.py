from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, model_validator

from horizon.domain.common import Contract
from horizon.domain.task import Identifier, PositiveInt, Text, relative_pattern

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
TimeoutSeconds = Annotated[float, Field(gt=0, le=5)]

PROCESS_TIMEOUT_DEMO_BOUNDARIES = (
    "one_local_direct_child_process_is_exercised",
    "the_child_confirms_the_exact_run_lease_epoch_and_launch_event_before_timeout",
    "the_parent_control_path_persists_a_cancellation_fence_before_requesting_process_stop",
    "a_synchronous_reap_receipt_precedes_terminal_cancellation",
    "no_model_tool_network_docker_or_repository_code_is_executed",
    "parent_crash_os_restart_process_trees_and_cross_host_workers_are_not_covered",
    "arbitrary_effect_recovery_and_exactly_once_execution_are_not_proved",
    "self_consistent_hashes_are_not_origin_authentication",
)


class ProcessTimeoutReadyObservation(Contract):
    """Identity a real child observes before it announces that it is waiting."""

    schema_version: Literal[1] = 1
    run_id: Identifier
    lease_id: Identifier
    worker_id: Identifier
    epoch: PositiveInt
    launch_event_seq: PositiveInt
    launch_event_hash: Sha256
    process_launch_id: Identifier
    process_id: PositiveInt


class ProcessTimeoutSupervisionObservation(Contract):
    """The process-owning parent's bounded timeout and synchronous reap result."""

    schema_version: Literal[1] = 1
    run_id: Identifier
    disposition: Literal["cancelled"] = "cancelled"
    worker_slices: Literal[1] = 1
    worker_handoffs: Literal[0] = 0
    process_receipt_count: Literal[1] = 1
    process_id: PositiveInt
    exit_code: StrictInt
    interruption_reason: Literal["worker_timeout"] = "worker_timeout"
    timeout_seconds: TimeoutSeconds
    stop_method: Literal["already_exited", "terminate", "kill"]


class ProcessTimeoutDemoEvidence(Contract):
    schema_version: Literal[1] = 1
    run_id: Identifier
    task_id: Identifier
    terminal_status: Literal["CANCELLED"] = "CANCELLED"
    lease_id: Identifier
    worker_id: Identifier
    lease_epoch: PositiveInt
    launch_event_seq: PositiveInt
    launch_event_hash: Sha256
    process_launch_id: Identifier
    process_id: PositiveInt
    exit_code: StrictInt
    interruption_reason: Literal["worker_timeout"] = "worker_timeout"
    timeout_seconds: TimeoutSeconds
    stop_method: Literal["already_exited", "terminate", "kill"]
    process_launched_event_seq: PositiveInt
    process_start_authorized_event_seq: PositiveInt
    cancellation_fence_event_seq: PositiveInt
    process_reaped_event_seq: PositiveInt
    worker_stopped_event_seq: PositiveInt
    lease_released_event_seq: PositiveInt
    terminal_cancellation_event_seq: PositiveInt
    event_count: PositiveInt
    event_hash: Sha256
    projection_hash: Sha256
    direct_child_ready: Literal[True] = True
    cancellation_fence_before_stop: Literal[True] = True
    synchronous_reap_recorded: Literal[True] = True
    trace_replay_verified: Literal[True] = True
    no_unknown_effects: Literal[True] = True
    no_open_effects: Literal[True] = True
    paid_model_called: Literal[False] = False
    tool_called: Literal[False] = False
    network_called: Literal[False] = False
    docker_called: Literal[False] = False
    repository_code_executed: Literal[False] = False
    external_cost_cny: Literal[0] = 0
    verification_mode: Literal["offline_recursive_trace_replay"] = "offline_recursive_trace_replay"
    claim_scope: Literal["single_direct_child_timeout_cancellation"] = (
        "single_direct_child_timeout_cancellation"
    )
    boundaries: tuple[Text, ...] = PROCESS_TIMEOUT_DEMO_BOUNDARIES

    @model_validator(mode="after")
    def validate_sequence(self) -> Self:
        if not (
            self.launch_event_seq
            < self.process_launched_event_seq
            < self.process_start_authorized_event_seq
            < self.cancellation_fence_event_seq
            < self.process_reaped_event_seq
            < self.worker_stopped_event_seq
            < self.lease_released_event_seq
            < self.terminal_cancellation_event_seq
            == self.event_count
        ):
            raise ValueError("Process-timeout evidence requires the exact cancellation ordering")
        if tuple(self.boundaries) != PROCESS_TIMEOUT_DEMO_BOUNDARIES:
            raise ValueError("Process-timeout claim boundaries must remain explicit")
        return self


class ProcessTimeoutEvidenceFile(Contract):
    role: Literal["ready_observation", "supervision_observation", "terminal_pack", "summary"]
    path: Text
    sha256: Sha256
    bytes: PositiveInt

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        relative_pattern(self.path)
        if any(character in self.path for character in "*?[]"):
            raise ValueError("Process-timeout evidence paths must be literal")
        return self


class ProcessTimeoutEvidencePack(Contract):
    schema_version: Literal[1] = 1
    pack_type: Literal["horizon.process-timeout-cancellation"] = (
        "horizon.process-timeout-cancellation"
    )
    evidence: ProcessTimeoutDemoEvidence
    files: Annotated[tuple[ProcessTimeoutEvidenceFile, ...], Field(min_length=4, max_length=4)]

    @model_validator(mode="after")
    def validate_files(self) -> Self:
        roles = [item.role for item in self.files]
        paths = [item.path.casefold() for item in self.files]
        expected = {
            "ready_observation",
            "supervision_observation",
            "terminal_pack",
            "summary",
        }
        if set(roles) != expected or len(roles) != len(expected):
            raise ValueError("Process-timeout EvidencePack requires all four evidence roles")
        if len(paths) != len(set(paths)):
            raise ValueError("Process-timeout EvidencePack paths must be unique")
        return self
