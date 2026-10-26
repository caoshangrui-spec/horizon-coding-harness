from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from horizon.domain.common import Contract, digest
from horizon.domain.task import Identifier, Mode, NonNegativeInt, TaskSpec, model_tools_for_mode

RequestKind = Literal["task", "status", "resume", "cancel", "approval", "clarification"]
TaskKind = Literal["bugfix", "feature", "refactor", "tests", "explain", "review", "unknown"]
RouteSource = Literal["explicit_command", "structured_task", "deterministic_rule", "unclassified"]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class UserRequest(Contract):
    text: Annotated[str, Field(min_length=1, max_length=20_000)]
    explicit_command: RequestKind | None = None
    target_run_id: Identifier | None = None
    target_request_id: Identifier | None = None
    task_spec: TaskSpec | None = None

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("User request text cannot be blank")
        return value

    @property
    def sha256(self) -> str:
        return digest(self)


class AdmissionContext(Contract):
    authority_scope: Mode = "read_only"
    repository_ref: Annotated[str, Field(min_length=1, max_length=2_048)] | None = None
    known_run_ids: tuple[Identifier, ...] = ()
    current_run_id: Identifier | None = None

    @model_validator(mode="after")
    def validate_run_context(self) -> Self:
        if len(self.known_run_ids) != len(set(self.known_run_ids)):
            raise ValueError("Known Run IDs must be unique")
        if self.current_run_id is not None and (
            self.known_run_ids and self.current_run_id not in self.known_run_ids
        ):
            raise ValueError("Current Run ID must be included in known Run IDs")
        return self


class IntentEvidenceSpan(Contract):
    field: Literal["request_kind", "task_kind", "execution_mode"]
    rule_id: Identifier
    text: Annotated[str, Field(min_length=1, max_length=500)]
    start: NonNegativeInt
    end: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.end <= self.start:
            raise ValueError("Intent evidence span must be nonempty")
        return self


class IntentEnvelope(Contract):
    schema_version: Literal["1.0"] = "1.0"
    request_ref: Sha256
    request_kind: RequestKind
    task_kind: TaskKind = "unknown"
    requested_execution_mode: Mode = "read_only"
    execution_mode: Mode = "read_only"
    authority_scope: Mode = "read_only"
    target_run_id: Identifier | None = None
    target_request_id: Identifier | None = None
    task_spec_hash: Sha256 | None = None
    route_source: RouteSource
    missing_fields: tuple[Identifier, ...] = ()
    evidence_spans: tuple[IntentEvidenceSpan, ...]
    effective_model_tools: tuple[Identifier, ...] = ()
    model_classification_required: bool = False
    admission_model_calls: NonNegativeInt = 0
    admission_cost_cny: Annotated[Decimal, Field(ge=0)] = Decimal("0")

    @model_validator(mode="after")
    def validate_authority_boundary(self) -> Self:
        ranks = {"read_only": 0, "plan_only": 1, "workspace_write": 2}
        if ranks[self.execution_mode] > ranks[self.requested_execution_mode]:
            raise ValueError("Intent routing cannot widen the requested execution mode")
        if ranks[self.execution_mode] > ranks[self.authority_scope]:
            raise ValueError("Intent routing cannot widen admission authority")
        if len(self.missing_fields) != len(set(self.missing_fields)):
            raise ValueError("Missing fields must be unique")
        if self.request_kind != "task":
            if self.task_kind != "unknown" or self.effective_model_tools:
                raise ValueError("Control requests cannot carry task tools or a task kind")
            if self.model_classification_required or self.admission_model_calls:
                raise ValueError("Control requests cannot invoke an intent model")
        if not set(self.effective_model_tools) <= set(model_tools_for_mode(self.execution_mode)):
            raise ValueError("Intent tools cannot exceed the effective execution mode")
        if self.admission_model_calls == 0 and self.admission_cost_cny != 0:
            raise ValueError("Zero admission model calls must have zero model cost")
        if self.model_classification_required and self.task_kind != "unknown":
            raise ValueError("Only an unknown task kind may require model classification")
        if not self.evidence_spans:
            raise ValueError("Intent routing must retain at least one request evidence span")
        return self
