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
SECONDS = {'5m': 300, '15m': 900, '1h': 3600, '4h': 14400}
HORIZONS = {'5m': 'scalp', '15m': 'intraday', '1h': 'intraday', '4h': 'swing'}
REVIEW_BARS = {'5m': 6, '15m': 12, '1h': 12, '4h': 12}


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


def build_structure_setups(market_context, *, current_price, price_step=0, trigger_timeframe=None, strict_ict=False):
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
    if strict_ict:
        features=micro['features']
        for shift in features.get('structure_shifts',[]):
            direction=shift['direction']
            if shift['kind']!='mss' or direction==macro or not 0<latest[0]-shift['at']<=6*SECONDS[micro_tf]:
                continue
            long=direction=='bullish'
            sweep=next((event for event in reversed(features.get('sweeps',[]))
                        if event['direction']==('low_sweep' if long else 'high_sweep')
                        and 0<shift['at']-event['at']<=3*SECONDS[micro_tf]),None)
            if sweep is None:
                continue
            for zone in features.get('fvgs',[]):
                if zone['at']!=shift['at'] or zone['direction']!=direction:
                    continue
                edge=zone['lower'] if long else zone['upper']
                if any((bar[3]<=edge if long else bar[2]>=edge)
                       for bar in micro['rows'] if bar[0]>shift['at']):
                    continue
                if not (latest[3]<=zone['upper']<latest[4] if long else latest[2]>=zone['lower']>latest[4]):
                    continue
                add('countertrend_sweep_reclaim','long' if long else 'short',
                    _reference(micro,micro_tf,sweep['at'],'low' if long else 'high'),
                    dict(kind='sweep_mss_fvg_retest',timeframe=micro_tf,sweep=sweep,shift=shift,zone=zone,
                         trigger_at=latest[0],expires_at=micro['closed_at']+SECONDS[micro_tf]))
        return hypotheses

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


HTF_ZONE_PATHS = (('4h', '15m', 'swing'), ('1h', '5m', 'intraday'))
HTF_ZONE_VERSION = 'htf-zone-setup-v1'
# The 5m reaction must reach within this many 1h ATRs of the weak level.
WEAK_PROXIMITY_ATR = .5
# Entry (current price) must still be within this many 1h ATRs of the weak level.
WEAK_ENTRY_MAX_ATR = 1.
# Early weak-swing proximity and stop stay on this short LTF window even when
# the market packet carries a longer touch-memory series for zone cards.
EARLY_REACTION_BARS = 12
# LTF bars kept for zone touch / confirmation memory (5m ≈ 4h, 15m ≈ 12h).
LTF_TOUCH_BARS = 48
# A confirmed zone CHoCH/MSS card stays a watch unless this is on. In the
# 10-day replay those cards lost about 0.9R per trade that cleared 1.25R net.
ZONE_CARDS_READY = False


def _confirmed_zone(block, higher, context_tf):
    if block.get('status') not in ('untested', 'retested'):
        return None
    direction = block.get('direction')
    if direction not in ('bullish', 'bearish'):
        return None
    origin = _reference(higher, context_tf, block.get('open_time'), 'low')
    high = _reference(higher, context_tf, block.get('open_time'), 'high')
    confirmation = next((row for row in higher['rows'] if row[0] == block.get('confirmed_by')), None)
    if not origin or not high or not confirmation or not origin['open_time'] < confirmation[0]:
        return None
    low_price, high_price = origin['price'], high['price']
    if low_price >= high_price or low_price != block.get('low') or high_price != block.get('high'):
        return None
    long = direction == 'bullish'
    if not (confirmation[4] > high_price if long else confirmation[4] < low_price):
        return None
    if any((row[4] < low_price if long else row[4] > high_price)
           for row in higher['rows'] if row[0] > confirmation[0]):
        return None
    return dict(kind='block', timeframe=context_tf, direction=direction, low=low_price, high=high_price,
                open_time=origin['open_time'], confirmed_at=confirmation[0] + SECONDS[context_tf])


def _origin_block(block, higher):
    """The block re-anchored to the origin of the move that broke structure.

    The origin is the extreme candle between the broken swing and the break:
    the lowest low for a bullish block, the highest high for a bearish one.
    Its status is re-read from closes after the break. None when the break
    or its swing is not in the supplied rows.
    """
    direction = block.get('direction')
    at = block.get('confirmed_by')
    brk = next((row for row in higher['features'].get('breaks') or []
                if isinstance(row, dict) and row.get('at') == at and row.get('direction') == direction), None)
    if brk is None or not _positive(brk.get('swing_at')):
        return None
    span = [row for row in higher['rows'] if brk['swing_at'] <= row[0] < at]
    if not span or span[0][0] != brk['swing_at']:
        return None
    long = direction == 'bullish'
    origin = (min(reversed(span), key=lambda row: row[3]) if long
              else max(reversed(span), key=lambda row: row[2]))
    later = [row for row in higher['rows'] if row[0] > at]
    if any(row[4] < origin[3] if long else row[4] > origin[2] for row in later):
        status = 'invalidated'
    elif any(row[3] <= origin[2] and row[2] >= origin[3] for row in later):
        status = 'retested'
    else:
        status = 'untested'
    return dict(block, open_time=origin[0], low=origin[3], high=origin[2], status=status, origin='move')


def _flow_filled(gap, view):
    """A later closed wick traded through the far edge, so the gap is filled.

    A bullish gap is filled at its lower edge, a bearish gap at its upper
    edge. Only closed rows after the gap candle count.
    """
    at = gap.get('at')
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return False
    if gap.get('direction') == 'bullish':
        return any(row[3] <= gap['lower'] for row in view['rows'] if row[0] > at)
    return any(row[2] >= gap['upper'] for row in view['rows'] if row[0] > at)


def _confirmed_flow(gap, higher, context_tf):
    """A fair-value gap is a flow. It confirms the same way a block does."""
    direction = gap.get('direction')
    if direction not in ('bullish', 'bearish'):
        return None
    lower, upper = gap.get('lower'), gap.get('upper')
    if not _positive(lower) or not _positive(upper) or lower >= upper:
        return None
    origin = next((row for row in higher['rows'] if row[0] == gap.get('at')), None)
    if origin is None or _flow_filled(gap, higher):
        return None
    long = direction == 'bullish'
    later = [row for row in higher['rows'] if row[0] > origin[0]]
    confirmation = next((row for row in later if (row[4] > upper if long else row[4] < lower)), None)
    if confirmation is None:
        return None
    if any((row[4] < lower if long else row[4] > upper)
           for row in higher['rows'] if row[0] > confirmation[0]):
        return None
    return dict(kind='flow', timeframe=context_tf, direction=direction, low=lower, high=upper,
                open_time=origin[0], confirmed_at=confirmation[0] + SECONDS[context_tf])


def _wick_reference(view, tf, price, field):
    """A closed candle whose named wick is exactly this price."""
    for row in view['rows']:
        if (row[2] if field == 'high' else row[3]) == price:
            return _reference(view, tf, row[0], field)
    return None


def _opposing_locations(view, tf, *, long, entry):
    """Nearest opposing block or flow on one timeframe.

    A long meets bearish supply above. A short meets bullish demand below.
    The referenced wick is the block or flow extreme on that side.
    """
    found = []
    field = 'high' if long else 'low'

    def add(kind, price, at):
        if not _positive(price):
            return
        if long and not price > entry:
            return
        if not long and not price < entry:
            return
        ref = _reference(view, tf, at, field)
        if ref is None or ref['price'] != price:
            ref = _wick_reference(view, tf, price, field)
        if ref is None or ref in found:
            return
        found.append(dict(ref, kind=kind))

    for block in view['features'].get('order_block_candidates') or []:
        if not isinstance(block, dict) or block.get('status') == 'invalidated':
            continue
        direction = block.get('direction')
        if long and direction != 'bearish':
            continue
        if not long and direction != 'bullish':
            continue
        price = block.get('high') if long else block.get('low')
        add('block', price, block.get('open_time'))
    for gap in view['features'].get('fvgs') or []:
        if not isinstance(gap, dict):
            continue
        direction = gap.get('direction')
        if long and direction != 'bearish':
            continue
        if not long and direction != 'bullish':
            continue
        if not _positive(gap.get('lower')) or not _positive(gap.get('upper')) or _flow_filled(gap, view):
            continue
        price = gap.get('upper') if long else gap.get('lower')
        add('flow', price, gap.get('at'))
    found.sort(key=lambda ref: (ref['price'] if long else -ref['price'], ref['open_time']))
    return found


def _liquidity_taken(view, swing, field):
    """A sweep or a later close through the swing takes its liquidity."""
    price = swing['price']
    want = 'low_sweep' if field == 'low' else 'high_sweep'
    for event in view['features'].get('sweeps') or []:
        if isinstance(event, dict) and event.get('direction') == want and event.get('level') == price:
            return True
    for row in view['rows']:
        if row[0] <= swing['open_time']:
            continue
        if field == 'low' and row[4] < price:
            return True
        if field == 'high' and row[4] > price:
            return True
    return False


def _reaction_candle(view, swing, long):
    """A bullish candle at the weak low, or the short mirror. A sweep is not required.

    The swing candle traded the level. A later candle counts when it trades
    back into that candle and closes with the level.
    """
    price = swing['price']
    origin = next((row for row in view['rows'] if row[0] == swing['open_time']), None)
    if origin is None:
        return None
    found = None
    for row in view['rows']:
        if row[0] < swing['open_time']:
            continue
        overlaps = row[3] <= origin[2] and row[2] >= origin[3]
        if not overlaps and not (row[3] <= price <= row[2]):
            continue
        if long and row[4] > row[1] and row[4] >= price:
            found = row
        if not long and row[4] < row[1] and row[4] <= price:
            found = row
    return found


def _trigger_shift(lower, direction):
    """Latest 5m CHoCH or MSS that closed through its level.

    It does not have to be one of the last three bars, and it is not a 15m
    close. A later close back through that level removes it.
    """
    long = direction == 'bullish'
    rows = {row[0]: row for row in lower['rows']}
    for event in reversed(lower['features'].get('structure_shifts') or []):
        if not isinstance(event, dict):
            continue
        if event.get('direction') != direction or event.get('kind') not in ('choch', 'mss'):
            continue
        row = rows.get(event.get('at'))
        level = event.get('level')
        if row is None or not _positive(level):
            continue
        if long and not row[4] > level:
            continue
        if not long and not row[4] < level:
            continue
        if any((item[4] < level if long else item[4] > level)
               for item in lower['rows'] if item[0] > row[0]):
            continue
        return event, row
    return None, None


def _reaction_stop(lower, trigger_tf, shift, long):
    """The lowest trigger low up to the CHoCH candle, or the highest high for a short."""
    field = 'low' if long else 'high'
    span = [row for row in lower['rows'] if row[0] <= shift['at']]
    if not span:
        return None
    pick = min(span, key=lambda row: (row[3], -row[0])) if long else max(span, key=lambda row: (row[2], row[0]))
    return _reference(lower, trigger_tf, pick[0], field)


def _fallback_targets(higher, *, long, entry):
    """With no opposing 1h block, the next unfilled 1h flow, then the nearest untaken 1h swing."""
    flows = [ref for ref in _opposing_locations(higher, '1h', long=long, entry=entry) if ref.get('kind') == 'flow']
    if flows:
        return flows[:1]
    field = 'high' if long else 'low'
    swings = [dict(ref, kind='swing') for ref in _swings({'1h': higher}, field)
              if (ref['price'] > entry if long else ref['price'] < entry)
              and not _liquidity_taken(higher, ref, field)]
    swings.sort(key=lambda ref: (abs(ref['price'] - entry), ref['open_time']))
    return swings[:1]


def _structural_targets(view, tf, *, long, entry):
    """Untaken opposing swing-structure levels beyond entry, nearest first.

    Swings alternate: consecutive same-side pivots keep only the extreme.
    Two or more untaken raw pivots within a tenth of an ATR form an equal
    high/low pool; the pool level is its outer wick.
    """
    field = 'high' if long else 'low'
    other = 'low' if long else 'high'
    raw = sorted([dict(ref, side=field) for ref in _swings({tf: view}, field)]
                 + [dict(ref, side=other) for ref in _swings({tf: view}, other)],
                 key=lambda ref: ref['open_time'])
    swings = []
    for ref in raw:
        if swings and swings[-1]['side'] == ref['side']:
            outer = ref['price'] > swings[-1]['price'] if ref['side'] == 'high' else ref['price'] < swings[-1]['price']
            if outer:
                swings[-1] = ref
            continue
        swings.append(ref)
    beyond = lambda ref: ref['price'] > entry if long else ref['price'] < entry
    untaken = [ref for ref in raw if ref['side'] == field and beyond(ref) and not _liquidity_taken(view, ref, field)]
    found = {}
    for ref in swings:
        if ref['side'] == field and ref in untaken:
            found[ref['price']] = dict({key: ref[key] for key in ('timeframe', 'open_time', 'field', 'price')}, kind='swing')
    tolerance = .1 * _atr(view['rows'])
    for ref in untaken:
        cluster = [row for row in untaken if abs(row['price'] - ref['price']) <= tolerance]
        if len(cluster) < 2:
            continue
        outer = max(cluster, key=lambda row: row['price']) if long else min(cluster, key=lambda row: row['price'])
        for row in cluster:
            found.pop(row['price'], None)
        found[outer['price']] = dict({key: outer[key] for key in ('timeframe', 'open_time', 'field', 'price')}, kind='pool')
    return sorted(found.values(), key=lambda ref: (abs(ref['price'] - entry), ref['open_time']))


def _swing_points(views):
    points = []
    for tf, view in views.items():
        if not isinstance(view, dict):
            continue
        for field in ('high', 'low'):
            for ref in _swings({tf: view}, field):
                points.append(dict(timeframe=tf, kind=field, price=ref['price'], open_time=ref['open_time']))
    return points


def _target_blocks(structure, structure_tf, book, *, long, entry, aligned):
    """Next opposing block on the structure timeframe.

    With the higher timeframe, a farther 4h block stays available as the better
    target. Against it, only the next structure block is returned.
    """
    blocks = [ref for ref in _opposing_locations(structure, structure_tf, long=long, entry=entry)
              if ref.get('kind') == 'block']
    blocks.sort(key=lambda ref: (ref['price'] if long else -ref['price'], ref['open_time']))
    if not blocks:
        return []
    if not aligned or book is None or structure_tf != '1h':
        return blocks[:1]
    nearest = blocks[0]['price']
    for ref in _opposing_locations(book, '4h', long=long, entry=entry):
        if ref.get('kind') != 'block':
            continue
        if long and not ref['price'] > nearest:
            continue
        if not long and not ref['price'] < nearest:
            continue
        ident = (ref['timeframe'], ref['open_time'], ref['field'], ref['price'])
        if ident in {(row['timeframe'], row['open_time'], row['field'], row['price']) for row in blocks}:
            continue
        blocks.append(ref)
    blocks.sort(key=lambda ref: (ref['price'] if long else -ref['price'], ref['open_time']))
    return blocks


def _early_weak_cards(views, *, current_price, price_step, observed, symbol):
    """1h weak swing, trigger on 5m. 4h is a better target, not a 15m wait.

    A weak low or high is a swing whose liquidity has not been taken. The
    entry does not wait for a sweep, and it does not wait for a 15m or 4h CHoCH.
    The 5m reaction must reach within half a 1h ATR of the weak level, the
    current price must still be within one 1h ATR of it, and the nearest such
    level is tried first. The stop is beyond the 5m extreme up to
    the CHoCH candle. With no opposing 1h block, the target is the next
    unfilled 1h flow, then the nearest untaken 1h swing.
    """
    if '1h' not in views or '5m' not in views:
        return []
    higher, lower = views['1h'], views['5m']
    # Zone cards may see ~48 LTF bars; early proximity/stop stay on the last 12.
    early = dict(lower, rows=lower['rows'][-EARLY_REACTION_BARS:])
    book = views.get('4h')
    higher_trend = (book or higher)['features'].get('trend', 'unknown')
    seconds = SECONDS['5m']
    if not (observed < lower['closed_at'] + seconds):
        return []
    points = _swing_points({'4h': book, '1h': higher, '5m': early} if book else {'1h': higher, '5m': early})
    hourly_atr = _atr(higher['rows'])
    if not hourly_atr > 0:
        return []
    band = WEAK_PROXIMITY_ATR * hourly_atr
    cards = []
    for long, field, direction in ((True, 'low', 'bullish'), (False, 'high', 'bearish')):
        shift, trigger = _trigger_shift(early, direction)
        if shift is None:
            continue
        reach = min(row[3] for row in early['rows']) if long else max(row[2] for row in early['rows'])
        nearby = [swing for swing in _swings({'1h': higher}, field) if abs(reach - swing['price']) <= band]
        nearby.sort(key=lambda swing: (abs(current_price - swing['price']), -swing['open_time']))
        for swing in nearby:
            if abs(current_price - swing['price']) > WEAK_ENTRY_MAX_ATR * hourly_atr:
                continue
            if _liquidity_taken(higher, swing, field):
                continue
            if swing['open_time'] > shift['at']:
                continue
            reaction = _reaction_candle(higher, swing, long)
            if reaction is None:
                continue
            stop_ref = _reaction_stop(early, '5m', shift, long)
            if stop_ref is None:
                continue
            entry = current_price
            aligned = higher_trend == direction and higher_trend in ('bullish', 'bearish')
            ordered = (_target_blocks(higher, '1h', book, long=long, entry=entry, aligned=aligned)
                       or _fallback_targets(higher, long=long, entry=entry))
            if not ordered:
                continue
            target_ref = ordered[0]
            stop = _tick(stop_ref['price'] + (-1 if long else 1) * _atr(early['rows']) * .1,
                         price_step, up=not long)
            target = _tick(target_ref['price'], price_step, up=not long)
            if not (0 < stop < entry < target if long else 0 < target < entry < stop):
                continue
            target_kind = target_ref.get('kind', 'block')
            read = (
                f"1h {field} {swing['price']:g} not swept, "
                f"5m reached {reach:g}, "
                f"{'bullish' if long else 'bearish'} candle, "
                f"5m through {shift['level']:g}, next 1h {target_kind} {target_ref['price']:g}"
            )
            evidence = dict(
                kind='htf_zone_internal', stage='confirmed',
                zone=dict(kind='swing', timeframe='1h', direction=direction,
                          low=swing['price'] if long else target_ref['price'],
                          high=target_ref['price'] if long else swing['price'],
                          open_time=swing['open_time']),
                weak_level=swing['price'], weak_field=field,
                weak_distance_atr=round(abs(entry - swing['price']) / hourly_atr, 3),
                reaction_extreme=reach, proximity_band_atr=WEAK_PROXIMITY_ATR,
                trigger_candle=dict(timeframe='5m', open_time=trigger[0], open=trigger[1],
                                    high=trigger[2], low=trigger[3], close=trigger[4]),
                candidate_block=dict({key: target_ref[key] for key in ('timeframe', 'open_time', 'field', 'price')},
                                     kind=target_kind),
                shift=shift, touch_at=reaction[0] + SECONDS['1h'],
                confirmation_at=lower['closed_at'], expires_at=lower['closed_at'] + seconds,
                alignment='with_higher_timeframe' if aligned else 'against_higher_timeframe')
            family = 'pullback' if aligned else 'countertrend_sweep_reclaim'
            material = dict(version=HTF_ZONE_VERSION, symbol=symbol, family=family,
                            side='long' if long else 'short', horizon='intraday', evidence=evidence,
                            trigger=None, state='ready', stop=stop, target=target, algorithm_read=read)
            ident = sha256(json.dumps(material, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:24]
            reference = lambda ref: {key: ref[key] for key in ('timeframe', 'open_time', 'field')}
            levels = [{key: ref[key] for key in ('timeframe', 'open_time', 'field', 'price')} for ref in ordered]
            for level, ref in zip(levels, ordered):
                level['kind'] = ref.get('kind', 'block')
            cards.append(dict(
                setup_id=ident, **material, macro_trend=higher['features'].get('trend', 'unknown'),
                opposing_target_levels=levels, context_timeframe='1h', trigger_timeframe='5m',
                source_closed_at=lower['closed_at'], requires_full_context=False,
                entry_reference=entry, stop_loss_price=stop, take_profit_price=target,
                reward_risk=abs(target - entry) / abs(entry - stop),
                trigger_condition=None, invalidation_condition='below' if long else 'above',
                invalidation_price=swing['price'],
                stop_reference=reference(stop_ref), target_reference=reference(target_ref),
                swing_highs=[point for point in points if point['kind'] == 'high'],
                swing_lows=[point for point in points if point['kind'] == 'low'],
                management_posture='reduced_risk_countertrend' if family == 'countertrend_sweep_reclaim' else 'normal_policy',
                caveat='1h weak swing and 5m trigger. The read is not a fact the model must obey.'))
            break
    return cards


def _zone_option(zone, higher, lower, trigger_tf, horizon, current_price, price_step, observed, symbol, zone_ready):
    direction = zone['direction']
    long = direction == 'bullish'
    edge = zone['high'] if long else zone['low']
    distal = zone['low'] if long else zone['high']
    outside_zone = current_price <= distal if long else current_price >= distal
    if outside_zone:
        return None
    # Touch memory uses the longer LTF series (up to LTF_TOUCH_BARS). Confirmation
    # and stop ATR stay on the recent tail so older pads do not reshape the stop.
    memory = dict(lower, rows=lower['rows'][-LTF_TOUCH_BARS:])
    recent = dict(lower, rows=lower['rows'][-EARLY_REACTION_BARS:])
    subsequent = [row for row in memory['rows'] if row[0] >= zone['confirmed_at']]
    if any((row[4] < distal if long else row[4] > distal) for row in subsequent):
        return None
    touches = [row for row in subsequent if row[3] <= zone['high'] and row[2] >= zone['low']]
    seconds = SECONDS[trigger_tf]
    latest = lower['rows'][-1]
    shift = next((event for event in reversed(lower['features'].get('structure_shifts', []))
                  if event.get('at') == latest[0] and event.get('direction') == direction
                  and event.get('kind') in ('choch', 'mss') and _positive(event.get('level'))
                  and event.get('structure', 'swing') == 'swing'
                  and (latest[4] > event['level'] if long else latest[4] < event['level'])), None)
    prior_touches = [row for row in touches if row[0] < latest[0]]
    ready = shift is not None and bool(prior_touches) and observed < lower['closed_at'] + seconds

    touch = None
    if ready:
        touch = prior_touches[-1]
        # The initial stop is the confirming CHoCH/MSS candle, not another wick.
        anchor = next(row for row in lower['rows'] if row[0] == shift['at'])
        trigger = condition = None
    elif touches:
        touch = touches[-1]
        field = 'high' if long else 'low'
        swings = _swings({trigger_tf: recent}, field)
        levels = [ref['price'] for ref in swings
                  if (ref['price'] > max(current_price, latest[4]) if long
                      else ref['price'] < min(current_price, latest[4]))]
        trigger = (min(levels) if long else max(levels)) if levels else None
        condition = ('closed_candle_cross_above' if long else 'closed_candle_cross_below') if levels else None
        anchor = touch
    else:
        approaching = current_price > edge if long else current_price < edge
        trigger = edge if approaching else None
        condition = ('price_below' if long else 'price_above') if approaching else None
        anchor = None

    entry = trigger if trigger is not None else current_price
    field = 'low' if long else 'high'
    stop_ref = (_reference(lower, trigger_tf, anchor[0], field) if anchor is not None
                else _reference(higher, zone['timeframe'], zone['open_time'], field))
    if stop_ref is None:
        return None
    stop = _tick(stop_ref['price'] + (-1 if long else 1) * _atr(recent['rows']) * .1,
                 price_step, up=not long)
    macro = higher['features'].get('trend', 'unknown')
    # With the higher timeframe, farther structural levels stay available.
    # Against it, the only target is the nearer of the first structural level
    # and the next opposing block or flow on the trigger timeframe.
    aligned = macro == direction and macro in ('bullish', 'bearish')
    structural = _structural_targets(higher, zone['timeframe'], long=long, entry=entry)
    if aligned:
        ordered = structural
        if not ordered:
            targets = _swings({zone['timeframe']: higher}, 'high' if long else 'low')
            possible = [ref for ref in targets if (ref['price'] > entry if long else ref['price'] < entry)]
            ordered = sorted(possible, key=lambda ref: (ref['price'] if long else -ref['price'], ref['open_time']))
    else:
        ordered = sorted(structural[:1] + _opposing_locations(lower, trigger_tf, long=long, entry=entry)[:1],
                         key=lambda ref: abs(ref['price'] - entry))[:1]
    if not ordered:
        return None
    target_ref = ordered[0]
    target = _tick(target_ref['price'], price_step, up=not long)
    if not (0 < stop < entry < target if long else 0 < target < entry < stop):
        return None

    features = higher['features']
    last_break = next(iter(reversed(features.get('breaks', []))), None)
    last_shift = next(iter(reversed(features.get('structure_shifts', []))), None)
    evidence = dict(kind=('htf_zone_' + shift['kind']) if ready else 'htf_zone_watch',
                    stage='confirmed' if ready else 'awaiting_confirmation' if touch else 'awaiting_touch',
                    zone=zone,
                    # Touch metadata stays on the card so LLM packets need not carry the full LTF pad.
                    touch_open_time=touch[0] if touch else None,
                    touch_at=touch[0] + seconds if touch else None,
                    confirmation_at=lower['closed_at'] if ready else None,
                    context_last_break=last_break, context_last_shift=last_shift)
    if ready:
        evidence.update(shift=shift, expires_at=lower['closed_at'] + seconds)
    evidence['alignment'] = 'with_higher_timeframe' if aligned else 'against_higher_timeframe'
    if ready and not zone_ready:
        evidence['watch_only'] = True
    family = 'countertrend_sweep_reclaim' if macro in ('bullish', 'bearish') and macro != direction else 'pullback'
    state = 'ready' if ready and zone_ready else 'watch'
    material = dict(version=HTF_ZONE_VERSION, symbol=symbol, family=family,
                    side='long' if long else 'short', horizon=horizon, evidence=evidence,
                    trigger=trigger, state=state, stop=stop, target=target)
    ident = sha256(json.dumps(material, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:24]
    reference = lambda ref: {key: ref[key] for key in ('timeframe', 'open_time', 'field')}
    opposing_target_levels = [dict({key: ref[key] for key in ('timeframe', 'open_time', 'field', 'price')},
                                   **({'kind': ref['kind']} if ref.get('kind') else {})) for ref in ordered]
    return dict(setup_id=ident, **material, macro_trend=macro, opposing_target_levels=opposing_target_levels,
                context_timeframe=zone['timeframe'], trigger_timeframe=trigger_tf,
                source_closed_at=lower['closed_at'], requires_full_context=False,
                entry_reference=entry, stop_loss_price=stop, take_profit_price=target,
                reward_risk=abs(target - entry) / abs(entry - stop),
                trigger_condition=condition, invalidation_condition='below' if long else 'above',
                invalidation_price=distal,
                stop_reference=reference(stop_ref), target_reference=reference(target_ref),
                management_posture='reduced_risk_countertrend' if family == 'countertrend_sweep_reclaim' else 'normal_policy',
                caveat='Closed HTF zone and LTF evidence; not a success probability or execution approval.')


def build_htf_zone_setups(market_context, *, current_price, price_step=0, zone_ready=None):
    """Use a confirmed HTF block or flow, a later closed LTF touch, then a closed CHoCH/MSS.

    A block is re-anchored to the origin of the move: the extreme candle between
    the broken swing and the break. Swing is 4h structure with a 15m trigger.
    Intraday is 1h structure with a 5m trigger. Both directions use that same path. A ready long stops just
    beyond the CHoCH low. A ready short stops just beyond the CHoCH high.
    The trigger candle must close through the CHoCH level, and that CHoCH must
    be a swing (not internal) break. Targets are untaken alternating swings or
    equal-high/low pools. When the trade agrees with the higher timeframe,
    farther levels stay supplied. When it does not, the only target is the
    nearer of the first such level and the next opposing block or flow on the
    trigger timeframe. Unless ``zone_ready`` (default ``ZONE_CARDS_READY``) is
    on, a confirmed zone card stays a watch with ``watch_only`` evidence.
    A touch or a future level can produce a watch. The LTF trigger must close
    after the touch candle, so same-candle intrabar ordering is never inferred.
    Zone touch memory reads up to ``LTF_TOUCH_BARS`` closed LTF candles so a
    touch older than the previous 12-bar window stays ``awaiting_confirmation``;
    the card stores ``touch_open_time`` / ``touch_at`` rather than shipping the
    whole pad into the model packet. Early weak-swing cards still score
    proximity and reaction stops on the last ``EARLY_REACTION_BARS`` only.
    An early 1h card does not wait for a 15m or 4h CHoCH. A weak swing is
    liquidity that has not been taken, the 5m reaction must reach near it, and
    price must not have run more than one 1h ATR away from it. The 5m CHoCH is the trigger, the stop is beyond the 5m extreme before it,
    and the target is the next 1h block, else the next unfilled 1h flow, else
    the nearest untaken 1h swing. A 4h block is only the better target.
    A filled flow is neither a zone nor a target.
    BTC/ETH context stays independent of the symbol's executable evidence.
    """
    if (not _positive(current_price) or isinstance(price_step, bool)
            or not isinstance(price_step, (int, float)) or not isfinite(price_step) or price_step < 0):
        return []
    if zone_ready is None:
        zone_ready = ZONE_CARDS_READY
    views = _views(market_context)
    options = []
    for context_tf, trigger_tf, horizon in HTF_ZONE_PATHS:
        if context_tf not in views or trigger_tf not in views:
            continue
        higher, lower = views[context_tf], views[trigger_tf]
        zones = []
        for block in higher['features'].get('order_block_candidates', []):
            zone = _confirmed_zone(_origin_block(block, higher) or block, higher, context_tf)
            if zone is not None:
                zones.append(zone)
        for gap in higher['features'].get('fvgs', []) or []:
            zone = _confirmed_flow(gap, higher, context_tf)
            if zone is not None:
                zones.append(zone)
        for zone in zones:
            option = _zone_option(zone, higher, lower, trigger_tf, horizon, current_price, price_step,
                                  market_context['observed_at'], market_context.get('symbol'), zone_ready)
            if option is not None:
                options.append(option)
    options.extend(_early_weak_cards(
        views, current_price=current_price, price_step=price_step,
        observed=market_context['observed_at'], symbol=market_context.get('symbol')))
    return options
