from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, model_validator

from horizon.domain.common import Contract
from horizon.domain.errors import (
    ProviderConnectionError,
    ProviderError,
    ProviderHTTPError,
    ProviderProtocolError,
)

RetryAction = Literal["retry", "stop"]
FailureCategory = Literal[
    "rate_limited",
    "transient_http",
    "connection",
    "authentication",
    "configuration",
    "protocol",
    "provider_error",
]
DispatchState = Literal["not_dispatched", "unknown"]
CostState = Literal["not_incurred", "unknown"]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
PositiveInt = Annotated[StrictInt, Field(gt=0)]


class RetryPolicy(Contract):
    """One logical operation's complete retry budget.

    ``transport_attempts_per_dispatch`` is intentionally fixed at one. A transport SDK
    cannot multiply the controller's total-attempt bound with an inner retry loop.
    """

    max_total_attempts: Annotated[StrictInt, Field(ge=1, le=3)] = 1
    max_total_wait_seconds: Annotated[StrictInt, Field(ge=0, le=60)] = 60
    base_delay_seconds: Annotated[StrictInt, Field(ge=0, le=30)] = 1
    max_delay_seconds: Annotated[StrictInt, Field(ge=0, le=60)] = 30
    transport_attempts_per_dispatch: Literal[1] = 1

    @model_validator(mode="after")
    def check_delays(self) -> Self:
        if self.base_delay_seconds > self.max_delay_seconds:
            raise ValueError("Retry base delay cannot exceed its per-delay ceiling")
        return self


class RetryState(Contract):
    attempts_completed: PositiveInt
    total_wait_seconds: NonNegativeInt = 0


class ProviderFailure(Contract):
    category: FailureCategory
    transient: bool
    dispatch_state: DispatchState
    cost_state: CostState
    retry_after_seconds: Annotated[StrictInt, Field(ge=0, le=60)] | None = None

    @model_validator(mode="after")
    def check_retry_after(self) -> Self:
        if self.retry_after_seconds is not None and not self.transient:
            raise ValueError("Only transient failures may carry Retry-After")
        return self


class RetryDecision(Contract):
    action: RetryAction
    reason_code: Literal[
        "eligible",
        "non_retryable_failure",
        "unknown_dispatch_or_cost",
        "attempt_limit",
        "wait_limit",
        "budget_limit",
        "deadline_limit",
    ]
    delay_seconds: NonNegativeInt = 0
    next_attempt: PositiveInt | None = None

    @model_validator(mode="after")
    def check_shape(self) -> Self:
        if self.action == "retry" and self.next_attempt is None:
            raise ValueError("A retry decision requires the next attempt index")
        if self.action == "stop" and (self.next_attempt is not None or self.delay_seconds):
            raise ValueError("A stop decision cannot schedule another attempt")
        if (self.action == "retry") != (self.reason_code == "eligible"):
            raise ValueError("Only an eligible decision may schedule a retry")
        return self


def classify_provider_failure(error: ProviderError) -> ProviderFailure:
    """Classify an adapter failure without pretending dispatch or billing was absent."""

    category: FailureCategory
    transient = False
    if isinstance(error, ProviderHTTPError):
        if error.status_code == 429:
            category = "rate_limited"
        elif error.status_code in {401, 403}:
            category = "authentication"
        elif error.status_code in {404, 422}:
            category = "configuration"
        elif error.retryable:
            category = "transient_http"
        else:
            category = "provider_error"
        transient = error.retryable
    elif isinstance(error, ProviderConnectionError):
        category = "connection"
        transient = True
    elif isinstance(error, ProviderProtocolError):
        category = "protocol"
    else:
        category = "provider_error"

    # ProviderError means dispatch started but no trusted receipt exists. The existing
    # accounting contract therefore retains the reservation and refuses to infer zero cost.
    return ProviderFailure(
        category=category,
        transient=transient,
        dispatch_state="unknown",
        cost_state="unknown",
    )


def decide_retry(
    policy: RetryPolicy,
    state: RetryState,
    failure: ProviderFailure,
    *,
    budget_allows_next_attempt: bool,
    deadline_allows_wait: bool,
) -> RetryDecision:
    """Return a deterministic decision; this function never sleeps or dispatches."""

    if not failure.transient:
        return RetryDecision(action="stop", reason_code="non_retryable_failure")
    if failure.dispatch_state != "not_dispatched" or failure.cost_state != "not_incurred":
        return RetryDecision(action="stop", reason_code="unknown_dispatch_or_cost")
    if state.attempts_completed >= policy.max_total_attempts:
        return RetryDecision(action="stop", reason_code="attempt_limit")
    if not budget_allows_next_attempt:
        return RetryDecision(action="stop", reason_code="budget_limit")

    delay = (
        failure.retry_after_seconds
        if failure.retry_after_seconds is not None
        else min(
            policy.max_delay_seconds,
            policy.base_delay_seconds * (2 ** (state.attempts_completed - 1)),
        )
    )
    if state.total_wait_seconds + delay > policy.max_total_wait_seconds:
        return RetryDecision(action="stop", reason_code="wait_limit")
    if not deadline_allows_wait:
        return RetryDecision(action="stop", reason_code="deadline_limit")
    return RetryDecision(
        action="retry",
        reason_code="eligible",
        delay_seconds=delay,
        next_attempt=state.attempts_completed + 1,
    )
