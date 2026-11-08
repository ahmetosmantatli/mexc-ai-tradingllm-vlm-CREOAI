"""Candidate signals emitted by quant strategies."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from trading.models.domain import Side


class StrategyName(str, Enum):
    WATCH_REVIEW = "watch_review"
    TREND = "trend"
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"


class CandidateSignal(BaseModel):
    """Quant opportunity before AI / Risk gating."""

    signal_id: str = Field(default_factory=lambda: str(uuid4()))
    symbol: str
    side: Side
    strategy: StrategyName
    entry_price: float = Field(gt=0)
    stop_loss_price: float = Field(gt=0)
    take_profit_price: float = Field(gt=0)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""
    fee_free_pair: bool = False
    features: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("symbol")
    @classmethod
    def _symbol_nonempty(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not cleaned:
            raise ValueError("symbol is required")
        return cleaned

    @property
    def stop_distance_pct(self) -> float:
        return abs(self.entry_price - self.stop_loss_price) / self.entry_price
