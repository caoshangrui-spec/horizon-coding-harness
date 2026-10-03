from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import ValidationError

from horizon.adapters.model.config import ProviderConfig, ProviderCredential
from horizon.domain.errors import (
    ProviderConnectionError,
    ProviderHTTPError,
    ProviderProtocolError,
)
from horizon.domain.model import (
    FunctionCall,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
)

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class HTTPResult:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


HTTPPost = Callable[[str, bytes, Mapping[str, str], int], HTTPResult]


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        raise HTTPError(request.full_url, code, "Redirects are disabled", headers, file_pointer)


def urllib_post(url: str, body: bytes, headers: Mapping[str, str], timeout: int) -> HTTPResult:
    request = Request(url, data=body, headers=dict(headers), method="POST")
    opener = build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ProviderProtocolError("Provider response exceeded the size limit")
            return HTTPResult(
                status_code=response.status,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=payload,
            )
    except HTTPError as exc:
        # Do not expose response bodies: gateways sometimes echo submitted content.
        raise ProviderHTTPError(exc.code, retryable=exc.code in RETRYABLE_STATUS) from None
    except (TimeoutError, URLError, OSError) as exc:
        raise ProviderConnectionError("Provider connection failed or timed out") from exc


def _message_payload(message: ModelMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": json.dumps(
                        call.function.arguments,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                },
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    return payload


def request_payload(request: ModelRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": [_message_payload(message) for message in request.messages],
        "stream": False,
        "max_tokens": request.max_output_tokens,
        "temperature": float(request.temperature),
        "enable_thinking": request.enable_thinking,
    }
    if request.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in request.tools
        ]
        payload["tool_choice"] = request.tool_choice
    return payload


def _parse_tool_calls(raw: Any) -> tuple[ToolCall, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ProviderProtocolError("Provider tool_calls is not an array")
    calls = []
    for item in raw:
        try:
            function = item["function"]
            arguments = json.loads(function["arguments"])
            if not isinstance(arguments, dict):
                raise TypeError
            calls.append(
                ToolCall(
                    id=item["id"],
                    type=item.get("type", "function"),
                    function=FunctionCall(name=function["name"], arguments=arguments),
                )
            )
        except (KeyError, TypeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise ProviderProtocolError("Provider returned an invalid tool call") from exc
    return tuple(calls)


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderProtocolError(f"Provider usage field {field} is invalid")
    return value


def _parse_usage(raw: Any) -> ModelUsage:
    if not isinstance(raw, dict):
        raise ProviderProtocolError("Provider response omitted trusted usage")
    input_tokens = _nonnegative_integer(raw.get("prompt_tokens"), "prompt_tokens")
    output_tokens = _nonnegative_integer(raw.get("completion_tokens"), "completion_tokens")
    prompt_details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    cached = raw.get("prompt_cache_hit_tokens", prompt_details.get("cached_tokens", 0))
    reasoning = completion_details.get("reasoning_tokens", 0)
    try:
        return ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=_nonnegative_integer(cached, "cached_input_tokens"),
            reasoning_tokens=_nonnegative_integer(reasoning, "reasoning_tokens"),
        )
    except ValidationError as exc:
        raise ProviderProtocolError("Provider usage subtotals are inconsistent") from exc


class OpenAICompatibleModelGateway:
    def __init__(
        self,
        config: ProviderConfig,
        credential: ProviderCredential,
        post: HTTPPost = urllib_post,
    ):
        self.config = config
        self._credential = credential
        self._post = post

    def generate(self, request: ModelRequest, trace_id: str) -> ModelResponse:
        if request.model != self.config.model.id:
            raise ProviderProtocolError("Request model does not match the configured model")
        body = json.dumps(
            request_payload(request),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        result = self._post(
            f"{self.config.base_url.rstrip('/')}/chat/completions",
            body,
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._credential.value.get_secret_value()}",
                "X-Trace-Id": trace_id,
                "User-Agent": "horizon-coding-harness/0.1.0",
            },
            self.config.request.timeout_seconds,
        )
        if result.status_code != 200:
            raise ProviderHTTPError(
                result.status_code,
                retryable=result.status_code in RETRYABLE_STATUS,
            )
        try:
            raw = json.loads(result.body)
            if not isinstance(raw, dict):
                raise TypeError
            choices = raw["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise TypeError
            choice = choices[0]
            message = choice["message"]
            if message.get("role", "assistant") != "assistant":
                raise TypeError
            parsed_message = ModelMessage(
                role="assistant",
                content=message.get("content"),
                tool_calls=_parse_tool_calls(message.get("tool_calls")),
            )
            response = ModelResponse(
                response_id=raw["id"],
                model=raw["model"],
                message=parsed_message,
                finish_reason=choice.get("finish_reason"),
                usage=_parse_usage(raw.get("usage")),
                provider_trace_id=(
                    result.headers.get("x-siliconcloud-trace-id")
                    or result.headers.get("x-trace-id")
                ),
            )
        except ProviderProtocolError:
            raise
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise ProviderProtocolError("Provider returned an invalid chat completion") from exc
        if response.model != request.model:
            raise ProviderProtocolError("Provider silently changed the requested model")
        return response
