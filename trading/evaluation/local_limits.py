"""Model entry prices that stay off the exchange.

A named limit is a local review trigger. It does not reserve margin and it is
never sent as a resting order. When price reaches it, the symbol is queued for
a fresh analysis; confirmation is still required before any order.
"""
import copy
from time import time
from uuid import uuid4


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def rank_local_limits(rows):
    """Higher cost-adjusted R first. Missing or non-numeric R sorts last."""
    def key(row):
        raw = row.get('cost_adjusted_r')
        try:
            reward = float(raw)
        except (TypeError, ValueError):
            reward = float('-inf')
        if reward != reward:
            reward = float('-inf')
        return (-reward, str(row.get('symbol') or ''), str(row.get('id') or ''))
    return sorted(list(rows or []), key=key)


def save_local_limit(store, *, symbol, side, limit_price, setup_id=None, rationale='',
                     decision_mode=None, trace_id=None, trigger_timeframe=None,
                     cost_adjusted_r=None, stop=None, target=None, now=None):
    now = time() if now is None else now
    row = dict(id=uuid4().hex, symbol=symbol, side=side, limit_price=float(limit_price),
               setup_id=setup_id, rationale=str(rationale)[:1200], decision_mode=decision_mode,
               trace_id=trace_id, trigger_timeframe=trigger_timeframe, status='resting',
               cost_adjusted_r=float(cost_adjusted_r) if _finite(cost_adjusted_r) else None,
               stop=float(stop) if _finite(stop) else None,
               target=float(target) if _finite(target) else None,
               created_at=now, expires_at=now + 86400)
    rows = [item for item in store.get('local_entry_limits', [])
            if not (item.get('symbol') == symbol and item.get('side') == side
                    and item.get('status') in ('resting', 'review_queued'))]
    rows.append(row)
    store.put('local_entry_limits', rows[-80:])
    return copy.deepcopy(row)


def claim_reached(store, price_of, now=None):
    """Mark resting limits whose price has been reached. Does not place orders."""
    now = time() if now is None else now
    rows = store.get('local_entry_limits', [])
    due = []
    for row in rows:
        if row.get('expires_at', 0) <= now and row.get('status') == 'resting':
            row['status'] = 'expired'
            continue
        if row.get('status') != 'resting':
            continue
        try:
            price = price_of(row['symbol'])
        except Exception:
            continue
        if not isinstance(price, (int, float)) or isinstance(price, bool) or price <= 0:
            continue
        reached = price <= row['limit_price'] if row.get('side') == 'long' else price >= row['limit_price']
        if not reached:
            continue
        row['status'] = 'review_queued'
        row['trigger_price'] = float(price)
        row['triggered_at'] = now
        due.append(copy.deepcopy(row))
    store.put('local_entry_limits', rows)
    return rank_local_limits(due)


def save_local_plan(store, *, symbol, side, limit_price=None, stop=None, target=None, entry=None,
                    cost_adjusted_r=None, rationale='', decision_mode=None, now=None):
    """Remember a late VLM plan for the next trigger. This never places an order."""
    now = time() if now is None else now
    row = dict(id=uuid4().hex, symbol=symbol, side=side,
               limit_price=float(limit_price) if _finite(limit_price) else None,
               stop=float(stop) if _finite(stop) else None,
               target=float(target) if _finite(target) else None,
               entry=float(entry) if _finite(entry) else None,
               cost_adjusted_r=float(cost_adjusted_r) if _finite(cost_adjusted_r) else None,
               rationale=str(rationale)[:1200], decision_mode=decision_mode,
               status='next_trigger', created_at=now)
    rows = [item for item in store.get('local_plan_updates', [])
            if not (item.get('symbol') == symbol and item.get('side') == side
                    and item.get('status') == 'next_trigger')]
    rows.append(row)
    store.put('local_plan_updates', rows[-80:])
    if row['limit_price'] is not None:
        save_local_limit(store, symbol=symbol, side=side, limit_price=row['limit_price'],
                         rationale=row['rationale'], decision_mode=decision_mode,
                         cost_adjusted_r=row['cost_adjusted_r'], stop=row['stop'],
                         target=row['target'], now=now)
    return copy.deepcopy(row)


def consume_symbol(store, symbol):
    rows = store.get('local_entry_limits', [])
    for row in rows:
        if row.get('symbol') == symbol and row.get('status') in ('resting', 'review_queued'):
            row['status'] = 'consumed'
    store.put('local_entry_limits', rows)
