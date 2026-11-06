"""Application settings (Pydantic). Paper-first; LIVE defaults OFF."""

from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RiskProfile(str, Enum):
    CAPITAL_PRESERVATION = "capital_preservation"
    BALANCED = "balanced"
    AGGRESSIVE_GROWTH = "aggressive_growth"
    USER_CUSTOM = "user_custom"


class BrokerMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    """Runtime configuration. Secrets come from env / Docker secrets only."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Safety gates ---
    live_trading_enabled: bool = Field(
        default=False,
        description="Hard gate for live order path. Default false; no silent promotion.",
    )
    broker_mode: BrokerMode = Field(
        default=BrokerMode.PAPER,
        description="Active broker. Paper never hits private order endpoints.",
    )

    # --- Product default profile ---
    risk_profile: RiskProfile = Field(
        default=RiskProfile.AGGRESSIVE_GROWTH,
        description="Default risk preset for new installs (Aggressive Growth).",
    )

    # --- Account / floor / horizon (SEED ONLY — not live cash register) ---
    starting_capital_try: float = Field(
        default=700.0,
        ge=0,
        description=(
            "Seed default for first boot / empty DB only (örnek). "
            "Ahmet may deposit 500, 700, or any amount — runtime equity comes from "
            "persisted paper state or MEXC read-only balance, not this constant."
        ),
    )
    capital_floor_try: float = Field(
        default=700.0,
        ge=0,
        description=(
            "Seed floor F for empty state only. After first equity observation, "
            "floor tracks deposit basis via settings API / persisted snapshot."
        ),
    )
    target_equity_try: float = Field(default=8000.0, ge=0)
    stretch_target_equity_try: float = Field(default=100_000.0, ge=0)
    currency: str = Field(default="TRY")
    equity_reset: bool = Field(
        default=False,
        description=(
            "When true, clear persisted equity and re-seed from STARTING_CAPITAL_TRY / "
            "CAPITAL_FLOOR_TRY. Default false — restarts restore last equity."
        ),
    )
    usdt_try_rate: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Optional display FX for converting MEXC USDT available balance into TRY "
            "equity view. Not a trading rate guarantee."
        ),
    )

    # --- Aggressive Growth operational defaults ---
    always_on_scan: bool = Field(default=True)
    fee_free_preference: bool = Field(default=True)
    chart_vision_enabled: bool = Field(default=False)
    chart_vision_cache_ttl_seconds: int = Field(default=300, ge=0)

    # --- Risk hard caps (engine may tighten via preset) ---
    risk_per_trade_pct: float = Field(default=0.03, ge=0, le=0.05)
    abs_max_loss_pct: float = Field(default=0.05, ge=0, le=0.05)
    residual_min_pct: float = Field(
        default=0.40,
        ge=0,
        le=1,
        description="Minimum equity fraction that must remain after a single stop-out.",
    )

    # --- API / worker ---
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8080, ge=1, le=65535)
    log_level: str = Field(default="INFO")
    worker_loop_interval_seconds: float = Field(
        default=60.0,
        ge=1.0,
        description="Seconds between scan → propose → risk → paper cycles.",
    )
    default_target_leverage: float = Field(default=10.0, gt=0, le=25.0)

    # --- Scanner ---
    scanner_min_quote_volume_24h: float = Field(default=500_000.0, ge=0)
    scanner_max_spread_bps: float = Field(default=25.0, ge=0)
    scanner_max_symbols: int = Field(default=40, ge=1, le=500)

    # --- Infra ---
    database_url: str = Field(
        default="postgresql+psycopg://mexc:mexc@localhost:5432/mexc_ai"
    )
    redis_url: str = Field(default="redis://localhost:6379/0")
    compose_project_name: str = Field(default="mexc-ai")

    # --- MEXC (read-only expected; no order placement in Phase 2) ---
    mexc_api_key: str = Field(default="")
    mexc_api_secret: str = Field(default="")
    mexc_base_url: str = Field(default="https://contract.mexc.com")
    mexc_recv_window_ms: int = Field(default=5000, ge=0)

    # --- Anthropic (optional; graph falls back to deterministic stub) ---
    anthropic_api_key: str = Field(
        default="",
        description="Read from ANTHROPIC_API_KEY only. Never hardcode. Empty → stub mode.",
    )

    @field_validator("broker_mode", mode="before")
    @classmethod
    def _normalize_broker_mode(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("risk_profile", mode="before")
    @classmethod
    def _normalize_risk_profile(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @property
    def mexc_api_key_configured(self) -> bool:
        return bool(self.mexc_api_key and self.mexc_api_secret)

    def effective_broker_mode(self) -> BrokerMode:
        """Live broker only when both the mode and hard gate agree."""
        if self.broker_mode == BrokerMode.LIVE and self.live_trading_enabled:
            return BrokerMode.LIVE
        return BrokerMode.PAPER

    def public_dict(self) -> dict:
        """Settings safe for /settings (no secrets)."""
        return {
            "live_trading_enabled": self.live_trading_enabled,
            "broker_mode": self.effective_broker_mode().value,
            "risk_profile": self.risk_profile.value,
            "starting_capital_try": self.starting_capital_try,
            "capital_floor_try": self.capital_floor_try,
            "starting_capital_is_seed_only": True,
            "capital_seed_note": (
                "STARTING_CAPITAL_TRY / CAPITAL_FLOOR_TRY are örnek/seed defaults for "
                "first boot or empty DB only — not the live cash register (gerçek kasa değil). "
                "Runtime equity comes from persisted paper equity or MEXC read-only balance."
            ),
            "target_equity_try": self.target_equity_try,
            "stretch_target_equity_try": self.stretch_target_equity_try,
            "currency": self.currency,
            "equity_reset": self.equity_reset,
            "usdt_try_rate": self.usdt_try_rate,
            "always_on_scan": self.always_on_scan,
            "fee_free_preference": self.fee_free_preference,
            "chart_vision_enabled": self.chart_vision_enabled,
            "chart_vision_cache_ttl_seconds": self.chart_vision_cache_ttl_seconds,
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "abs_max_loss_pct": self.abs_max_loss_pct,
            "residual_min_pct": self.residual_min_pct,
            "mexc_base_url": self.mexc_base_url,
            "mexc_api_key_configured": self.mexc_api_key_configured,
            "anthropic_api_key_configured": bool(self.anthropic_api_key),
            "worker_loop_interval_seconds": self.worker_loop_interval_seconds,
            "scanner_min_quote_volume_24h": self.scanner_min_quote_volume_24h,
            "scanner_max_spread_bps": self.scanner_max_spread_bps,
            "scanner_max_symbols": self.scanner_max_symbols,
            "compose_project_name": self.compose_project_name,
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
