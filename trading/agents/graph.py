"""LangGraph workflow: Market Analyst → Strategy Reviewer → Portfolio Analyst.

Produces structured EvaluationResult (TradeProposal or HOLD).
When ANTHROPIC_API_KEY is missing, nodes run in deterministic stub mode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from trading.agents.anthropic_client import AnthropicClient
from trading.agents.schemas import (
    EvaluationResult,
    GraphAction,
    GraphState,
    MarketAnalystNote,
    PortfolioAnalystNote,
    StrategyReviewNote,
)
from trading.equity.sizing import max_loss_budget, notional_for_stop
from trading.equity.state import EquityState
from trading.models.domain import Side, StopLossSpec, TradeProposal
from trading.quant.signals import CandidateSignal

logger = logging.getLogger(__name__)

MIN_CONFIDENCE_TO_TRADE = 0.55


def _signal_by_id(state: GraphState, signal_id: str | None) -> CandidateSignal | None:
    if not signal_id:
        return None
    for cand in state.candidates:
        if cand.signal_id == signal_id:
            return cand
    return None


def _best_candidate(state: GraphState) -> CandidateSignal | None:
    if not state.candidates:
        return None
    return max(state.candidates, key=lambda c: c.confidence)


def size_proposal_from_signal(
    signal: CandidateSignal,
    *,
    equity: float | EquityState,
    risk_per_trade_pct: float,
    target_leverage: float,
    rationale_prefix: str = "",
) -> TradeProposal:
    """Convert a CandidateSignal into a TradeProposal sized from live equity E.

    ``equity`` must be the current bankroll (or EquityState). Do not pass the
    static STARTING_CAPITAL_TRY env constant unless that is truly the live E.
    """
    stop_pct = max(signal.stop_distance_pct, 1e-4)
    max_loss = max(max_loss_budget(equity, risk_per_trade_pct), 1e-6)
    notional = notional_for_stop(
        equity,
        risk_per_trade_pct=risk_per_trade_pct,
        stop_distance_pct=stop_pct,
    )
    leverage = max(1.0, min(float(target_leverage), 25.0))
    entry = signal.entry_price
    stop = signal.stop_loss_price
    pct_from_entry = abs(entry - stop) / entry
    rationale = signal.rationale
    if rationale_prefix:
        rationale = f"{rationale_prefix} | {rationale}"
    live_e = float(equity.equity) if isinstance(equity, EquityState) else float(equity)
    return TradeProposal(
        symbol=signal.symbol,
        side=signal.side,
        leverage=leverage,
        notional=round(notional, 4),
        entry_price=entry,
        stop_loss=StopLossSpec(price=stop, percent_from_entry=pct_from_entry),
        take_profit_price=signal.take_profit_price,
        max_loss_if_stopped=round(max_loss, 4),
        expected_edge=signal.confidence,
        fee_free_pair=signal.fee_free_pair,
        rationale=rationale,
        metadata={
            "strategy": signal.strategy.value,
            "signal_id": signal.signal_id,
            "confidence": signal.confidence,
            "sized_from_equity": live_e,
        },
    )


def market_analyst_node(state: GraphState, llm: AnthropicClient) -> GraphState:
    symbols = sorted({c.symbol for c in state.candidates})
    stub = MarketAnalystNote(
        summary=(
            f"Stub market view over {len(state.candidates)} candidates "
            f"across {len(symbols)} symbols; always_on={state.always_on_scan}."
        ),
        regime="mixed" if state.candidates else "quiet",
        favor_symbols=symbols[:5],
        concerns=[] if state.candidates else ["no_candidates"],
        mode="stub",
    )
    if llm.available:
        payload = llm.complete_json(
            system=(
                "You are a futures market analyst. Reply ONLY with JSON keys: "
                "summary, regime, favor_symbols, concerns. Do not place orders."
            ),
            user=(
                f"risk_profile={state.risk_profile} equity={state.equity} "
                f"candidates={[c.model_dump(mode='json') for c in state.candidates[:8]]}"
            ),
        )
        if payload:
            stub = MarketAnalystNote(
                summary=str(payload.get("summary") or stub.summary),
                regime=str(payload.get("regime") or stub.regime),
                favor_symbols=list(payload.get("favor_symbols") or stub.favor_symbols),
                concerns=list(payload.get("concerns") or stub.concerns),
                mode="llm",
            )
    state.market = stub
    state.llm_enabled = llm.available
    return state


def strategy_reviewer_node(state: GraphState, llm: AnthropicClient) -> GraphState:
    best = _best_candidate(state)
    rejected = [c.signal_id for c in state.candidates if best and c.signal_id != best.signal_id]
    stub = StrategyReviewNote(
        selected_signal_id=best.signal_id if best else None,
        rejected_signal_ids=rejected,
        review=(
            f"Selected {best.strategy.value} on {best.symbol} "
            f"(confidence={best.confidence:.2f})"
            if best
            else "No candidate met selection criteria"
        ),
        confidence_adjustment=0.0,
        mode="stub",
    )
    if llm.available and state.candidates:
        payload = llm.complete_json(
            system=(
                "You are a strategy reviewer. Reply ONLY with JSON keys: "
                "selected_signal_id, rejected_signal_ids, review, confidence_adjustment. "
                "Never place orders."
            ),
            user=(
                f"market={state.market.model_dump() if state.market else {}} "
                f"candidates={[c.model_dump(mode='json') for c in state.candidates[:8]]}"
            ),
        )
        if payload:
            selected = payload.get("selected_signal_id")
            if selected and _signal_by_id(state, str(selected)) is None:
                selected = stub.selected_signal_id
            stub = StrategyReviewNote(
                selected_signal_id=str(selected) if selected else None,
                rejected_signal_ids=list(payload.get("rejected_signal_ids") or rejected),
                review=str(payload.get("review") or stub.review),
                confidence_adjustment=float(payload.get("confidence_adjustment") or 0.0),
                mode="llm",
            )
    state.strategy = stub
    return state


def portfolio_analyst_node(state: GraphState, llm: AnthropicClient) -> GraphState:
    selected = _signal_by_id(state, state.strategy.selected_signal_id if state.strategy else None)
    adj_confidence = (selected.confidence + (state.strategy.confidence_adjustment if state.strategy else 0.0)) if selected else 0.0
    adj_confidence = max(0.0, min(1.0, adj_confidence))

    max_loss_budget = state.equity * state.risk_per_trade_pct
    should_trade = (
        selected is not None
        and adj_confidence >= MIN_CONFIDENCE_TO_TRADE
        and state.equity > 0
    )

    hold_reason = None
    if selected is None:
        hold_reason = "no_selected_signal"
    elif adj_confidence < MIN_CONFIDENCE_TO_TRADE:
        hold_reason = "confidence_below_threshold"
    elif state.equity <= 0:
        hold_reason = "equity_zero"

    if llm.available:
        payload = llm.complete_json(
            system=(
                "You are a portfolio analyst. Reply ONLY with JSON keys: "
                "action ('trade'|'hold'), sizing_notes, hold_reason. "
                "Respect risk_per_trade_pct. Never place exchange orders."
            ),
            user=(
                f"equity={state.equity} open_risk={state.open_risk} "
                f"open_positions={state.open_positions} "
                f"risk_per_trade_pct={state.risk_per_trade_pct} "
                f"selected={selected.model_dump(mode='json') if selected else None} "
                f"adj_confidence={adj_confidence}"
            ),
        )
        if payload and str(payload.get("action", "")).lower() == "hold":
            should_trade = False
            hold_reason = str(payload.get("hold_reason") or hold_reason or "llm_hold")
        elif payload and str(payload.get("action", "")).lower() == "trade" and selected:
            should_trade = adj_confidence >= MIN_CONFIDENCE_TO_TRADE

    if llm.available and (not payload or str(payload.get("action", "")).lower() not in {"trade", "hold"}):
        should_trade = False
        hold_reason = "llm_unavailable_or_invalid"

    if should_trade and selected is not None:
        proposal = size_proposal_from_signal(
            selected,
            equity=state.equity,
            risk_per_trade_pct=state.risk_per_trade_pct,
            target_leverage=state.target_leverage,
            rationale_prefix="portfolio_ok",
        )
        portfolio = PortfolioAnalystNote(
            action=GraphAction.TRADE,
            sizing_notes=f"max_loss_budget={max_loss_budget:.4f}",
            max_loss_budget=max_loss_budget,
            mode="llm" if llm.available and payload else "stub",
        )
        state.portfolio = portfolio
        state.result = EvaluationResult(
            action=GraphAction.TRADE,
            proposal=proposal,
            market=state.market,
            strategy=state.strategy,
            portfolio=portfolio,
            selected_signal=selected,
            metadata={"adj_confidence": adj_confidence, "llm_enabled": llm.available},
        )
        return state

    portfolio = PortfolioAnalystNote(
        action=GraphAction.HOLD,
        sizing_notes="no trade sized",
        max_loss_budget=max_loss_budget,
        mode="llm" if llm.available and payload else "stub",
    )
    state.portfolio = portfolio
    state.result = EvaluationResult(
        action=GraphAction.HOLD,
        hold_reason=hold_reason or "hold",
        market=state.market,
        strategy=state.strategy,
        portfolio=portfolio,
        selected_signal=selected,
        metadata={"adj_confidence": adj_confidence, "llm_enabled": llm.available},
    )
    return state


NodeFn = Callable[[GraphState], GraphState]


@dataclass
class EvaluationGraph:
    """LangGraph-backed evaluator with sequential fallback."""

    llm: AnthropicClient = field(default_factory=AnthropicClient)

    def _nodes(self) -> list[tuple[str, NodeFn]]:
        return [
            ("market_analyst", lambda s: market_analyst_node(s, self.llm)),
            ("strategy_reviewer", lambda s: strategy_reviewer_node(s, self.llm)),
            ("portfolio_analyst", lambda s: portfolio_analyst_node(s, self.llm)),
        ]

    def _run_sequential(self, state: GraphState) -> EvaluationResult:
        current = state
        for name, fn in self._nodes():
            logger.debug("graph node %s", name)
            current = fn(current)
        if current.result is None:
            return EvaluationResult(action=GraphAction.HOLD, hold_reason="empty_result")
        return current.result

    def _run_langgraph(self, state: GraphState) -> EvaluationResult | None:
        try:
            from langgraph.graph import END, StateGraph  # type: ignore
        except ImportError:
            return None

        def _to_dict(s: GraphState) -> dict[str, Any]:
            return s.model_dump()

        def _from_dict(d: dict[str, Any]) -> GraphState:
            return GraphState.model_validate(d)

        builder: Any = StateGraph(dict)

        def wrap(fn: NodeFn) -> Callable[[dict[str, Any]], dict[str, Any]]:
            def _inner(payload: dict[str, Any]) -> dict[str, Any]:
                return _to_dict(fn(_from_dict(payload)))

            return _inner

        for name, fn in self._nodes():
            builder.add_node(name, wrap(fn))
        builder.set_entry_point("market_analyst")
        builder.add_edge("market_analyst", "strategy_reviewer")
        builder.add_edge("strategy_reviewer", "portfolio_analyst")
        builder.add_edge("portfolio_analyst", END)
        app = builder.compile()
        out = app.invoke(_to_dict(state))
        final = _from_dict(out)
        if final.result is None:
            return EvaluationResult(action=GraphAction.HOLD, hold_reason="empty_result")
        return final.result

    def run(self, state: GraphState) -> EvaluationResult:
        # Prefer LangGraph when installed; always fall back to sequential for robustness.
        result = self._run_langgraph(state)
        if result is not None:
            return result
        return self._run_sequential(state)

    def evaluate_candidates(
        self,
        candidates: list[CandidateSignal],
        *,
        equity: float,
        open_risk: float = 0.0,
        open_positions: int = 0,
        risk_profile: str = "aggressive_growth",
        risk_per_trade_pct: float = 0.03,
        target_leverage: float = 10.0,
        always_on_scan: bool = True,
    ) -> EvaluationResult:
        if not candidates:
            return EvaluationResult(action=GraphAction.HOLD, hold_reason="no_selected_signal")
        state = GraphState(
            candidates=candidates,
            equity=equity,
            open_risk=open_risk,
            open_positions=open_positions,
            risk_profile=risk_profile,
            risk_per_trade_pct=risk_per_trade_pct,
            target_leverage=target_leverage,
            always_on_scan=always_on_scan,
        )
        return self.run(state)
