"""Deterministic Runner 3 allocation and evidence rules."""
from datetime import datetime, timedelta
from math import floor, isfinite
from time import time
from zoneinfo import ZoneInfo

COHORT = 'runner3-htf-zone-v3'
MARGIN_RESERVE_FRACTION = .15
MIN_NET_TARGET_R = 1.25
MAX_COST_SHARE = .30
SMALL_ACCOUNT_EQUITY_USDT = 100.
MIN_OPEN_MARGIN_USDT = 1.


def uses_fixed_position_limit(runner_id, equity):
    return not (runner_id == 'runner3' and 0 < equity < SMALL_ACCOUNT_EQUITY_USDT)


def evidence_grade(setup):
    kind = setup['evidence']['kind']
    return 'strong' if kind in ('sweep_mss_fvg_retest', 'htf_zone_mss') or (
        kind == 'closed_bos' and setup['reward_risk'] >= 2 and
        setup['macro_trend'] == ('bullish' if setup['side']=='long' else 'bearish')
    ) else 'standard'


def validate_evidence(setup, plan, now=None):
    now = time() if now is None else now
    if not setup or plan is None:
        raise ValueError('runner3_requires_verified_setup')
    evidence = setup['evidence']
    if evidence.get('stage') in ('awaiting_touch', 'awaiting_confirmation'):
        raise ValueError('touch_is_not_an_entry')
    if evidence.get('expires_at', float('inf')) <= now:
        raise ValueError('runner3_evidence_expired')
    if setup.get('context_timeframe'):
        if setup.get('state') != 'ready' or evidence.get('kind') not in ('htf_zone_choch', 'htf_zone_mss'):
            raise ValueError('runner3_requires_verified_setup')
        if plan.entry_timeframe != setup['trigger_timeframe']:
            raise ValueError('runner3_trigger_timeframe_mismatch')
        if plan.target_reference.model_dump() != setup['target_reference']:
            raise ValueError('runner3_target_reference_mismatch')
    if plan.stop_reference.model_dump() != setup['stop_reference']:
        raise ValueError('runner3_stop_reference_mismatch')
    if (plan.stop_loss_price > setup['stop_loss_price'] if setup['side']=='long'
            else plan.stop_loss_price < setup['stop_loss_price']):
        raise ValueError('runner3_structural_stop_too_tight')


def margin_policy(equity, capital, available, grade, horizon):
    if not all(type(v) in (int, float) and isfinite(v) and v >= 0 for v in (equity, capital, available)):
        raise ValueError('runner3_invalid_margin_context')
    if grade not in ('standard','strong') or horizon not in ('scalp','intraday','swing'):
        raise ValueError('runner3_invalid_margin_context')
    small_account = 0 < equity < SMALL_ACCOUNT_EQUITY_USDT
    if small_account:
        fraction = .70 if grade == 'strong' else .55
        if horizon == 'swing':
            fraction = min(fraction, .60)
    else:
        fraction = .20 if grade == 'strong' else .10
    reserve = capital * MARGIN_RESERVE_FRACTION
    target = capital * fraction
    return dict(grade=grade, horizon=horizon, equity_usdt=equity, margin_fraction=fraction,
                target_margin_usdt=target, reserve_usdt=reserve,
                max_entry_cost_usdt=max(0., min(target, available - reserve)))


def confirming_extreme(reference, evidence):
    """Wick of the confirming CHoCH/MSS candle. A missing candle fails closed."""
    if reference is None or not isinstance(evidence, dict):
        return None
    timeframe = getattr(reference, 'timeframe', None) or (reference.get('timeframe') if isinstance(reference, dict) else None)
    open_time = getattr(reference, 'open_time', None)
    if open_time is None and isinstance(reference, dict):
        open_time = reference.get('open_time')
    field = getattr(reference, 'field', None) or (reference.get('field') if isinstance(reference, dict) else None)
    rows = evidence.get(timeframe) or []
    matches = [row for row in rows if isinstance(row, (list, tuple)) and len(row) >= 4
               and type(row[0]) in (int, float) and row[0] == open_time]
    if len(matches) != 1 or field not in ('low', 'high'):
        return None
    wick = matches[0][3] if field == 'low' else matches[0][2]
    if isinstance(wick, bool) or not isinstance(wick, (int, float)) or not isfinite(wick) or wick <= 0:
        return None
    return float(wick)


def drift_respects_choch_risk(*, side, fresh_entry, stop, target, choch_extreme, quantity, risk_budget, fee, slippage):
    """A moved quote still enters only while the CHoCH stop and the planned loss both hold.

    The quantity is the one already sized at the planned entry. A price that has run
    far enough for that loss to exceed the risk budget is not chased by shrinking it.
    """
    values = (fresh_entry, stop, target, choch_extreme, quantity, risk_budget)
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value > 0 for value in values):
        return False
    if side == 'long':
        intact = stop <= choch_extreme < fresh_entry < target
    elif side == 'short':
        intact = target < fresh_entry < choch_extreme <= stop
    else:
        return False
    if not intact:
        return False
    _, _, unit = ioc_target_economics(
        side=side, entry=fresh_entry, stop=stop, target=target, fee=fee, slippage=slippage)
    return unit * quantity <= risk_budget + 1e-8


def leverage_for_choch_loss(*, notional, entry, stop, free_margin, equity, max_leverage,
                            maintenance, initial_rate, fee, liquidation_buffer):
    """Leverage that fits the CHoCH-stop loss into free margin, and the notional used.

    The lowest leverage that can post the risk-sized notional is used. The exchange
    maximum and a fixed default are only the top of that range. If the full loss does
    not fit, the size is the largest share of free margin still inside the loss cap,
    never a slice below 1 USDT while equity is under 100.
    """
    values = (notional, entry, stop, free_margin, equity, max_leverage, maintenance, initial_rate, fee, liquidation_buffer)
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) for value in values):
        raise ValueError('runner3_margin_reserve')
    if min(notional, entry, free_margin, max_leverage) <= 0 or stop <= 0 or stop == entry:
        raise ValueError('runner3_margin_reserve')
    cap = floor(max_leverage)
    if cap < 1 or cap > 100000:
        raise ValueError('runner3_margin_reserve')
    long = stop < entry

    def fit(leverage, size):
        margin_rate = max(1 / leverage, initial_rate)
        margin = size * margin_rate
        if margin + size * fee > free_margin + 1e-9:
            return None
        distance = 1 / leverage - maintenance - fee
        if distance <= 0:
            return None
        liquidation = entry * (1 - distance if long else 1 + distance)
        buffer = (stop - liquidation) / entry if long else (liquidation - stop) / entry
        if buffer + 1e-12 < liquidation_buffer:
            return None
        if 0 < equity < SMALL_ACCOUNT_EQUITY_USDT and margin < MIN_OPEN_MARGIN_USDT:
            return None
        return leverage, margin, size

    for leverage in range(1, cap + 1):
        selected = fit(leverage, notional)
        if selected is not None:
            return selected
    best = None
    for leverage in range(1, cap + 1):
        margin_rate = max(1 / leverage, initial_rate)
        fitted = min(notional, free_margin / (margin_rate + fee))
        selected = fit(leverage, fitted)
        if selected is not None and (best is None or selected[2] > best[2]):
            best = selected
    if best is not None:
        return best
    full_margin = notional * max(1., initial_rate)
    if 0 < equity < SMALL_ACCOUNT_EQUITY_USDT and min(full_margin, free_margin) < MIN_OPEN_MARGIN_USDT:
        raise ValueError('runner3_margin_below_minimum')
    raise ValueError('runner3_margin_reserve')


def ioc_target_economics(*, side, entry, stop, target, fee, slippage):
    """Per-unit gross reward, net reward and modeled stop loss at the IOC limit."""
    direction = 1 if side == 'long' else -1
    stop_fill = stop * (1 - direction * slippage)
    target_fill = target * (1 - direction * slippage)
    loss = abs(entry - stop_fill) + fee * (entry + stop_fill)
    net = direction * (target_fill - entry) - fee * (entry + target_fill)
    return abs(target - entry), net, loss


def validate_economics(gross, net, loss):
    if loss <= 0 or net / loss < MIN_NET_TARGET_R:
        raise ValueError('runner3_net_target_r_too_low')
    if gross <= 0 or (gross - net) / gross > MAX_COST_SHARE:
        raise ValueError('runner3_cost_share_too_high')

def small_account_risk_target(equity, ceiling, grade):
    if 0 < equity < SMALL_ACCOUNT_EQUITY_USDT:
        return ceiling if grade == 'strong' else ceiling * .80
    return None


def cost_adjusted_r(*, side, entry, stop, target, fee, slippage):
    """Net reward divided by modeled stop loss, after fees and slippage."""
    _gross, net, loss = ioc_target_economics(
        side=side, entry=entry, stop=stop, target=target, fee=fee, slippage=slippage)
    if loss <= 0 or not isfinite(net) or not isfinite(loss):
        return None
    return net / loss


def thesis_invalidated(position, price):
    """The open thesis is invalidated only when price has crossed its structural level."""
    if not isinstance(position, dict):
        return False
    level = position.get('invalidation_price')
    if not isinstance(level, (int, float)) or isinstance(level, bool):
        level = position.get('stop')
    if not isinstance(level, (int, float)) or isinstance(level, bool) or level <= 0:
        return False
    if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0:
        return False
    if position.get('side') == 'long':
        return price <= level
    if position.get('side') == 'short':
        return price >= level
    return False


def rank_confirmed_plans(plans):
    """Stronger evidence and payoff first. Ties stay on symbol and setup id."""
    def key(plan):
        grade = 1 if plan.get('grade') == 'strong' or plan.get('evidence_kind') == 'htf_zone_mss' else 0
        try:
            reward = float(plan.get('capital_rank') if plan.get('capital_rank') is not None else plan.get('reward_risk') or 0)
        except (TypeError, ValueError):
            reward = 0.
        return (-grade, -reward, str(plan.get('symbol') or ''), str(plan.get('setup_id') or ''))
    return sorted(list(plans or []), key=key)


def position_brief(position):
    """PnL, thesis, and room to the planned target for a capital comparison."""
    return dict(symbol=position.get('symbol'), side=position.get('side'),
                unrealized_pnl=position.get('unrealized'),
                thesis=str(position.get('thesis') or '')[:500],
                remaining_room=copy_room(position.get('remaining_room')),
                leverage=position.get('leverage'), entry=position.get('entry'),
                stop=position.get('stop'), target=position.get('target'),
                mark=position.get('mark'),
                invalidation_price=position.get('invalidation_price'))


def copy_room(room):
    if not isinstance(room, dict):
        return None
    return {key: room.get(key) for key in ('to_target_price', 'to_target_r')}


def daily_objective(closed, target, now):
    local = datetime.fromtimestamp(now, ZoneInfo('Europe/Istanbul'))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = [row for row in closed if (row.get('closed_at') or 0)/1000 >= start.timestamp()]
    complete = all(row.get('realized_complete') is True and type(row.get('net_pnl')) in (float,int)
                   for row in rows)
    # Missing close timestamps cannot be assigned to a day.
    complete = complete and all(row.get('closed_at') for row in closed)
    net = sum(row['net_pnl'] for row in rows) if complete else None
    return dict(timezone='Europe/Istanbul',scope='managed_closed_trade_net_only',
                target_usdt=target,realized_net_usdt=net,
                remaining_usdt=max(0.,target-net) if target is not None and net is not None else None,
                remaining_seconds=max(0.,(start+timedelta(days=1)).timestamp()-now),
                affects_risk=False)
