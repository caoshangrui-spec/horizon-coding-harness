from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from horizon.application.model_probe import conservative_input_sizing, legacy_input_estimate
from horizon.domain.common import canonical_json
from horizon.domain.model import (
    FunctionCall,
    ModelMessage,
    ModelRequest,
    ToolCall,
    ToolDefinition,
)
from horizon.tools.gateway import tool_definitions

MAX_BOUNDED_TOOL_RESULT_CHARS = 8 * 1024


@dataclass(frozen=True)
class AgentExecutionSizingBoundary:
    """One exact, offline request at the configured Agent context boundary."""

    request: ModelRequest
    context_chars: int
    tool_result_chars: int


def build_agent_execution_sizing_boundary(
    model: str,
    *,
    allowed_tool_names: tuple[str, ...],
    max_context_chars: int,
    max_output_tokens: int,
    enable_thinking: bool,
) -> AgentExecutionSizingBoundary:
    """Build a deterministic post-tool Agent request without network or model access.

    The boundary uses the task's real tool schemas plus ``submit`` and fills one completed tool
    turn with at most 8 KiB of bounded output.  Its canonical message projection never exceeds
    ``max_context_chars``.  This is deliberately a sizing gate, not a prediction of model
    quality or of the exact first execution turn.
    """

    known = {definition.name: definition for definition in tool_definitions()}
    requested = set(allowed_tool_names) | {"submit"}
    unknown = requested - set(known)
    if unknown:
        raise ValueError(f"Unknown Agent tools in sizing boundary: {sorted(unknown)}")
    tools = tuple(definition for definition in known.values() if definition.name in requested)
    if not tools:
        raise ValueError("Agent execution sizing requires at least one tool")
    observed_tool = known.get("retrieve_code") if "retrieve_code" in requested else tools[0]
    boundary_arguments = {
        "search_repo": {"query": "sizing boundary"},
        "read_file": {"path": "src/example.py"},
        "retrieve_code": {"query": "sizing boundary"},
        "replace_text": {"path": "src/example.py", "old": "a", "new": "b"},
        "apply_patch": {"edits": [{"path": "src/example.py", "old": "a", "new": "b"}]},
        "create_file": {"path": "src/new_file.py", "content": ""},
        "run_check": {"check_id": "acceptance"},
        "submit": {"summary": "sizing boundary"},
    }
    tool_call = ToolCall(
        id="call-agent-sizing-boundary",
        function=FunctionCall(
            name=observed_tool.name,
            arguments=boundary_arguments[observed_tool.name],
        ),
    )

    def messages(result_chars: int) -> tuple[ModelMessage, ...]:
        return (
            ModelMessage(
                role="system",
                content="Use revision-bound repository evidence before editing.",
            ),
            ModelMessage(
                role="user",
                content="Complete the bounded coding task and preserve the acceptance contract.",
            ),
            ModelMessage(role="assistant", tool_calls=(tool_call,)),
            ModelMessage(
                role="tool",
                tool_call_id=tool_call.id,
                content="x" * result_chars,
            ),
        )

    def projected_chars(result_chars: int) -> int:
        return len(
            canonical_json([message.model_dump(mode="json") for message in messages(result_chars)])
        )

    minimum = projected_chars(0)
    if minimum > max_context_chars:
        raise ValueError("Configured context limit cannot hold one completed Agent tool turn")
    low = 0
    high = MAX_BOUNDED_TOOL_RESULT_CHARS
    while low < high:
        candidate = (low + high + 1) // 2
        if projected_chars(candidate) <= max_context_chars:
            low = candidate
        else:
            high = candidate - 1
    boundary_messages = messages(low)
    request = ModelRequest(
        model=model,
        messages=boundary_messages,
        tools=tools,
        tool_choice="auto",
        max_output_tokens=max_output_tokens,
        temperature=Decimal("0"),
        enable_thinking=enable_thinking,
    )
    return AgentExecutionSizingBoundary(
        request=request,
        context_chars=projected_chars(low),
        tool_result_chars=low,
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
    agent_tool_names = {"read_file", "retrieve_code", "replace_text", "submit"}
    agent_tools = tuple(tool for tool in tool_definitions() if tool.name in agent_tool_names)
    agent_retrieval_call = ToolCall(
        id="call-agent-retrieval-1",
        function=FunctionCall(
            name="retrieve_code",
            arguments={"query": "tenumerate definition"},
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
            "agent_retrieval_turn",
            "A multi-tool Coding Agent turn followed by a bounded retrieval observation.",
            ModelRequest(
                **shared,
                messages=(
                    ModelMessage(
                        role="system",
                        content="Use revision-bound repository evidence before editing.",
                    ),
                    ModelMessage(
                        role="user",
                        content="Make tenumerate honor a caller-provided start index.",
                    ),
                    ModelMessage(role="assistant", tool_calls=(agent_retrieval_call,)),
                    ModelMessage(
                        role="tool",
                        tool_call_id=agent_retrieval_call.id,
                        content=(
                            '{"path":"tqdm/contrib/__init__.py","start_line":1,'
                            '"end_line":40,"snippet":"' + "x" * 2_048 + '"}'
                        ),
                    ),
                ),
                tools=agent_tools,
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
                "tool_names": [tool.name for tool in request.tools],
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
