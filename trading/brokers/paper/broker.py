"""Paper broker — simulates fills; NEVER hits private order endpoints."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from trading.brokers.base import Broker, BrokerOrderRequest, BrokerOrderResult
from trading.exchange.order_deny import (
    PRIVATE_ORDER_PATH_DENYLIST,
    PrivateOrderEndpointDenied,
    assert_not_private_order_path,
    is_private_order_path,
)


@dataclass
class PaperBroker(Broker):
    """In-memory paper execution for Phase 1 wiring and isolation tests."""

    starting_equity: float = 700.0
    name: str = "paper"
    _equity: float = field(init=False)
    _orders: dict[str, dict[str, Any]] = field(default_factory=dict, init=False)
    _http_calls: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._equity = float(self.starting_equity)

    def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        # Defense in depth: if any caller sneaks a private path into metadata, deny.
        for key in ("url", "path", "endpoint"):
            value = request.metadata.get(key)
            if isinstance(value, str) and value:
                assert_not_private_order_path(value)

        fill_price = float(request.metadata.get("mark_price") or 1.0)
        record = {
            "client_order_id": request.client_order_id,
            "proposal_id": request.proposal_id,
            "symbol": request.symbol,
            "side": request.side.value,
            "notional": request.notional,
            "leverage": request.leverage,
            "stop_loss_price": request.stop_loss_price,
            "status": "filled",
            "fill_price": fill_price,
        }
        self._orders[request.client_order_id] = record
        return BrokerOrderResult(
            client_order_id=request.client_order_id,
            status="filled",
            symbol=request.symbol,
            side=request.side.value,
            filled_notional=request.notional,
            fill_price=fill_price,
            is_simulated=True,
            message="paper fill (simulated)",
        )

    def cancel_order(self, client_order_id: str) -> BrokerOrderResult:
        order = self._orders.get(client_order_id)
        if order is None:
            return BrokerOrderResult(
                client_order_id=client_order_id,
                status="not_found",
                symbol="",
                side="",
                is_simulated=True,
                message="unknown paper order",
            )
        order["status"] = "canceled"
        return BrokerOrderResult(
            client_order_id=client_order_id,
            status="canceled",
            symbol=order["symbol"],
            side=order["side"],
            is_simulated=True,
            message="paper cancel (simulated)",
        )

    def get_equity(self) -> float:
        return self._equity

    def set_equity(self, equity: float) -> None:
        self._equity = float(equity)

    def request(self, method: str, url: str, **_: Any) -> None:
        """Guard used by isolation tests — paper must refuse order URLs."""
        self._http_calls.append(f"{method.upper()} {url}")
        if is_private_order_path(url):
            raise PrivateOrderEndpointDenied(
                f"PaperBroker refused private order endpoint: {url!r}"
            )
        raise RuntimeError(
            "PaperBroker does not perform exchange HTTP; use MexcPublicClient stubs."
        )

    def simulate_fill_id(self) -> str:
        return f"paper-fill-{uuid4().hex[:12]}"

    @staticmethod
    def denied_order_paths() -> tuple[str, ...]:
        return PRIVATE_ORDER_PATH_DENYLIST
