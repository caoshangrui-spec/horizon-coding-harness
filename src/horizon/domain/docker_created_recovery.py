from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from horizon.domain.common import Contract
from horizon.domain.task import Identifier, PositiveInt, Text, relative_pattern

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
DockerImageId = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]

DOCKER_CREATED_RECOVERY_BOUNDARIES = (
    "only_the_docker_create_to_start_crash_window_is_exercised",
    "workspace_marker_absence_does_not_prove_absence_of_arbitrary_external_effects",
    "host_daemon_kernel_and_distributed_crashes_are_not_covered",
    "self_consistent_hashes_are_not_origin_authentication",
)


class DockerCreatedCrashObservation(Contract):
    schema_version: Literal[1] = 1
    call_id: Identifier
    worker_exit_code: Literal[34] = 34
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
    worker_exit_code: Literal[34] = 34
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
