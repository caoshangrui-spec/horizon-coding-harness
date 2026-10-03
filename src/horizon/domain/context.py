from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, model_validator

from horizon.domain.common import Contract, canonical_json, digest
from horizon.domain.model import InputTokenBudget, ModelMessage
from horizon.domain.task import Identifier, Mode, PositiveInt

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class MandatoryFactLedger(Contract):
    """Controller-owned facts that must survive every model-context projection."""

    schema_version: Literal[2] = 2
    run_id: Identifier
    task_spec_hash: Sha256
    objective_hash: Sha256
    plan_hash: Sha256
    plan_version: PositiveInt
    work_item_id: Identifier
    work_item_hash: Sha256
    completed_work_item_ids: tuple[Identifier, ...] = ()
    completed_work_items_hash: Sha256 = digest(())
    allowed_paths_hash: Sha256
    denied_paths_hash: Sha256
    required_acceptance_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    required_acceptance_hash: Sha256
    budget_hash: Sha256
    execution_mode: Mode
    authority_scope: Mode
    model_policy_hash: Sha256
    workspace_revision: Sha256
    tool_schema_hash: Sha256

    @model_validator(mode="after")
    def validate_completed_work_items(self) -> Self:
        if len(self.completed_work_item_ids) != len(set(self.completed_work_item_ids)):
            raise ValueError("Completed work item IDs must be unique")
        if self.completed_work_items_hash != digest(self.completed_work_item_ids):
            raise ValueError("Completed work item hash does not match its IDs")
        if self.work_item_id in self.completed_work_item_ids:
            raise ValueError("The active work item cannot already be completed")
        return self

    @property
    def sha256(self) -> str:
        return digest(self)


class ContextProjection(Contract):
    schema_version: Literal[2] = 2
    max_chars: Annotated[StrictInt, Field(ge=2_000, le=1_000_000)]
    preserve_recent_units: Annotated[StrictInt, Field(ge=1, le=50)]
    source_message_count: Annotated[StrictInt, Field(ge=2)]
    projected_message_count: Annotated[StrictInt, Field(ge=2)]
    projected_chars: Annotated[StrictInt, Field(ge=1)]
    input_token_budget: InputTokenBudget
    compacted_unit_count: Annotated[StrictInt, Field(ge=0)]
    source_digest: Sha256
    mandatory_facts_ref: Sha256 | None = None
    mandatory_facts_hash: Sha256 | None = None
    run_memory_ref: Sha256 | None = None
    run_memory_hash: Sha256 | None = None
    run_memory_entry_count: Annotated[StrictInt, Field(ge=0)] = 0
    compacted: bool
    messages: Annotated[tuple[ModelMessage, ...], Field(min_length=2)]

    @model_validator(mode="after")
    def check_shape(self) -> ContextProjection:
        if self.projected_message_count != len(self.messages):
            raise ValueError("Projected message count must match the stored messages")
        if self.projected_message_count > self.source_message_count:
            raise ValueError("A projection cannot contain more messages than its source")
        actual_chars = len(
            canonical_json([message.model_dump(mode="json") for message in self.messages])
        )
        if self.projected_chars != actual_chars or self.projected_chars > self.max_chars:
            raise ValueError("Projected character usage does not match its configured budget")
        if self.compacted != (self.compacted_unit_count > 0):
            raise ValueError("Compaction flag and compacted unit count are inconsistent")
        if (self.mandatory_facts_ref is None) != (self.mandatory_facts_hash is None):
            raise ValueError("Mandatory facts require both an artifact reference and hash")
        if (
            self.mandatory_facts_ref is not None
            and self.mandatory_facts_ref != self.mandatory_facts_hash
        ):
            raise ValueError("Mandatory fact artifacts must be content addressed by their hash")
        if (self.run_memory_ref is None) != (self.run_memory_hash is None):
            raise ValueError("Run memory requires both an artifact reference and hash")
        if self.run_memory_ref is None and self.run_memory_entry_count != 0:
            raise ValueError("Run memory entry count requires a memory artifact")
        if self.run_memory_ref is not None and self.run_memory_ref != self.run_memory_hash:
            raise ValueError("Run memory artifacts must be content addressed by their hash")
        if self.messages[0].role != "system" or self.messages[1].role != "user":
            raise ValueError(
                "A context projection must preserve the initial system and user messages"
            )
        return self
