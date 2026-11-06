"""LangGraph evaluation schemas — structured TradeProposal or HOLD."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from trading.models.domain import TradeProposal
from trading.quant.signals import CandidateSignal


class GraphAction(str, Enum):
    TRADE = "trade"
    HOLD = "hold"


class MarketAnalystNote(BaseModel):
    summary: str = ""
    regime: str = "unknown"
    favor_symbols: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    mode: Literal["llm", "stub"] = "stub"


class StrategyReviewNote(BaseModel):
    selected_signal_id: str | None = None
    rejected_signal_ids: list[str] = Field(default_factory=list)
    review: str = ""
    confidence_adjustment: float = 0.0
    mode: Literal["llm", "stub"] = "stub"


class PortfolioAnalystNote(BaseModel):
    action: GraphAction = GraphAction.HOLD
    sizing_notes: str = ""
    max_loss_budget: float | None = None
    mode: Literal["llm", "stub"] = "stub"


class EvaluationResult(BaseModel):
    """Final graph output: TRADE with TradeProposal, or HOLD."""

    action: GraphAction
    proposal: TradeProposal | None = None
    hold_reason: str | None = None
    market: MarketAnalystNote | None = None
    strategy: StrategyReviewNote | None = None
    portfolio: PortfolioAnalystNote | None = None
    selected_signal: CandidateSignal | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistency(self) -> EvaluationResult:
        if self.action == GraphAction.TRADE and self.proposal is None:
            raise ValueError("TRADE action requires a TradeProposal")
        if self.action == GraphAction.HOLD and self.proposal is not None:
            raise ValueError("HOLD action must not carry a TradeProposal")
        if self.action == GraphAction.TRADE and self.proposal is not None:
            # Mandatory SL already on TradeProposal; double-check presence.
            if self.proposal.stop_loss is None:
                raise ValueError("TradeProposal requires stop_loss")
        return self


class GraphState(BaseModel):
    """Mutable state passed through Market Analyst → Strategy Reviewer → Portfolio Analyst."""

    candidates: list[CandidateSignal] = Field(default_factory=list)
    # Default 0 — callers must pass live equity; do not imply a static 700 seed.
    equity: float = 0.0
    open_risk: float = 0.0
    open_positions: int = 0
    risk_profile: str = "aggressive_growth"
    risk_per_trade_pct: float = 0.03
    target_leverage: float = 10.0
    always_on_scan: bool = True
    market: MarketAnalystNote | None = None
    strategy: StrategyReviewNote | None = None
    portfolio: PortfolioAnalystNote | None = None
    result: EvaluationResult | None = None
    llm_enabled: bool = False
    errors: list[str] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}
