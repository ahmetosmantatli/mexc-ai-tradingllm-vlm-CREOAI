"""Position sizing helpers — always take live equity E, never a static env constant."""

from __future__ import annotations

from trading.equity.state import EquityState


def _as_equity(equity: float | EquityState) -> float:
    if isinstance(equity, EquityState):
        return max(float(equity.equity), 0.0)
    return max(float(equity), 0.0)


def max_loss_budget(equity: float | EquityState, risk_per_trade_pct: float) -> float:
    """Absolute max loss for one trade = E * risk%."""
    return _as_equity(equity) * max(float(risk_per_trade_pct), 0.0)


def notional_for_stop(
    equity: float | EquityState,
    *,
    risk_per_trade_pct: float,
    stop_distance_pct: float,
) -> float:
    """Approx notional so a stop of ``stop_distance_pct`` loses ~risk% of E."""
    stop_pct = max(float(stop_distance_pct), 1e-4)
    budget = max(max_loss_budget(equity, risk_per_trade_pct), 1e-6)
    return budget / stop_pct


def size_from_equity(
    equity: float | EquityState,
    *,
    risk_per_trade_pct: float,
    stop_distance_pct: float,
) -> dict[str, float]:
    """Return sizing fields derived from the live equity object."""
    e = _as_equity(equity)
    budget = max_loss_budget(e, risk_per_trade_pct)
    notional = notional_for_stop(
        e,
        risk_per_trade_pct=risk_per_trade_pct,
        stop_distance_pct=stop_distance_pct,
    )
    return {
        "equity": e,
        "max_loss_budget": budget,
        "notional": notional,
        "risk_per_trade_pct": float(risk_per_trade_pct),
        "stop_distance_pct": float(stop_distance_pct),
    }
