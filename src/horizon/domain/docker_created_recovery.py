from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from horizon.domain.common import Contract
from horizon.domain.task import Identifier, PositiveInt, Text, relative_pattern

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
DockerImageId = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]

DOCKER_CREATED_RECOVERY_BOUNDARIES = (
    "only_the_docker_create_to_start_crash_window_is_exercised",
    "docker_effect_executor_exits_while_lease_owning_controller_survives",
    "lease_owning_agent_worker_crash_and_automatic_takeover_are_not_exercised",
    "workspace_marker_absence_does_not_prove_absence_of_arbitrary_external_effects",
    "host_daemon_kernel_and_distributed_crashes_are_not_covered",
    "self_consistent_hashes_are_not_origin_authentication",
)

DOCKER_CREATED_RESUME_BOUNDARIES = (
    "only_the_docker_create_to_start_crash_window_is_injected",
    "docker_effect_executor_exits_while_lease_owning_controller_survives",
    "lease_owning_agent_worker_crash_and_automatic_takeover_are_not_exercised",
    "resume_rejects_a_docker_image_id_change_between_boundary_and_continuation",
    "resume_model_is_deterministic_and_does_not_measure_llm_coding_ability",
    "acceptance_command_is_a_synthetic_workspace_marker_not_a_real_issue",
    "paid_model_and_network_flags_describe_the_constructed_demo_path_not_external_attestation",
    "host_daemon_kernel_and_distributed_crashes_are_not_covered",
    "self_consistent_hashes_are_not_origin_authentication",
)


class DockerCreatedCrashObservation(Contract):
    schema_version: Literal[1] = 1
    call_id: Identifier
    executor_exit_code: Literal[34] = 34
    image_id: DockerImageId
    container_name: Text
    state_before_recovery: Literal["created"] = "created"
    state_after_recovery: Literal["missing"] = "missing"
    command_marker_path: Text
    command_marker_absent_before_recovery: Literal[True] = True
    command_marker_absent_after_recovery: Literal[True] = True

    @model_validator(mode="after")
    def validate_marker_path(self) -> Self:
        relative_pattern(self.command_marker_path)
        if any(character in self.command_marker_path for character in "*?[]"):
            raise ValueError("Docker recovery marker path must be literal")
        return self


class DockerCreatedRecoveryEvidence(Contract):
    schema_version: Literal[1] = 1
    run_id: Identifier
    task_id: Identifier
    call_id: Identifier
    run_status: Literal["RUNNING"] = "RUNNING"
    event_count: PositiveInt
    event_hash: Sha256
    projection_hash: Sha256
    workspace_revision_before: Sha256
    workspace_revision_after: Sha256
    tool_status: Literal["cancelled"] = "cancelled"
    recovery_disposition: Literal["discard_check"] = "discard_check"
    recovery_artifact_sha256: Sha256
    executor_exit_code: Literal[34] = 34
    image_id: DockerImageId
    container_name: Text
    state_before_recovery: Literal["created"] = "created"
    state_after_recovery: Literal["missing"] = "missing"
    command_marker_absent: Literal[True] = True
    trace_replay_verified: Literal[True] = True
    safe_to_resume: Literal[True] = True
    no_unknown_effects: Literal[True] = True
    no_open_effects: Literal[True] = True
    paid_model_called: Literal[False] = False
    network_called: Literal[False] = False
    repository_code_executed: Literal[False] = False
    external_cost_cny: Literal[0] = 0
    claim_scope: Literal["docker_created_attempt_reconciliation"] = (
        "docker_created_attempt_reconciliation"
    )
    boundaries: tuple[Text, ...] = DOCKER_CREATED_RECOVERY_BOUNDARIES

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if self.workspace_revision_before != self.workspace_revision_after:
            raise ValueError("Docker created-state recovery requires an unchanged workspace")
        if tuple(self.boundaries) != DOCKER_CREATED_RECOVERY_BOUNDARIES:
            raise ValueError("Docker created-state recovery boundaries must remain explicit")
        return self


class DockerCreatedRecoveryFile(Contract):
    role: Literal[
        "trace",
        "final_state",
        "crash_observation",
        "recovery_receipt",
        "workspace_manifest",
        "recovery_artifact",
        "summary",
    ]
    path: Text
    sha256: Sha256
    bytes: PositiveInt

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        relative_pattern(self.path)
        if any(character in self.path for character in "*?[]"):
            raise ValueError("Docker recovery evidence paths must be literal")
        return self


class DockerCreatedRecoveryPack(Contract):
    schema_version: Literal[1] = 1
    pack_type: Literal["horizon.docker-created-recovery"] = "horizon.docker-created-recovery"
    evidence: DockerCreatedRecoveryEvidence
    files: Annotated[
        tuple[DockerCreatedRecoveryFile, ...],
        Field(min_length=7, max_length=7),
    ]

    @model_validator(mode="after")
    def validate_files(self) -> Self:
        roles = [item.role for item in self.files]
        paths = [item.path.casefold() for item in self.files]
        expected = {
            "trace",
            "final_state",
            "crash_observation",
            "recovery_receipt",
            "workspace_manifest",
            "recovery_artifact",
            "summary",
        }
        if set(roles) != expected or len(roles) != len(expected):
            raise ValueError("Docker created-state EvidencePack requires all evidence roles")
        if len(paths) != len(set(paths)):
            raise ValueError("Docker created-state EvidencePack paths must be unique")
        return self


class DockerCreatedResumeObservation(Contract):
    """Runtime identities captured at the resumed Docker dispatch boundary."""

    schema_version: Literal[1] = 1
    run_id: Identifier
    boundary_image_id: DockerImageId
    resumed_image_id: DockerImageId
    resume_worker_id: Text
    resume_worker_epoch: PositiveInt
    resumed_check_call_id: Identifier
    image_identity_matched: Literal[True] = True

    @model_validator(mode="after")
    def validate_image_identity(self) -> Self:
        if self.boundary_image_id != self.resumed_image_id:
            raise ValueError("Docker image identity changed before resumed validation")
        return self


class DockerCreatedResumeEvidence(Contract):
    """Causal evidence that a reconciled created attempt can reach terminal success."""

    schema_version: Literal[1] = 1
    run_id: Identifier
    task_id: Identifier
    crash_call_id: Identifier
    resumed_check_call_id: Identifier
    executor_exit_code: Literal[34] = 34
    boundary_image_id: DockerImageId
    resumed_image_id: DockerImageId
    resume_worker_id: Text
    boundary_status: Literal["RUNNING"] = "RUNNING"
    terminal_status: Literal["SUCCEEDED"] = "SUCCEEDED"
    boundary_event_count: PositiveInt
    terminal_event_count: PositiveInt
    boundary_event_hash: Sha256
    terminal_event_hash: Sha256
    boundary_projection_hash: Sha256
    terminal_projection_hash: Sha256
    boundary_workspace_revision: Sha256
    terminal_workspace_revision: Sha256
    boundary_lease_epoch: PositiveInt
    resume_worker_epoch: PositiveInt
    recovery_disposition: Literal["discard_check"] = "discard_check"
    recovered_tool_status: Literal["cancelled"] = "cancelled"
    resumed_check_status: Literal["success"] = "success"
    state_before_recovery: Literal["created"] = "created"
    state_after_recovery: Literal["missing"] = "missing"
    safe_to_resume_at_boundary: Literal[True] = True
    created_trace_is_terminal_prefix: Literal[True] = True
    call_identity_rotated: Literal[True] = True
    command_marker_absent_at_boundary: Literal[True] = True
    command_marker_present_after_resume: Literal[True] = True
    protected_validation_passed: Literal[True] = True
    task_succeeded: Literal[True] = True
    no_duplicate_original_receipt: Literal[True] = True
    no_unknown_effects: Literal[True] = True
    no_open_effects: Literal[True] = True
    model_calls_before_resume: PositiveInt
    model_calls_after_resume: PositiveInt
    tool_calls_before_resume: PositiveInt
    tool_calls_after_resume: PositiveInt
    paid_model_called: Literal[False] = False
    network_called: Literal[False] = False
    external_cost_cny: Literal[0] = 0
    simulated_model_cost_cny: Annotated[Decimal, Field(ge=0)]
    claim_scope: Literal["docker_created_resume_to_terminal_success"] = (
        "docker_created_resume_to_terminal_success"
    )
    boundaries: tuple[Text, ...] = DOCKER_CREATED_RESUME_BOUNDARIES

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if self.boundary_image_id != self.resumed_image_id:
            raise ValueError("Resume evidence must bind one immutable Docker image ID")
        if self.crash_call_id == self.resumed_check_call_id:
            raise ValueError("Resumed validation must use a new durable tool call identity")
        if self.terminal_event_count <= self.boundary_event_count:
            raise ValueError("Terminal Trace must extend the created-state boundary Trace")
        if self.resume_worker_epoch <= self.boundary_lease_epoch:
            raise ValueError("Terminal continuation must be owned by a newer Worker epoch")
        if self.boundary_workspace_revision == self.terminal_workspace_revision:
            raise ValueError("Synthetic resumed validation must create its execution marker")
        if self.model_calls_after_resume <= self.model_calls_before_resume:
            raise ValueError("Resume-to-terminal evidence requires a new model turn")
        if self.tool_calls_after_resume <= self.tool_calls_before_resume:
            raise ValueError("Resume-to-terminal evidence requires new tool receipts")
        if tuple(self.boundaries) != DOCKER_CREATED_RESUME_BOUNDARIES:
            raise ValueError("Docker resume-to-terminal boundaries must remain explicit")
        return self


class DockerCreatedResumeFile(Contract):
    role: Literal[
        "created_boundary_pack",
        "terminal_pack",
        "resume_observation",
        "final_workspace_manifest",
        "resumed_check_artifact",
        "command_marker",
        "summary",
    ]
    path: Text
    sha256: Sha256
    bytes: PositiveInt

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        relative_pattern(self.path)
        if any(character in self.path for character in "*?[]"):
            raise ValueError("Docker resume-to-terminal evidence paths must be literal")
        return self


class DockerCreatedResumePack(Contract):
    schema_version: Literal[1] = 1
    pack_type: Literal["horizon.docker-created-resume"] = "horizon.docker-created-resume"
    evidence: DockerCreatedResumeEvidence
    files: Annotated[
        tuple[DockerCreatedResumeFile, ...],
        Field(min_length=7, max_length=7),
    ]

    @model_validator(mode="after")
    def validate_files(self) -> Self:
        roles = [item.role for item in self.files]
        paths = [item.path.casefold() for item in self.files]
        expected = {
            "created_boundary_pack",
            "terminal_pack",
            "resume_observation",
            "final_workspace_manifest",
            "resumed_check_artifact",
            "command_marker",
            "summary",
        }
        if set(roles) != expected or len(roles) != len(expected):
            raise ValueError("Docker resume-to-terminal EvidencePack requires all evidence roles")
        if len(paths) != len(set(paths)):
            raise ValueError("Docker resume-to-terminal EvidencePack paths must be unique")
        return self
