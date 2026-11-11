"""Numeric levels that bring a Runner 3 hold back for review.

A hold stores the zone edge, the CHoCH level, and the block or flow price,
plus the short rationale. When one of those numbers prints, the symbol is
queued again with the old rationale and the market from that hold. A ready
card is entered without another model call. A card that is still awaiting
touch, or that has no closed CHoCH, stays a hold and stores a new trigger.
Nothing here places an order.
"""
import copy
import logging
from datetime import datetime, timezone
from time import time

from trading.quant.signals import CandidateSignal, StrategyName

logger = logging.getLogger('worker')


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 and value == value


def _add(rows, kind, price):
    if not _positive(price):
        return
    price = float(price)
    if any(abs(price - item['price']) <= max(1e-8, abs(price) * 1e-9) for item in rows):
        return
    rows.append(dict(kind=kind, price=price))


def trigger_levels(setup, candidate=None):
    """Zone edges, the CHoCH level, and block or flow prices. Never empty when a price exists."""
    rows = []
    if isinstance(setup, dict):
        evidence = setup.get('evidence') if isinstance(setup.get('evidence'), dict) else {}
        zone = evidence.get('zone') if isinstance(evidence.get('zone'), dict) else {}
        shift = evidence.get('shift') if isinstance(evidence.get('shift'), dict) else {}
        kind = zone.get('kind') if zone.get('kind') in ('block', 'flow') else 'zone_edge'
        _add(rows, kind if kind != 'zone_edge' else 'zone_edge', zone.get('low'))
        _add(rows, kind if kind != 'zone_edge' else 'zone_edge', zone.get('high'))
        _add(rows, 'choch', shift.get('level'))
        for level in setup.get('opposing_target_levels') or []:
            if isinstance(level, dict):
                _add(rows, level.get('kind') if level.get('kind') in ('block', 'flow') else 'block', level.get('price'))
        _add(rows, 'zone_edge', setup.get('invalidation_price'))
    if not rows and candidate is not None:
        _add(rows, 'price', getattr(candidate, 'entry_price', None))
        _add(rows, 'price', getattr(candidate, 'stop_loss_price', None))
        _add(rows, 'price', getattr(candidate, 'take_profit_price', None))
    return rows


def snapshot_market(market):
    """Old market kept with the hold. Bounded to the candles and the structure labels."""
    if not isinstance(market, dict):
        return None
    frames = []
    for view in market.get('timeframes') or []:
        if not isinstance(view, dict):
            continue
        features = view.get('features') if isinstance(view.get('features'), dict) else {}
        frames.append(dict(
            timeframe=view.get('timeframe'), closed_at=view.get('closed_at'),
            recent_ohlcv=list(view.get('recent_ohlcv') or [])[-12:],
            trend=features.get('trend'),
            structure_shifts=list(features.get('structure_shifts') or [])[-4:],
            order_block_candidates=list(features.get('order_block_candidates') or [])[-4:],
            fvgs=list(features.get('fvgs') or [])[-4:]))
    return dict(symbol=market.get('symbol'), observed_at=market.get('observed_at'), timeframes=frames)


def _prints(price, level, tick):
    tolerance = max(float(tick or 0), abs(level) * 1e-8, 1e-8)
    return abs(float(price) - float(level)) <= tolerance


def log_hold(symbol, rationale, levels):
    """Worker stdout. `docker compose logs worker | grep USELESS` matches the symbol."""
    rendered = ' '.join(f"{item['kind']}:{item['price']:.8g}" for item in levels)
    text = ' '.join(str(rationale or '').split())[:240]
    logger.info('HOLD %s rationale=%s triggers=%s', symbol, text, rendered)


def save_hold(store, *, symbol, rationale, levels, side=None, setup_id=None, old_market=None,
              trigger_timeframe=None, price=None, tick=0, now=None, single_trigger=False,
              model_accepted=False):
    """Replace this symbol's hold. A hold with no numeric level is refused."""
    if not levels:
        raise ValueError('hold_without_trigger')
    now = time() if now is None else now
    latched = []
    if _positive(price):
        latched = [item['price'] for item in levels if _prints(price, item['price'], tick)]
    row = dict(symbol=symbol, side=side, setup_id=setup_id, rationale=str(rationale or '')[:500],
               triggers=copy.deepcopy(levels), old_market=copy.deepcopy(old_market),
               trigger_timeframe=trigger_timeframe, latched=latched, status='armed', created_at=now,
               single_trigger=bool(single_trigger), model_accepted=bool(model_accepted))
    rows = [item for item in store.get('price_action_holds', []) if item.get('symbol') != symbol]
    rows.append(row)
    store.put('price_action_holds', rows[-80:])
    log_hold(symbol, row['rationale'], levels)
    return copy.deepcopy(row)


def clear_hold(store, symbol):
    rows = [item for item in store.get('price_action_holds', []) if item.get('symbol') != symbol]
    store.put('price_action_holds', rows)


def claim_printed(store, price_of, *, tick_of=None, now=None):
    """Queue holds whose stored number has printed. A level already under price stays latched."""
    now = time() if now is None else now
    rows = store.get('price_action_holds', [])
    due = []
    for row in rows:
        if row.get('status') != 'armed':
            continue
        try:
            price = price_of(row['symbol'])
        except Exception:
            continue
        if not _positive(price):
            continue
        tick = 0
        if tick_of is not None:
            try:
                tick = tick_of(row['symbol']) or 0
            except Exception:
                tick = 0
        latched = [level for level in row.get('latched') or [] if _prints(price, level, tick)]
        row['latched'] = latched
        hit = next((item for item in row.get('triggers') or []
                    if _prints(price, item.get('price'), tick) and not any(_prints(item['price'], level, tick) for level in latched)), None)
        if hit is None:
            continue
        row['status'] = 'review_queued'
        row['trigger_price'] = float(hit['price'])
        row['printed_price'] = float(price)
        row['triggered_at'] = now
        due.append(copy.deepcopy(row))
        log_hold(row['symbol'], row.get('rationale'), [hit])
    store.put('price_action_holds', rows)
    return due


def review_signal(row):
    """Queue the symbol again with the prior rationale and the old market."""
    price = float(row['printed_price'])
    long = row.get('side') != 'short'
    prior = dict(rationale=row.get('rationale'), triggers=row.get('triggers'),
                 printed_trigger=row.get('trigger_price'), old_market=row.get('old_market'),
                 model_accepted=bool(row.get('model_accepted')))
    return CandidateSignal(
        symbol=row['symbol'], side='long' if long else 'short', strategy=StrategyName.STRUCTURE_SETUP,
        entry_price=price, stop_loss_price=price * (0.98 if long else 1.02),
        take_profit_price=price * (1.02 if long else 0.98), confidence=1.,
        rationale=str(row.get('rationale') or ''),
        created_at=datetime.now(timezone.utc),
        features=dict(hold_review=True, htf_zone_seed=True, structure_seed=True,
                      seed_timeframe=row.get('trigger_timeframe') or '15m',
                      source_open_at=time() - 60, source_closed_at=time() - 1,
                      dedup_key='hold:' + row['symbol'] + ':' + str(row.get('triggered_at')),
                      setup_id=row.get('setup_id'), prior_hold=prior,
                      capital_rank=0))
