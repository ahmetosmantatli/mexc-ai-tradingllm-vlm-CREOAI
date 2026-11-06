"""LangGraph evaluation: structured TradeProposal / HOLD validation."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from trading.agents.anthropic_client import AnthropicClient
from trading.agents.graph import EvaluationGraph, size_proposal_from_signal
from trading.agents.schemas import EvaluationResult, GraphAction
from trading.config import Settings
from trading.models.domain import Side, StopLossSpec, TradeProposal
from trading.quant.signals import CandidateSignal, StrategyName


def _signal(
    *,
    confidence: float = 0.8,
    symbol: str = "BTC_USDT",
    side: Side = Side.LONG,
) -> CandidateSignal:
    entry = 100.0
    return CandidateSignal(
        symbol=symbol,
        side=side,
        strategy=StrategyName.TREND,
        entry_price=entry,
        stop_loss_price=entry * 0.98,
        take_profit_price=entry * 1.04,
        confidence=confidence,
        rationale="unit-test signal",
        fee_free_pair=True,
    )


def test_evaluation_result_trade_requires_proposal() -> None:
    with pytest.raises(ValidationError):
        EvaluationResult(action=GraphAction.TRADE, proposal=None)


def test_evaluation_result_hold_forbids_proposal() -> None:
    proposal = TradeProposal(
        symbol="BTC_USDT",
        side=Side.LONG,
        leverage=5.0,
        notional=50.0,
        entry_price=100.0,
        stop_loss=StopLossSpec(percent_from_entry=0.02),
        max_loss_if_stopped=10.0,
    )
    with pytest.raises(ValidationError):
        EvaluationResult(action=GraphAction.HOLD, proposal=proposal, hold_reason="x")


def test_graph_stub_mode_emits_valid_trade_proposal() -> None:
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
        settings = Settings(_env_file=None, anthropic_api_key="")
        graph = EvaluationGraph(llm=AnthropicClient(settings=settings))
        assert graph.llm.available is False
        result = graph.evaluate_candidates(
            [_signal(confidence=0.85)],
            equity=700.0,
            risk_per_trade_pct=0.03,
            target_leverage=10.0,
        )
    assert result.action == GraphAction.TRADE
    assert result.proposal is not None
    # Structured output validates as TradeProposal with mandatory SL
    dumped = result.proposal.model_dump()
    rehydrated = TradeProposal.model_validate(dumped)
    assert rehydrated.stop_loss.price is not None or rehydrated.stop_loss.percent_from_entry is not None
    assert rehydrated.max_loss_if_stopped > 0
    assert rehydrated.symbol == "BTC_USDT"
    assert result.market is not None
    assert result.market.mode == "stub"
    assert result.portfolio is not None
    assert result.portfolio.action == GraphAction.TRADE


def test_graph_hold_when_confidence_low() -> None:
    settings = Settings(_env_file=None, anthropic_api_key="")
    graph = EvaluationGraph(llm=AnthropicClient(settings=settings))
    result = graph.evaluate_candidates(
        [_signal(confidence=0.2)],
        equity=700.0,
    )
    assert result.action == GraphAction.HOLD
    assert result.proposal is None
    assert result.hold_reason == "confidence_below_threshold"


def test_graph_hold_when_no_candidates() -> None:
    settings = Settings(_env_file=None, anthropic_api_key="")
    graph = EvaluationGraph(llm=AnthropicClient(settings=settings))
    result = graph.evaluate_candidates([], equity=700.0)
    assert result.action == GraphAction.HOLD
    assert result.hold_reason == "no_selected_signal"


def test_size_proposal_from_signal_has_stop() -> None:
    proposal = size_proposal_from_signal(
        _signal(),
        equity=700.0,
        risk_per_trade_pct=0.03,
        target_leverage=10.0,
    )
    assert proposal.stop_loss.price == pytest.approx(98.0)
    assert proposal.max_loss_if_stopped == pytest.approx(21.0)
    assert proposal.notional > 0
