from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from horizon.adapters.persistence.campaign_budget import CampaignBudgetLedger
from horizon.application.model_probe import ModelProbeService, build_probe_request
from horizon.domain.errors import ProviderConnectionError, ProviderHTTPError
from horizon.domain.model import CampaignBudget, PriceCard
from horizon.reliability.breaker import (
    CircuitBreakerPolicy,
    CircuitOpenError,
    ProviderHealthKey,
    ProviderHealthStore,
    ProviderReliabilityController,
)
from horizon.reliability.retry import (
    ProviderFailure,
    RetryDecision,
    RetryPolicy,
    RetryState,
    classify_provider_failure,
    decide_retry,
)


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, seconds: int):
        self.value += timedelta(seconds=seconds)


def health_key() -> ProviderHealthKey:
    return ProviderHealthKey(
        endpoint="https://provider.example/v1",
        provider_id="provider",
        model_id="model-a",
        operation="probe",
        credential_ref="PROVIDER_API_KEY",
    )


def controller(path, clock, *, threshold=3):
    return ProviderReliabilityController(
        ProviderHealthStore(path, clock),
        health_key(),
        breaker_policy=CircuitBreakerPolicy(
            failure_threshold=threshold,
            cooldown_seconds=30,
            half_open_lease_seconds=30,
        ),
        retry_policy=RetryPolicy(max_total_attempts=3),
    )


def test_retry_decision_is_bounded_and_does_not_multiply_transport_attempts():
    retryable_http = classify_provider_failure(ProviderHTTPError(503, retryable=True))
    uncertain = decide_retry(
        RetryPolicy(max_total_attempts=3),
        RetryState(attempts_completed=1),
        retryable_http,
        budget_allows_next_attempt=True,
        deadline_allows_wait=True,
    )
    assert uncertain.action == "stop"
    assert uncertain.reason_code == "unknown_dispatch_or_cost"

    authentication = decide_retry(
        RetryPolicy(max_total_attempts=3),
        RetryState(attempts_completed=1),
        classify_provider_failure(ProviderHTTPError(401, retryable=False)),
        budget_allows_next_attempt=True,
        deadline_allows_wait=True,
    )
    assert authentication.reason_code == "non_retryable_failure"

    safe_predispatch = ProviderFailure(
        category="connection",
        transient=True,
        dispatch_state="not_dispatched",
        cost_state="not_incurred",
    )
    eligible = decide_retry(
        RetryPolicy(max_total_attempts=3),
        RetryState(attempts_completed=1),
        safe_predispatch,
        budget_allows_next_attempt=True,
        deadline_allows_wait=True,
    )
    assert eligible.action == "retry"
    assert eligible.next_attempt == 2
    assert eligible.delay_seconds == 1

    exhausted = decide_retry(
        RetryPolicy(max_total_attempts=3),
        RetryState(attempts_completed=3),
        safe_predispatch,
        budget_allows_next_attempt=True,
        deadline_allows_wait=True,
    )
    assert exhausted.reason_code == "attempt_limit"

    with pytest.raises(ValidationError):
        RetryPolicy(transport_attempts_per_dispatch=2)
    with pytest.raises(ValidationError, match="eligible decision"):
        RetryDecision(
            action="retry",
            reason_code="attempt_limit",
            next_attempt=2,
        )


def test_breaker_persists_across_restart_and_allows_one_half_open_probe(tmp_path):
    path = tmp_path / "provider.sqlite3"
    clock = MutableClock()
    first = controller(path, clock, threshold=2)

    permit = first.acquire()
    decision = first.record_provider_failure(
        permit,
        ProviderConnectionError("first uncertain disconnect"),
    )
    assert decision.reason_code == "unknown_dispatch_or_cost"
    assert first.store.snapshot(first.key).state == "closed"

    permit = first.acquire()
    first.record_provider_failure(
        permit,
        ProviderConnectionError("second uncertain disconnect"),
    )
    opened = first.store.snapshot(first.key)
    assert opened.state == "open"
    assert opened.consecutive_failures == 2

    restarted = controller(path, clock)
    with pytest.raises(CircuitOpenError):
        restarted.acquire()

    clock.advance(31)
    probe = restarted.acquire()
    assert probe.state_at_acquire == "half_open"
    with pytest.raises(CircuitOpenError):
        restarted.acquire()

    restarted.record_success(probe)
    closed = restarted.store.snapshot(restarted.key)
    assert closed.state == "closed"
    assert closed.consecutive_failures == 0
    event_types = [event.event_type for event in restarted.store.events(restarted.key)]
    assert event_types.count("opened") == 1
    assert event_types[-2:] == ["success", "closed"]


def test_authentication_failure_never_increments_transient_breaker(tmp_path):
    clock = MutableClock()
    reliability = controller(tmp_path / "provider.sqlite3", clock, threshold=1)
    permit = reliability.acquire()

    decision = reliability.record_provider_failure(
        permit,
        ProviderHTTPError(401, retryable=False),
    )

    snapshot = reliability.store.snapshot(reliability.key)
    assert decision.reason_code == "non_retryable_failure"
    assert snapshot.state == "closed"
    assert snapshot.consecutive_failures == 0
    assert reliability.store.events(reliability.key)[-1].event_type == (
        "nontransient_failure_ignored"
    )


class FailingGateway:
    def __init__(self):
        self.calls = 0

    def generate(self, request, trace_id):
        self.calls += 1
        raise ProviderConnectionError("simulated uncertain provider disconnect")


def test_probe_breaker_blocks_before_a_fourth_reservation_or_dispatch(tmp_path):
    path = tmp_path / "shared.sqlite3"
    clock = MutableClock()
    budget = CampaignBudget(
        campaign_id="breaker-probe",
        currency="CNY",
        max_cost="10",
        max_cost_per_call="2",
    )
    pricing = PriceCard(
        currency="CNY",
        input_per_million=Decimal("1"),
        cached_input_per_million=Decimal("1"),
        output_per_million=Decimal("1"),
        version="offline",
        source_url="https://example.com/pricing",
    )
    request = build_probe_request("model-a", max_output_tokens=16, enable_thinking=False)
    ledger = CampaignBudgetLedger(path)
    reliability = controller(path, clock)
    gateway = FailingGateway()
    service = ModelProbeService(gateway, ledger, reliability)

    for _ in range(3):
        with pytest.raises(ProviderConnectionError):
            service.run(
                provider_id="provider",
                request=request,
                pricing=pricing,
                budget=budget,
            )

    with pytest.raises(CircuitOpenError):
        service.run(
            provider_id="provider",
            request=request,
            pricing=pricing,
            budget=budget,
        )

    attempts = ledger.attempts(budget.campaign_id)
    assert gateway.calls == 3
    assert len(attempts) == 3
    assert {attempt.status for attempt in attempts} == {"unknown"}
    assert all(attempt.reserved_cost > 0 for attempt in attempts)
    failures = [
        event
        for event in reliability.store.events(reliability.key)
        if event.event_type == "transient_failure"
    ]
    assert len(failures) == 3
    assert {event.retry_reason for event in failures} == {"unknown_dispatch_or_cost"}
