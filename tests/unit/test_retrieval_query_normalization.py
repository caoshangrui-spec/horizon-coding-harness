import pytest

from horizon.adapters.retrieval.query_normalization import normalize_retrieval_query


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "test_generate_context_decodes_non_ascii_chars",
            "generateContextDecodesNonAsciiChars generate context decodes decode "
            "non ascii chars character",
        ),
        (
            "SchedulerApiTest test_status_wont_override",
            "schedulerApi scheduler api statusWontOverride status wont not override",
        ),
        (
            "test_custom_repr_single",
            "customReprSingle custom repr representation single",
        ),
        (
            "HTTPClientCommonTestCase test_redirect_put_without_body",
            "httpClientCommon http client common redirectPutWithoutBody redirect put without body",
        ),
        (
            "生成上下文时无法解码非 ASCII 字符",
            "generate context decode non ascii character",
        ),
        (
            "调度器状态不应覆盖已有状态",
            "scheduler status not override existing",
        ),
        (
            "单个值的自定义表示失败",
            "single value custom representation repr failure",
        ),
        (
            "HTTP 客户端重定向无请求体的 PUT 失败",
            "http client redirect without request body put failure",
        ),
    ],
)
def test_normalization_is_frozen_for_query_robustness_inputs(query, expected):
    result = normalize_retrieval_query(query)

    assert result.normalized_query == expected
    assert len(result.terms) <= 20
    assert len(result.normalized_query) <= 200


def test_normalization_removes_only_test_scaffolding_and_low_signal_words():
    result = normalize_retrieval_query("EdgeCaseTest should preserve the edge case")

    assert result.normalized_query == "edgeCase edge case preserve"
    assert result.applied_rules == (
        "test_scaffold_drop_v1",
        "identifier_split_v1",
        "low_signal_drop_v1",
    )


def test_normalization_preserves_an_unmapped_non_ascii_query():
    result = normalize_retrieval_query("未知术语")

    assert result.normalized_query == "未知术语"
    assert result.terms == ()
    assert result.applied_rules == ("preserve_unmapped_query_v1",)


@pytest.mark.parametrize("query", ["", "x" * 201])
def test_normalization_rejects_queries_outside_runtime_contract(query):
    with pytest.raises(ValueError, match="1 to 200"):
        normalize_retrieval_query(query)
