from __future__ import annotations

from decimal import Decimal
from typing import Any

from horizon.application.model_probe import conservative_input_sizing, legacy_input_estimate
from horizon.domain.model import (
    FunctionCall,
    ModelMessage,
    ModelRequest,
    ToolCall,
    ToolDefinition,
)


def _boundary_requests(
    model: str,
    *,
    max_output_tokens: int,
    enable_thinking: bool,
) -> tuple[tuple[str, str, ModelRequest], ...]:
    shared = {
        "model": model,
        "max_output_tokens": max_output_tokens,
        "temperature": Decimal("0"),
        "enable_thinking": enable_thinking,
    }
    lookup_tool = ToolDefinition(
        name="lookup_symbol",
        description="Return bounded source evidence for one symbol.",
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 8,
                },
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    )
    tool_call = ToolCall(
        id="call-boundary-1",
        function=FunctionCall(
            name=lookup_tool.name,
            arguments={"symbol": "解析器", "paths": ["src/parser.py", "tests/test_parser.py"]},
        ),
    )
    return (
        (
            "minimal_ascii",
            "One short user message without tools.",
            ModelRequest(
                **shared,
                messages=(ModelMessage(role="user", content="Return OK."),),
            ),
        ),
        (
            "unicode_messages",
            "System and user messages containing multibyte text and emoji.",
            ModelRequest(
                **shared,
                messages=(
                    ModelMessage(role="system", content="你是代码审查助手。"),
                    ModelMessage(role="user", content="检查解析器边界：空值、换行与 emoji 🧪。"),
                ),
            ),
        ),
        (
            "nested_tool_schema",
            "A nested function schema with required tool choice.",
            ModelRequest(
                **shared,
                messages=(ModelMessage(role="user", content="Locate the parser implementation."),),
                tools=(lookup_tool,),
                tool_choice="required",
            ),
        ),
        (
            "tool_call_arguments",
            "An assistant tool call with nested, multibyte JSON-in-JSON arguments.",
            ModelRequest(
                **shared,
                messages=(
                    ModelMessage(role="system", content="Use repository evidence."),
                    ModelMessage(role="user", content="Find the parser."),
                    ModelMessage(role="assistant", tool_calls=(tool_call,)),
                    ModelMessage(
                        role="tool",
                        tool_call_id=tool_call.id,
                        content='{"path":"src/parser.py","line":42,"status":"命中"}',
                    ),
                ),
                tools=(lookup_tool,),
                tool_choice="auto",
            ),
        ),
        (
            "large_tool_result",
            "A bounded 8 KiB tool result representing a large observation turn.",
            ModelRequest(
                **shared,
                messages=(
                    ModelMessage(role="system", content="Use repository evidence."),
                    ModelMessage(role="user", content="Inspect a large source excerpt."),
                    ModelMessage(role="assistant", tool_calls=(tool_call,)),
                    ModelMessage(
                        role="tool",
                        tool_call_id=tool_call.id,
                        content="x" * 8_192,
                    ),
                ),
                tools=(lookup_tool,),
                tool_choice="auto",
            ),
        ),
    )


def analyze_model_request_sizing(
    model: str,
    *,
    max_output_tokens: int,
    enable_thinking: bool,
) -> dict[str, Any]:
    """Replay deterministic request shapes through the production wire encoder without I/O."""

    cases = []
    for case_id, description, request in _boundary_requests(
        model,
        max_output_tokens=max_output_tokens,
        enable_thinking=enable_thinking,
    ):
        estimate, payload = conservative_input_sizing(request)
        legacy = legacy_input_estimate(request)
        cases.append(
            {
                "case_id": case_id,
                "description": description,
                "request_hash": request.sha256,
                "message_count": len(request.messages),
                "tool_count": len(request.tools),
                "legacy_domain_request_bytes": legacy.request_bytes,
                "wire_minus_legacy_bytes": payload.payload_bytes - legacy.request_bytes,
                "request_payload": payload.model_dump(mode="json"),
                "production_input_estimate": estimate.model_dump(mode="json"),
                "candidate_input_token_ceiling": payload.payload_bytes + 1024,
            }
        )
    payload_sizes = [case["request_payload"]["payload_bytes"] for case in cases]
    return {
        "schema_version": 1,
        "model": model,
        "max_output_tokens": max_output_tokens,
        "enable_thinking": enable_thinking,
        "case_count": len(cases),
        "payload_bytes": {"min": min(payload_sizes), "max": max(payload_sizes)},
        "cases": cases,
        "production_change": {
            "request_byte_basis_changed": True,
            "token_ceiling_formula_changed": False,
            "automatic_candidate_promotion": False,
        },
        "limitations": {
            "provider_token_usage_observed": False,
            "candidate_safety_conclusion": False,
            "synthetic_cases_are_not_real_task_quality_evidence": True,
        },
        "network_called": False,
        "paid_model_called": False,
    }
