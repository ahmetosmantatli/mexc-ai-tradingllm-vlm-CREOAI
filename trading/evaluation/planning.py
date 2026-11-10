"""Bounded sizing for validated candidate or model-price proposals.

Risk fractions use .01 for1%. Prices are supplied by a quant candidate or a
separately grounded/validated price-plan clone. Reapply with fresh equity and
current policy/exchange limits immediately before execution. Fees, margin,
contract minima and liquidation remain mandatory gates; a too-small plan is
rejected rather than increased to the exchange minimum.
"""
from math import isfinite
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from trading.equity.state import EquityState
from trading.models.domain import Side, StopLossSpec, TradeProposal
from trading.quant.signals import CandidateSignal


def _usable_margin_share(value):
    """A fraction of equity. Anything else is silence, not a larger share."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not isfinite(value) or not 0 < value <= 1:
        return None
    return value


class PlanSizing(BaseModel):
    """Leverage and risk are required when a model supplies a sizing plan.

    margin_share is a Runner 3 margin target as a fraction of equity.
    A missing or unusable value stays empty so the policy can select its default.
    It does not reject the rest of the plan and it cannot move the stop.
    """

    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    leverage: int = Field(ge=1)
    risk_per_trade_pct: float = Field(gt=0, le=1, description='Account risk fraction; .01 means 1%.')
    margin_share: float | None = Field(default=None, description='Margin target as a fraction of equity when supported by the active policy.')

    @model_validator(mode='before')
    @classmethod
    def ignore_unusable_margin_share(cls, value):
        if isinstance(value, dict) and 'margin_share' in value:
            value = dict(value)
            value['margin_share'] = _usable_margin_share(value.get('margin_share'))
        return value


class PlanSizingError(ValueError):
    """Invalid sizing is a HOLD, not permission to substitute larger sizing."""


def _number(value: Any, label: str, *, minimum: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanSizingError(f'invalid_{label}')
    value = float(value)
    if not isfinite(value) or value <= minimum:
        raise PlanSizingError(f'invalid_{label}')
    return value


def apply_decision_sizing(
    candidate: CandidateSignal,
    decisionplan: PlanSizing | dict | None,
    equity: float | EquityState,
    policy_risk: float,
    policy_leverage: float,
    exchange_cap: float,
) -> TradeProposal:
    """Size one selected candidate; raise PlanSizingError instead of clamping.

    ``None`` retains deterministic policy sizing with the lower current policy
    and exchange leverage. An explicit plan exceeding *either* limit is rejected.
    The exchange maximum is information, not a replacement for user policy.
    Unknown fields (including symbol, cash amount or prices) are rejected.
    """
    current_equity = _number(equity.equity if isinstance(equity, EquityState) else equity, 'equity')
    risk_limit = _number(policy_risk, 'policy_risk')
    leverage_limit = _number(policy_leverage, 'policy_leverage')
    exchange_limit = _number(exchange_cap, 'exchange_cap')
    if risk_limit > 1 or min(leverage_limit, exchange_limit) < 1:
        raise PlanSizingError('invalid_sizing_limits')
    plan = None
    if decisionplan is not None:
        try:
            # Revalidate instances too: model_construct/model_copy may bypass validation.
            raw = decisionplan.model_dump() if isinstance(decisionplan, PlanSizing) else decisionplan
            plan = PlanSizing.model_validate(raw)
        except (ValidationError, TypeError, ValueError):
            raise PlanSizingError('invalid_model_sizing') from None
        if plan.risk_per_trade_pct > risk_limit:
            raise PlanSizingError('model_risk_exceeds_policy')
        if plan.leverage > leverage_limit or plan.leverage > exchange_limit:
            raise PlanSizingError('model_leverage_exceeds_limit')
    risk = plan.risk_per_trade_pct if plan else risk_limit
    leverage = plan.leverage if plan else min(leverage_limit, exchange_limit)
    entry = _number(candidate.entry_price, 'candidate_entry')
    stop = _number(candidate.stop_loss_price, 'candidate_stop')
    target = _number(candidate.take_profit_price, 'candidate_target')
    long = candidate.side == Side.LONG
    if not (stop < entry < target if long else target < entry < stop):
        raise PlanSizingError('invalid_candidate_price_direction')
    stop_fraction = abs(entry-stop)/entry
    risk_budget = current_equity*risk
    notional = risk_budget/stop_fraction
    if not all(isfinite(value) and value > 0 for value in (stop_fraction, risk_budget, notional)):
        raise PlanSizingError('invalid_sized_amount')
    return TradeProposal(
        symbol=candidate.symbol, side=candidate.side, leverage=leverage,
        notional=notional, entry_price=entry,
        stop_loss=StopLossSpec(price=stop, percent_from_entry=stop_fraction),
        take_profit_price=target, max_loss_if_stopped=risk_budget,
        expected_edge=candidate.confidence, fee_free_pair=candidate.fee_free_pair,
        rationale=candidate.rationale,
        metadata=dict(strategy=candidate.strategy.value, signal_id=candidate.signal_id,
                      confidence=candidate.confidence, sized_from_equity=current_equity,
                      sizing_source='model' if plan else 'policy',
                      sizing_plan=plan.model_dump(exclude_none=True) if plan else None,
                      risk_per_trade_pct=risk, policy_risk_limit=risk_limit,
                      policy_leverage_limit=leverage_limit, exchange_leverage_limit=exchange_limit),
    )
