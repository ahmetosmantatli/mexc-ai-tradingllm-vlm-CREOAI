"""Resolve the active broker from settings (paper by default)."""

from __future__ import annotations

from trading.brokers.base import Broker
from trading.brokers.live.broker import LiveBroker, LiveBrokerNotEnabled
from trading.brokers.paper import PaperBroker
from trading.config import BrokerMode, Settings, get_settings


def get_broker(settings: Settings | None = None) -> Broker:
    cfg = settings or get_settings()
    mode = cfg.effective_broker_mode()
    if mode == BrokerMode.LIVE:
        try:
            return LiveBroker(cfg)
        except LiveBrokerNotEnabled:
            # Fall back to paper if live gate blocks.
            return PaperBroker(starting_equity=cfg.starting_capital_try)
    return PaperBroker(starting_equity=cfg.starting_capital_try)
