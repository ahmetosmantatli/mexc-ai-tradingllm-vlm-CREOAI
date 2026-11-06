"""In-memory domain schemas (TradeProposal, RiskDecision)."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class DecisionAction(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"


class StopLossSpec(BaseModel):
    """Mandatory stop-loss for every proposal."""

    price: float | None = Field(default=None, gt=0)
    percent_from_entry: float | None = Field(default=None, gt=0, le=1)
    style: str = Field(default="fixed_percent")
    invalidation_note: str | None = None

    @model_validator(mode="after")
    def _require_price_or_percent(self) -> StopLossSpec:
        if self.price is None and self.percent_from_entry is None:
            raise ValueError("stop_loss requires price and/or percent_from_entry")
        return self


class TradeProposal(BaseModel):
    """Structured AI / Quant output. Not an order — Risk Engine must gate it."""

    proposal_id: str = Field(default_factory=lambda: str(uuid4()))
    symbol: str
    side: Side
    leverage: float = Field(gt=0)
    notional: float = Field(gt=0, description="Notional size in account currency units.")
    entry_price: float | None = Field(default=None, gt=0)
    stop_loss: StopLossSpec
    take_profit_price: float | None = Field(default=None, gt=0)
    max_loss_if_stopped: float = Field(
        gt=0,
        description="Absolute loss if SL hits (account currency).",
    )
    expected_edge: float | None = Field(default=None)
    fee_free_pair: bool = False
    rationale: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def _symbol_nonempty(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned:
            raise ValueError("symbol is required")
        return cleaned


class RiskDecision(BaseModel):
    action: DecisionAction
    proposal_id: str
    reason_codes: list[str] = Field(default_factory=list)
    notes: str = ""
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def accepted(self) -> bool:
        return self.action == DecisionAction.ACCEPT
