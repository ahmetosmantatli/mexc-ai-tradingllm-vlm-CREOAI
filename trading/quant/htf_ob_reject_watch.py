"""Parallel HTF OB-rejection watch toward a still-valid weak extreme.

Additive watch/card family. Does **not** auto-enter, does not flip
``ZONE_CARDS_READY``, and does not rewrite early ``confirm_bar`` /
``ob_retest`` paths. Prefer WATCH + defensive management hooks when an open
position faces this threat.
"""
from hashlib import sha256
import json
from math import isfinite

from trading.quant.structure_setups import (
    EARLY_WEAK_PATHS,
    SECONDS,
    _atr,
    _liquidity_taken,
    _positive,
    _reference,
    _swings,
    _tick,
    _trigger_shift,
    _views,
)

HTF_OB_REJECT_PATHS = EARLY_WEAK_PATHS  # 1h/5m + 4h/15m
HTF_OB_REJECT_VERSION = 'htf-ob-reject-watch-v1'
HTF_OB_REJECT_KIND = 'htf_ob_reject_watch'
# Price within this fraction of the OB proximal edge arms the proximity alert.
PROXIMITY_PCT = .02
# Milder near-band used for messaging (1%); arming uses PROXIMITY_PCT.
PROXIMITY_PCT_ALERT = .01
# Defensive stop keeps this fraction of initial risk when a threat activates.
DEFENSIVE_STOP_RISK_KEEP = .75
# Defensive TP keeps this fraction of remaining target distance from entry.
DEFENSIVE_TP_KEEP = .5
STAGES = ('ob_armed', 'prox_alert', 'htf_wick_ok', 'ltf_choch')


def _proximal_distal(block, *, supply):
    """Supply (bearish) approaches from below; demand (bullish) from above."""
    low, high = block['low'], block['high']
    if supply:
        return low, high  # proximal, distal
    return high, low


def _first_untouched_opposing_ob(view, *, weak_field, weak_price):
    """Nearest untested HTF OB opposing the weak extreme.

    Weak low → first untested supply (bearish) OB above the weak price.
    Weak high → first untested demand (bullish) OB below the weak price.
    """
    supply = weak_field == 'low'
    want = 'bearish' if supply else 'bullish'
    found = []
    for block in view['features'].get('order_block_candidates') or []:
        if not isinstance(block, dict) or block.get('status') != 'untested':
            continue
        if block.get('direction') != want:
            continue
        low, high = block.get('low'), block.get('high')
        if not _positive(low) or not _positive(high) or low >= high:
            continue
        if supply and not low > weak_price:
            continue
        if not supply and not high < weak_price:
            continue
        found.append(block)
    if not found:
        return None
    if supply:
        found.sort(key=lambda b: (b['low'], b.get('open_time') or 0))
    else:
        found.sort(key=lambda b: (-b['high'], b.get('open_time') or 0))
    return found[0]


def _ob_close_through(view, block, *, supply):
    """Later close through the OB distal invalidates the watch."""
    _, distal = _proximal_distal(block, supply=supply)
    open_time = block.get('open_time')
    for row in view['rows']:
        if open_time is not None and row[0] <= open_time:
            continue
        if supply and row[4] > distal:
            return True
        if not supply and row[4] < distal:
            return True
    return False


def _ob_wick_reject(view, block, *, supply):
    """Wick traded into the OB box without a close through the distal edge."""
    proximal, distal = _proximal_distal(block, supply=supply)
    open_time = block.get('open_time')
    for row in reversed(view['rows']):
        if open_time is not None and row[0] <= open_time:
            break
        if supply:
            into = row[2] >= proximal and row[3] <= distal
            closed_through = row[4] > distal
        else:
            into = row[3] <= proximal and row[2] >= distal
            closed_through = row[4] < distal
        if into and not closed_through:
            return row
    return None


def _proximity_fraction(price, proximal):
    if not _positive(price) or not _positive(proximal):
        return None
    return abs(price - proximal) / price


def _stage_for(*, proximity, wick_row, shift):
    if shift is not None and wick_row is not None:
        return 'ltf_choch'
    if wick_row is not None:
        return 'htf_wick_ok'
    if proximity is not None and proximity <= PROXIMITY_PCT:
        return 'prox_alert'
    return 'ob_armed'


def _card_material(*, symbol, horizon, structure_tf, trigger_tf, side, evidence,
                   observed, weak_price, block, proximal, distal, current_price, price_step,
                   book_levels=None, macro_trend='unknown', stop_ref=None, target_ref=None):
    family = 'htf_ob_reject_threat'
    read = (
        f"{structure_tf} weak {evidence['weak_field']} {weak_price:g} still valid, "
        f"first untested {block['direction']} OB {block['low']:g}-{block['high']:g}, "
        f"stage {evidence['stage']}, watch only (no auto-entry)"
    )
    material = dict(
        version=HTF_OB_REJECT_VERSION, symbol=symbol, family=family,
        side=side, horizon=horizon, evidence=evidence,
        trigger=proximal, state='watch',
        stop=distal, target=weak_price, algorithm_read=read)
    ident = sha256(json.dumps(material, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:24]
    stop = _tick(distal + (-1 if side == 'long' else 1) * abs(distal - proximal) * .05,
                 price_step, up=side != 'long')
    target = _tick(weak_price, price_step, up=side != 'long')
    levels = list(book_levels or [])
    if stop_ref is None:
        stop_ref = dict(timeframe=structure_tf, open_time=block.get('open_time'),
                        field='high' if side == 'short' else 'low')
    if target_ref is None:
        target_ref = dict(timeframe=structure_tf, open_time=evidence.get('weak_open_time'),
                          field=evidence.get('weak_field') or ('low' if side == 'short' else 'high'))
    risk = abs((current_price or proximal) - stop)
    reward_risk = (abs(target - (current_price or proximal)) / risk) if risk > 0 else 0.
    return dict(
        setup_id=ident, **material,
        macro_trend=macro_trend or 'unknown',
        opposing_target_levels=levels,
        context_timeframe=structure_tf, trigger_timeframe=trigger_tf,
        source_closed_at=evidence.get('source_closed_at'),
        requires_full_context=False,
        entry_reference=None,
        stop_loss_price=stop, take_profit_price=target,
        reward_risk=reward_risk,
        trigger_condition='htf_ob_wick_reject_then_ltf_choch',
        invalidation_condition='above' if side == 'short' else 'below',
        invalidation_price=distal,
        stop_reference={key: stop_ref[key] for key in ('timeframe', 'open_time', 'field') if key in stop_ref},
        target_reference={key: target_ref[key] for key in ('timeframe', 'open_time', 'field') if key in target_ref},
        management_posture='defensive_threat_watch',
        watch_only=True,
        auto_entry=False,
        caveat=('HTF OB rejection toward weak extreme is a parallel watch. '
                'It does not auto-enter and does not replace early confirm_bar/ob_retest.'),
        observed_at=observed, current_price=current_price)


def _path_cards(views, structure_tf, trigger_tf, horizon, *, current_price, price_step, observed, symbol):
    if structure_tf not in views or trigger_tf not in views:
        return []
    higher, lower = views[structure_tf], views[trigger_tf]
    seconds = SECONDS[trigger_tf]
    if not (observed < lower['closed_at'] + seconds):
        return []
    book = views.get('4h') if structure_tf == '1h' else None
    cards = []
    for long_threat, field in ((False, 'low'), (True, 'high')):
        # Threat trade direction is toward the weak extreme.
        side = 'long' if long_threat else 'short'
        supply = field == 'low'
        for swing in _swings({structure_tf: higher}, field):
            if _liquidity_taken(higher, swing, field):
                continue
            block = _first_untouched_opposing_ob(higher, weak_field=field, weak_price=swing['price'])
            if block is None:
                continue
            if _ob_close_through(higher, block, supply=supply):
                continue
            proximal, distal = _proximal_distal(block, supply=supply)
            proximity = _proximity_fraction(current_price, proximal)
            wick_row = _ob_wick_reject(higher, block, supply=supply)
            # LTF CHoCH toward the weak extreme (bearish for weak low, bullish for weak high).
            direction = 'bullish' if long_threat else 'bearish'
            shift, trigger = _trigger_shift(lower, direction)
            stage = _stage_for(proximity=proximity, wick_row=wick_row, shift=shift)
            zone = dict(kind='block', timeframe=structure_tf, direction=block['direction'],
                        low=block['low'], high=block['high'], open_time=block.get('open_time'))
            evidence = dict(
                kind=HTF_OB_REJECT_KIND,
                stage=stage,
                watch_only=True,
                auto_entry=False,
                zone=zone,
                weak_level=swing['price'],
                weak_field=field,
                weak_open_time=swing.get('open_time'),
                threat_side=side,
                htf_ob=dict(direction=block['direction'], low=block['low'], high=block['high'],
                            open_time=block.get('open_time'), status=block.get('status'),
                            proximal=proximal, distal=distal),
                proximity_pct=None if proximity is None else round(proximity, 5),
                proximity_armed=bool(proximity is not None and proximity <= PROXIMITY_PCT),
                proximity_alert_band=PROXIMITY_PCT_ALERT,
                proximity_arm_band=PROXIMITY_PCT,
                htf_wick_ok=wick_row is not None,
                source_closed_at=lower['closed_at'],
                expires_at=lower['closed_at'] + seconds,
            )
            if proximity is not None and proximity <= PROXIMITY_PCT:
                evidence['touch_at'] = lower['closed_at']
            if wick_row is not None:
                evidence['wick_reject'] = dict(
                    timeframe=structure_tf, open_time=wick_row[0],
                    high=wick_row[2], low=wick_row[3], close=wick_row[4])
            if shift is not None:
                evidence['shift'] = shift
                evidence['trigger_candle'] = dict(
                    timeframe=trigger_tf, open_time=trigger[0], open=trigger[1],
                    high=trigger[2], low=trigger[3], close=trigger[4])
            book_levels = []
            if book is not None:
                # 1h/5m path may still cite 4h structure as a safer TP book when long threat.
                for other in book['features'].get('order_block_candidates') or []:
                    if not isinstance(other, dict) or other.get('status') == 'invalidated':
                        continue
                    price = other.get('high') if long_threat else other.get('low')
                    if not _positive(price):
                        continue
                    if long_threat and not price > current_price:
                        continue
                    if not long_threat and not price < current_price:
                        continue
                    ref = _reference(book, '4h', other.get('open_time'), 'high' if long_threat else 'low')
                    if ref is None:
                        continue
                    book_levels.append(dict(ref, kind='block', book='4h_context'))
                book_levels.sort(key=lambda r: (r['price'] if long_threat else -r['price'], r['open_time']))
                book_levels = book_levels[:3]
            stop_ref = dict(timeframe=structure_tf, open_time=block.get('open_time'),
                            field='high' if supply else 'low')
            target_ref = dict(timeframe=structure_tf, open_time=swing.get('open_time'), field=field)
            cards.append(_card_material(
                symbol=symbol, horizon=horizon, structure_tf=structure_tf, trigger_tf=trigger_tf,
                side=side, evidence=evidence, observed=observed, weak_price=swing['price'],
                block=block, proximal=proximal, distal=distal, current_price=current_price,
                price_step=price_step, book_levels=book_levels,
                macro_trend=higher['features'].get('trend', 'unknown'),
                stop_ref=stop_ref, target_ref=target_ref))
            break  # nearest valid weak extreme per direction
    return cards


def build_htf_ob_reject_watch(market_context, *, current_price, price_step=0, views=None):
    """Build parallel OB-reject watch cards for both TF pairs. Always WATCH."""
    if (not _positive(current_price) or isinstance(price_step, bool)
            or not isinstance(price_step, (int, float)) or not isfinite(price_step) or price_step < 0):
        return []
    resolved = views if views is not None else _views(market_context)
    observed = market_context['observed_at']
    symbol = market_context.get('symbol')
    cards = []
    for structure_tf, trigger_tf, horizon in HTF_OB_REJECT_PATHS:
        cards.extend(_path_cards(
            resolved, structure_tf, trigger_tf, horizon,
            current_price=current_price, price_step=price_step,
            observed=observed, symbol=symbol))
    return cards


def is_ob_reject_watch(card):
    if not isinstance(card, dict):
        return False
    evidence = card.get('evidence') if isinstance(card.get('evidence'), dict) else {}
    return evidence.get('kind') == HTF_OB_REJECT_KIND


def threat_cards_against_position(cards, *, position_side):
    """Threats whose direction opposes an open book (wrong-side trap risk)."""
    side = getattr(position_side, 'value', position_side)
    if side not in ('long', 'short'):
        return []
    opposite = 'short' if side == 'long' else 'long'
    found = []
    for card in cards or []:
        if not is_ob_reject_watch(card):
            continue
        evidence = card.get('evidence') or {}
        threat = evidence.get('threat_side') or card.get('side')
        stage = evidence.get('stage')
        if threat != opposite:
            continue
        # Only armed proximity / wick / choch stages count as active threat.
        if stage not in ('prox_alert', 'htf_wick_ok', 'ltf_choch'):
            continue
        found.append(card)
    return found


def propose_defensive_management(*, position_side, entry, stop, target, threat_card,
                                 current_price=None, price_step=0):
    """Defensive plan when an open position faces an OB-reject threat.

    Bias: shorten TP and/or tighten stop. Never flip side. Never widen stop.
    Does not instruct a reverse entry.
    """
    side = getattr(position_side, 'value', position_side)
    if side not in ('long', 'short'):
        return dict(linked=False, reason='unknown_side', flip=False)
    threats = threat_cards_against_position([threat_card], position_side=side)
    if not threats:
        return dict(linked=False, reason='no_opposing_threat', flip=False, side=side)
    if not all(_positive(v) for v in (entry, stop, target)):
        return dict(linked=False, reason='invalid_levels', flip=False, side=side)
    long = side == 'long'
    if not (stop < entry < target if long else target < entry < stop):
        return dict(linked=False, reason='geometry_invalid', flip=False, side=side)
    risk = abs(entry - stop)
    remaining = abs(target - entry)
    if risk <= 0 or remaining <= 0:
        return dict(linked=False, reason='zero_risk_or_target', flip=False, side=side)
    keep_stop = DEFENSIVE_STOP_RISK_KEEP * risk
    keep_tp = DEFENSIVE_TP_KEEP * remaining
    if long:
        new_stop = entry - keep_stop
        new_target = entry + keep_tp
        # Stop must tighten (rise); target must shorten (fall) without crossing price.
        if not new_stop > stop:
            new_stop = stop
        if not new_target < target:
            new_target = target
        if current_price is not None and _positive(current_price):
            if not new_stop < current_price:
                new_stop = stop
            if not new_target > current_price:
                new_target = target
    else:
        new_stop = entry + keep_stop
        new_target = entry - keep_tp
        if not new_stop < stop:
            new_stop = stop
        if not new_target > target:
            new_target = target
        if current_price is not None and _positive(current_price):
            if not new_stop > current_price:
                new_stop = stop
            if not new_target < current_price:
                new_target = target
    new_stop = _tick(new_stop, price_step, up=not long)
    new_target = _tick(new_target, price_step, up=not long)
    tighten = (new_stop > stop) if long else (new_stop < stop)
    shorten = (new_target < target) if long else (new_target > target)
    evidence = (threat_card.get('evidence') or {}) if isinstance(threat_card, dict) else {}
    return dict(
        linked=True,
        flip=False,
        side=side,
        threat_side=evidence.get('threat_side') or threat_card.get('side'),
        threat_stage=evidence.get('stage'),
        threat_setup_id=threat_card.get('setup_id'),
        action='defensive_protect',
        tighten_stop=tighten,
        shorten_target=shorten,
        stop=new_stop if tighten else stop,
        target=new_target if shorten else target,
        previous_stop=stop,
        previous_target=target,
        reason=('Open position faces HTF OB-rejection toward weak extreme; '
                'bias defense (tighten stop / shorten TP). Do not flip.'),
    )


def management_link_for_open_position(*, position_side, entry, stop, target, cards,
                                      current_price=None, price_step=0):
    """Pick the strongest opposing threat and propose a defensive plan."""
    threats = threat_cards_against_position(cards, position_side=position_side)
    if not threats:
        return dict(linked=False, reason='no_opposing_threat', flip=False)
    rank = {name: i for i, name in enumerate(STAGES)}
    threats.sort(key=lambda c: (
        -rank.get((c.get('evidence') or {}).get('stage'), -1),
        (c.get('evidence') or {}).get('proximity_pct') or 99,
    ))
    return propose_defensive_management(
        position_side=position_side, entry=entry, stop=stop, target=target,
        threat_card=threats[0], current_price=current_price, price_step=price_step)
