from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from horizon.domain.common import Contract, digest


class NewEvent(Contract):
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    # Decision-time metadata for facts whose deadline must be measured from the exact event
    # timestamp. It is consumed by the store and never enters the persisted event schema.
    occurred_at: datetime | None = Field(default=None, exclude=True)


class Event(NewEvent):
    event_id: str
    run_id: str
    seq: int = Field(ge=1)
    schema_version: Literal[1] = 1
    created_at: str
    causation_id: str | None = None
    correlation_id: str
    previous_hash: str
    event_hash: str

    def calculated_hash(self) -> str:
        return digest(self.model_dump(mode="json", exclude={"event_hash"}))
