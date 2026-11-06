"""FastAPI skeleton: health + settings (no secrets)."""

from __future__ import annotations

from fastapi import FastAPI

from trading import __version__
from trading.config import get_settings

app = FastAPI(
    title="MEXC Hybrid AI Trading API",
    version=__version__,
    description=(
        "Phase 2. Paper-first Quant → LangGraph → Risk → PaperBroker. "
        "LIVE_TRADING_ENABLED defaults false. "
        "Aggressive Growth is the default risk profile — not a profit promise."
    ),
)


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "live_trading_enabled": settings.live_trading_enabled,
        "broker_mode": settings.effective_broker_mode().value,
        "risk_profile": settings.risk_profile.value,
        "always_on_scan": settings.always_on_scan,
    }


@app.get("/settings")
def settings_public() -> dict:
    """Return non-secret settings for operators / dashboard wiring."""
    return get_settings().public_dict()


def create_app() -> FastAPI:
    return app
