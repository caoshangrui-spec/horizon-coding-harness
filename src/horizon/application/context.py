from __future__ import annotations

from dataclasses import dataclass

from horizon.domain.common import canonical_json, digest
from horizon.domain.context import ContextProjection, MandatoryFactLedger
from horizon.domain.errors import Conflict
from horizon.domain.memory import RunMemorySnapshot
from horizon.domain.model import ModelMessage, ToolDefinition
from horizon.domain.run import Run


@dataclass(frozen=True)
class _MessageUnit:
    messages: tuple[ModelMessage, ...]
    complete: bool


def _units(messages: tuple[ModelMessage, ...]) -> tuple[_MessageUnit, ...]:
    units: list[_MessageUnit] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.role == "tool":
            raise Conflict("Canonical Agent transcript contains an orphan tool result")
        if message.role != "assistant" or not message.tool_calls:
            units.append(_MessageUnit(messages=(message,), complete=True))
            index += 1
            continue
        expected = {call.id for call in message.tool_calls}
        collected = [message]
        observed: set[str] = set()
        cursor = index + 1
        while cursor < len(messages) and messages[cursor].role == "tool":
            tool_message = messages[cursor]
            if tool_message.tool_call_id not in expected or tool_message.tool_call_id in observed:
                raise Conflict("Canonical Agent transcript contains mismatched tool results")
            collected.append(tool_message)
            observed.add(tool_message.tool_call_id)
            cursor += 1
        complete = observed == expected
        units.append(_MessageUnit(messages=tuple(collected), complete=complete))
        index = cursor
    return tuple(units)


def _unit_fact(unit: _MessageUnit) -> dict:
    facts: list[dict] = []
    for message in unit.messages:
        if message.role == "assistant" and message.tool_calls:
            facts.append(
                {
                    "role": "assistant",
                    "tools": [
                        {
                            "name": call.function.name,
                            "arguments_hash": digest(call.function.arguments),
                        }
                        for call in message.tool_calls
                    ],
                }
            )
        else:
            content = message.content or ""
            facts.append(
                {
                    "role": message.role,
                    "tool_call_id": message.tool_call_id,
                    "content_hash": digest(content),
                    "excerpt": content[:240],
                }
            )
    return {
        "unit_hash": digest([item.model_dump(mode="json") for item in unit.messages]),
        "facts": facts,
    }


class ContextProjector:
    """Build a bounded model view while preserving the canonical transcript elsewhere.

    Recent complete units are retained when they fit. Under the hard context cap, retention is
    best-effort: the oldest additional complete units are deterministically compacted. Incomplete
    tool turns are never compacted.
    """

    def __init__(self, max_chars: int, preserve_recent_units: int):
        if max_chars < 2_000 or preserve_recent_units < 1:
            raise ValueError("Context projection limits are too small")
        self.max_chars = max_chars
        self.preserve_recent_units = preserve_recent_units

    @staticmethod
    def _size(messages: tuple[ModelMessage, ...]) -> int:
        return len(canonical_json([item.model_dump(mode="json") for item in messages]))

    @staticmethod
    def _bind_mandatory_facts(
        messages: tuple[ModelMessage, ...],
        mandatory_facts: MandatoryFactLedger | None,
        mandatory_facts_ref: str | None,
        run_memory: RunMemorySnapshot | None,
        run_memory_ref: str | None,
    ) -> tuple[ModelMessage, ...]:
        if (mandatory_facts is None) != (mandatory_facts_ref is None):
            raise ValueError(
                "Mandatory facts and their artifact reference must be supplied together"
            )
        if (run_memory is None) != (run_memory_ref is None):
            raise ValueError("Run memory and its artifact reference must be supplied together")
        if mandatory_facts is None and run_memory is None:
            return messages
        if mandatory_facts is not None and mandatory_facts.sha256 != mandatory_facts_ref:
            raise Conflict("Mandatory fact ledger does not match its content-addressed artifact")
        if run_memory is not None and run_memory.sha256 != run_memory_ref:
            raise Conflict("Run memory does not match its content-addressed artifact")
        system = messages[0]
        bindings: list[str] = []
        if mandatory_facts is not None:
            visible_binding = {
                "ledger_ref": mandatory_facts_ref,
                "task_spec_hash": mandatory_facts.task_spec_hash,
                "plan_hash": mandatory_facts.plan_hash,
                "work_item_id": mandatory_facts.work_item_id,
                "completed_work_item_ids": mandatory_facts.completed_work_item_ids,
                "required_acceptance_ids": mandatory_facts.required_acceptance_ids,
                "execution_mode": mandatory_facts.execution_mode,
                "authority_scope": mandatory_facts.authority_scope,
                "workspace_revision": mandatory_facts.workspace_revision,
                "tool_schema_hash": mandatory_facts.tool_schema_hash,
            }
            bindings.append(
                "Controller-owned mandatory facts. These are integrity bindings, not model "
                "memory; do not reinterpret or override them:\n"
                f"{canonical_json(visible_binding)}"
            )
        if run_memory is not None:
            recent_entries = []
            for entry in run_memory.entries[-2:]:
                recent_entries.append(
                    {
                        "source_event_seq": entry.source_event_seq,
                        "work_item_id": entry.work_item_id,
                        "tool": entry.tool_name,
                        "outcome": entry.outcome,
                        "status": entry.status,
                        "excerpt": (entry.excerpt[:80] if entry.status == "active" else ""),
                    }
                )
            visible_memory = {
                "snapshot_ref": run_memory_ref,
                "omitted_entry_count": run_memory.omitted_entry_count,
                "stale_count": run_memory.stale_count,
                "unresolved_count": run_memory.unresolved_count,
                "recent_entries": recent_entries,
            }
            bindings.append(
                "Controller-derived Run memory. Only active excerpts describe the current "
                "workspace; stale entries are provenance, and unresolved effects are not facts:\n"
                f"{canonical_json(visible_memory)}"
            )
        bound_system = ModelMessage(
            role="system",
            content=f"{system.content}\n" + "\n".join(bindings),
        )
        return (bound_system, *messages[1:])

    def project(
        self,
        messages: tuple[ModelMessage, ...],
        *,
        mandatory_facts: MandatoryFactLedger | None = None,
        mandatory_facts_ref: str | None = None,
        run_memory: RunMemorySnapshot | None = None,
        run_memory_ref: str | None = None,
    ) -> ContextProjection:
        if len(messages) < 2 or messages[0].role != "system" or messages[1].role != "user":
            raise Conflict("Canonical Agent transcript must start with system and user messages")
        source_digest = digest([item.model_dump(mode="json") for item in messages])
        visible_messages = self._bind_mandatory_facts(
            messages,
            mandatory_facts,
            mandatory_facts_ref,
            run_memory,
            run_memory_ref,
        )
        units = _units(messages[2:])
        if self._size(visible_messages) <= self.max_chars:
            return ContextProjection(
                max_chars=self.max_chars,
                preserve_recent_units=self.preserve_recent_units,
                source_message_count=len(messages),
                projected_message_count=len(messages),
                compacted_unit_count=0,
                source_digest=source_digest,
                mandatory_facts_ref=mandatory_facts_ref,
                mandatory_facts_hash=(mandatory_facts.sha256 if mandatory_facts else None),
                run_memory_ref=run_memory_ref,
                run_memory_hash=(run_memory.sha256 if run_memory else None),
                run_memory_entry_count=(run_memory.included_entry_count if run_memory else 0),
                compacted=False,
                messages=visible_messages,
            )

        prefix = visible_messages[:2]
        first_incomplete = next(
            (index for index, unit in enumerate(units) if not unit.complete),
            len(units),
        )
        preferred_end = min(
            first_incomplete,
            max(0, len(units) - self.preserve_recent_units),
        )

        def assemble(
            old_units: tuple[_MessageUnit, ...],
            kept_units: tuple[_MessageUnit, ...],
            compacted_digest: str,
            retained_facts: list[dict],
        ) -> tuple[ModelMessage, ...]:
            summary = ModelMessage(
                role="user",
                content=(
                    "Deterministic history projection. The immutable canonical transcript "
                    "remains authoritative; omitted units must not be treated as new "
                    "evidence.\n"
                    + canonical_json(
                        {
                            "compacted_unit_count": len(old_units),
                            "compacted_units_digest": compacted_digest,
                            "retained_fact_count": len(retained_facts),
                            "omitted_fact_count": len(old_units) - len(retained_facts),
                            "latest_compacted_facts": retained_facts,
                        }
                    )
                ),
            )
            return (
                *prefix,
                summary,
                *(message for unit in kept_units for message in unit.messages),
            )

        for compactable_end in range(max(1, preferred_end), first_incomplete + 1):
            old_units = units[:compactable_end]
            kept_units = units[compactable_end:]
            facts = [_unit_fact(unit) for unit in old_units[-20:]]
            compacted_digest = digest(
                [[item.model_dump(mode="json") for item in unit.messages] for unit in old_units]
            )
            projected = assemble(old_units, kept_units, compacted_digest, facts)
            while facts and self._size(projected) > self.max_chars:
                facts = facts[1:]
                projected = assemble(old_units, kept_units, compacted_digest, facts)
            if self._size(projected) <= self.max_chars:
                return ContextProjection(
                    max_chars=self.max_chars,
                    preserve_recent_units=self.preserve_recent_units,
                    source_message_count=len(messages),
                    projected_message_count=len(projected),
                    compacted_unit_count=len(old_units),
                    source_digest=source_digest,
                    mandatory_facts_ref=mandatory_facts_ref,
                    mandatory_facts_hash=(mandatory_facts.sha256 if mandatory_facts else None),
                    run_memory_ref=run_memory_ref,
                    run_memory_hash=(run_memory.sha256 if run_memory else None),
                    run_memory_entry_count=(run_memory.included_entry_count if run_memory else 0),
                    compacted=True,
                    messages=projected,
                )
        raise Conflict(
            "Protected prefix or incomplete Agent turn exceeds the context projection budget"
        )


def build_mandatory_fact_ledger(
    run: Run,
    work_item_id: str,
    workspace_revision: str,
    tool_definitions: tuple[ToolDefinition, ...],
) -> MandatoryFactLedger:
    if run.plan is None:
        raise Conflict("Mandatory facts require an active plan")
    if run.model_policy is None:
        raise Conflict("Mandatory facts require a bound model policy")
    items = {item.work_item_id: item for item in run.plan.items}
    item = items.get(work_item_id)
    if item is None or item.work_item_id in run.passed_items:
        raise Conflict("Mandatory facts require an incomplete plan work item")
    if not set(item.dependencies) <= run.passed_items:
        raise Conflict("Mandatory facts require a dependency-ready work item")
    completed = tuple(
        candidate.work_item_id
        for candidate in run.plan.items
        if candidate.work_item_id in run.passed_items
    )
    required_acceptance_ids = tuple(check.id for check in run.task.acceptance if check.required)
    return MandatoryFactLedger(
        run_id=run.run_id,
        task_spec_hash=run.task.sha256,
        objective_hash=digest(run.task.objective),
        plan_hash=digest(run.plan),
        plan_version=run.plan.version,
        work_item_id=item.work_item_id,
        work_item_hash=digest(item),
        completed_work_item_ids=completed,
        completed_work_items_hash=digest(completed),
        allowed_paths_hash=digest(run.task.constraints.allowed_paths),
        denied_paths_hash=digest(run.task.constraints.denied_paths),
        required_acceptance_ids=required_acceptance_ids,
        required_acceptance_hash=digest(required_acceptance_ids),
        budget_hash=digest(run.task.budgets),
        execution_mode=run.task.execution_mode,
        authority_scope=run.task.authority_scope,
        model_policy_hash=digest(run.model_policy),
        workspace_revision=workspace_revision,
        tool_schema_hash=digest(
            [definition.model_dump(mode="json") for definition in tool_definitions]
        ),
    )
