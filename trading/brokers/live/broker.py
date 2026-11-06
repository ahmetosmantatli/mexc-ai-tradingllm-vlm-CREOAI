"""Live broker stub. Raises unless LIVE_TRADING_ENABLED is explicitly true.

Phase 1–4: this module must not place MEXC orders.
"""

from __future__ import annotations

from trading.brokers.base import Broker, BrokerOrderRequest, BrokerOrderResult
from trading.config import Settings


class LiveBrokerNotEnabled(RuntimeError):
    pass


class LiveBroker(Broker):
    """Placeholder for Phase 5. Refuses all orders while gated off."""

    name = "live"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        if not settings.live_trading_enabled:
            raise LiveBrokerNotEnabled(
                "LIVE_TRADING_ENABLED is false; live broker unavailable."
            )

    def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        raise LiveBrokerNotEnabled(
            "Live order placement is not implemented in Phase 1. "
            "Paper broker only until Phase 5 exit criteria."
        )

    def cancel_order(self, client_order_id: str) -> BrokerOrderResult:
        raise LiveBrokerNotEnabled("Live cancel not implemented in Phase 1.")

    def get_equity(self) -> float:
        raise LiveBrokerNotEnabled("Live equity fetch not implemented in Phase 1.")
