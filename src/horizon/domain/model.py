from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import Field, StrictInt, field_validator, model_validator

from horizon.domain.common import Contract, canonical_json, digest
from horizon.domain.task import Identifier, PositiveInt, Text

Currency = Literal["CNY", "USD"]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
NonNegativeMoney = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
PositiveMoney = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
CONSERVATIVE_INPUT_TOKEN_ESTIMATOR = "request_utf8_bytes_x2_plus_1024_v1"
OPENAI_PAYLOAD_INPUT_TOKEN_ESTIMATOR = "openai_payload_utf8_bytes_x2_plus_1024_v2"
OPENAI_COMPATIBLE_PAYLOAD_ENCODING = "openai_compatible_canonical_json_v1"


class ModelRequestPayloadEvidence(Contract):
    """Exact byte composition of the canonical OpenAI-compatible request body."""

    schema_version: Literal[1] = 1
    encoding: Literal["openai_compatible_canonical_json_v1"] = OPENAI_COMPATIBLE_PAYLOAD_ENCODING
    payload_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    payload_bytes: PositiveInt
    field_value_bytes: dict[str, NonNegativeInt]
    json_structure_bytes: NonNegativeInt

    @model_validator(mode="after")
    def check_composition(self) -> Self:
        required = {
            "model",
            "messages",
            "stream",
            "max_tokens",
            "temperature",
            "enable_thinking",
        }
        optional = {"tools", "tool_choice"}
        fields = set(self.field_value_bytes)
        if not required <= fields or fields - required - optional:
            raise ValueError("Request payload evidence has an invalid top-level field set")
        if ("tools" in fields) != ("tool_choice" in fields):
            raise ValueError(
                "Tools and tool choice must appear together in request payload evidence"
            )
        if self.payload_bytes != sum(self.field_value_bytes.values()) + self.json_structure_bytes:
            raise ValueError("Request payload byte composition does not sum to its total")
        return self


class InputTokenEstimate(Contract):
    """Deterministic upper bound for one complete provider request."""

    schema_version: Literal[1] = 1
    estimator: Literal[
        "request_utf8_bytes_x2_plus_1024_v1",
        "openai_payload_utf8_bytes_x2_plus_1024_v2",
    ] = CONSERVATIVE_INPUT_TOKEN_ESTIMATOR
    request_bytes: NonNegativeInt
    token_ceiling: PositiveInt

    @model_validator(mode="after")
    def check_ceiling(self) -> Self:
        if self.token_ceiling != 2 * self.request_bytes + 1024:
            raise ValueError("Input token ceiling does not match the declared estimator")
        return self


class InputTokenBudget(Contract):
    """Configured request ceiling paired with the estimate that must fit it."""

    schema_version: Literal[1] = 1
    max_input_tokens: Annotated[StrictInt, Field(ge=2_000, le=2_000_000)]
    estimate: InputTokenEstimate

    @model_validator(mode="after")
    def check_limit(self) -> Self:
        if self.estimate.token_ceiling > self.max_input_tokens:
            raise ValueError("Input token ceiling exceeds the configured request budget")
        return self


class ModelRequestBudgetEvidence(Contract):
    """Pre-dispatch request sizing persisted when a monetary gate stops a model call."""

    schema_version: Literal[1] = 1
    call_id: Identifier
    purpose: Literal["execution", "planning"]
    request_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    input_token_budget: InputTokenBudget
    output_token_ceiling: PositiveInt
    request_payload: ModelRequestPayloadEvidence | None = None

    @model_validator(mode="after")
    def check_payload_binding(self) -> Self:
        _check_request_payload_binding(self.input_token_budget.estimate, self.request_payload)
        return self

    def as_dict(self) -> dict[str, Any]:
        result = self.model_dump(mode="json")
        if self.request_payload is None:
            result.pop("request_payload")
        return result


def _check_request_payload_binding(
    estimate: InputTokenEstimate,
    payload: ModelRequestPayloadEvidence | None,
) -> None:
    if estimate.estimator == OPENAI_PAYLOAD_INPUT_TOKEN_ESTIMATOR:
        if payload is None or payload.payload_bytes != estimate.request_bytes:
            raise ValueError("OpenAI payload estimator requires matching request payload evidence")
    elif payload is not None:
        raise ValueError("Legacy request estimator cannot carry OpenAI payload evidence")


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


def _openai_compatible_message_payload(message: ModelMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": canonical_json(call.function.arguments),
                },
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    return payload


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

    def openai_compatible_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_openai_compatible_message_payload(message) for message in self.messages],
            "stream": False,
            "max_tokens": self.max_output_tokens,
            "temperature": float(self.temperature),
            "enable_thinking": self.enable_thinking,
        }
        if self.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in self.tools
            ]
            payload["tool_choice"] = self.tool_choice
        return payload

    def openai_compatible_body(self) -> bytes:
        return canonical_json(self.openai_compatible_payload()).encode("utf-8")

    def openai_compatible_payload_evidence(self) -> ModelRequestPayloadEvidence:
        payload = self.openai_compatible_payload()
        body = canonical_json(payload).encode("utf-8")
        field_value_bytes = {
            key: len(canonical_json(value).encode("utf-8")) for key, value in payload.items()
        }
        return ModelRequestPayloadEvidence(
            payload_sha256=digest(payload),
            payload_bytes=len(body),
            field_value_bytes=field_value_bytes,
            json_structure_bytes=len(body) - sum(field_value_bytes.values()),
        )


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
    input_token_budget: InputTokenBudget | None = None
    request_payload: ModelRequestPayloadEvidence | None = None

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
        if self.input_token_budget is None:
            if self.request_payload is not None:
                raise ValueError("Request payload evidence requires an input-token estimate")
        else:
            _check_request_payload_binding(self.input_token_budget.estimate, self.request_payload)
        return self

    def as_dict(self) -> dict[str, Any]:
        """Preserve the pre-token-budget wire shape for historical reservations."""

        result = self.model_dump(mode="json")
        if self.input_token_budget is None:
            result.pop("input_token_budget")
        if self.request_payload is None:
            result.pop("request_payload")
        return result

    def budget_evidence(self, output_token_ceiling: int) -> ModelRequestBudgetEvidence:
        if self.input_token_budget is None:
            raise ValueError("Budget evidence requires an input-token estimate")
        return ModelRequestBudgetEvidence(
            call_id=self.call_id,
            purpose=self.purpose,
            request_hash=self.request_hash,
            input_token_budget=self.input_token_budget,
            output_token_ceiling=output_token_ceiling,
            request_payload=self.request_payload,
        )


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
