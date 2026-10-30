from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StrictInt, model_validator

from horizon.domain.common import Contract, digest
from horizon.domain.model import ModelRequestBudgetEvidence


class HorizonError(Exception):
    """A user-visible, non-secret control-plane error."""


class Conflict(HorizonError):
    pass


class NotFound(HorizonError):
    pass


class InvalidTransition(HorizonError):
    pass


class IntegrityError(HorizonError):
    pass


class PolicyDenied(HorizonError):
    pass


class BudgetStopReason(StrEnum):
    RUN_MODEL_COST_LIMIT = "run_model_cost_limit"
    CAMPAIGN_CALL_COST_LIMIT = "campaign_call_cost_limit"
    CAMPAIGN_COST_LIMIT = "campaign_cost_limit"


class ContextStopReason(StrEnum):
    PROTECTED_PREFIX_LIMIT = "protected_prefix_exceeds_limit"
    INCOMPLETE_TOOL_TURN_LIMIT = "incomplete_tool_turn_exceeds_limit"


class ContextCapacityStop(Contract):
    """Persistable evidence that the smallest safe projection still cannot fit."""

    schema_version: Literal[1] = 1
    reason_code: ContextStopReason
    max_context_chars: Annotated[StrictInt, Field(ge=2_000, le=1_000_000)]
    max_input_tokens: Annotated[StrictInt, Field(ge=2_000, le=2_000_000)]
    required_context_chars: Annotated[StrictInt, Field(ge=1)]
    required_input_tokens: Annotated[StrictInt, Field(ge=1)]
    source_message_count: Annotated[StrictInt, Field(ge=2)]
    attempted_projected_message_count: Annotated[StrictInt, Field(ge=2)]
    compacted_unit_count: Annotated[StrictInt, Field(ge=0)]
    incomplete_unit_count: Annotated[StrictInt, Field(ge=0)]
    source_digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

    @model_validator(mode="after")
    def valid_stop(self):
        if (
            self.required_context_chars <= self.max_context_chars
            and self.required_input_tokens <= self.max_input_tokens
        ):
            raise ValueError("Context stop requires at least one exceeded projection limit")
        if self.attempted_projected_message_count > self.source_message_count + 1:
            raise ValueError("Context stop projection cannot add more than its summary message")
        if (self.reason_code == ContextStopReason.INCOMPLETE_TOOL_TURN_LIMIT) != (
            self.incomplete_unit_count > 0
        ):
            raise ValueError("Context stop reason does not match its incomplete-turn evidence")
        return self

    @property
    def sha256(self) -> str:
        return digest(self)


class BudgetStop(Contract):
    """Persistable evidence for a deterministic pre-dispatch monetary stop."""

    reason_code: BudgetStopReason
    scope: Literal["run", "campaign"]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    required_cost: Annotated[Decimal, Field(gt=0)]
    available_cost: Annotated[Decimal, Field(ge=0)]

    @model_validator(mode="after")
    def valid_stop(self):
        expected_scope = (
            "run" if self.reason_code == BudgetStopReason.RUN_MODEL_COST_LIMIT else "campaign"
        )
        if self.scope != expected_scope:
            raise ValueError("Budget stop reason does not match its scope")
        if self.required_cost <= self.available_cost:
            raise ValueError("Budget stop requires cost above the available amount")
        return self


class BudgetExceeded(HorizonError):
    def __init__(
        self,
        message: str,
        *,
        stop: BudgetStop | None = None,
        model_request_budget: ModelRequestBudgetEvidence | None = None,
    ):
        self.stop = stop
        self.model_request_budget = model_request_budget
        super().__init__(message)


class ContextWindowExceeded(Conflict):
    """A safe context projection is impossible under the configured hard limits."""

    def __init__(self, stop: ContextCapacityStop):
        self.stop = stop
        super().__init__(
            "Protected prefix or incomplete Agent turn exceeds the context projection "
            "character/token budget"
        )


class RunDeadlineExceeded(BudgetExceeded):
    """The persisted wall-clock deadline prevents any new Run work."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        super().__init__(f"Run {run_id} wall-clock deadline has expired; downtime is not refunded")


class RunCancellationRequested(InvalidTransition):
    """A durable cancellation fence prevents any new Run work."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        super().__init__(f"Run {run_id} cancellation has been requested; no new work may start")


class LeaseConflict(Conflict):
    pass


class PlanProposalError(Conflict):
    """A settled planner response did not satisfy the controller's Plan contract."""


class ProviderConfigurationError(HorizonError):
    """A provider configuration or local credential reference is invalid."""


class ProviderError(HorizonError):
    """A provider request was dispatched but did not produce a trusted response."""


class ProviderConnectionError(ProviderError):
    pass


class ProviderHTTPError(ProviderError):
    def __init__(self, status_code: int, *, retryable: bool):
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(f"Provider returned HTTP {status_code}")


class ProviderProtocolError(ProviderError):
    pass
