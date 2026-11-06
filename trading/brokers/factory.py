"""Resolve the active broker from settings (paper by default)."""

from __future__ import annotations

from trading.brokers.base import Broker
from trading.brokers.live.broker import LiveBroker, LiveBrokerNotEnabled
from trading.brokers.paper import PaperBroker
from trading.config import BrokerMode, Settings, get_settings
from trading.equity.state import EquityState


def get_broker(
    settings: Settings | None = None,
    *,
    equity_state: EquityState | None = None,
    starting_equity: float | None = None,
) -> Broker:
    """Build the active broker.

    Paper starting equity prefers a resolved ``EquityState`` (persisted / MEXC /
    seed). Env ``STARTING_CAPITAL_TRY`` is used only as the last-resort seed when
    no equity state is supplied — never as a hard lock after first boot.
    """
    cfg = settings or get_settings()
    if starting_equity is not None:
        seed = float(starting_equity)
    elif equity_state is not None:
        seed = float(equity_state.equity)
    else:
        seed = float(cfg.starting_capital_try)

    mode = cfg.effective_broker_mode()
    if mode == BrokerMode.LIVE:
        try:
            return LiveBroker(cfg)
        except LiveBrokerNotEnabled:
            # Fall back to paper if live gate blocks.
            return PaperBroker(starting_equity=seed)
    return PaperBroker(starting_equity=seed)
