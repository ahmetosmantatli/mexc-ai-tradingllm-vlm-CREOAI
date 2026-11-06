"""Pipeline: Risk Engine reject path before PaperBroker; paper-only."""

from __future__ import annotations

from trading.agents.graph import EvaluationGraph
from trading.agents.schemas import EvaluationResult, GraphAction
from trading.brokers.paper import PaperBroker
from trading.config import RiskProfile, Settings
from trading.exchange.mexc_public import MexcPublicClient, TickerSnapshot
from trading.models.domain import DecisionAction, Side, StopLossSpec, TradeProposal
from trading.pipeline import TradingPipeline
from trading.quant.scanner import ScanResult
from trading.quant.signals import CandidateSignal, StrategyName
from trading.risk.engine import RiskEngine
from trading.risk.presets import get_preset


class _FixedGraph(EvaluationGraph):
    """Inject a fixed EvaluationResult (bypass scanner/LLM)."""

    def __init__(self, result: EvaluationResult) -> None:
        super().__init__()
        self._fixed = result

    def evaluate_candidates(self, *args, **kwargs) -> EvaluationResult:  # noqa: ANN002
        return self._fixed


class _EmptyScanPipeline(TradingPipeline):
    def _scanner(self):  # type: ignore[override]
        class _S:
            def scan(self, symbols=None):  # noqa: ANN001
                return ScanResult(scanned=0, kept=0, candidates=[])

        return _S()


def _wipe_proposal() -> TradeProposal:
    return TradeProposal(
        symbol="BTC_USDT",
        side=Side.LONG,
        leverage=25.0,
        notional=700.0,
        entry_price=100.0,
        stop_loss=StopLossSpec(percent_from_entry=0.02),
        take_profit_price=110.0,
        max_loss_if_stopped=700.0,  # full wipe → risk reject
        rationale="force risk reject",
    )


def test_pipeline_risk_reject_does_not_execute_paper() -> None:
    settings = Settings(
        _env_file=None,
        live_trading_enabled=False,
        risk_profile=RiskProfile.AGGRESSIVE_GROWTH,
    )
    broker = PaperBroker(starting_equity=700.0)
    proposal = _wipe_proposal()
    graph = _FixedGraph(
        EvaluationResult(
            action=GraphAction.TRADE,
            proposal=proposal,
            selected_signal=CandidateSignal(
                symbol="BTC_USDT",
                side=Side.LONG,
                strategy=StrategyName.TREND,
                entry_price=100.0,
                stop_loss_price=98.0,
                take_profit_price=104.0,
                confidence=0.9,
            ),
        )
    )
    pipeline = _EmptyScanPipeline(
        settings=settings,
        public_client=MexcPublicClient(use_fixtures=True),
        graph=graph,
        risk_engine=RiskEngine(
            settings=settings,
            preset=get_preset(RiskProfile.AGGRESSIVE_GROWTH),
        ),
        broker=broker,
    )
    audit = pipeline.run_cycle()
    assert audit.decision is not None
    assert audit.decision.action == DecisionAction.REJECT
    assert "SINGLE_TRADE_WIPE_FORBIDDEN" in audit.decision.reason_codes
    assert audit.execution is None
    assert audit.skipped == "risk_reject"
    assert broker._orders == {}  # noqa: SLF001 — no paper fills


def test_pipeline_hold_skips_risk_and_broker() -> None:
    settings = Settings(_env_file=None, live_trading_enabled=False)
    broker = PaperBroker(starting_equity=700.0)
    graph = _FixedGraph(
        EvaluationResult(action=GraphAction.HOLD, hold_reason="no_edge")
    )
    pipeline = _EmptyScanPipeline(
        settings=settings,
        public_client=MexcPublicClient(use_fixtures=True),
        graph=graph,
        risk_engine=RiskEngine(settings=settings),
        broker=broker,
    )
    audit = pipeline.run_cycle()
    assert audit.decision is None
    assert audit.execution is None
    assert audit.skipped == "no_edge"


def test_pipeline_accept_places_paper_fill_only() -> None:
    settings = Settings(
        _env_file=None,
        live_trading_enabled=False,
        risk_per_trade_pct=0.03,
    )
    broker = PaperBroker(starting_equity=700.0)
    proposal = TradeProposal(
        symbol="ETH_USDT",
        side=Side.LONG,
        leverage=10.0,
        notional=100.0,
        entry_price=2000.0,
        stop_loss=StopLossSpec(price=1960.0, percent_from_entry=0.02),
        take_profit_price=2080.0,
        max_loss_if_stopped=21.0,
        fee_free_pair=True,
        rationale="accepted paper path",
    )
    graph = _FixedGraph(EvaluationResult(action=GraphAction.TRADE, proposal=proposal))
    pipeline = _EmptyScanPipeline(
        settings=settings,
        public_client=MexcPublicClient(
            use_fixtures=True,
            fixture_tickers={
                "ETH_USDT": TickerSnapshot(symbol="ETH_USDT", last_price=2000.0)
            },
        ),
        graph=graph,
        risk_engine=RiskEngine(
            settings=settings,
            preset=get_preset(RiskProfile.AGGRESSIVE_GROWTH),
        ),
        broker=broker,
    )
    audit = pipeline.run_cycle()
    assert audit.decision is not None
    assert audit.decision.accepted
    assert audit.execution is not None
    assert audit.execution["is_simulated"] is True
    assert audit.execution["status"] == "filled"
    assert len(broker._orders) == 1  # noqa: SLF001


def test_pipeline_refuses_live_flag() -> None:
    settings = Settings(_env_file=None, live_trading_enabled=True)
    pipeline = TradingPipeline(
        settings=settings,
        public_client=MexcPublicClient(use_fixtures=True),
        broker=PaperBroker(starting_equity=700.0),
    )
    audit = pipeline.run_cycle()
    assert audit.skipped == "LIVE_TRADING_ENABLED_REFUSED"
