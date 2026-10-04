import json

from typer.testing import CliRunner

from horizon.application.model_probe import conservative_input_sizing
from horizon.application.reservation_analysis import analyze_reservation_traces
from horizon.domain.budget import Usage
from horizon.domain.errors import BudgetStop, BudgetStopReason
from horizon.domain.model import (
    InputTokenBudget,
    InputTokenEstimate,
    ModelCallRecord,
    ModelCallReservation,
    ModelMessage,
    ModelPolicyBinding,
    ModelRequest,
    ModelUsage,
)
from horizon.interfaces.cli.app import app


def test_replay_verified_reservation_report_quantifies_dispatch_pressure(
    tmp_path,
    store,
    service,
    running,
):
    run, token = running
    service.bind_model_policy(
        run.run_id,
        ModelPolicyBinding(
            policy_id="unconfigured",
            provider_id="siliconflow",
            model="deepseek-ai/DeepSeek-V4-Flash",
            campaign_id="diagnostic",
            currency="CNY",
            max_run_cost="0.50",
            price_card_hash="a" * 64,
        ),
        token,
        "bind-model",
    )
    reservation = ModelCallReservation(
        call_id="model-call-1",
        request_hash="b" * 64,
        provider_id="siliconflow",
        model="deepseek-ai/DeepSeek-V4-Flash",
        currency="CNY",
        reserved_cost="0.020",
        input_token_budget=InputTokenBudget(
            max_input_tokens=10_000,
            estimate=InputTokenEstimate(request_bytes=1_000, token_ceiling=3_024),
        ),
    )
    service.reserve_model_call(
        run.run_id,
        reservation,
        Usage(model_calls=1, input_tokens=3_024, output_tokens=50),
        token,
        "reserve-model",
    )
    service.settle_model_call(
        run.run_id,
        ModelCallRecord(
            call_id=reservation.call_id,
            request_hash=reservation.request_hash,
            provider_id=reservation.provider_id,
            model=reservation.model,
            currency=reservation.currency,
            estimated_cost="0.005",
            response_id="response-1",
            provider_trace_id="provider-trace-1",
            finish_reason="tool_calls",
            usage=ModelUsage(input_tokens=1_008, output_tokens=10),
        ),
        Usage(model_calls=1, input_tokens=1_008, output_tokens=10),
        token,
        "settle-model",
    )
    stopped_request = ModelRequest(
        model="deepseek-ai/DeepSeek-V4-Flash",
        messages=(ModelMessage(role="user", content="Inspect parser 🧪"),),
        max_output_tokens=60,
    )
    stopped_estimate, stopped_payload = conservative_input_sizing(stopped_request)
    stopped_reservation = ModelCallReservation(
        call_id="model-call-stopped",
        request_hash=stopped_request.sha256,
        provider_id="siliconflow",
        model="deepseek-ai/DeepSeek-V4-Flash",
        currency="CNY",
        reserved_cost="0.020",
        input_token_budget=InputTokenBudget(
            max_input_tokens=10_000,
            estimate=stopped_estimate,
        ),
        request_payload=stopped_payload,
    )
    service.fail_budget_stop(
        run.run_id,
        BudgetStop(
            reason_code=BudgetStopReason.RUN_MODEL_COST_LIMIT,
            scope="run",
            currency="CNY",
            required_cost="0.020",
            available_cost="0.010",
        ),
        token,
        "stop-model",
        model_request_budget=stopped_reservation.budget_evidence(60),
    )
    trace = tmp_path / "trace.jsonl"
    trace.write_text(store.export_jsonl(run.run_id), encoding="utf-8")

    report = analyze_reservation_traces([trace])
    assert report["trace_count"] == 1
    assert report["settled_model_call_count"] == 1
    assert report["calls"][0]["reserved_input_to_reported_input_ratio"] == "3.000000"
    assert report["calls"][0]["candidate_input_token_ceiling"] == 2_024
    assert report["calls"][0]["candidate_input_to_reported_input_ratio"] == "2.007937"
    assert report["calls"][0]["reserved_output_to_reported_output_ratio"] == "5.000000"
    assert report["calls"][0]["reserved_to_settled_cost_ratio"] == "4.000000"
    assert report["summaries"][0]["released_after_settlement_cost"] == "0.015"
    assert report["budget_stop_count"] == 1
    assert report["budget_stops"][0]["call_id"] == "model-call-stopped"
    assert report["budget_stops"][0]["request_bytes"] == stopped_payload.payload_bytes
    assert report["budget_stops"][0]["input_token_ceiling"] == stopped_estimate.token_ceiling
    assert report["budget_stops"][0]["output_token_ceiling"] == 60
    assert report["budget_stops"][0]["candidate_input_token_ceiling"] == (
        stopped_payload.payload_bytes + 1024
    )
    assert report["budget_stops"][0]["request_byte_basis"] == stopped_payload.encoding
    assert report["budget_stops"][0]["request_payload_sha256"] == stopped_payload.payload_sha256
    assert report["candidate_estimator_replays"] == [
        {
            "estimator": "request_utf8_bytes_plus_1024_candidate_v1",
            "formula": "request_bytes + 1024",
            "settled_call_count": 1,
            "request_byte_basis_counts": {"model_request_canonical_json_legacy_v1": 1},
            "budget_stop_count_with_request_metadata": 1,
            "budget_stop_request_byte_basis_counts": {"openai_compatible_canonical_json_v1": 1},
            "observed_underestimate_count": 0,
            "candidate_input_to_reported_input_ratio": {
                "count": 1,
                "min": "2.007937",
                "median": "2.007937",
                "max": "2.007937",
            },
            "production_gate_changed": False,
        }
    ]
    assert report["limitations"] == {
        "calls_without_request_byte_metadata": 0,
        "settled_calls_with_exact_wire_payload_metadata": 0,
        "budget_stops_with_exact_wire_payload_metadata": 1,
        "mixed_request_byte_bases_present": False,
        "settled_cost_is_local_price_card_estimate_not_provider_invoice": True,
        "pre_dispatch_budget_stops_have_no_provider_usage": True,
        "automatic_estimator_change_performed": False,
    }

    result = CliRunner().invoke(app, ["trace", "reservation-report", str(trace)])
    assert result.exit_code == 0, result.output
    cli_report = json.loads(result.stdout)
    assert cli_report["traces"][0]["run_id"] == run.run_id
    assert cli_report["summaries"][0]["aggregate_reserved_to_settled_cost_ratio"] == ("4.000000")


def test_reservation_report_rejects_duplicate_run_inputs(tmp_path, store, running):
    run, _ = running
    trace = tmp_path / "trace.jsonl"
    trace.write_text(store.export_jsonl(run.run_id), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["trace", "reservation-report", str(trace), str(trace)],
    )

    assert result.exit_code == 2
    assert "appears in more than one input Trace" in result.output
