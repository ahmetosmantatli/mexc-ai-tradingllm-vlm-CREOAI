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
    skipped: str | None = None


@dataclass
class TradingPipeline:
    """Wires Quant scanner → LangGraph → Risk Engine → PaperBroker.

    LIVE path is never selected here; worker refuses LIVE_TRADING_ENABLED.
    """

    settings: Settings = field(default_factory=get_settings)
    public_client: MexcPublicClient | None = None
    graph: EvaluationGraph | None = None
    risk_engine: RiskEngine | None = None
    broker: Broker | None = None
    open_risk: float = 0.0
    open_positions: int = 0
    floor_activated: bool = False
    halt_new_risk: bool = False
    audit_log: list[CycleAudit] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.public_client is None:
            self.public_client = MexcPublicClient(base_url=self.settings.mexc_base_url)
        if self.graph is None:
            self.graph = EvaluationGraph()
        if self.risk_engine is None:
            preset = get_preset(self.settings.risk_profile)
            self.risk_engine = RiskEngine(settings=self.settings, preset=preset)
        if self.broker is None:
            self.broker = get_broker(self.settings)

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

    def _risk_context(self, equity: float) -> RiskContext:
        return RiskContext(
            equity=equity,
            starting_capital=self.settings.starting_capital_try,
            capital_floor=self.settings.capital_floor_try,
            open_risk=self.open_risk,
            open_positions=self.open_positions,
            always_on_scan=self.settings.always_on_scan,
            floor_activated=self.floor_activated or equity > self.settings.starting_capital_try,
            halt_new_risk=self.halt_new_risk,
        )

    def run_cycle(self, symbols: list[str] | None = None) -> CycleAudit:
        if self.settings.live_trading_enabled:
            audit = CycleAudit(skipped="LIVE_TRADING_ENABLED_REFUSED")
            self.audit_log.append(audit)
            logger.error("refusing cycle: LIVE_TRADING_ENABLED must stay false in Phase 2")
            return audit

        assert self.graph is not None and self.risk_engine is not None and self.broker is not None

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

        audit = CycleAudit(scan=scan, evaluation=evaluation)

        if evaluation.action == GraphAction.HOLD or evaluation.proposal is None:
            audit.skipped = evaluation.hold_reason or "hold"
            self.audit_log.append(audit)
            logger.info(
                "cycle HOLD | scanned=%s kept=%s candidates=%s reason=%s",
                scan.scanned,
                scan.kept,
                len(scan.candidates),
                audit.skipped,
            )
            return audit

        proposal: TradeProposal = evaluation.proposal
        decision = self.risk_engine.evaluate(proposal, self._risk_context(equity))
        audit.decision = decision

        if decision.action != DecisionAction.ACCEPT:
            audit.skipped = "risk_reject"
            self.audit_log.append(audit)
            logger.info(
                "cycle RISK_REJECT | proposal=%s reasons=%s",
                proposal.proposal_id,
                decision.reason_codes,
            )
            return audit

        request = BrokerOrderRequest.from_proposal(proposal)
        if proposal.entry_price:
            request.metadata["mark_price"] = proposal.entry_price
        result = self.broker.place_order(request)
        self.open_positions += 1
        self.open_risk += proposal.max_loss_if_stopped
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
            "cycle PAPER_FILL | symbol=%s side=%s notional=%.4f proposal=%s",
            proposal.symbol,
            proposal.side.value,
            proposal.notional,
            proposal.proposal_id,
        )
        return audit
