"""Bounded bidirectional PA hypotheses from already closed multi-timeframe data.

These are explicit definitions for evaluation, not calibrated trading edges.
No network/model calls, fabricated targets or mandatory alignment of all TFs.
"""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from hashlib import sha256
import json
from math import isfinite

VERSION = 'structure-setups-v1'
FAMILIES = ('trend_continuation', 'pullback', 'countertrend_sweep_reclaim')
SECONDS = {'5m': 300, '15m': 900, '4h': 14400}
HORIZONS = {'5m': 'scalp', '15m': 'intraday', '4h': 'swing'}
REVIEW_BARS = {'5m': 6, '15m': 12, '4h': 12}


def trade_horizon(frame, now):
    return dict(style=HORIZONS[frame], review_timeframe=frame,
                review_due_at=now+SECONDS[frame]*REVIEW_BARS[frame])


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value > 0


def _tick(value, step, up=False):
    if not step:
        return value
    number, unit = Decimal(str(value)), Decimal(str(step))
    return float((number / unit).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * unit)


def _views(context):
    """Validate consumed rows rather than interpreting open/future bars as proof."""
    observed = context.get('observed_at')
    if not _positive(observed):
        return {}
    result = {}
    for view in context.get('timeframes', []):
        tf = view.get('timeframe')
        rows = view.get('recent_ohlcv', [])
        if tf not in SECONDS or len(rows) < 3:
            continue
        clean = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                break
            at, op, hi, lo, close, volume = row[:6]
            if (not _positive(at) or int(at) != at or at + SECONDS[tf] > observed
                    or not all(_positive(v) for v in (op, hi, lo, close))
                    or not isinstance(volume, (int, float)) or not isfinite(volume) or volume < 0
                    or hi < max(op, close) or lo > min(op, close) or hi < lo
                    or clean and at - clean[-1][0] != SECONDS[tf]):
                break
            clean.append(row[:6])
        else:
            closed = clean[-1][0] + SECONDS[tf]
            if observed - closed < 2 * SECONDS[tf]:
                result[tf] = dict(rows=clean, features=view.get('features', {}), closed_at=closed)
    return result


def _reference(view, tf, at, field):
    row = next((r for r in view['rows'] if r[0] == at), None)
    if row is None:
        return None
    return dict(timeframe=tf, open_time=at, field=field, price=row[2 if field == 'high' else 3])


def _swings(views, field):
    refs = []
    for tf, view in views.items():
        features = view['features']
        values = features.get('swings', []) + [features.get('last_swing_' + field)]
        for swing in values:
            if (not isinstance(swing, dict) or swing.get('kind', field) != field
                    or not _positive(swing.get('confirmed_at'))
                    or swing['confirmed_at'] > view['closed_at']):
                continue
            ref = _reference(view, tf, swing.get('pivot_at'), field)
            if ref and ref['price'] == swing.get('price') and ref not in refs:
                refs.append(ref)
    return refs


def _atr(rows):
    return sum(max(r[2] - r[3], abs(r[2] - rows[i-1][4]), abs(r[3] - rows[i-1][4]))
               for i, r in enumerate(rows) if i) / (len(rows) - 1)


def build_structure_setups(market_context, *, current_price, price_step=0, trigger_timeframe=None):
    """Return grounded READY/WATCH alternatives with stable IDs and price refs.

    READY means the specified closed-bar pattern occurred, never authorization to
    execute. WATCH entry prices are future trigger references, not pending orders.
    A missing structural stop/target produces no executable hypothesis. Every
    selected live plan still needs fresh quote/risk/account/exchange validation.
    """
    if (not _positive(current_price) or isinstance(price_step, bool)
            or not isinstance(price_step, (int, float)) or not isfinite(price_step) or price_step < 0):
        return []
    views = _views(market_context)
    micro_tf = trigger_timeframe or ('5m' if '5m' in views else '15m')
    if micro_tf not in views:
        return []
    macro = views.get('4h', {}).get('features', {}).get('trend', 'unknown')
    micro = views[micro_tf]; latest = micro['rows'][-1]
    bias = macro if macro in ('bullish', 'bearish') else micro['features'].get('trend')
    if bias not in ('bullish', 'bearish'):
        return []
    target_views = {micro_tf: micro} if trigger_timeframe else views
    targets = {'long': _swings(target_views, 'high'), 'short': _swings(target_views, 'low')}
    hypotheses = []

    def add(family, side, stop_ref, evidence, *, ready=True, trigger=None):
        if stop_ref is None:
            return
        long = side == 'long'
        entry = current_price if ready else trigger
        # Price references must remain on their proper side after tick rounding.
        stop = _tick(stop_ref['price'] + (-1 if long else 1) * _atr(micro['rows']) * .1,
                     price_step, up=not long)
        possible = [ref for ref in targets[side] if ref['price'] > entry] if long else [ref for ref in targets[side] if ref['price'] < entry]
        if not possible:
            return
        target_ref = min(possible, key=lambda ref: abs(ref['price'] - entry))
        target = _tick(target_ref['price'], price_step, up=not long)
        if not (0 < stop < entry < target if long else 0 < target < entry < stop):
            return
        reference = lambda ref: {k: ref[k] for k in ('timeframe', 'open_time', 'field')}
        material = dict(version=VERSION, symbol=market_context.get('symbol'), family=family, side=side,
                        source_closed_at=micro['closed_at'], **({'horizon': HORIZONS[micro_tf]} if trigger_timeframe else {}), evidence=evidence, stop=stop, target=target,
                        trigger=trigger, state='ready' if ready else 'watch')
        ident = sha256(json.dumps(material, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:24]
        hypotheses.append(dict(setup_id=ident, **material, macro_trend=macro, requires_full_context=any(tf not in views for tf in ('4h','15m','5m')),
            entry_reference=current_price if ready else trigger,
            stop_loss_price=stop, take_profit_price=target,
            reward_risk=abs(target-entry)/abs(entry-stop),
            trigger_condition=None if ready else 'closed_candle_cross_above' if long else 'closed_candle_cross_below',
            trigger_timeframe=micro_tf, stop_reference=reference(stop_ref), target_reference=reference(target_ref),
            management_posture='reduced_risk_countertrend' if family=='countertrend_sweep_reclaim' else 'normal_policy',
            caveat='Pattern hypothesis, not a success probability or execution approval.'))

    macro_side = 'long' if bias == 'bullish' else 'short'
    field = 'low' if macro_side == 'long' else 'high'
    stops = [r for r in _swings({micro_tf: micro}, field)
             if r['price'] < current_price] if macro_side == 'long' else [r for r in _swings({micro_tf: micro}, field) if r['price'] > current_price]
    stop = max(stops, key=lambda r: r['open_time']) if stops else None
    fresh_break = next((b for b in reversed(micro['features'].get('breaks', []))
                        if b.get('at') == latest[0] and b.get('direction') == bias), None)
    if fresh_break:
        add('trend_continuation', macro_side, stop, dict(kind='closed_bos', timeframe=micro_tf, at=latest[0], level=fresh_break.get('level')))
    else:
        trigger_field = 'high' if macro_side == 'long' else 'low'
        triggers = _swings({micro_tf: micro}, trigger_field)
        triggers = [r for r in triggers if r['price'] > current_price] if macro_side=='long' else [r for r in triggers if r['price'] < current_price]
        if triggers:
            trigger_ref = min(triggers, key=lambda r: abs(r['price']-current_price))
            add('trend_continuation', macro_side, stop,
                dict(kind='confirmed_swing_break_required', timeframe=micro_tf, level=trigger_ref['price'], reference=trigger_ref),
                ready=False, trigger=trigger_ref['price'])

    # Pullback: retest/reclaim a *previous* 15m break level in macro direction.
    # The 5m trend may oppose 4h during the pullback; universal TF alignment is not required.
    retest_tf = {'5m':'15m','15m':'4h','4h':'4h'}[micro_tf] if trigger_timeframe else '15m'
    for br in reversed(views.get(retest_tf, {}).get('features', {}).get('breaks', [])):
        level = br.get('level')
        if br.get('direction') != bias or not _positive(level) or br.get('at', float('inf')) + SECONDS[retest_tf] > latest[0]:
            continue
        reclaimed = latest[3] <= level < latest[4] if macro_side=='long' else latest[2] >= level > latest[4]
        if reclaimed:
            ref = _reference(micro, micro_tf, latest[0], field)
            add('pullback', macro_side, ref, dict(kind=f'prior_{retest_tf}_bos_retest_reclaim', entry_timeframe=micro_tf, level=level, break_at=br['at'], reclaim_at=latest[0]))
            break

    # Countertrend: actual wick sweep AND same closed candle reclaim, never a
    # stretched SMA/ROC alone. Both bullish and bearish mirrors are evaluated.
    for tf in ((micro_tf,) if trigger_timeframe else ('5m', '15m')):
        view = views.get(tf)
        if not view:
            continue
        candle = view['rows'][-1]
        for event in view['features'].get('sweeps', []):
            if event.get('at') != candle[0] or not _positive(event.get('level')):
                continue
            low_sweep = event.get('direction') == 'low_sweep'
            high_sweep = event.get('direction') == 'high_sweep'
            if macro == 'bearish' and low_sweep and candle[3] < event['level'] <= candle[4]:
                add('countertrend_sweep_reclaim', 'long', _reference(view, tf, candle[0], 'low'),
                    dict(kind='low_sweep_closed_reclaim', timeframe=tf, at=candle[0], level=event['level']))
            elif macro == 'bullish' and high_sweep and candle[2] > event['level'] >= candle[4]:
                add('countertrend_sweep_reclaim', 'short', _reference(view, tf, candle[0], 'high'),
                    dict(kind='high_sweep_closed_reclaim', timeframe=tf, at=candle[0], level=event['level']))
    return hypotheses
