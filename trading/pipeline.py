"""Paper-only loop: scan → propose → risk → paper execute."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from trading.agents.graph import EvaluationGraph
from trading.agents.schemas import EvaluationResult, GraphAction
from trading.brokers.base import Broker, BrokerOrderRequest
from trading.brokers.factory import get_broker
from trading.config import Settings, get_settings
from trading.equity.service import EquityService
from trading.equity.state import EquityState
from trading.exchange.mexc_public import MexcPublicClient
from trading.models.domain import DecisionAction, RiskDecision, TradeProposal
from trading.quant.scanner import LiquidityFilter, MarketScanner, ScanResult
from trading.risk.engine import RiskContext, RiskEngine
from trading.risk.presets import get_preset

logger = logging.getLogger(__name__)


@dataclass
class CycleAudit:
    scan: ScanResult | None = None
    evaluation: EvaluationResult | None = None
    decision: RiskDecision | None = None
    execution: dict[str, Any] | None = None
    equity: EquityState | None = None
    skipped: str | None = None


@dataclass
class TradingPipeline:
    """Wires Quant scanner → LangGraph → Risk Engine → PaperBroker.

    LIVE path is never selected here; worker refuses LIVE_TRADING_ENABLED.
    Risk / sizing use live equity E from EquityService — not static env capital.
    """

    settings: Settings = field(default_factory=get_settings)
    public_client: MexcPublicClient | None = None
    graph: EvaluationGraph | None = None
    risk_engine: RiskEngine | None = None
    broker: Broker | None = None
    equity_service: EquityService | None = None
    open_risk: float = 0.0
    open_positions: int = 0
    floor_activated: bool = False
    halt_new_risk: bool = False
    audit_log: list[CycleAudit] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.equity_service is None:
            self.equity_service = EquityService.from_settings(self.settings)
        equity_state = self.equity_service.resolve()
        if self.public_client is None:
            self.public_client = MexcPublicClient(base_url=self.settings.mexc_base_url)
        if self.graph is None:
            self.graph = EvaluationGraph()
        if self.risk_engine is None:
            preset = get_preset(self.settings.risk_profile)
            self.risk_engine = RiskEngine(settings=self.settings, preset=preset)
        if self.broker is None:
            self.broker = get_broker(self.settings, equity_state=equity_state)
        else:
            # Keep injected broker equity aligned with resolved state on boot.
            if hasattr(self.broker, "set_equity"):
                self.broker.set_equity(equity_state.equity)

    def _scanner(self) -> MarketScanner:
        assert self.public_client is not None
        return MarketScanner(
            client=self.public_client,
            liquidity=LiquidityFilter(
                min_quote_volume_24h=self.settings.scanner_min_quote_volume_24h,
                max_spread_bps=self.settings.scanner_max_spread_bps,
            ),
            prefer_fee_free=self.settings.fee_free_preference,
            max_symbols=self.settings.scanner_max_symbols,
        )

    def _equity_state(self) -> EquityState:
        assert self.equity_service is not None
        return self.equity_service.current or self.equity_service.resolve()

    def _risk_context(self, equity: float) -> RiskContext:
        state = self._equity_state()
        return RiskContext(
            equity=equity,
            starting_capital=state.starting_capital,
            capital_floor=state.capital_floor,
            open_risk=self.open_risk,
            open_positions=self.open_positions,
            always_on_scan=self.settings.always_on_scan,
            floor_activated=self.floor_activated or equity > state.starting_capital,
            halt_new_risk=self.halt_new_risk,
        )

    def run_cycle(self, symbols: list[str] | None = None) -> CycleAudit:
        if self.settings.live_trading_enabled:
            audit = CycleAudit(skipped="LIVE_TRADING_ENABLED_REFUSED")
            self.audit_log.append(audit)
            logger.error("refusing cycle: LIVE_TRADING_ENABLED must stay false in Phase 2")
            return audit

        assert (
            self.graph is not None
            and self.risk_engine is not None
            and self.broker is not None
            and self.equity_service is not None
        )

        # Refresh equity view (MEXC read-only when keys present; else persisted paper).
        equity_state = self.equity_service.resolve()
        if hasattr(self.broker, "set_equity") and equity_state.source.value == "mexc_readonly":
            self.broker.set_equity(equity_state.equity)

        scan = self._scanner().scan(symbols)
        equity = float(self.broker.get_equity())
        evaluation = self.graph.evaluate_candidates(
            scan.candidates,
            equity=equity,
            open_risk=self.open_risk,
            open_positions=self.open_positions,
            risk_profile=self.settings.risk_profile.value,
            risk_per_trade_pct=self.settings.risk_per_trade_pct,
            target_leverage=self.settings.default_target_leverage,
            always_on_scan=self.settings.always_on_scan,
        )

        audit = CycleAudit(scan=scan, evaluation=evaluation, equity=equity_state)

        if evaluation.action == GraphAction.HOLD or evaluation.proposal is None:
            audit.skipped = evaluation.hold_reason or "hold"
            self.equity_service.persist_paper_equity(
                equity,
                starting_capital=equity_state.starting_capital,
                capital_floor=equity_state.capital_floor,
            )
            self.audit_log.append(audit)
            logger.info(
                "cycle HOLD | scanned=%s kept=%s candidates=%s reason=%s equity=%.4f source=%s",
                scan.scanned,
                scan.kept,
                len(scan.candidates),
                audit.skipped,
                equity,
                equity_state.source.value,
            )
            return audit

        proposal: TradeProposal = evaluation.proposal
        decision = self.risk_engine.evaluate(proposal, self._risk_context(equity))
        audit.decision = decision

        if decision.action != DecisionAction.ACCEPT:
            audit.skipped = "risk_reject"
            self.equity_service.persist_paper_equity(
                equity,
                starting_capital=equity_state.starting_capital,
                capital_floor=equity_state.capital_floor,
            )
            self.audit_log.append(audit)
            logger.info(
                "cycle RISK_REJECT | proposal=%s reasons=%s equity=%.4f",
                proposal.proposal_id,
                decision.reason_codes,
                equity,
            )
            return audit

        request = BrokerOrderRequest.from_proposal(proposal)
        if proposal.entry_price:
            request.metadata["mark_price"] = proposal.entry_price
        result = self.broker.place_order(request)
        self.open_positions += 1
        self.open_risk += proposal.max_loss_if_stopped
        # Persist post-cycle paper equity (broker may adjust later via PnL hooks).
        self.equity_service.persist_paper_equity(
            float(self.broker.get_equity()),
            starting_capital=equity_state.starting_capital,
            capital_floor=equity_state.capital_floor,
        )
        audit.execution = {
            "client_order_id": result.client_order_id,
            "status": result.status,
            "is_simulated": result.is_simulated,
            "fill_price": result.fill_price,
            "message": result.message,
            "audit_id": str(uuid4()),
        }
        self.audit_log.append(audit)
        logger.info(
            "cycle PAPER_FILL | symbol=%s side=%s notional=%.4f proposal=%s equity=%.4f",
            proposal.symbol,
            proposal.side.value,
            proposal.notional,
            proposal.proposal_id,
            float(self.broker.get_equity()),
        )
        return audit
