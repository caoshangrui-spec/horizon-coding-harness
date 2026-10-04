import json
from pathlib import Path

from typer.testing import CliRunner

from horizon.application.model_sizing import analyze_model_request_sizing
from horizon.interfaces.cli.app import app

CONFIG = Path(__file__).resolve().parents[2] / "config/providers/siliconflow.yaml"
MODEL = "deepseek-ai/DeepSeek-V4-Flash"


def test_request_sizing_boundary_cases_have_exact_byte_composition():
    report = analyze_model_request_sizing(
        MODEL,
        max_output_tokens=512,
        enable_thinking=False,
    )

    assert report["case_count"] == 5
    assert [case["case_id"] for case in report["cases"]] == [
        "minimal_ascii",
        "unicode_messages",
        "nested_tool_schema",
        "tool_call_arguments",
        "large_tool_result",
    ]
    for case in report["cases"]:
        payload = case["request_payload"]
        estimate = case["production_input_estimate"]
        assert payload["payload_bytes"] == (
            sum(payload["field_value_bytes"].values()) + payload["json_structure_bytes"]
        )
        assert estimate["estimator"] == "openai_payload_utf8_bytes_x2_plus_1024_v2"
        assert estimate["request_bytes"] == payload["payload_bytes"]
        assert estimate["token_ceiling"] == 2 * payload["payload_bytes"] + 1024
        assert case["candidate_input_token_ceiling"] == payload["payload_bytes"] + 1024
    assert report["payload_bytes"]["max"] > report["payload_bytes"]["min"] + 8_000
    deltas = [case["wire_minus_legacy_bytes"] for case in report["cases"]]
    assert any(delta > 0 for delta in deltas)
    assert any(delta < 0 for delta in deltas)
    assert report["network_called"] is False
    assert report["paid_model_called"] is False
    assert report["limitations"]["candidate_safety_conclusion"] is False


def test_model_sizing_report_cli_is_offline_and_structured():
    result = CliRunner().invoke(
        app,
        ["model", "sizing-report", "--config", str(CONFIG)],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["model"] == MODEL
    assert report["case_count"] == 5
    assert report["production_change"] == {
        "request_byte_basis_changed": True,
        "token_ceiling_formula_changed": False,
        "automatic_candidate_promotion": False,
    }
    assert report["network_called"] is False
    assert report["paid_model_called"] is False
