"""Model entry prices that stay off the exchange.

A named limit is a local review trigger. It does not reserve margin and it is
never sent as a resting order. When price reaches it, the symbol is queued for
a fresh analysis; confirmation is still required before any order.
"""
import copy
from time import time
from uuid import uuid4


def save_local_limit(store, *, symbol, side, limit_price, setup_id=None, rationale='',
                     decision_mode=None, trace_id=None, trigger_timeframe=None, now=None):
    now = time() if now is None else now
    row = dict(id=uuid4().hex, symbol=symbol, side=side, limit_price=float(limit_price),
               setup_id=setup_id, rationale=str(rationale)[:1200], decision_mode=decision_mode,
               trace_id=trace_id, trigger_timeframe=trigger_timeframe, status='resting',
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
    return due


def consume_symbol(store, symbol):
    rows = store.get('local_entry_limits', [])
    for row in rows:
        if row.get('symbol') == symbol and row.get('status') in ('resting', 'review_queued'):
            row['status'] = 'consumed'
    store.put('local_entry_limits', rows)
