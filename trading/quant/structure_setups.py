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
# Early weak-swing cards: structure TF + trigger TF + horizon.
# 1h reacts on 5m; 4h reacts on 15m (FET-style). Do not dual-fire 5m on the 4h path.
EARLY_WEAK_PATHS = (('1h', '5m', 'intraday'), ('4h', '15m', 'swing'))
HTF_ZONE_VERSION = 'htf-zone-setup-v1'
# The trigger reaction must reach within this many structure-TF ATRs of the weak level.
WEAK_PROXIMITY_ATR = .5
# Entry (current price) must still be within this many structure-TF ATRs of the weak level.
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
    """A sweep, a swept equal-H/L pool member, or a later close takes liquidity.

    Pool sweeps from ``market.structure_features`` carry every member. A double
    bottom/top is taken when the pool outer wick is swept, not only when the
    latest swing alone is.
    """
    price = swing['price']
    pivot_at = swing.get('open_time')
    want = 'low_sweep' if field == 'low' else 'high_sweep'
    pool_kind = 'eql' if field == 'low' else 'eqh'

    def _member_hit(members):
        for member in members or []:
            if not isinstance(member, dict):
                continue
            if member.get('price') == price or (pivot_at is not None and member.get('pivot_at') == pivot_at):
                return True
        return False

    for event in view['features'].get('sweeps') or []:
        if not isinstance(event, dict) or event.get('direction') != want:
            continue
        if event.get('level') == price or _member_hit(event.get('members')):
            return True
    for pool in view['features'].get('liquidity_pools') or []:
        if not isinstance(pool, dict) or pool.get('kind') != pool_kind or not pool.get('swept_at'):
            continue
        if _member_hit(pool.get('members')):
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
    """Latest trigger-TF CHoCH or MSS that closed through its level.

    It does not have to be one of the last three bars. A later close back
    through that level removes it. The caller supplies the trigger window
    (5m for 1h early cards, 15m for 4h early cards).
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


def _continuation_bar(rows, choch_at, long):
    """First closed LTF candle after the CHoCH close that continues in trade direction.

    A naked CHoCH wick through a strong high/low is not enough. Long needs a
    later bullish body (close > open); short needs a later bearish body.
    """
    for row in rows:
        if row[0] <= choch_at:
            continue
        if long and row[4] > row[1]:
            return row
        if not long and row[4] < row[1]:
            return row
    return None


def _nearby_ltf_ob(view, *, long, choch_at, current_price):
    """Nearest valid LTF demand (bullish) or supply (bearish) OB for a pullback entry.

    Only blocks formed at or before the CHoCH count. Nearby means the box mid
    is within two LTF ATRs of price, or price still sits on the trade side of
    the distal edge within that band (blue-box style retest, not a chase).
    """
    want = 'bullish' if long else 'bearish'
    atr = _atr(view['rows'])
    band = atr if atr > 0 else 0
    found = []
    for block in view['features'].get('order_block_candidates') or []:
        if not isinstance(block, dict) or block.get('status') == 'invalidated':
            continue
        if block.get('direction') != want:
            continue
        low, high = block.get('low'), block.get('high')
        at = block.get('open_time')
        if not _positive(low) or not _positive(high) or low >= high or not _positive(at) or at > choch_at:
            continue
        mid = (low + high) / 2
        if band > 0:
            if abs(current_price - mid) > 2 * band:
                if long and current_price < low - band:
                    continue
                if not long and current_price > high + band:
                    continue
                if abs(current_price - mid) > 2 * band:
                    continue
        found.append(block)
    found.sort(key=lambda block: (abs(current_price - (block['low'] + block['high']) / 2),
                                  -block.get('open_time', 0)))
    return found[0] if found else None


def _ob_touch_after(rows, ob, choch_at):
    """A closed LTF bar after the CHoCH that trades back into the OB box."""
    for row in rows:
        if row[0] <= choch_at:
            continue
        if row[3] <= ob['high'] and row[2] >= ob['low']:
            return row
    return None


def _ob_retest_active(ob, *, long, current_price, atr, touch_row, latest):
    """Pullback still in play: price at the box, or the latest bar is the touch."""
    if touch_row is None:
        return False
    if latest is not None and touch_row[0] == latest[0]:
        return True
    pad = .1 * atr if atr > 0 else 0
    return ob['low'] - pad <= current_price <= ob['high'] + pad


def _ob_retest_stop(lower, trigger_tf, ob, long):
    """Stop beyond the OB distal, or the last LTF swing low/high if more protective."""
    field = 'low' if long else 'high'
    distal = ob['low'] if long else ob['high']
    swings = _swings({trigger_tf: lower}, field)
    stop_price = distal
    stop_at = ob.get('open_time')
    if swings:
        swing = min(swings, key=lambda ref: ref['price']) if long else max(swings, key=lambda ref: ref['price'])
        if long and swing['price'] <= distal:
            stop_price, stop_at = swing['price'], swing['open_time']
        if not long and swing['price'] >= distal:
            stop_price, stop_at = swing['price'], swing['open_time']
    ref = _reference(lower, trigger_tf, stop_at, field)
    if ref is not None and ref['price'] == stop_price:
        return ref
    wick = _wick_reference(lower, trigger_tf, stop_price, field)
    if wick is not None:
        return wick
    if ref is not None:
        return dict(ref, price=stop_price)
    return dict(timeframe=trigger_tf, open_time=stop_at, field=field, price=stop_price)


def _early_entry_path(early, trigger_tf, shift, long, current_price):
    """After LTF CHoCH: confirm_bar first, else OB retest. Naked CHoCH is neither.

    Returns (path, stop_ref, touch_meta). path is 'confirm_bar', 'ob_retest', or None.
    """
    confirm = _continuation_bar(early['rows'], shift['at'], long)
    if confirm is not None:
        stop_ref = _reaction_stop(early, trigger_tf, shift, long)
        return 'confirm_bar', stop_ref, dict(confirm_bar_open_time=confirm[0])
    ob = _nearby_ltf_ob(early, long=long, choch_at=shift['at'], current_price=current_price)
    if ob is None:
        return None, _reaction_stop(early, trigger_tf, shift, long), {}
    touch = _ob_touch_after(early['rows'], ob, shift['at'])
    atr = _atr(early['rows'])
    latest = early['rows'][-1] if early['rows'] else None
    meta = dict(ob=dict(direction=ob.get('direction'), open_time=ob.get('open_time'),
                        low=ob.get('low'), high=ob.get('high'), status=ob.get('status')),
                touch_open_time=touch[0] if touch is not None else None)
    if not _ob_retest_active(ob, long=long, current_price=current_price, atr=atr,
                             touch_row=touch, latest=latest):
        return None, _ob_retest_stop(early, trigger_tf, ob, long), meta
    return 'ob_retest', _ob_retest_stop(early, trigger_tf, ob, long), meta


def _fallback_targets(higher, structure_tf, *, long, entry):
    """With no opposing structure block, the next unfilled flow, then the nearest untaken swing."""
    flows = [ref for ref in _opposing_locations(higher, structure_tf, long=long, entry=entry)
             if ref.get('kind') == 'flow']
    if flows:
        return flows[:1]
    field = 'high' if long else 'low'
    swings = [dict(ref, kind='swing') for ref in _swings({structure_tf: higher}, field)
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
    """Weak-swing early cards for each EARLY_WEAK_PATHS pair.

    A weak low or high is a swing whose liquidity has not been taken. The
    entry does not wait for a sweep. The 1h path triggers on 5m and does not
    wait for a 15m or 4h CHoCH; the 4h path triggers on 15m and does not wait
    for a higher CHoCH. Proximity uses half a structure ATR, entry stays within
    one structure ATR, nearest weak level first. After the LTF CHoCH, READY
    needs a same-direction continuation bar (confirm_bar) or a pullback touch
    into a nearby LTF demand/supply OB (ob_retest); a naked CHoCH stays WATCH.
    confirm_bar keeps the reaction stop; ob_retest stops beyond the OB distal
    or the last LTF swing. The target is the next opposing structure block
    else unfilled flow else untaken swing. On the 1h path a farther 4h block
    can be the better target. The 4h path does not also fire a 5m trigger in
    this release.
    """
    cards = []
    for structure_tf, trigger_tf, horizon in EARLY_WEAK_PATHS:
        cards.extend(_early_weak_path(
            views, structure_tf, trigger_tf, horizon,
            current_price=current_price, price_step=price_step,
            observed=observed, symbol=symbol))
    return cards


def _early_weak_path(views, structure_tf, trigger_tf, horizon, *, current_price, price_step, observed, symbol):
    if structure_tf not in views or trigger_tf not in views:
        return []
    higher, lower = views[structure_tf], views[trigger_tf]
    # Zone cards may see ~48 LTF bars; early proximity/stop stay on the last 12.
    early = dict(lower, rows=lower['rows'][-EARLY_REACTION_BARS:])
    # Only the 1h path uses 4h as a better-target book.
    book = views.get('4h') if structure_tf == '1h' else None
    higher_trend = (book or higher)['features'].get('trend', 'unknown')
    seconds = SECONDS[trigger_tf]
    if not (observed < lower['closed_at'] + seconds):
        return []
    point_views = {structure_tf: higher, trigger_tf: early}
    if book is not None:
        point_views['4h'] = book
    elif structure_tf == '4h' and '1h' in views:
        point_views['1h'] = views['1h']
    points = _swing_points(point_views)
    structure_atr = _atr(higher['rows'])
    if not structure_atr > 0:
        return []
    band = WEAK_PROXIMITY_ATR * structure_atr
    cards = []
    for long, field, direction in ((True, 'low', 'bullish'), (False, 'high', 'bearish')):
        shift, trigger = _trigger_shift(early, direction)
        if shift is None:
            continue
        reach = min(row[3] for row in early['rows']) if long else max(row[2] for row in early['rows'])
        nearby = [swing for swing in _swings({structure_tf: higher}, field)
                  if abs(reach - swing['price']) <= band]
        nearby.sort(key=lambda swing: (abs(current_price - swing['price']), -swing['open_time']))
        for swing in nearby:
            if abs(current_price - swing['price']) > WEAK_ENTRY_MAX_ATR * structure_atr:
                continue
            if _liquidity_taken(higher, swing, field):
                continue
            if swing['open_time'] > shift['at']:
                continue
            reaction = _reaction_candle(higher, swing, long)
            if reaction is None:
                continue
            path, stop_ref, path_meta = _early_entry_path(
                early, trigger_tf, shift, long, current_price)
            if stop_ref is None:
                continue
            ready = path is not None
            entry = current_price
            aligned = higher_trend == direction and higher_trend in ('bullish', 'bearish')
            ordered = (_target_blocks(higher, structure_tf, book, long=long, entry=entry, aligned=aligned)
                       or _fallback_targets(higher, structure_tf, long=long, entry=entry))
            if not ordered:
                continue
            target_ref = ordered[0]
            stop = _tick(stop_ref['price'] + (-1 if long else 1) * _atr(early['rows']) * .1,
                         price_step, up=not long)
            target = _tick(target_ref['price'], price_step, up=not long)
            if not (0 < stop < entry < target if long else 0 < target < entry < stop):
                continue
            target_kind = target_ref.get('kind', 'block')
            path_note = (f", entry_path {path}" if path else
                         f", awaiting confirm bar or {trigger_tf} OB retest")
            read = (
                f"{structure_tf} {field} {swing['price']:g} not swept, "
                f"{trigger_tf} reached {reach:g}, "
                f"{'bullish' if long else 'bearish'} candle, "
                f"{trigger_tf} through {shift['level']:g}"
                f"{path_note}, "
                f"next {structure_tf} {target_kind} {target_ref['price']:g}"
            )
            ob_meta = path_meta.get('ob')
            trigger_price = None
            if not ready and isinstance(ob_meta, dict):
                trigger_price = ob_meta['high'] if long else ob_meta['low']
            evidence = dict(
                kind='htf_zone_internal',
                stage='confirmed' if ready else 'awaiting_entry',
                zone=dict(kind='swing', timeframe=structure_tf, direction=direction,
                          low=swing['price'] if long else target_ref['price'],
                          high=target_ref['price'] if long else swing['price'],
                          open_time=swing['open_time']),
                weak_level=swing['price'], weak_field=field,
                weak_distance_atr=round(abs(entry - swing['price']) / structure_atr, 3),
                reaction_extreme=reach, proximity_band_atr=WEAK_PROXIMITY_ATR,
                trigger_candle=dict(timeframe=trigger_tf, open_time=trigger[0], open=trigger[1],
                                    high=trigger[2], low=trigger[3], close=trigger[4]),
                candidate_block=dict({key: target_ref[key] for key in ('timeframe', 'open_time', 'field', 'price')},
                                     kind=target_kind),
                shift=shift, touch_at=reaction[0] + SECONDS[structure_tf],
                confirmation_at=lower['closed_at'] if ready else None,
                expires_at=lower['closed_at'] + seconds,
                alignment='with_higher_timeframe' if aligned else 'against_higher_timeframe')
            if path:
                evidence['entry_path'] = path
            if path_meta.get('confirm_bar_open_time') is not None:
                evidence['confirm_bar_open_time'] = path_meta['confirm_bar_open_time']
            if isinstance(ob_meta, dict):
                evidence['ltf_ob'] = ob_meta
            if path_meta.get('touch_open_time') is not None:
                evidence['ob_touch_open_time'] = path_meta['touch_open_time']
            family = 'pullback' if aligned else 'countertrend_sweep_reclaim'
            material = dict(version=HTF_ZONE_VERSION, symbol=symbol, family=family,
                            side='long' if long else 'short', horizon=horizon, evidence=evidence,
                            trigger=trigger_price, state='ready' if ready else 'watch',
                            stop=stop, target=target, algorithm_read=read)
            ident = sha256(json.dumps(material, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:24]
            reference = lambda ref: {key: ref[key] for key in ('timeframe', 'open_time', 'field')}
            levels = [{key: ref[key] for key in ('timeframe', 'open_time', 'field', 'price')} for ref in ordered]
            for level, ref in zip(levels, ordered):
                level['kind'] = ref.get('kind', 'block')
            cards.append(dict(
                setup_id=ident, **material, macro_trend=higher['features'].get('trend', 'unknown'),
                opposing_target_levels=levels, context_timeframe=structure_tf, trigger_timeframe=trigger_tf,
                source_closed_at=lower['closed_at'], requires_full_context=False,
                entry_reference=entry if ready else trigger_price,
                stop_loss_price=stop, take_profit_price=target,
                reward_risk=abs(target - entry) / abs(entry - stop),
                trigger_condition=(None if ready else
                                   ('closed_candle_overlap_ob' if trigger_price is not None
                                    else 'closed_continuation_or_ob_retest')),
                invalidation_condition='below' if long else 'above',
                invalidation_price=swing['price'],
                stop_reference=reference(stop_ref), target_reference=reference(target_ref),
                swing_highs=[point for point in points if point['kind'] == 'high'],
                swing_lows=[point for point in points if point['kind'] == 'low'],
                management_posture='reduced_risk_countertrend' if family == 'countertrend_sweep_reclaim' else 'normal_policy',
                caveat=f'{structure_tf} weak swing and {trigger_tf} trigger. The read is not a fact the model must obey.'))
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
    An early 1h card does not wait for a 15m or 4h CHoCH; an early 4h card
    triggers on 15m and does not wait for a higher CHoCH. A weak swing is
    liquidity that has not been taken, the trigger reaction must reach near it
    (≤0.5 structure ATR), and price must not have run more than one structure
    ATR away from it. After the LTF CHoCH, READY needs a same-direction
    continuation bar or an LTF demand/supply OB retest (``entry_path``); a
    naked CHoCH stays WATCH. confirm_bar stops beyond the trigger extreme
    before the CHoCH; ob_retest stops beyond the OB distal or last LTF swing.
    The target is the next structure block, else the next unfilled flow,
    else the nearest untaken swing. On the 1h path a 4h block is only the
    better target. The 4h early path does not also fire a 5m trigger here.
    A filled flow is neither a zone nor a target. ``ZONE_CARDS_READY`` stays
    off unless the caller forces ``zone_ready``.
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
