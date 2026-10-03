from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import Field, SecretStr, field_validator, model_validator

from horizon.domain.common import Contract
from horizon.domain.errors import ProviderConfigurationError
from horizon.domain.model import CampaignBudget, Currency, PositiveMoney, PriceCard
from horizon.domain.task import Identifier, PositiveInt, Text, relative_pattern

ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")


class ProviderModelConfig(Contract):
    id: Text
    supports_tools: bool = True


class RequestPolicy(Contract):
    timeout_seconds: Annotated[PositiveInt, Field(le=600)] = 120
    max_output_tokens: PositiveInt = 2048
    probe_max_output_tokens: Annotated[PositiveInt, Field(le=512)] = 128
    max_attempts: Annotated[PositiveInt, Field(le=3)] = 2
    enable_thinking: bool = False
    max_context_chars: Annotated[PositiveInt, Field(ge=2_000, le=1_000_000)] = 60_000
    max_input_tokens: Annotated[PositiveInt, Field(ge=2_000, le=2_000_000)] = 120_000
    preserve_recent_context_units: Annotated[PositiveInt, Field(le=50)] = 6

    @model_validator(mode="after")
    def check_probe_limit(self) -> Self:
        if self.probe_max_output_tokens > self.max_output_tokens:
            raise ValueError("Probe output limit cannot exceed the normal request limit")
        return self


class RunBudgetPolicy(Contract):
    currency: Currency
    max_cost: PositiveMoney


class ProviderConfig(Contract):
    schema_version: Literal["1.0"] = "1.0"
    policy_id: Identifier
    provider_id: Identifier
    api_type: Literal["openai_chat_completions"]
    base_url: Text
    credential_env: Text
    model: ProviderModelConfig
    request: RequestPolicy
    pricing: PriceCard
    campaign: CampaignBudget
    run_budget: RunBudgetPolicy
    ledger_path: Text
    fallback_enabled: Literal[False] = False

    @field_validator("credential_env")
    @classmethod
    def check_credential_name(cls, value: str) -> str:
        if not ENVIRONMENT_NAME.fullmatch(value):
            raise ValueError("credential_env must be an uppercase environment variable name")
        return value

    @field_validator("ledger_path")
    @classmethod
    def check_ledger_path(cls, value: str) -> str:
        return relative_pattern(value)

    @model_validator(mode="after")
    def check_endpoint_and_currency(self) -> Self:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Provider base_url must be a credential-free HTTPS origin/path")
        if self.provider_id == "siliconflow" and self.base_url.rstrip("/") != (
            "https://api.siliconflow.cn/v1"
        ):
            raise ValueError("SiliconFlow credentials may only be sent to its canonical API URL")
        if len({self.pricing.currency, self.campaign.currency, self.run_budget.currency}) != 1:
            raise ValueError("Pricing, campaign, and Run budget currencies must match")
        if self.run_budget.max_cost > self.campaign.max_cost:
            raise ValueError("Per-Run cost cannot exceed the campaign budget")
        return self


class ProviderCredential(Contract):
    source: Text
    value: SecretStr


def load_provider_config(path: Path) -> ProviderConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProviderConfigurationError(f"Cannot read provider config: {path}") from exc
    except yaml.YAMLError as exc:
        raise ProviderConfigurationError("Provider config is not valid YAML") from exc
    try:
        return ProviderConfig.model_validate(raw)
    except ValueError as exc:
        raise ProviderConfigurationError("Provider config failed schema validation") from exc


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _dotenv_value(path: Path, name: str) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProviderConfigurationError(f"Cannot read credential file: {path}") from exc
    found: str | None = None
    for line in lines:
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if candidate.startswith("export "):
            candidate = candidate[7:].lstrip()
        key, separator, raw_value = candidate.partition("=")
        if not separator or key.strip() != name:
            continue
        if found is not None:
            raise ProviderConfigurationError(f"Credential {name} is defined more than once")
        found = _unquote(raw_value.strip())
    return found


def resolve_credential(
    config: ProviderConfig,
    dotenv_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> ProviderCredential:
    environment = os.environ if environ is None else environ
    value = environment.get(config.credential_env)
    source = f"environment:{config.credential_env}"
    if value is None and dotenv_path is not None:
        value = _dotenv_value(dotenv_path, config.credential_env)
        source = f"dotenv:{dotenv_path.name}:{config.credential_env}"
    if value is None or not value.strip():
        raise ProviderConfigurationError(
            f"Credential {config.credential_env} is not set in the environment or dotenv file"
        )
    value = value.strip()
    if "\n" in value or "\r" in value:
        raise ProviderConfigurationError("Credential must be a single non-empty line")
    return ProviderCredential(source=source, value=SecretStr(value))
