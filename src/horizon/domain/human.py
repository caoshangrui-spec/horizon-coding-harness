from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import Field, TypeAdapter, model_validator

from horizon.domain.common import Contract
from horizon.domain.promotion import Sha256
from horizon.domain.task import Identifier, PositiveInt
from horizon.domain.tools import ToolCallRecord

NoProgressPattern = Literal[
    "identical_action",
    "alternating_two_action_cycle",
    "periodic_action_cycle",
]

NO_PROGRESS_GUARDED_TOOLS = frozenset(
    {
        "search_repo",
        "read_file",
        "retrieve_code",
        "replace_text",
        "apply_patch",
        "create_file",
    }
)
ALTERNATING_SOFT_BLOCK_LENGTH = 5
ALTERNATING_HARD_STOP_LENGTH = 6
MIN_PERIODIC_CYCLE_PERIOD = 3
MAX_EXACT_CYCLE_PERIOD = 4
EXACT_CYCLE_REPETITIONS = 3


@dataclass(frozen=True)
class NoProgressDecision:
    """Exact, revision-bound controller decision for the next tool action."""

    pattern: NoProgressPattern | None = None
    hard_stop: bool = False
    identical_prior_count: int = 0
    alternating_window: tuple[tuple[str, str], ...] = ()
    cycle_period: int | None = None
    cycle_window: tuple[tuple[str, str], ...] = ()


def _exact_cycle_window(
    signatures: tuple[tuple[str, str], ...],
    period: int,
    size: int,
) -> tuple[tuple[str, str], ...] | None:
    if len(signatures) < size:
        return None
    window = signatures[-size:]
    seed = window[:period]
    expected = (seed * ((size + period - 1) // period))[:size]
    if window != expected:
        return None
    # Report the smallest exact period only. This keeps A/B repetition classified as the
    # historical period-2 pattern instead of also labelling it period 4.
    for smaller in range(1, period):
        smaller_expected = (window[:smaller] * ((size + smaller - 1) // smaller))[:size]
        if window == smaller_expected:
            return None
    return window


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
    for period in range(2, MAX_EXACT_CYCLE_PERIOD + 1):
        hard_length = (
            ALTERNATING_HARD_STOP_LENGTH if period == 2 else period * EXACT_CYCLE_REPETITIONS
        )
        soft_length = ALTERNATING_SOFT_BLOCK_LENGTH if period == 2 else hard_length - 1
        for size, hard_stop in ((hard_length, True), (soft_length, False)):
            window = _exact_cycle_window(signatures, period, size)
            if window is None:
                continue
            if period == 2:
                return NoProgressDecision(
                    pattern="alternating_two_action_cycle",
                    hard_stop=hard_stop,
                    identical_prior_count=identical_prior_count,
                    alternating_window=window,
                    cycle_window=window,
                )
            return NoProgressDecision(
                pattern="periodic_action_cycle",
                hard_stop=hard_stop,
                identical_prior_count=identical_prior_count,
                cycle_period=period,
                cycle_window=window,
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
    cycle_period: (
        Annotated[
            int,
            Field(ge=MIN_PERIODIC_CYCLE_PERIOD, le=MAX_EXACT_CYCLE_PERIOD),
        ]
        | None
    ) = None
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

    @model_validator(mode="after")
    def validate_cycle_period(self) -> Self:
        if (self.pattern == "periodic_action_cycle") != (self.cycle_period is not None):
            raise ValueError("Only a periodic action-cycle request carries its exact period")
        return self


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
    cycle_period: int | None = None,
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
        if cycle_period is not None:
            return False
        if "identical tool name and arguments" not in detail:
            return False
        window = recent[-2:]
        if len(window) != 2 or any(record.status != "error" for record in window):
            return False
        signatures = {(record.name, record.arguments_hash) for record in window}
        if len(signatures) != 1:
            return False
    elif pattern == "alternating_two_action_cycle":
        if cycle_period is not None:
            return False
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
    elif pattern == "periodic_action_cycle":
        if (
            cycle_period is None
            or not MIN_PERIODIC_CYCLE_PERIOD <= cycle_period <= MAX_EXACT_CYCLE_PERIOD
            or f"exact period-{cycle_period} action cycle" not in detail
        ):
            return False
        window = recent[-cycle_period * EXACT_CYCLE_REPETITIONS :]
        if len(window) != cycle_period * EXACT_CYCLE_REPETITIONS or any(
            record.status != "error" for record in window[-2:]
        ):
            return False
        signatures_in_order = tuple((record.name, record.arguments_hash) for record in window)
        if (
            _exact_cycle_window(
                signatures_in_order,
                cycle_period,
                cycle_period * EXACT_CYCLE_REPETITIONS,
            )
            != signatures_in_order
        ):
            return False
        signatures = set(signatures_in_order[:cycle_period])
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
