from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, field_validator, model_validator

from horizon.domain.common import Contract, digest
from horizon.domain.states import TERMINAL, RunStatus
from horizon.domain.task import Identifier, Text, relative_pattern
from horizon.domain.terminal_evidence import Sha256

BatchCaseState = Literal["pending", "running", "attention", "terminal"]

AGENT_BATCH_BOUNDARIES = (
    "cases_execute_sequentially_on_one_host",
    "terminal_failure_is_recorded_and_does_not_become_success",
    "nonterminal_or_uncertain_case_blocks_later_cases",
    "batch_state_is_a_recovery_checkpoint_not_origin_authentication",
    "terminal_evidence_is_reused_not_reimplemented",
    "no_concurrency_or_throughput_claim",
)


class AgentBatchCase(Contract):
    case_id: Identifier
    task_path: Text
    plan_path: Text | None = None
    auto_plan: bool = False
    pilot_preflight_path: Text | None = None

    @field_validator("task_path", "plan_path", "pilot_preflight_path")
    @classmethod
    def validate_relative_paths(cls, value: str | None) -> str | None:
        if value is None:
            return None
        relative_pattern(value)
        if any(character in value for character in "*?[]"):
            raise ValueError("Agent-batch paths must be literal")
        return value

    @model_validator(mode="after")
    def validate_plan_source(self) -> Self:
        if self.auto_plan == (self.plan_path is not None):
            raise ValueError("Each Agent-batch case requires exactly one Plan source")
        if self.pilot_preflight_path is not None and not self.auto_plan:
            raise ValueError("A pilot preflight requires automatic planning")
        return self


class AgentBatchManifest(Contract):
    schema_version: Literal[1] = 1
    batch_id: Identifier
    cases: Annotated[tuple[AgentBatchCase, ...], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def validate_cases(self) -> Self:
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Agent-batch case IDs must be unique")
        return self

    @property
    def sha256(self) -> str:
        return digest(self)


class AgentRunLaunchReceipt(Contract):
    schema_version: Literal[1] = 1
    invocation_id: Identifier
    run_id: Identifier
    task_id: Identifier
    task_spec_hash: Sha256
    source_revision: Sha256
    workspace_path_hash: Sha256


class AgentBatchCaseRecord(Contract):
    sequence: Annotated[StrictInt, Field(gt=0, le=100)]
    case_id: Identifier
    state: BatchCaseState = "pending"
    attempt_count: Annotated[StrictInt, Field(ge=0)] = 0
    invocation_id: Identifier | None = None
    run_id: Identifier | None = None
    run_status: RunStatus | None = None
    failure_reason: Text | None = None
    last_error_type: Identifier | None = None
    last_error_message: Text | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if (self.last_error_type is None) != (self.last_error_message is None):
            raise ValueError("Agent-batch error type and message must be recorded together")
        if self.state == "pending":
            if any(
                value is not None
                for value in (self.invocation_id, self.run_id, self.run_status, self.failure_reason)
            ):
                raise ValueError("Pending Agent-batch cases cannot carry execution state")
        else:
            if self.attempt_count < 1 or self.invocation_id is None:
                raise ValueError("Started Agent-batch cases require an invocation identity")
        if self.run_status is not None and self.run_id is None:
            raise ValueError("Agent-batch Run status requires a Run identity")
        if self.state == "terminal":
            if self.run_id is None or self.run_status not in TERMINAL:
                raise ValueError("Terminal Agent-batch cases require a terminal Run")
            if (self.run_status == RunStatus.FAILED) != (self.failure_reason is not None):
                raise ValueError("Only failed Agent-batch cases carry a failure reason")
        elif self.run_status in TERMINAL:
            raise ValueError("A terminal Run must use the terminal Agent-batch state")
        return self


class AgentBatchState(Contract):
    schema_version: Literal[1] = 1
    batch_id: Identifier
    manifest_digest: Sha256
    revision: Annotated[StrictInt, Field(ge=0)]
    complete: bool = False
    terminal_suite_path: Text | None = None
    cases: Annotated[tuple[AgentBatchCaseRecord, ...], Field(min_length=1, max_length=100)]
    boundaries: tuple[Text, ...] = AGENT_BATCH_BOUNDARIES

    @field_validator("terminal_suite_path")
    @classmethod
    def validate_suite_path(cls, value: str | None) -> str | None:
        if value is not None:
            relative_pattern(value)
        return value

    @model_validator(mode="after")
    def validate_totals(self) -> Self:
        if tuple(self.boundaries) != AGENT_BATCH_BOUNDARIES:
            raise ValueError("Agent-batch claim boundaries must remain explicit")
        if [case.sequence for case in self.cases] != list(range(1, len(self.cases) + 1)):
            raise ValueError("Agent-batch case sequence must be contiguous and ordered")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Agent-batch case IDs must be unique")
        all_terminal = all(case.state == "terminal" for case in self.cases)
        if self.complete != (all_terminal and self.terminal_suite_path is not None):
            raise ValueError("Agent-batch completion must match terminal cases and suite output")
        if not self.complete and self.terminal_suite_path is not None:
            raise ValueError("Incomplete Agent batches cannot reference a terminal suite")
        return self
