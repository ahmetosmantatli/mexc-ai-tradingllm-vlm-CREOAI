"""Broker abstractions. Paper is default; live is gated OFF."""

from trading.brokers.base import Broker, BrokerOrderRequest, BrokerOrderResult
from trading.brokers.factory import get_broker

__all__ = [
    "Broker",
    "BrokerOrderRequest",
    "BrokerOrderResult",
    "get_broker",
]
