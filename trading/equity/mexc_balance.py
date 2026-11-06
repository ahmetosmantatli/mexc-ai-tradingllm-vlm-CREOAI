"""Parse MEXC read-only account assets into an equity view (TRY / USDT display)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MexcBalanceView:
    """Available balances extracted from a read-only assets payload."""

    available_usdt: float = 0.0
    available_try: float = 0.0
    equity_display: float = 0.0
    display_currency: str = "TRY"
    usdt_try_rate: float | None = None
    raw_assets: tuple[dict[str, Any], ...] = ()

    @property
    def has_balance(self) -> bool:
        return self.available_usdt > 0 or self.available_try > 0


def _num(value: object, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _asset_rows(payload: dict[str, Any] | list[Any] | None) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("assets", "data", "balances"):
        nested = payload.get(key)
        if isinstance(nested, list):
            return [x for x in nested if isinstance(x, dict)]
        if isinstance(nested, dict):
            # Some responses nest again under data.assets
            inner = nested.get("assets")
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
    return []


def _currency_code(row: dict[str, Any]) -> str:
    for key in ("currency", "coin", "asset", "symbol"):
        val = row.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().upper().replace("USDT.E", "USDT")
    return ""


def _available(row: dict[str, Any]) -> float:
    for key in (
        "availableBalance",
        "available",
        "availableCash",
        "cashBalance",
        "equity",
        "balance",
    ):
        if key in row:
            return _num(row.get(key))
    return 0.0


def parse_mexc_assets(
    payload: dict[str, Any] | list[Any] | None,
    *,
    display_currency: str = "TRY",
    usdt_try_rate: float | None = None,
) -> MexcBalanceView:
    """Extract available USDT/TRY and build a single display equity figure.

    Conversion is display-only: when the operator currency is TRY and only USDT
    is present, multiply by ``usdt_try_rate`` (operator-provided display rate).
    """
    rows = _asset_rows(payload)
    usdt = 0.0
    try_bal = 0.0
    for row in rows:
        code = _currency_code(row)
        avail = _available(row)
        if code in {"USDT", "USD"}:
            usdt += avail
        elif code == "TRY":
            try_bal += avail

    display = display_currency.upper()
    rate = float(usdt_try_rate) if usdt_try_rate and usdt_try_rate > 0 else None
    equity_display = 0.0
    if display == "TRY":
        converted = (usdt * rate) if rate is not None else 0.0
        equity_display = try_bal + converted
        if equity_display <= 0 and usdt > 0 and rate is None:
            # No rate: surface USDT as equity with currency still marked for UI.
            equity_display = usdt
            display = "USDT"
    elif display == "USDT":
        equity_display = usdt + (try_bal / rate if rate else 0.0)
        if equity_display <= 0 and try_bal > 0 and rate is None:
            equity_display = try_bal
            display = "TRY"
    else:
        equity_display = try_bal or usdt

    return MexcBalanceView(
        available_usdt=usdt,
        available_try=try_bal,
        equity_display=equity_display,
        display_currency=display,
        usdt_try_rate=rate,
        raw_assets=tuple(rows),
    )
