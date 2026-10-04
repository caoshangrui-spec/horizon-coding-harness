import json

from typer.testing import CliRunner

from horizon.application.reservation_analysis import analyze_reservation_traces
from horizon.domain.budget import Usage
from horizon.domain.model import (
    InputTokenBudget,
    InputTokenEstimate,
    ModelCallRecord,
    ModelCallReservation,
    ModelPolicyBinding,
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
    trace = tmp_path / "trace.jsonl"
    trace.write_text(store.export_jsonl(run.run_id), encoding="utf-8")

    report = analyze_reservation_traces([trace])
    assert report["trace_count"] == 1
    assert report["settled_model_call_count"] == 1
    assert report["calls"][0]["reserved_input_to_reported_input_ratio"] == "3.000000"
    assert report["calls"][0]["reserved_output_to_reported_output_ratio"] == "5.000000"
    assert report["calls"][0]["reserved_to_settled_cost_ratio"] == "4.000000"
    assert report["summaries"][0]["released_after_settlement_cost"] == "0.015"
    assert report["limitations"] == {
        "calls_without_request_byte_metadata": 0,
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
