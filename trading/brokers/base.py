"""Broker interface shared by paper and (future) live implementations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from trading.models.domain import Side, TradeProposal


@dataclass
class BrokerOrderRequest:
    symbol: str
    side: Side
    notional: float
    leverage: float
    proposal_id: str | None = None
    client_order_id: str = field(default_factory=lambda: f"sim-{uuid4().hex[:16]}")
    stop_loss_price: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_proposal(cls, proposal: TradeProposal) -> BrokerOrderRequest:
        sl_price = proposal.stop_loss.price
        if sl_price is None and proposal.entry_price and proposal.stop_loss.percent_from_entry:
            pct = proposal.stop_loss.percent_from_entry
            if proposal.side == Side.LONG:
                sl_price = proposal.entry_price * (1 - pct)
            else:
                sl_price = proposal.entry_price * (1 + pct)
        return cls(
            symbol=proposal.symbol,
            side=proposal.side,
            notional=proposal.notional,
            leverage=proposal.leverage,
            proposal_id=proposal.proposal_id,
            stop_loss_price=sl_price,
            metadata={"max_loss_if_stopped": proposal.max_loss_if_stopped},
        )


@dataclass
class BrokerOrderResult:
    client_order_id: str
    status: str
    symbol: str
    side: str
    filled_notional: float = 0.0
    fill_price: float | None = None
    is_simulated: bool = True
    message: str = ""


class Broker(ABC):
    """Execution interface. Implementations must not bypass Risk Engine."""

    name: str

    @abstractmethod
    def place_order(self, request: BrokerOrderRequest) -> BrokerOrderResult:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, client_order_id: str) -> BrokerOrderResult:
        raise NotImplementedError

    @abstractmethod
    def get_equity(self) -> float:
        raise NotImplementedError
