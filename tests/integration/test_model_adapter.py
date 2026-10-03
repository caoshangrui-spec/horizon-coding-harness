import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from horizon.adapters.model.config import ProviderCredential, load_provider_config
from horizon.adapters.model.openai_compatible import (
    HTTPResult,
    OpenAICompatibleModelGateway,
)
from horizon.application.model_probe import build_probe_request
from horizon.domain.errors import ProviderHTTPError, ProviderProtocolError

CONFIG = Path(__file__).resolve().parents[2] / "config/providers/siliconflow.yaml"


def gateway(post):
    return OpenAICompatibleModelGateway(
        load_provider_config(CONFIG),
        ProviderCredential(source="test", value=SecretStr("test-secret")),
        post=post,
    )


def response_body(*, arguments='{"value":"HORIZON_OK"}', include_usage=True):
    body = {
        "id": "response-1",
        "model": "deepseek-ai/DeepSeek-V4-Flash",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "horizon_probe",
                                "arguments": arguments,
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    if include_usage:
        body["usage"] = {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_cache_hit_tokens": 10,
            "completion_tokens_details": {"reasoning_tokens": 5},
        }
    return json.dumps(body).encode()


def probe_request():
    return build_probe_request(
        "deepseek-ai/DeepSeek-V4-Flash",
        max_output_tokens=128,
        enable_thinking=False,
    )


def test_adapter_sends_bounded_nonstreaming_tool_request_and_parses_usage():
    captured = {}

    def post(url, body, headers, timeout):
        captured.update(url=url, body=body, headers=headers, timeout=timeout)
        return HTTPResult(
            200,
            {"x-siliconcloud-trace-id": "provider-trace"},
            response_body(),
        )

    response = gateway(post).generate(probe_request(), "client-trace")
    payload = json.loads(captured["body"])
    assert captured["url"] == "https://api.siliconflow.cn/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-secret"
    assert b"test-secret" not in captured["body"]
    assert payload["stream"] is False
    assert payload["enable_thinking"] is False
    assert payload["max_tokens"] == 128
    assert payload["tools"][0]["function"]["name"] == "horizon_probe"
    assert response.provider_trace_id == "provider-trace"
    assert response.usage.cached_input_tokens == 10
    assert response.usage.reasoning_tokens == 5
    assert response.message.tool_calls[0].function.arguments == {"value": "HORIZON_OK"}


def test_adapter_rejects_http_errors_without_exposing_body():
    def post(url, body, headers, timeout):
        return HTTPResult(401, {}, b'test-secret {"message":"bad"}')

    with pytest.raises(ProviderHTTPError) as error:
        gateway(post).generate(probe_request(), "trace")
    assert error.value.status_code == 401
    assert error.value.retryable is False
    assert "test-secret" not in str(error.value)


@pytest.mark.parametrize(
    "body",
    [
        response_body(include_usage=False),
        response_body(arguments="not-json"),
        json.dumps({"id": "x", "model": "wrong", "choices": []}).encode(),
    ],
)
def test_adapter_rejects_untrusted_or_incomplete_responses(body):
    def post(url, request_body, headers, timeout):
        return HTTPResult(200, {}, body)

    with pytest.raises(ProviderProtocolError):
        gateway(post).generate(probe_request(), "trace")
