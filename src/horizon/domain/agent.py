from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StrictInt

from horizon.domain.common import Contract
from horizon.domain.model import ModelMessage
from horizon.domain.task import Identifier, PositiveInt

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Iteration = Annotated[StrictInt, Field(ge=1, le=10_000)]
EventSequence = Annotated[StrictInt, Field(ge=1)]


class AgentSession(Contract):
    schema_version: Literal[1] = 1
    run_id: Identifier
    task_spec_hash: Sha256
    plan_version: PositiveInt
    work_item_id: Identifier
    next_iteration: Iteration
    covered_event_seq: EventSequence
    workspace_revision: Sha256
    messages: Annotated[tuple[ModelMessage, ...], Field(min_length=2)]


class AgentSessionRecord(Contract):
    artifact_ref: Sha256
    task_spec_hash: Sha256
    plan_version: PositiveInt
    work_item_id: Identifier
    next_iteration: Iteration
    covered_event_seq: EventSequence
    workspace_revision: Sha256
    message_count: Annotated[StrictInt, Field(ge=2)]
