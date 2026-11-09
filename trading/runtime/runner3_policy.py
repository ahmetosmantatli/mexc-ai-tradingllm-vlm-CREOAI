"""Deterministic Runner 3 allocation and evidence rules."""
from datetime import datetime, timedelta
from math import isfinite
from time import time
from zoneinfo import ZoneInfo

COHORT = 'runner3-evidence-allocation-v1'
MARGIN_RESERVE_FRACTION = .15
MIN_NET_TARGET_R = 1.25
MAX_COST_SHARE = .30


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
    blend = min(1., max(0., (100. - equity) / 20.))
    large, small = (.20, .35) if grade == 'strong' else (.10, .20)
    fraction = large + (small - large) * blend
    if horizon == 'swing':
        fraction = min(fraction, .25)
    reserve = capital * MARGIN_RESERVE_FRACTION
    target = capital * fraction
    return dict(grade=grade, horizon=horizon, equity_usdt=equity, margin_fraction=fraction,
                target_margin_usdt=target, reserve_usdt=reserve,
                max_entry_cost_usdt=max(0., min(target, available - reserve)))


def validate_economics(gross, net, loss):
    if loss <= 0 or net / loss < MIN_NET_TARGET_R:
        raise ValueError('runner3_net_target_r_too_low')
    if gross <= 0 or (gross - net) / gross > MAX_COST_SHARE:
        raise ValueError('runner3_cost_share_too_high')

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
