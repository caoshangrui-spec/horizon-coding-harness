from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from statistics import median
from typing import Any

from horizon.adapters.persistence.sqlite import SQLiteEventStore
from horizon.domain.budget import Usage
from horizon.domain.errors import BudgetStop
from horizon.domain.events import Event
from horizon.domain.model import ModelCallRecord, ModelCallReservation

RATIO_QUANTUM = Decimal("0.000001")


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _ratio_decimal_text(value: Decimal) -> str:
    return _decimal_text(value.quantize(RATIO_QUANTUM, rounding=ROUND_HALF_UP))


def _ratio(numerator: int | Decimal, denominator: int | Decimal) -> Decimal | None:
    if denominator == 0:
        return None
    return (Decimal(numerator) / Decimal(denominator)).quantize(
        RATIO_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def _ratio_text(numerator: int | Decimal, denominator: int | Decimal) -> str | None:
    value = _ratio(numerator, denominator)
    return _decimal_text(value) if value is not None else None


def _distribution(values: Sequence[Decimal]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "min": None, "median": None, "max": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": _ratio_decimal_text(ordered[0]),
        "median": _ratio_decimal_text(median(ordered)),
        "max": _ratio_decimal_text(ordered[-1]),
    }


def _summarize_calls(calls: Sequence[dict[str, Any]], *, currency: str, purpose: str):
    reserved_cost = sum((call["_reserved_cost"] for call in calls), Decimal("0"))
    settled_cost = sum((call["_settled_cost"] for call in calls), Decimal("0"))
    input_ratios = [
        value
        for call in calls
        if (value := _ratio(call["reserved_input_tokens"], call["actual_input_tokens"])) is not None
    ]
    output_ratios = [
        value
        for call in calls
        if (value := _ratio(call["reserved_output_tokens"], call["actual_output_tokens"]))
        is not None
    ]
    cost_ratios = [
        value
        for call in calls
        if (value := _ratio(call["_reserved_cost"], call["_settled_cost"])) is not None
    ]
    byte_ratios = [
        value
        for call in calls
        if call["request_bytes"] is not None
        and (value := _ratio(call["actual_input_tokens"], call["request_bytes"])) is not None
    ]
    return {
        "currency": currency,
        "purpose": purpose,
        "settled_call_count": len(calls),
        "reserved_at_dispatch_cost": _decimal_text(reserved_cost),
        "settled_price_card_cost": _decimal_text(settled_cost),
        "released_after_settlement_cost": _decimal_text(reserved_cost - settled_cost),
        "aggregate_reserved_to_settled_cost_ratio": _ratio_text(
            reserved_cost,
            settled_cost,
        ),
        "per_call_reserved_to_settled_cost_ratio": _distribution(cost_ratios),
        "reserved_input_to_reported_input_ratio": _distribution(input_ratios),
        "reserved_output_to_reported_output_ratio": _distribution(output_ratios),
        "reported_input_tokens_per_request_byte": _distribution(byte_ratios),
    }


def analyze_reservation_traces(paths: Sequence[Path]) -> dict[str, Any]:
    """Measure reservation pressure from replay-verified traces without calling a provider."""

    if not paths:
        raise ValueError("At least one Trace JSONL path is required")

    traces: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    budget_stops: list[dict[str, Any]] = []
    seen_run_ids: set[str] = set()

    for path in paths:
        trace_bytes = path.read_bytes()
        content = trace_bytes.decode("utf-8")
        replayed = SQLiteEventStore.replay_jsonl(content)
        if replayed.run_id in seen_run_ids:
            raise ValueError(f"Run {replayed.run_id} appears in more than one input Trace")
        seen_run_ids.add(replayed.run_id)
        events = [Event.model_validate_json(line) for line in content.splitlines() if line]

        budget_reservations: dict[str, Usage] = {}
        model_reservations: dict[str, ModelCallReservation] = {}
        settled_call_ids: set[str] = set()
        trace_call_count = 0
        trace_stop_count = 0

        for event in events:
            if event.event_type == "BUDGET_RESERVED":
                reservation_id = event.payload.get("reservation_id")
                if isinstance(reservation_id, str):
                    budget_reservations[reservation_id] = Usage.model_validate(
                        event.payload["amount"]
                    )
                continue

            if event.event_type == "MODEL_CALL_RESERVED":
                reservation = ModelCallReservation.model_validate(event.payload["reservation"])
                model_reservations[reservation.call_id] = reservation
                continue

            if event.event_type == "MODEL_CALL_SETTLED":
                record = ModelCallRecord.model_validate(event.payload["record"])
                reservation = model_reservations.get(record.call_id)
                budget = budget_reservations.get(record.call_id)
                if reservation is None or budget is None:
                    raise ValueError(
                        f"Settled model call {record.call_id} lacks its reservation evidence"
                    )
                if record.call_id in settled_call_ids:
                    raise ValueError(f"Model call {record.call_id} was settled more than once")
                if (
                    record.request_hash != reservation.request_hash
                    or record.provider_id != reservation.provider_id
                    or record.model != reservation.model
                    or record.currency != reservation.currency
                    or record.purpose != reservation.purpose
                ):
                    raise ValueError(
                        f"Model call {record.call_id} settlement does not match its reservation"
                    )
                if reservation.input_token_budget is not None and (
                    reservation.input_token_budget.estimate.token_ceiling != budget.input_tokens
                ):
                    raise ValueError(
                        f"Model call {record.call_id} has inconsistent input reservation evidence"
                    )

                settled_call_ids.add(record.call_id)
                trace_call_count += 1
                estimate = (
                    reservation.input_token_budget.estimate
                    if reservation.input_token_budget
                    else None
                )
                reserved_cost = reservation.reserved_cost
                settled_cost = record.estimated_cost
                calls.append(
                    {
                        "trace": str(path),
                        "run_id": event.run_id,
                        "call_id": record.call_id,
                        "purpose": record.purpose,
                        "provider_id": record.provider_id,
                        "model": record.model,
                        "currency": record.currency,
                        "estimator": estimate.estimator if estimate else None,
                        "request_bytes": estimate.request_bytes if estimate else None,
                        "reserved_input_tokens": budget.input_tokens,
                        "actual_input_tokens": record.usage.input_tokens,
                        "reserved_input_to_reported_input_ratio": _ratio_text(
                            budget.input_tokens,
                            record.usage.input_tokens,
                        ),
                        "reserved_output_tokens": budget.output_tokens,
                        "actual_output_tokens": record.usage.output_tokens,
                        "reserved_output_to_reported_output_ratio": _ratio_text(
                            budget.output_tokens,
                            record.usage.output_tokens,
                        ),
                        "reserved_cost": _decimal_text(reserved_cost),
                        "settled_price_card_cost": _decimal_text(settled_cost),
                        "released_after_settlement_cost": _decimal_text(
                            reserved_cost - settled_cost
                        ),
                        "reserved_to_settled_cost_ratio": _ratio_text(
                            reserved_cost,
                            settled_cost,
                        ),
                        "_reserved_cost": reserved_cost,
                        "_settled_cost": settled_cost,
                    }
                )
                continue

            if event.event_type == "RUN_FAILED" and "budget_stop" in event.payload:
                stop = BudgetStop.model_validate(event.payload["budget_stop"])
                trace_stop_count += 1
                budget_stops.append(
                    {
                        "trace": str(path),
                        "run_id": event.run_id,
                        "reason_code": stop.reason_code.value,
                        "scope": stop.scope,
                        "currency": stop.currency,
                        "required_cost": _decimal_text(stop.required_cost),
                        "available_cost": _decimal_text(stop.available_cost),
                        "shortfall": _decimal_text(stop.required_cost - stop.available_cost),
                    }
                )

        traces.append(
            {
                "path": str(path),
                "sha256": hashlib.sha256(trace_bytes).hexdigest(),
                "run_id": replayed.run_id,
                "event_count": len(events),
                "settled_model_call_count": trace_call_count,
                "budget_stop_count": trace_stop_count,
                "open_model_reservation_count": len(
                    set(model_reservations).difference(settled_call_ids)
                ),
            }
        )

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_currency: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for call in calls:
        grouped[(call["currency"], call["purpose"])].append(call)
        by_currency[call["currency"]].append(call)

    summaries = [
        _summarize_calls(items, currency=currency, purpose="all")
        for currency, items in sorted(by_currency.items())
    ]
    summaries.extend(
        _summarize_calls(items, currency=currency, purpose=purpose)
        for (currency, purpose), items in sorted(grouped.items())
    )

    public_calls = [
        {key: value for key, value in call.items() if not key.startswith("_")} for call in calls
    ]
    calls_without_estimator = sum(call["request_bytes"] is None for call in calls)
    return {
        "schema_version": 1,
        "trace_count": len(traces),
        "run_count": len(seen_run_ids),
        "settled_model_call_count": len(calls),
        "budget_stop_count": len(budget_stops),
        "summaries": summaries,
        "calls": public_calls,
        "budget_stops": budget_stops,
        "traces": traces,
        "limitations": {
            "calls_without_request_byte_metadata": calls_without_estimator,
            "settled_cost_is_local_price_card_estimate_not_provider_invoice": True,
            "pre_dispatch_budget_stops_have_no_provider_usage": True,
            "automatic_estimator_change_performed": False,
        },
    }
