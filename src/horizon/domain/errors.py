from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from horizon.domain.common import Contract
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
