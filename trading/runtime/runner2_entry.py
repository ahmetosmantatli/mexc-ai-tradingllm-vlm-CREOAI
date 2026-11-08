"""Price limit for a Runner 2 IOC proposal, independent of order submission."""

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR


def _decimal(value, *, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError('invalid_entry_number')
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or not allow_zero and result == 0:
        raise ValueError('invalid_entry_number')
    return result


def ioc_price_limit(*, side, stop, target, contracts, contract_size, price_step,
                    loss_budget, fee_rate, exit_slippage_bps):
    """Return a tick-aligned entry limit or reject an infeasible risk budget.

    This bounds modeled loss at the planned stop with the given costs.
    A stop fill can still slip beyond the modeled exit price.
    """
    if side not in ('long', 'short'):
        raise ValueError('invalid_entry_side')
    stop, target, contracts, contract_size, tick, budget = (
        _decimal(value) for value in
        (stop, target, contracts, contract_size, price_step, loss_budget)
    )
    fee = _decimal(fee_rate, allow_zero=True)
    slip = _decimal(exit_slippage_bps, allow_zero=True) / 10000
    if fee >= 1 or slip >= 1:
        raise ValueError('invalid_entry_cost')
    risk_per_unit = budget / (contracts * contract_size)

    if side == 'long':
        worst_stop = stop * (1 - slip)
        raw_limit = (risk_per_unit + worst_stop * (1 - fee)) / (1 + fee)
        limit = (raw_limit / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        # An IOC must not admit an entry at or beyond the planned target.
        target_bound = (target / tick).to_integral_value(rounding=ROUND_CEILING) * tick - tick
        limit = min(limit, target_bound)
        if not stop < limit < target:
            raise ValueError('no_risk_bounded_entry_tick')
    else:
        worst_stop = stop * (1 + slip)
        raw_limit = (worst_stop * (1 + fee) - risk_per_unit) / (1 - fee)
        limit = (raw_limit / tick).to_integral_value(rounding=ROUND_CEILING) * tick
        target_bound = (target / tick).to_integral_value(rounding=ROUND_FLOOR) * tick + tick
        limit = max(limit, target_bound)
        if not 0 < target < limit < stop:
            raise ValueError('no_risk_bounded_entry_tick')
    return limit
