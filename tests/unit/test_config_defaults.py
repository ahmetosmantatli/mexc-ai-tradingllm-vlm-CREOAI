"""Config defaults: LIVE off, paper broker, Aggressive Growth profile."""

from __future__ import annotations

import os
from unittest.mock import patch

from trading.config import BrokerMode, RiskProfile, Settings, get_settings


def test_live_trading_disabled_by_default() -> None:
    with patch.dict(os.environ, {}, clear=True):
        # Avoid picking up a developer .env during CI-like isolation
        settings = Settings(_env_file=None)
    assert settings.live_trading_enabled is False
    assert settings.broker_mode == BrokerMode.PAPER
    assert settings.effective_broker_mode() == BrokerMode.PAPER
    assert settings.live_runner3_structure_auto_entry_enabled is True


def test_aggressive_growth_is_default_profile() -> None:
    settings = Settings(_env_file=None)
    assert settings.risk_profile == RiskProfile.AGGRESSIVE_GROWTH
    assert settings.always_on_scan is True
    assert settings.fee_free_preference is True
    assert settings.starting_capital_try == 700.0
    assert settings.capital_floor_try == 700.0


def test_live_mode_ignored_when_gate_false() -> None:
    settings = Settings(
        _env_file=None,
        broker_mode=BrokerMode.LIVE,
        live_trading_enabled=False,
    )
    assert settings.effective_broker_mode() == BrokerMode.PAPER


def test_public_dict_redacts_secrets() -> None:
    settings = Settings(
        _env_file=None,
        mexc_api_key="super-secret-key",
        mexc_api_secret="super-secret-secret",
        anthropic_api_key="should-not-leak",
    )
    public = settings.public_dict()
    assert "mexc_api_secret" not in public
    assert "mexc_api_key" not in public
    assert "anthropic_api_key" not in public
    assert public["mexc_api_key_configured"] is True
    assert public["anthropic_api_key_configured"] is True
    assert public["live_trading_enabled"] is False
    assert public["risk_profile"] == "aggressive_growth"
    assert public["starting_capital_is_seed_only"] is True
    assert public["equity_reset"] is False


def test_starting_capital_documented_as_seed() -> None:
    settings = Settings(_env_file=None)
    desc = Settings.model_fields["starting_capital_try"].description or ""
    assert "seed" in desc.lower()
    assert settings.equity_reset is False


def test_get_settings_cached_singleton() -> None:
    get_settings.cache_clear()
    a = get_settings()
    b = get_settings()
    assert a is b
    get_settings.cache_clear()
