from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, model_validator

from horizon.domain.common import Contract, digest
from horizon.domain.task import Identifier

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Count = Annotated[StrictInt, Field(ge=0)]


class RunMemoryEntry(Contract):
    """A controller-derived observation; never an unsupported model-authored fact."""

    schema_version: Literal[1] = 1
    memory_id: Sha256
    scope_type: Literal["run"] = "run"
    scope_id: Identifier
    source_run_id: Identifier
    task_spec_hash: Sha256
    work_item_id: Identifier
    source_event_id: Identifier
    source_event_seq: Annotated[StrictInt, Field(ge=1)]
    source_event_hash: Sha256
    source_call_id: Identifier
    kind: Literal["observation", "workspace_change", "validation", "unresolved_effect"]
    tool_name: Identifier
    outcome: Literal["success", "error", "cancelled", "unknown"]
    confidence: Literal["observed"] = "observed"
    status: Literal["active", "stale", "unresolved"]
    dependency_revision: Sha256 | None = None
    evidence_ref: Sha256 | None = None
    evidence_hash: Sha256 | None = None
    statement: Annotated[str, Field(min_length=1, max_length=600)]
    excerpt: Annotated[str, Field(max_length=1_000)] = ""

    def identity_payload(self) -> dict:
        return self.model_dump(mode="json", exclude={"memory_id"})

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if self.scope_id != self.source_run_id:
            raise ValueError("Run memory cannot cross run scope")
        if self.outcome == "unknown":
            if (
                self.status != "unresolved"
                or self.evidence_ref is not None
                or self.evidence_hash is not None
                or self.excerpt
            ):
                raise ValueError("Unknown effects must remain unresolved without output evidence")
        elif (
            self.status == "unresolved"
            or self.evidence_ref is None
            or self.evidence_hash is None
            or self.evidence_ref != self.evidence_hash
        ):
            raise ValueError("Settled observations require content-addressed output evidence")
        if self.memory_id != digest(self.identity_payload()):
            raise ValueError("Run memory ID does not match its canonical observation")
        return self

    @classmethod
    def create(cls, **values) -> RunMemoryEntry:
        provisional = cls.model_construct(memory_id="0" * 64, **values)
        return cls(memory_id=digest(provisional.identity_payload()), **values)


class RunMemorySnapshot(Contract):
    """A bounded projection; the EventLog and evidence Artifacts remain authoritative."""

    schema_version: Literal[1] = 1
    run_id: Identifier
    task_spec_hash: Sha256
    plan_version: Annotated[StrictInt, Field(ge=1)]
    work_item_id: Identifier
    covered_event_seq: Annotated[StrictInt, Field(ge=1)]
    workspace_revision: Sha256
    relevant_event_digest: Sha256
    max_entries: Annotated[StrictInt, Field(ge=1, le=50)]
    excerpt_chars: Annotated[StrictInt, Field(ge=0, le=1_000)]
    total_entry_count: Count
    included_entry_count: Count
    omitted_entry_count: Count
    active_count: Count
    stale_count: Count
    unresolved_count: Count
    entries: Annotated[tuple[RunMemoryEntry, ...], Field(max_length=50)] = ()

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.included_entry_count != len(self.entries):
            raise ValueError("Included Run memory count does not match entries")
        if self.total_entry_count != self.included_entry_count + self.omitted_entry_count:
            raise ValueError("Run memory total does not match included and omitted counts")
        if self.included_entry_count > self.max_entries:
            raise ValueError("Run memory exceeds its entry bound")
        if self.active_count != sum(entry.status == "active" for entry in self.entries):
            raise ValueError("Run memory active count does not match entries")
        if self.stale_count != sum(entry.status == "stale" for entry in self.entries):
            raise ValueError("Run memory stale count does not match entries")
        if self.unresolved_count != sum(entry.status == "unresolved" for entry in self.entries):
            raise ValueError("Run memory unresolved count does not match entries")
        if any(
            entry.scope_id != self.run_id
            or entry.task_spec_hash != self.task_spec_hash
            or entry.source_event_seq > self.covered_event_seq
            for entry in self.entries
        ):
            raise ValueError("Run memory entry is outside its snapshot boundary")
        return self

    @property
    def sha256(self) -> str:
        return digest(self)
