from __future__ import annotations

from typing import Annotated, Self

from pydantic import Field, model_validator

from horizon.domain.common import Contract, digest
from horizon.domain.errors import PolicyDenied
from horizon.domain.task import Identifier, PositiveInt, TaskSpec, Text, model_tools_for_mode

MAX_EXECUTION_REPLANS = 1
MAX_EXECUTION_REPLAN_ITEMS = 8
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


def permitted_plan_tools(task: TaskSpec) -> tuple[str, ...]:
    mode_tools = model_tools_for_mode(task.execution_mode)
    allowed_tools = task.constraints.allowed_tools
    if allowed_tools is None:
        return mode_tools
    allowed = set(allowed_tools)
    return tuple(tool for tool in mode_tools if tool in allowed)


class WorkItem(Contract):
    work_item_id: Identifier
    title: Text
    objective: Text
    dependencies: tuple[Identifier, ...] = ()
    expected_artifacts: Annotated[tuple[Text, ...], Field(min_length=1)]
    acceptance_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    allowed_tools: tuple[Identifier, ...] = ("search_repo", "read_file")
    requirement_ids: tuple[Identifier, ...] = ()
    evidence_refs: tuple[Text, ...] = ()


class Plan(Contract):
    version: PositiveInt = 1
    items: Annotated[tuple[WorkItem, ...], Field(min_length=1, max_length=1000)]

    @property
    def sha256(self) -> str:
        return digest(self)

    @model_validator(mode="after")
    def validate_dag(self) -> Self:
        ids = [item.work_item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("Work item IDs must be unique")
        known = set(ids)
        pending = {item.work_item_id: set(item.dependencies) for item in self.items}
        for item in self.items:
            if len(item.dependencies) != len(set(item.dependencies)):
                raise ValueError("Duplicate dependency")
            if item.work_item_id in item.dependencies or not set(item.dependencies) <= known:
                raise ValueError("Unknown or self dependency")
        visited: set[str] = set()
        while pending:
            ready = {key for key, deps in pending.items() if deps <= visited}
            if not ready:
                raise ValueError("Work item dependencies contain a cycle")
            visited.update(ready)
            for key in ready:
                del pending[key]
        return self

    def check_task(
        self,
        task: TaskSpec,
        *,
        enforce_unique_acceptance_ownership: bool = True,
    ) -> None:
        known = {check.id for check in task.acceptance}
        required = {check.id for check in task.acceptance if check.required}
        assignments = [check for item in self.items for check in item.acceptance_ids]
        covered = set(assignments)
        if not covered <= known or not required <= covered:
            raise PolicyDenied("Plan must cover every required check without inventing checks")
        if enforce_unique_acceptance_ownership and len(assignments) != len(covered):
            raise PolicyDenied("Each acceptance check must belong to exactly one WorkItem")
        permitted = set(permitted_plan_tools(task))
        if any(not set(item.allowed_tools) <= permitted for item in self.items):
            raise PolicyDenied("Plan contains tools outside the task execution authority")

    def ready_items(self, passed: set[str]) -> tuple[WorkItem, ...]:
        known = {item.work_item_id for item in self.items}
        if not passed <= known:
            raise ValueError("Unknown passed work item")
        return tuple(
            item
            for item in self.items
            if item.work_item_id not in passed and set(item.dependencies) <= passed
        )


class ExecutionReplanProposal(Contract):
    """One model-proposed Plan revision grounded in evidence from the active run."""

    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    items: Annotated[
        tuple[WorkItem, ...], Field(min_length=1, max_length=MAX_EXECUTION_REPLAN_ITEMS)
    ]

    @property
    def sha256(self) -> str:
        return digest(self)

    def plan(self, version: int) -> Plan:
        return Plan(version=version, items=self.items)


class ExecutionReplanRecord(Contract):
    """Replayable provenance for an accepted execution-time Plan revision."""

    source_model_call_id: Identifier
    source_tool_call_id: Identifier
    proposal_hash: Sha256
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    old_plan_version: PositiveInt
    old_plan_hash: Sha256
    new_plan_version: PositiveInt
    new_plan_hash: Sha256
    workspace_revision: Sha256
    preserved_work_item_ids: tuple[Identifier, ...] = ()


def check_execution_replan(
    old_plan: Plan,
    new_plan: Plan,
    passed_items: set[str],
    task: TaskSpec,
    *,
    enforce_unique_acceptance_ownership: bool = True,
) -> None:
    """Enforce the intentionally narrow first runtime-replanning contract."""

    if new_plan.version != old_plan.version + 1:
        raise PolicyDenied("Execution replan version must advance by exactly one")
    if len(new_plan.items) > MAX_EXECUTION_REPLAN_ITEMS:
        raise PolicyDenied("Execution replan exceeds the bounded work-item count")
    new_plan.check_task(
        task,
        enforce_unique_acceptance_ownership=enforce_unique_acceptance_ownership,
    )
    old_items = {item.work_item_id: item for item in old_plan.items}
    new_items = {item.work_item_id: item for item in new_plan.items}
    if not passed_items <= old_items.keys():
        raise PolicyDenied("Passed work items do not belong to the current Plan")
    if any(new_items.get(item_id) != old_items[item_id] for item_id in passed_items):
        raise PolicyDenied("Execution replan must preserve every passed WorkItem verbatim")
    if old_plan.items == new_plan.items:
        raise PolicyDenied("Execution replan must change the remaining Plan structure")
    if len(passed_items) == len(new_plan.items) or not new_plan.ready_items(passed_items):
        raise PolicyDenied("Execution replan must leave a dependency-ready incomplete WorkItem")
