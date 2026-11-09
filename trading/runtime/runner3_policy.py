"""Deterministic Runner 3 allocation and evidence rules."""
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean
from math import isfinite
from time import time
from zoneinfo import ZoneInfo

COHORT = 'runner3-evidence-allocation-v1'
MARGIN_RESERVE_FRACTION = .15
MIN_NET_TARGET_R = 1.25
MAX_COST_SHARE = .30
SMALL_ACCOUNT_EQUITY_USDT = 100.


def uses_fixed_position_limit(runner_id, equity):
    return not (runner_id == 'runner3' and 0 < equity < SMALL_ACCOUNT_EQUITY_USDT)


def evidence_grade(setup):
    kind = setup['evidence']['kind']
    return 'strong' if kind == 'sweep_mss_fvg_retest' or (
        kind == 'closed_bos' and setup['reward_risk'] >= 2 and
        setup['macro_trend'] == ('bullish' if setup['side']=='long' else 'bearish')
    ) else 'standard'


def validate_evidence(setup, plan, now=None):
    now = time() if now is None else now
    if not setup or plan is None:
        raise ValueError('runner3_requires_verified_setup')
    evidence = setup['evidence']
    if evidence.get('expires_at', float('inf')) <= now:
        raise ValueError('runner3_evidence_expired')
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
    blend = min(1., max(0., (SMALL_ACCOUNT_EQUITY_USDT - equity) / 20.))
    large, small = (.20, .35) if grade == 'strong' else (.10, .20)
    fraction = large + (small - large) * blend
    if horizon == 'swing':
        fraction = min(fraction, .25)
    reserve = capital * MARGIN_RESERVE_FRACTION
    target = capital * fraction
    return dict(grade=grade, horizon=horizon, equity_usdt=equity, margin_fraction=fraction,
                target_margin_usdt=target, reserve_usdt=reserve,
                max_entry_cost_usdt=max(0., min(target, available - reserve)))


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

def evidence_risk_groups(intents, now):
    """Compare confirmed, unboosted returns in two non-overlapping 30-day windows."""
    groups = defaultdict(lambda: defaultdict(list))
    for row in intents:
        metadata = row.get('proposal', {}).get('metadata') or {}
        allocation = metadata.get('runner3_allocation') or {}
        segment = (metadata.get('setup_family'), allocation.get('horizon'),
                   allocation.get('macro_trend'), metadata.get('analysis_version'))
        grade = allocation.get('grade')
        closed_at = row.get('closed_at')
        net = row.get('net_pnl')
        risk = (row.get('entry_metrics') or {}).get('initial_stop_risk_usdt')
        if (metadata.get('runner_id') != 'runner3' or metadata.get('strategy_cohort') != COHORT
                or row.get('status') != 'closed' or row.get('realized_complete') is not True
                or not row.get('entry_fill_price') or allocation.get('risk_boost_applied')
                or not all(segment) or grade not in ('standard', 'strong')
                or not all(type(v) in (int, float) and isfinite(v) for v in (closed_at, net, risk))
                or risk <= 0):
            continue
        age_days = (now - closed_at / 1000) / 86400
        if 0 <= age_days < 60:
            groups[segment][int(age_days >= 30), grade].append(net / risk)

    results = {}
    for segment, values in groups.items():
        windows = []
        for index in (1, 0):
            strong, standard = values[index, 'strong'], values[index, 'standard']
            strong_r = mean(strong) if strong else None
            standard_r = mean(standard) if standard else None
            qualifies = (len(strong) >= 10 and len(standard) >= 10
                         and strong_r > 0 and strong_r - standard_r >= .25)
            windows.append(dict(strong_count=len(strong), standard_count=len(standard),
                                strong_mean_net_r=strong_r, standard_mean_net_r=standard_r,
                                qualifies=qualifies))
        results[segment] = dict(prior_30_days=windows[0], recent_30_days=windows[1],
                                eligible=all(window['qualifies'] for window in windows))
    return results


def evidence_risk_budget(model_fraction, ceiling, *, grade, comparison, same_side_open):
    if (grade == 'strong' and model_fraction < ceiling and comparison
            and comparison['eligible'] and not same_side_open):
        return min(ceiling, model_fraction * 1.25, model_fraction + .0025)
    return model_fraction


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
