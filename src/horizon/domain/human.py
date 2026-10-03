from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from horizon.domain.common import Contract
from horizon.domain.promotion import Sha256
from horizon.domain.task import Identifier, PositiveInt
from horizon.domain.tools import ToolCallRecord

NoProgressPattern = Literal["identical_action", "alternating_two_action_cycle"]

NO_PROGRESS_GUARDED_TOOLS = frozenset(
    {"search_repo", "read_file", "retrieve_code", "replace_text", "apply_patch"}
)
ALTERNATING_SOFT_BLOCK_LENGTH = 5
ALTERNATING_HARD_STOP_LENGTH = 6


@dataclass(frozen=True)
class NoProgressDecision:
    """Exact, revision-bound controller decision for the next tool action."""

    pattern: NoProgressPattern | None = None
    hard_stop: bool = False
    identical_prior_count: int = 0
    alternating_window: tuple[tuple[str, str], ...] = ()


def classify_no_progress(
    records: Sequence[ToolCallRecord],
    reset_tool_count: int,
    *,
    name: str,
    arguments_hash: str,
    workspace_revision: str,
    max_identical_actions: int,
) -> NoProgressDecision:
    """Classify an exact repeated action without guessing semantic equivalence."""

    if reset_tool_count < 0 or reset_tool_count > len(records):
        raise ValueError("No-progress reset boundary exceeds the tool history")
    if max_identical_actions < 1:
        raise ValueError("No-progress identical-action limit must be positive")
    if name not in NO_PROGRESS_GUARDED_TOOLS:
        return NoProgressDecision()

    unchanged_tail: list[ToolCallRecord] = []
    for record in reversed(records[reset_tool_count:]):
        if (
            record.name not in NO_PROGRESS_GUARDED_TOOLS
            or record.workspace_revision_before != workspace_revision
            or record.workspace_revision_after != workspace_revision
        ):
            break
        unchanged_tail.append(record)
    unchanged_tail.reverse()

    identical_prior_count = 0
    for record in reversed(unchanged_tail):
        if record.name != name or record.arguments_hash != arguments_hash:
            break
        identical_prior_count += 1
    if identical_prior_count >= max_identical_actions:
        return NoProgressDecision(
            pattern="identical_action",
            hard_stop=identical_prior_count > max_identical_actions,
            identical_prior_count=identical_prior_count,
        )

    signatures = tuple(
        [(record.name, record.arguments_hash) for record in unchanged_tail]
        + [(name, arguments_hash)]
    )
    for size, hard_stop in (
        (ALTERNATING_HARD_STOP_LENGTH, True),
        (ALTERNATING_SOFT_BLOCK_LENGTH, False),
    ):
        if len(signatures) < size:
            continue
        window = signatures[-size:]
        first, second = window[:2]
        expected = (first, second) * (size // 2) + ((first,) if size % 2 else ())
        if first != second and window == expected:
            return NoProgressDecision(
                pattern="alternating_two_action_cycle",
                hard_stop=hard_stop,
                identical_prior_count=identical_prior_count,
                alternating_window=window,
            )
    return NoProgressDecision(identical_prior_count=identical_prior_count)


class HumanPlanRequest(Contract):
    """A narrow, durable request for a human replacement of an invalid generated Plan."""

    request_id: Identifier
    kind: Literal["replacement_plan_required"] = "replacement_plan_required"
    reason_code: Literal["invalid_generated_plan"] = "invalid_generated_plan"
    detail: Annotated[str, Field(min_length=1, max_length=2000)]
    source_model_call_id: Identifier
    response_artifact_ref: Sha256
    task_spec_hash: Sha256
    requested_plan_version: PositiveInt
    resume_state: Literal["PLANNING"] = "PLANNING"


class HumanPlanDecision(Contract):
    """The trusted local-control decision that supplies a replacement Plan."""

    decision_id: Identifier
    request_id: Identifier
    kind: Literal["replacement_plan_supplied"] = "replacement_plan_supplied"
    actor: Literal["local_cli"] = "local_cli"
    plan_hash: Sha256
    task_spec_hash: Sha256


class HumanGuidanceRequest(Contract):
    """A bounded request for operator guidance after evidence-backed execution stagnation."""

    request_id: Identifier
    kind: Literal["operator_guidance_required"] = "operator_guidance_required"
    reason_code: Literal["repeated_action_no_progress"] = "repeated_action_no_progress"
    pattern: NoProgressPattern = "identical_action"
    detail: Annotated[str, Field(min_length=1, max_length=2000)]
    source_tool_call_id: Identifier
    evidence_artifact_ref: Sha256
    task_spec_hash: Sha256
    plan_version: PositiveInt
    plan_hash: Sha256
    work_item_id: Identifier
    workspace_revision: Sha256
    agent_session_artifact_ref: Sha256
    next_iteration: PositiveInt
    resume_state: Literal["RUNNING"] = "RUNNING"


class HumanGuidanceDecision(Contract):
    """A local operator decision bound to the exact guided Agent session artifact."""

    decision_id: Identifier
    request_id: Identifier
    kind: Literal["operator_guidance_supplied"] = "operator_guidance_supplied"
    actor: Literal["local_cli"] = "local_cli"
    guided_session_artifact_ref: Sha256
    task_spec_hash: Sha256
    plan_version: PositiveInt
    workspace_revision: Sha256
    next_iteration: PositiveInt


HumanRequest = HumanPlanRequest | HumanGuidanceRequest
HumanDecision = HumanPlanDecision | HumanGuidanceDecision

_REQUEST_ADAPTER = TypeAdapter(HumanRequest)
_DECISION_ADAPTER = TypeAdapter(HumanDecision)


def parse_human_request(value) -> HumanRequest:
    return _REQUEST_ADAPTER.validate_python(value)


def parse_human_decision(value) -> HumanDecision:
    return _DECISION_ADAPTER.validate_python(value)


def matches_no_progress_evidence(
    records: Sequence[ToolCallRecord],
    reset_tool_count: int,
    pattern: NoProgressPattern,
    source_tool_call_id: str,
    evidence_artifact_ref: str | None,
    expected_workspace_revision: str,
    detail: str,
) -> bool:
    """Verify the exact receipt shape that can justify operator guidance."""

    if reset_tool_count < 0 or reset_tool_count > len(records):
        return False
    recent = records[reset_tool_count:]
    if not recent:
        return False
    latest = recent[-1]
    if (
        latest.call_id != source_tool_call_id
        or latest.status != "error"
        or latest.artifact_ref is None
        or latest.artifact_ref != evidence_artifact_ref
        or hashlib.sha256(detail.encode("utf-8")).hexdigest() != evidence_artifact_ref
    ):
        return False

    if pattern == "identical_action":
        if "identical tool name and arguments" not in detail:
            return False
        window = recent[-2:]
        if len(window) != 2 or any(record.status != "error" for record in window):
            return False
        signatures = {(record.name, record.arguments_hash) for record in window}
        if len(signatures) != 1:
            return False
    elif pattern == "alternating_two_action_cycle":
        if "exact alternating two-action cycle" not in detail:
            return False
        window = recent[-6:]
        if len(window) != 6 or any(record.status != "error" for record in window[-2:]):
            return False
        signatures_in_order = [(record.name, record.arguments_hash) for record in window]
        first, second = signatures_in_order[:2]
        if first == second or signatures_in_order != [first, second] * 3:
            return False
        signatures = {first, second}
    else:
        return False

    revision = window[-1].workspace_revision_before
    return (
        revision == expected_workspace_revision
        and all(name in NO_PROGRESS_GUARDED_TOOLS for name, _ in signatures)
        and all(
            record.workspace_revision_before == revision
            and record.workspace_revision_after == revision
            for record in window
        )
    )
