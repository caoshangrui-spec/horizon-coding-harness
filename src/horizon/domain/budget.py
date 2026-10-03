from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import Field

from horizon.domain.common import Contract
from horizon.domain.errors import BudgetExceeded
from horizon.domain.task import BudgetSpec, NonNegativeInt


class Usage(Contract):
    input_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0
    cost_usd: Annotated[Decimal, Field(ge=0)] = Decimal("0")
    model_calls: NonNegativeInt = 0
    tool_calls: NonNegativeInt = 0
    steps: NonNegativeInt = 0
    repair_cycles: NonNegativeInt = 0

    def plus(self, other: Usage) -> Usage:
        return Usage(
            **{key: getattr(self, key) + getattr(other, key) for key in type(self).model_fields}
        )

    def exceeded(self, limits: BudgetSpec) -> tuple[str, ...]:
        return tuple(
            key
            for key in type(self).model_fields
            if getattr(self, key) > getattr(limits, f"max_{key}")
        )

    def check(self, limits: BudgetSpec) -> None:
        exceeded = self.exceeded(limits)
        if exceeded:
            raise BudgetExceeded(f"Hard budget would be exceeded: {', '.join(exceeded)}")
