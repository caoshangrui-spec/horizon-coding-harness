from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import Field, StrictInt, field_validator, model_validator

from horizon.domain.common import Contract, digest
from horizon.domain.task import Identifier, PositiveInt, Text

Currency = Literal["CNY", "USD"]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
NonNegativeMoney = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
PositiveMoney = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


class FunctionCall(Contract):
    name: Identifier
    arguments: dict[str, Any]


class ToolCall(Contract):
    id: Text
    type: Literal["function"] = "function"
    function: FunctionCall


class ModelMessage(Contract):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def check_role_shape(self) -> Self:
        if self.role in {"system", "user"} and (self.content is None or self.tool_calls):
            raise ValueError(
                "System and user messages require content and cannot contain tool calls"
            )
        if self.role == "assistant" and self.content is None and not self.tool_calls:
            raise ValueError("Assistant messages require content or tool calls")
        if self.role == "tool" and (self.content is None or not self.tool_call_id):
            raise ValueError("Tool messages require content and tool_call_id")
        if self.role != "tool" and self.tool_call_id is not None:
            raise ValueError("Only tool messages may carry tool_call_id")
        if self.role != "assistant" and self.tool_calls:
            raise ValueError("Only assistant messages may contain tool calls")
        return self


class ToolDefinition(Contract):
    name: Identifier
    description: Text
    parameters: dict[str, Any]

    @field_validator("parameters")
    @classmethod
    def check_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("type") != "object" or not isinstance(value.get("properties", {}), dict):
            raise ValueError("Tool parameters must be a JSON Schema object")
        return value


class ModelRequest(Contract):
    model: Text
    messages: Annotated[tuple[ModelMessage, ...], Field(min_length=1)]
    tools: tuple[ToolDefinition, ...] = ()
    tool_choice: Literal["auto", "none", "required"] = "auto"
    max_output_tokens: PositiveInt
    temperature: Annotated[Decimal, Field(ge=0, le=2, allow_inf_nan=False)] = Decimal("0")
    enable_thinking: bool = False

    @property
    def sha256(self) -> str:
        return digest(self)


class ModelUsage(Contract):
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    cached_input_tokens: NonNegativeInt = 0
    reasoning_tokens: NonNegativeInt = 0

    @model_validator(mode="after")
    def check_subtotals(self) -> Self:
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("Cached input tokens cannot exceed total input tokens")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("Reasoning tokens cannot exceed total output tokens")
        return self


class ModelResponse(Contract):
    response_id: Text
    model: Text
    message: ModelMessage
    finish_reason: str | None = None
    usage: ModelUsage
    provider_trace_id: str | None = None


class PriceCard(Contract):
    currency: Currency
    input_per_million: NonNegativeMoney
    cached_input_per_million: NonNegativeMoney
    output_per_million: NonNegativeMoney
    version: Text
    source_url: Text
    conservative_ceiling: bool = True

    def cost_for(self, usage: ModelUsage) -> Decimal:
        regular = usage.input_tokens - usage.cached_input_tokens
        return (
            Decimal(regular) * self.input_per_million
            + Decimal(usage.cached_input_tokens) * self.cached_input_per_million
            + Decimal(usage.output_tokens) * self.output_per_million
        ) / Decimal(1_000_000)

    def reserve_cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        return self.cost_for(ModelUsage(input_tokens=input_tokens, output_tokens=output_tokens))


class CampaignBudget(Contract):
    campaign_id: Identifier
    currency: Currency
    max_cost: PositiveMoney
    max_cost_per_call: PositiveMoney

    @model_validator(mode="after")
    def check_per_call(self) -> Self:
        if self.max_cost_per_call > self.max_cost:
            raise ValueError("Per-call cost cannot exceed the campaign budget")
        return self


class CampaignSummary(Contract):
    campaign_id: Identifier
    currency: Currency
    max_cost: PositiveMoney
    settled_cost: NonNegativeMoney
    reserved_cost: NonNegativeMoney
    unknown_cost: NonNegativeMoney
    occupied_cost: NonNegativeMoney
    remaining_cost: NonNegativeMoney


class CampaignAttempt(Contract):
    attempt_id: Text
    campaign_id: Identifier
    request_hash: Text
    reserved_cost: PositiveMoney
    actual_cost: NonNegativeMoney | None = None
    status: Literal["reserved", "settled", "unknown"]
    provider_trace_id: str | None = None
    error_type: str | None = None

    @model_validator(mode="after")
    def check_status_shape(self) -> Self:
        if self.status == "settled" and self.actual_cost is None:
            raise ValueError("Settled campaign attempts require actual_cost")
        if self.status != "settled" and self.actual_cost is not None:
            raise ValueError("Only settled campaign attempts may carry actual_cost")
        if self.status == "unknown" and not self.error_type:
            raise ValueError("Unknown campaign attempts require error_type")
        return self


class ModelProbeResult(Contract):
    passed: bool
    provider_id: Identifier
    model: Text
    response_id: Text
    finish_reason: str | None
    provider_trace_id: str | None
    tool_name: str | None
    usage: ModelUsage
    estimated_cost: NonNegativeMoney
    currency: Currency
    campaign: CampaignSummary


class ModelPolicyBinding(Contract):
    policy_id: Identifier
    provider_id: Identifier
    model: Text
    campaign_id: Identifier
    currency: Currency
    max_run_cost: PositiveMoney
    price_card_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class ModelCallReservation(Contract):
    call_id: Identifier
    purpose: Literal["execution", "planning"] = "execution"
    request_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    provider_id: Identifier
    model: Text
    currency: Currency
    reserved_cost: PositiveMoney
    context_projection_ref: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    mandatory_facts_ref: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    mandatory_facts_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    run_memory_ref: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    run_memory_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    planning_context_ref: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    planning_context_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    run_memory_covered_event_seq: NonNegativeInt = 0
    run_memory_entry_count: NonNegativeInt = 0
    source_message_count: NonNegativeInt = 0
    projected_message_count: NonNegativeInt = 0

    @model_validator(mode="after")
    def check_context_projection(self) -> Self:
        counts = (self.source_message_count, self.projected_message_count)
        if self.context_projection_ref is None and counts != (0, 0):
            raise ValueError("Context message counts require a projection artifact")
        if self.context_projection_ref is not None and (
            self.source_message_count < 2
            or self.projected_message_count < 2
            or self.projected_message_count > self.source_message_count
        ):
            raise ValueError("Context projection message counts are inconsistent")
        if (self.mandatory_facts_ref is None) != (self.mandatory_facts_hash is None):
            raise ValueError("Mandatory facts require both an artifact reference and hash")
        if self.mandatory_facts_ref is not None and (
            self.context_projection_ref is None
            or self.mandatory_facts_ref != self.mandatory_facts_hash
        ):
            raise ValueError("Mandatory facts must be content addressed and projection bound")
        if (self.run_memory_ref is None) != (self.run_memory_hash is None):
            raise ValueError("Run memory requires both an artifact reference and hash")
        if self.run_memory_ref is None and (
            self.run_memory_covered_event_seq != 0 or self.run_memory_entry_count != 0
        ):
            raise ValueError("Run memory metadata requires a memory artifact")
        if self.run_memory_ref is not None and (
            self.context_projection_ref is None
            or self.run_memory_ref != self.run_memory_hash
            or self.run_memory_covered_event_seq < 1
        ):
            raise ValueError("Run memory must be content addressed and projection bound")
        if (self.planning_context_ref is None) != (self.planning_context_hash is None):
            raise ValueError("Planning context requires both an artifact reference and hash")
        if self.purpose == "planning":
            if (
                self.planning_context_ref is None
                or self.planning_context_ref != self.planning_context_hash
                or self.context_projection_ref is not None
                or self.mandatory_facts_ref is not None
                or self.run_memory_ref is not None
            ):
                raise ValueError(
                    "Planning calls require one content-addressed planning context only"
                )
        elif self.planning_context_ref is not None:
            raise ValueError("Execution calls cannot carry a planning context")
        return self


class ModelCallRecord(Contract):
    call_id: Identifier
    purpose: Literal["execution", "planning"] = "execution"
    request_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    provider_id: Identifier
    model: Text
    currency: Currency
    estimated_cost: NonNegativeMoney
    response_id: Text
    response_artifact_ref: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    provider_trace_id: str | None = None
    finish_reason: str | None = None
    usage: ModelUsage
