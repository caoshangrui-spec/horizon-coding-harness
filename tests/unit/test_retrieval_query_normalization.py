import pytest

from horizon.adapters.retrieval.query_normalization import (
    normalize_retrieval_query,
    normalize_retrieval_query_conservatively,
    normalize_retrieval_query_selectively,
    retrieval_query_normalization_gate,
)


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


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ("test_generate_context_decodes_non_ascii_chars", "test_identifier_v1"),
        ("SchedulerApiTest test_status_wont_override", "test_identifier_v1"),
        ("test_custom_repr_single", "test_identifier_v1"),
        ("HTTPClientCommonTestCase test_redirect_put_without_body", "test_identifier_v1"),
        ("生成上下文时无法解码非 ASCII 字符", "cjk_text_v1"),
        ("调度器状态不应覆盖已有状态", "cjk_text_v1"),
        ("单个值的自定义表示失败", "cjk_text_v1"),
        ("HTTP 客户端重定向无请求体的 PUT 失败", "cjk_text_v1"),
    ],
)
def test_selective_gate_accepts_only_frozen_target_shapes(query, reason):
    assert retrieval_query_normalization_gate(query) == reason


@pytest.mark.parametrize(
    "query",
    [
        "context generation should decode non ascii characters",
        "scheduler api status override regression",
        "custom repr formatting failed for one value",
        "redirect put without body request failure",
        "integration test failure",
    ],
)
def test_selective_gate_bypasses_ordinary_english(query):
    assert retrieval_query_normalization_gate(query) is None
    result = normalize_retrieval_query_selectively(query)

    assert result.original_query == query
    assert result.normalized_query == query
    assert result.terms == ()
    assert result.applied_rules == ("gate_bypass_v1",)


def test_selective_normalization_records_gate_reason():
    result = normalize_retrieval_query_selectively("test_custom_repr_single")

    assert result.normalized_query == "customReprSingle custom repr representation single"
    assert result.applied_rules[0] == "gate_test_identifier_v1"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "生成上下文时无法解码非 ASCII 字符",
            "生成上下文时无法解码非 ASCII 字符 generate context decode non ascii character",
        ),
        (
            "调度器状态不应覆盖已有状态",
            "调度器状态不应覆盖已有状态 scheduler status not override existing",
        ),
        (
            "单个值的自定义表示失败",
            "单个值的自定义表示失败 single value custom representation repr failure",
        ),
        (
            "HTTP 客户端重定向无请求体的 PUT 失败",
            "HTTP 客户端重定向无请求体的 PUT 失败 "
            "http client redirect without request body put failure",
        ),
    ],
)
def test_conservative_normalization_augments_high_coverage_cjk(query, expected):
    result = normalize_retrieval_query_conservatively(query)

    assert result.normalized_query == expected
    assert result.applied_rules[0] == "gate_cjk_additive_v1"


@pytest.mark.parametrize(
    "query",
    [
        "下载时无法生成唯一文件名",
        "主机头被覆盖",
        "项目解析长分隔符转义失败",
        "git push 无法匹配 Bitbucket",
        "静态文件范围起点为负且结束小于起点",
    ],
)
def test_conservative_normalization_bypasses_low_coverage_cjk(query):
    result = normalize_retrieval_query_conservatively(query)

    assert result.normalized_query == query
    assert result.terms == ()
    assert result.applied_rules == ("gate_cjk_insufficient_coverage_v1",)


def test_conservative_normalization_keeps_test_identifier_branch():
    query = "test_custom_repr_single"

    assert normalize_retrieval_query_conservatively(query) == normalize_retrieval_query_selectively(
        query
    )


@pytest.mark.parametrize("query", ["", "x" * 201])
def test_normalization_rejects_queries_outside_runtime_contract(query):
    with pytest.raises(ValueError, match="1 to 200"):
        normalize_retrieval_query(query)
    with pytest.raises(ValueError, match="1 to 200"):
        normalize_retrieval_query_selectively(query)
    with pytest.raises(ValueError, match="1 to 200"):
        normalize_retrieval_query_conservatively(query)
