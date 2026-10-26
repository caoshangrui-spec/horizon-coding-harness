from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from horizon.adapters.model.config import ProviderConfig
from horizon.domain.common import utc_now
from horizon.reliability.breaker import (
    CircuitBreakerPolicy,
    ProviderHealthKey,
    ProviderHealthStore,
    ProviderOperation,
    ProviderReliabilityController,
)
from horizon.reliability.retry import RetryPolicy


def build_provider_reliability(
    config: ProviderConfig,
    operation: ProviderOperation,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> ProviderReliabilityController:
    """Bind health state to the same durable SQLite file as the Campaign ledger.

    The provider key stores only the credential reference name, never the credential value.
    """

    key = ProviderHealthKey(
        endpoint=config.base_url.rstrip("/"),
        provider_id=config.provider_id,
        model_id=config.model.id,
        operation=operation,
        credential_ref=config.credential_env,
    )
    return ProviderReliabilityController(
        ProviderHealthStore(Path(config.ledger_path), clock=clock),
        key,
        breaker_policy=CircuitBreakerPolicy(),
        retry_policy=RetryPolicy(max_total_attempts=config.request.max_attempts),
    )
