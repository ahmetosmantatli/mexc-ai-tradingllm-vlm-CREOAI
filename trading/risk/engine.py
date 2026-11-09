"""Phase 1 Risk Engine stub: mandatory SL, max loss / residual, floor fields, always-on flag."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite

from trading.config import RiskProfile, Settings, get_settings
from trading.models.domain import DecisionAction, RiskDecision, TradeProposal
from trading.risk.presets import RiskPreset, get_preset


@dataclass
class RiskContext:
    equity: float
    starting_capital: float
    capital_floor: float
    open_risk: float = 0.0
    open_positions: int = 0
    daily_loss_pct: float = 0.0
    weekly_loss_pct: float = 0.0
    drawdown_pct: float = 0.0
    exchange_max_leverage: float | None = None
    floor_activated: bool = False
    always_on_scan: bool = True
    halt_new_risk: bool = False
    enforce_position_limit: bool = True


@dataclass
class RiskEngine:
    """Deterministic gate. AI cannot bypass; every decision carries reason codes."""

    settings: Settings = field(default_factory=get_settings)
    preset: RiskPreset | None = None

    def __post_init__(self) -> None:
        if self.preset is None:
            self.preset = get_preset(self.settings.risk_profile)

    def evaluate(self, proposal: TradeProposal, context: RiskContext) -> RiskDecision:
        reasons: list[str] = []
        preset = self.preset or get_preset(RiskProfile.AGGRESSIVE_GROWTH)

        values = [context.equity, context.open_risk, proposal.notional, proposal.leverage,
                  proposal.max_loss_if_stopped, context.daily_loss_pct, context.weekly_loss_pct, context.drawdown_pct]
        if not all(isfinite(v) for v in values):
            return RiskDecision(action=DecisionAction.REJECT, proposal_id=proposal.proposal_id,
                                reason_codes=["NONFINITE_RISK_INPUT"])
        major = proposal.symbol in {"BTC_USDT", "ETH_USDT"}
        cap=preset.max_leverage_majors if major else preset.max_leverage_alts
        live_exchange=(getattr(self.settings.broker_mode,'value',self.settings.broker_mode)=='live'
            and getattr(self.settings,'live_leverage_mode','profile')=='exchange')
        if live_exchange:
            observed=context.exchange_max_leverage
            if isinstance(observed,bool) or not isinstance(observed,(int,float)) or not isfinite(observed) or observed<1:
                reasons.append('UNVERIFIED_EXCHANGE_LEVERAGE_CAP')
            else:
                cap=observed
        if proposal.leverage > cap:
            reasons.append("LEVERAGE_CAP")
        if proposal.notional / proposal.leverage > context.equity:
            reasons.append("INSUFFICIENT_MARGIN")
        if proposal.entry_price and proposal.stop_loss and proposal.stop_loss.price:
            entry, stop = proposal.entry_price, proposal.stop_loss.price
            if not isfinite(entry) or not isfinite(stop) or (stop >= entry if proposal.side.value == "long" else stop <= entry):
                reasons.append("INVALID_STOP_DIRECTION")
            modeled_loss = proposal.notional * abs(entry-stop) / entry
            if modeled_loss > proposal.max_loss_if_stopped + 1e-8:
                reasons.append("UNDERSTATED_STOP_LOSS")

        # 1) Mandatory stop-loss (schema usually enforces; double-check).
        if proposal.stop_loss is None:
            reasons.append("MISSING_STOP_LOSS")
        else:
            has_price = proposal.stop_loss.price is not None
            has_pct = proposal.stop_loss.percent_from_entry is not None
            if not has_price and not has_pct:
                reasons.append("MISSING_STOP_LOSS")

        if proposal.max_loss_if_stopped <= 0:
            reasons.append("INVALID_MAX_LOSS")

        # 2) Max loss per trade — one stop cannot wipe equity.
        equity = max(context.equity, 0.0)
        if equity <= 0:
            reasons.append("EQUITY_ZERO_HALT")

        risk_cap = min(preset.risk_per_trade_pct, preset.abs_max_loss_pct, self.settings.abs_max_loss_pct, self.settings.risk_per_trade_pct)
        max_allowed_loss = equity * risk_cap
        if proposal.max_loss_if_stopped > max_allowed_loss + 1e-9:
            reasons.append("MAX_LOSS_EXCEEDS_CAP")

        residual = equity - proposal.max_loss_if_stopped - max(context.open_risk, 0.0)
        residual_floor = equity * max(preset.residual_min_pct, self.settings.residual_min_pct)
        if residual < residual_floor - 1e-9:
            reasons.append("RESIDUAL_CAPITAL_TOO_LOW")

        # Single-trade wipe ban: stop must leave tradeable residual (> 0 and above residual_min).
        if proposal.max_loss_if_stopped >= equity - 1e-9:
            reasons.append("SINGLE_TRADE_WIPE_FORBIDDEN")

        # 3) Capital floor fields — halt new risk that would breach floor when activated.
        if preset.capital_floor_enabled and context.floor_activated:
            projected = equity - proposal.max_loss_if_stopped - max(context.open_risk, 0.0)
            if projected < context.capital_floor - 1e-9:
                reasons.append("CAPITAL_FLOOR_THREAT")

        # 4) Always-on scan is a settings/preset flag (surfaced for workers; not a reject).
        # Workers read context.always_on_scan / preset.always_on_scan; engine records profile.

        if context.halt_new_risk:
            reasons.append("HALT_NEW_RISK")

        if context.enforce_position_limit and context.open_positions >= preset.max_concurrent_positions:
            reasons.append("MAX_CONCURRENT_POSITIONS")

        if context.open_risk + proposal.max_loss_if_stopped > equity * preset.total_open_risk_pct + 1e-9:
            reasons.append("TOTAL_OPEN_RISK_CAP")

        if context.daily_loss_pct >= preset.daily_loss_halt_pct:
            reasons.append("DAILY_LOSS_HALT")

        if preset.weekly_loss_halt_enabled and context.weekly_loss_pct >= preset.weekly_loss_halt_pct:
            reasons.append("WEEKLY_LOSS_HALT")

        if reasons:
            return RiskDecision(
                action=DecisionAction.REJECT,
                proposal_id=proposal.proposal_id,
                reason_codes=reasons,
                notes="Phase 1 risk stub reject",
            )

        return RiskDecision(
            action=DecisionAction.ACCEPT,
            proposal_id=proposal.proposal_id,
            reason_codes=["OK"],
            notes=(
                f"Accepted under {preset.profile.value}; "
                f"always_on_scan={preset.always_on_scan and context.always_on_scan}"
            ),
        )
