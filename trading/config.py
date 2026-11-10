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

    # Live code is inert unless every deployment gate and account allowlist matches.
    live_price_plan_enabled: bool = False
    live_structure_setups_enabled: bool = False
    live_position_management_enabled: bool = False
    live_runner2_enabled: bool = False
    live_runner3_enabled: bool = False
    live_runner3_aggressive_model_sizing_enabled: bool = False
    live_runner3_continuous_aggressive_enabled: bool = False
    live_runner3_daily_net_target_usdt: float | None = Field(default=None, gt=0)
    live_management_min_interval_seconds: float = Field(default=60, ge=15, le=3600)
    live_leverage_mode: str = Field(default='profile',pattern='^(profile|exchange)$')
    live_max_leverage: int = Field(default=10,ge=1,le=25)
    live_max_positions: int = Field(default=4,ge=1,le=4)
    live_aggressive_daily_loss_limit_pct: float | None = Field(default=None,gt=0,le=.50)
    live_aggressive_weekly_loss_limit_enabled: bool = True
    live_automatic_enabled: bool = False
    live_allowed_accounts: tuple[str, ...] = ()
    live_readiness_ack: str = Field(default='', repr=False)

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
        description="Seconds between independent market scans; AI evaluations have a separate minimum interval.",
    )
    candidate_ttl_seconds: float = Field(default=900,ge=30,le=3600)
    candidate_queue_limit: int = Field(default=80,ge=1,le=500)
    anthropic_decision_max_tokens: int = Field(default=1200,ge=700,le=4096)
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
    mexc_base_url: str = Field(default="https://api.mexc.com")
    mexc_recv_window_ms: int = Field(default=5000, ge=0)

    # --- Anthropic (optional; graph falls back to deterministic stub) ---
    anthropic_api_key: str = Field(
        default="",
        description="Read from ANTHROPIC_API_KEY only. Never hardcode. Empty → stub mode.",
    )

    # Worker runtime: real account observation, independent simulated ledger.
    account_vault_key: str = ""
    ai_budget_enabled: bool = True
    position_update_seconds: float = Field(default=300, ge=30)
    accounts_file: str = "secrets/accounts.json"
    shared_budget_db_path: str = "data/shared-budget.sqlite3"
    analysis_version: str = "text"
    analysis_chart_dir: str = "data/charts"
    decision_ttl_seconds: float = Field(default=60, ge=5, le=300)
    protection_max_age_seconds: float = Field(default=30, ge=5)
    position_poll_seconds: float = Field(default=5, ge=1, le=60)
    runtime_db_path: str = "data/runtime.sqlite3"
    require_mexc_balance: bool = True
    paper_allocation_usdt: float | None = Field(default=None, gt=0)
    management_interval_seconds: float = Field(default=30, ge=5)
    paper_seed_usdt: float = Field(default=0, ge=0)
    paper_fee_rate: float = Field(default=0.0006, ge=0, le=0.01)
    paper_slippage_bps: float = Field(default=5, ge=0, le=100)
    paper_max_leverage: float = Field(default=3, ge=1, le=25)
    paper_max_positions: int = Field(default=2, ge=1, le=4)
    liquidation_buffer_pct: float = Field(default=0.02, gt=0, lt=0.5)
    market_max_age_seconds: float = Field(default=30, gt=0)
    daily_loss_limit_pct: float = Field(default=0.02, gt=0, le=0.1)
    drawdown_limit_pct: float = Field(default=0.08, gt=0, le=0.3)
    kill_switch_path: str = "data/STOP"
    research_goal_enabled: bool = False
    research_goal_multiple: float = Field(default=14, gt=1)
    research_goal_days: float = Field(default=7, gt=0)
    research_goal_stretch_multiple: float = Field(default=200, gt=1)
    ai_schedule_mode: str = Field(default="interval",pattern="^(interval|quota)$")
    ai_provider_pool_enabled: bool = False
    ai_free_model_routes: str = '[]'
    external_models_enabled: bool = False
    external_models_file: str = 'data/external-models.json'
    nvidia_api_key: str = Field(default='', repr=False)
    nvidia_models: str = '[]'
    nvidia_local_rpm: int = Field(default=6, ge=1, le=40)
    openrouter_api_key: str = Field(default='',repr=False)
    cloudflare_api_token: str = Field(default='',repr=False)
    cloudflare_account_id: str = Field(default='',repr=False)
    cloudflare_free_plan_confirmed: bool = False
    cloudflare_daily_neuron_budget: float = Field(default=9000,gt=0,le=10000)
    external_max_completion_tokens: int = Field(default=2048,ge=700,le=8192)
    ai_parallel_candidates: int = Field(default=2,ge=1,le=8)
    ai_dynamic_parallel_enabled: bool = True
    ai_pool_retry_seconds: float = Field(default=5,ge=0,le=10)
    ai_pool_max_retry_attempts: int = Field(default=1,ge=0,le=1)
    ai_pool_max_generation_requests: int = Field(default=3,ge=1,le=8)
    groq_api_key_2: str = Field(default='',repr=False)
    gemini_api_key_2: str = Field(default='',repr=False)
    groq_quota_group: str = Field(default='groq-main',pattern='^[A-Za-z0-9_-]{1,64}$')
    groq_quota_group_2: str = Field(default='groq-main',pattern='^[A-Za-z0-9_-]{1,64}$')
    gemini_quota_group: str = Field(default='gemini-main',pattern='^[A-Za-z0-9_-]{1,64}$')
    gemini_quota_group_2: str = Field(default='gemini-main',pattern='^[A-Za-z0-9_-]{1,64}$')
    groq_rpm: int = Field(default=30,ge=1)
    groq_tpm: int = Field(default=8000,ge=1)
    groq_rpd: int = Field(default=1000,ge=1)
    groq_tpd: int = Field(default=200000,ge=1)
    groq_daily_probe_enabled: bool = False
    gemini_rpm: int = Field(default=15,ge=1)
    gemini_tpm: int = Field(default=250000,ge=1)
    gemini_rpd: int = Field(default=500,ge=1)
    gemini_tpd: int = Field(default=0,ge=0)
    groq_api_key: str = Field(default='', repr=False)
    ai_primary_provider: str = Field(default='groq', pattern='^(groq|gemini|anthropic)$')
    ai_vision_primary_provider: str = Field(default='',pattern='^(|groq|gemini|anthropic)$')
    gemini_api_key: str = Field(default='',repr=False)
    gemini_text_model: str = 'gemini-3.5-flash-lite'
    gemini_vision_model: str = 'gemini-3.5-flash-lite'
    gemini_max_completion_tokens: int = Field(default=2048,ge=700,le=8192)
    ai_claude_fallback_enabled: bool = True
    groq_text_model: str = 'openai/gpt-oss-20b'
    groq_vision_model: str = 'qwen/qwen3.8-27b'
    groq_request_token_budget: int = Field(default=8000,ge=0)
    groq_max_completion_tokens: int = Field(default=2048,ge=700,le=8192)
    ai_provider_timeout_seconds: float = Field(default=25,ge=5,le=60)
    anthropic_model: str = "claude-sonnet-4-6"
    anthropic_daily_budget_usd: float = Field(default=3, ge=0)
    anthropic_total_budget_usd: float = Field(default=24, ge=0, le=25)
    anthropic_input_usd_per_million: float = Field(default=3, gt=0)
    anthropic_output_usd_per_million: float = Field(default=15, gt=0)
    anthropic_min_interval_seconds: float = Field(default=300, ge=0)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_status_interval_seconds: float = Field(default=3600, ge=60)
    telegram_activity_interval_seconds: float = Field(default=900, ge=120)

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
