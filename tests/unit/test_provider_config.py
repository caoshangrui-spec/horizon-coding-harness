from pathlib import Path

import pytest

from horizon.adapters.model.config import (
    ProviderConfig,
    load_provider_config,
    resolve_credential,
)
from horizon.domain.errors import ProviderConfigurationError

CONFIG = Path(__file__).resolve().parents[2] / "config/providers/siliconflow.yaml"


def test_checked_in_provider_config_is_strict_and_cny():
    config = load_provider_config(CONFIG)
    assert config.policy_id == "siliconflow-deepseek-v4-flash"
    assert config.provider_id == "siliconflow"
    assert config.model.id == "deepseek-ai/DeepSeek-V4-Flash"
    assert config.campaign.currency == "CNY"
    assert config.campaign.max_cost == 3
    assert config.run_budget.currency == "CNY"
    assert config.run_budget.max_cost == 1
    assert config.fallback_enabled is False
    assert config.request.max_context_chars == 60_000
    assert config.request.max_input_tokens == 120_000
    assert config.request.preserve_recent_context_units == 6


def test_siliconflow_key_cannot_be_redirected_to_another_host():
    raw = load_provider_config(CONFIG).model_dump(mode="json")
    raw["base_url"] = "https://attacker.example/v1"
    with pytest.raises(ValueError, match="canonical"):
        ProviderConfig.model_validate(raw)


def test_run_budget_must_match_currency_and_fit_campaign():
    raw = load_provider_config(CONFIG).model_dump(mode="json")
    raw["run_budget"]["currency"] = "USD"
    with pytest.raises(ValueError, match="currencies"):
        ProviderConfig.model_validate(raw)

    raw = load_provider_config(CONFIG).model_dump(mode="json")
    raw["run_budget"]["max_cost"] = "3.01"
    with pytest.raises(ValueError, match="campaign"):
        ProviderConfig.model_validate(raw)


def test_environment_credential_wins_and_is_redacted(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("SILICONFLOW_API_KEY=file-secret\n", encoding="utf-8")
    credential = resolve_credential(
        load_provider_config(CONFIG),
        dotenv,
        {"SILICONFLOW_API_KEY": "environment-secret"},
    )
    assert credential.source.startswith("environment:")
    assert credential.value.get_secret_value() == "environment-secret"
    assert "environment-secret" not in repr(credential)


def test_dotenv_duplicate_and_missing_credentials_fail_without_echo(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "SILICONFLOW_API_KEY=first-secret\nSILICONFLOW_API_KEY=second-secret\n",
        encoding="utf-8",
    )
    with pytest.raises(ProviderConfigurationError) as duplicate:
        resolve_credential(load_provider_config(CONFIG), dotenv, {})
    assert "first-secret" not in str(duplicate.value)
    assert "second-secret" not in str(duplicate.value)
    with pytest.raises(ProviderConfigurationError, match="not set"):
        resolve_credential(load_provider_config(CONFIG), tmp_path / "missing", {})
