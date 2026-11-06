"""FastAPI: health, settings, equity view (no secrets)."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from trading import __version__
from trading.config import get_settings
from trading.equity.service import EquityService
from trading.equity.store import InMemoryEquityStore

app = FastAPI(
    title="MEXC Hybrid AI Trading API",
    version=__version__,
    description=(
        "Phase 2+. Paper-first Quant → LangGraph → Risk → PaperBroker. "
        "LIVE_TRADING_ENABLED defaults false. "
        "Aggressive Growth is the default risk profile — not a profit promise. "
        "STARTING_CAPITAL_TRY is a seed only; runtime equity is dynamic."
    ),
)

# Process-local equity service; replaced when DATABASE_URL is reachable.
_equity_service: EquityService | None = None


def get_equity_service() -> EquityService:
    global _equity_service
    if _equity_service is None:
        settings = get_settings()
        try:
            _equity_service = EquityService.from_settings(settings)
        except Exception:  # noqa: BLE001
            _equity_service = EquityService(
                settings=settings,
                store=InMemoryEquityStore(),
            )
    return _equity_service


class DepositBody(BaseModel):
    amount_try: float = Field(gt=0, description="Deposit amount in TRY (updates E0/F).")
    raise_floor: bool = True


class FloorBody(BaseModel):
    capital_floor_try: float = Field(ge=0)


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


@app.get("/equity")
def equity_view() -> dict:
    """Current equity E, deposit basis E0, floor F — not the static env example."""
    state = get_equity_service().resolve()
    settings = get_settings()
    return {
        **state.to_public_dict(),
        "env_starting_capital_seed_try": settings.starting_capital_try,
        "env_capital_floor_seed_try": settings.capital_floor_try,
        "live_trading_enabled": settings.live_trading_enabled,
        "orders_are_paper": settings.effective_broker_mode().value == "paper"
        or not settings.live_trading_enabled,
    }


@app.post("/equity/deposit")
def equity_deposit(body: DepositBody) -> dict:
    """Record an operator deposit: raise equity, E0, and (by default) floor F."""
    try:
        state = get_equity_service().apply_deposit(
            body.amount_try,
            raise_floor=body.raise_floor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return state.to_public_dict()


@app.post("/equity/floor")
def equity_floor(body: FloorBody) -> dict:
    """Update capital floor F (conscious operator action; snapshot audited)."""
    try:
        state = get_equity_service().set_floor(body.capital_floor_try)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return state.to_public_dict()


@app.post("/equity/reset")
def equity_reset() -> dict:
    """Explicit reset: clear snapshots and re-seed from env (EQUITY_RESET path)."""
    state = get_equity_service().resolve(force_reset=True)
    return state.to_public_dict()


def create_app() -> FastAPI:
    return app
