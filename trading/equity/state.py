"""Runtime equity view. Not a static .env constant."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class EquitySource(str, Enum):
    """Where the current equity figure came from."""

    SEED = "seed"  # STARTING_CAPITAL_TRY on empty DB / first boot
    PAPER_DB = "paper_db"  # restored from persisted paper equity
    MEXC_READONLY = "mexc_readonly"  # synced available balance (orders still paper)
    DEPOSIT_API = "deposit_api"  # operator deposit / floor update
    RESET = "reset"  # explicit EQUITY_RESET / reset flag


@dataclass
class EquityState:
    """Current bankroll E, deposit basis E0, and floor F.

    ``STARTING_CAPITAL_TRY`` / ``CAPITAL_FLOOR_TRY`` seed this only when no
    snapshot exists (or on explicit reset). Runtime risk/sizing must use
    ``equity`` (live E), never the env constant alone.
    """

    equity: float
    starting_capital: float
    capital_floor: float
    currency: str = "TRY"
    source: EquitySource = EquitySource.SEED
    equity_try: float | None = None
    equity_usdt: float | None = None
    usdt_try_rate: float | None = None
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.source, str):
            self.source = EquitySource(self.source)
        if self.equity_try is None and self.currency.upper() == "TRY":
            self.equity_try = float(self.equity)
        if self.equity_usdt is None and self.currency.upper() == "USDT":
            self.equity_usdt = float(self.equity)

    def to_public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["source"] = self.source.value
        payload["seed_only_env_note"] = (
            "STARTING_CAPITAL_TRY / CAPITAL_FLOOR_TRY are seed defaults for "
            "first boot / empty DB only — not the live cash register."
        )
        return payload
