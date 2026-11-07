"""Research/paper-only allocator; not connected to the executable worker.

Risk and intended margin are inputs, not model authority to bypass account risk.
Uses the base-tier isolated approximation from the paper model, not exact MEXC
liquidation parity. No funding/depth/partial-fill guarantee is implied.
"""
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from math import floor, isfinite


class InfeasiblePlan(ValueError):
    pass


@dataclass(frozen=True)
class AdaptivePlan:
    side: str
    entry: float
    stop: float
    target: float
    estimated_stop_fill: float
    contracts: float
    quantity: float
    notional: float
    leverage: int
    margin: float
    entry_fee: float
    estimated_stop_loss: float
    risk_budget: float
    allowed_margin_with_fee: float
    estimated_liquidation: float
    stop_liquidation_buffer_pct: float
    exchange_max_leverage: float
    sizing_reason: str = 'minimum feasible leverage for stop-sized position'
    research_only: bool = True

    def audit(self):
        return asdict(self)


def _grid(value, step, rounding):
    decimal_step = Decimal(str(step))
    units = Decimal(str(value)) / decimal_step
    return float(units.to_integral_value(rounding=rounding) * decimal_step)


def choose_plan(eq, available, entry, stop, target, risk_pct,
                requested_margin_fraction, rules, fee_rate=.0006,
                slippage_bps=5, liquidation_buffer_pct=.02):
    """Choose the smallest feasible integer leverage, never a fixed profile cap.

    ``entry`` must already be a current executable-price estimate. Planned stop
    loss includes two-sided taker fees and adverse stop slippage. Prices are
    rounded conservatively to the contract grid before sizing. ``risk_pct`` is
    a caller-validated risk ceiling, not an instruction to deploy all equity.
    Missing/custom/tiered rules must be rejected by ContractRules.parse first.
    """
    values = (eq, available, entry, stop, target, risk_pct,
              requested_margin_fraction, fee_rate, slippage_bps,
              liquidation_buffer_pct, rules.max_leverage, rules.maintenance,
              rules.price_step)
    if not all(isfinite(float(v)) for v in values):
        raise InfeasiblePlan('nonfinite allocator input')
    if min(eq, available, entry, stop, target, rules.price_step) <= 0:
        raise InfeasiblePlan('equity, margin, prices and price step must be positive')
    if not 0 < risk_pct < 1 or not 0 < requested_margin_fraction <= 1:
        raise InfeasiblePlan('risk and margin fractions outside valid range')
    if not 0 <= fee_rate < .1 or not 0 <= slippage_bps < 10000:
        raise InfeasiblePlan('invalid execution cost estimate')
    if not 0 <= liquidation_buffer_pct < 1 or not 0 < rules.maintenance < 1:
        raise InfeasiblePlan('invalid liquidation assumptions')
    long = stop < entry < target
    short = target < entry < stop
    if not (long or short):
        raise InfeasiblePlan('stop/target direction inconsistent')
    stop = _grid(stop, rules.price_step, ROUND_FLOOR if long else ROUND_CEILING)
    target = _grid(target, rules.price_step, ROUND_FLOOR if long else ROUND_CEILING)
    if stop <= 0 or target <= 0 or not (stop < entry < target if long else target < entry < stop):
        raise InfeasiblePlan('price grid invalidates setup')
    slip = slippage_bps / 10000
    stop_fill = stop * (1-slip if long else 1+slip)
    loss_fraction = abs(entry-stop_fill)/entry + fee_rate*(1+stop_fill/entry)
    budget = eq * risk_pct
    requested_notional = budget / loss_fraction
    try:
        # Quantity constraints are independent of leverage; validate base tier first.
        contracts, notional = rules.size_order(requested_notional, entry, 1)
    except (ValueError, ArithmeticError) as exc:
        raise InfeasiblePlan('stop-sized quantity outside supported exchange/base-tier limits') from exc
    quantity = notional/entry
    entry_fee = notional*fee_rate
    allowed = min(eq*requested_margin_fraction, available)
    room = allowed-entry_fee
    if room <= 0:
        raise InfeasiblePlan('available margin cannot cover entry fee')
    max_leverage = floor(rules.max_leverage)
    # Iterate only the supported integer interval. Exchange max is dynamic, not
    # a trade request. Stop-distance feasibility normally bounds this far lower.
    if max_leverage < 1 or max_leverage > 100000:
        raise InfeasiblePlan('invalid exchange leverage maximum')
    selected = None
    for leverage in range(1, max_leverage+1):
        margin = notional/leverage
        if margin+entry_fee > allowed+1e-9:
            continue
        distance = 1/leverage-rules.maintenance-fee_rate
        if distance <= 0:
            continue
        liq = entry*(1-distance if long else 1+distance)
        buffer = (stop_fill-liq)/entry if long else (liq-stop_fill)/entry
        if buffer+1e-12 < liquidation_buffer_pct:
            continue
        selected = leverage, margin, liq, buffer
        break
    if selected is None:
        raise InfeasiblePlan('no leverage satisfies margin and stop/liquidation buffer')
    leverage, margin, liq, buffer = selected
    estimated_loss = abs(entry-stop_fill)*quantity + entry_fee + stop_fill*quantity*fee_rate
    if estimated_loss > budget+1e-8 or estimated_loss >= eq:
        raise InfeasiblePlan('rounded execution exceeds risk budget')
    return AdaptivePlan('long' if long else 'short', entry, stop, target,
                        stop_fill, contracts, quantity, notional, leverage,
                        margin, entry_fee, estimated_loss, budget, allowed,
                        liq, buffer, rules.max_leverage)
