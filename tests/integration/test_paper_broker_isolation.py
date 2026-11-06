"""Paper broker must never call private order / cancel / modify endpoints."""

from __future__ import annotations

import pytest

from trading.brokers.base import BrokerOrderRequest
from trading.brokers.paper import PaperBroker
from trading.exchange.order_deny import (
    PRIVATE_ORDER_PATH_DENYLIST,
    PrivateOrderEndpointDenied,
    is_private_order_path,
)
from trading.models.domain import Side


@pytest.mark.parametrize("path", list(PRIVATE_ORDER_PATH_DENYLIST))
def test_paper_broker_denies_private_order_paths(path: str) -> None:
    broker = PaperBroker(starting_equity=700.0)
    with pytest.raises(PrivateOrderEndpointDenied):
        broker.request("POST", path)


def test_paper_broker_denies_full_urls() -> None:
    broker = PaperBroker(starting_equity=700.0)
    url = "https://contract.mexc.com/api/v1/private/order/submit"
    assert is_private_order_path(url)
    with pytest.raises(PrivateOrderEndpointDenied):
        broker.request("POST", url)


def test_place_order_is_simulated_only() -> None:
    broker = PaperBroker(starting_equity=700.0)
    result = broker.place_order(
        BrokerOrderRequest(
            symbol="BTC_USDT",
            side=Side.LONG,
            notional=50.0,
            leverage=5.0,
            stop_loss_price=49_000.0,
            metadata={"mark_price": 50_000.0},
        )
    )
    assert result.is_simulated is True
    assert result.status == "filled"
    assert broker.get_equity() == 700.0
    # No HTTP calls recorded for local simulation
    assert broker._http_calls == []


def test_place_order_rejects_order_url_in_metadata() -> None:
    broker = PaperBroker(starting_equity=700.0)
    with pytest.raises(PrivateOrderEndpointDenied):
        broker.place_order(
            BrokerOrderRequest(
                symbol="ETH_USDT",
                side=Side.SHORT,
                notional=20.0,
                leverage=3.0,
                metadata={"endpoint": "/api/v1/private/order/submit"},
            )
        )


def test_denylist_covers_cancel_and_modify() -> None:
    assert any("cancel" in p for p in PRIVATE_ORDER_PATH_DENYLIST)
    assert any("change_price" in p or "modify" in p or "change_limit" in p for p in PRIVATE_ORDER_PATH_DENYLIST)


def test_readonly_paths_not_confused_with_submit() -> None:
    # Listing open orders is read-ish; our deny list targets mutation submit/cancel/modify.
    assert not is_private_order_path("/api/v1/private/order/list/open_orders")
    assert is_private_order_path("/api/v1/private/order/submit")
