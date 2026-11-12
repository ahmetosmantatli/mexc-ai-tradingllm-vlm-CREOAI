"""Parallel HTF OB-reject → weak-extreme watch + defensive management link."""
from copy import deepcopy

import pytest

from trading.quant.htf_ob_reject_watch import (
    HTF_OB_REJECT_KIND,
    PROXIMITY_PCT,
    build_htf_ob_reject_watch,
    management_link_for_open_position,
    propose_defensive_management,
    threat_cards_against_position,
)
from trading.quant.structure_setups import build_htf_zone_setups
from .test_structure_setups import htf_zone_context, view


def _would_auto_enter(card):
    """Mirror evaluation.service._confirm_path_ready_card early gate (no heavy imports)."""
    if not isinstance(card, dict) or card.get('state') != 'ready':
        return False
    evidence = card.get('evidence') if isinstance(card.get('evidence'), dict) else {}
    if evidence.get('kind') != 'htf_zone_internal':
        return False
    path = evidence.get('entry_path') or card.get('entry_path')
    return path in ('confirm_bar', 'ob_retest')


def _reject(market, price, step=.01):
    return [row for row in build_htf_zone_setups(market, current_price=price, price_step=step)
            if row['evidence'].get('kind') == HTF_OB_REJECT_KIND]


def _reject_market(*, structure_tf='1h', trigger_tf='5m', weak_low=90.,
                   ob_low=101., ob_high=104., price=100., wick=False, close_through=False,
                   ltf_choch=False):
    """Weak low + first untested supply OB above. Optional wick / close-through / LTF CHoCH."""
    ctx = htf_zone_context(structure_tf, trigger_tf)
    higher = view(ctx, structure_tf)
    # Clear zone OB so only our supply block remains.
    higher['features']['order_block_candidates'] = []
    higher['features']['fvgs'] = []
    higher['features']['sweeps'] = []
    higher['features']['structure_shifts'] = []
    higher['features']['breaks'] = []
    # Keep the weak low swing; ensure liquidity not taken.
    swing = next(row for row in higher['features']['swings'] if row['kind'] == 'low')
    swing['price'] = weak_low
    pivot = next(row for row in higher['recent_ohlcv'] if row[0] == swing['pivot_at'])
    pivot[3] = weak_low
    pivot[1] = weak_low + 1
    pivot[4] = weak_low + 2
    pivot[2] = max(pivot[2], pivot[4])
    # Place an untested bearish supply OB above the weak low (and near price when desired).
    bar = higher['recent_ohlcv'][1]
    bar[2] = max(bar[2], ob_high)
    bar[3] = min(bar[3], ob_low)
    higher['features']['order_block_candidates'] = [dict(
        direction='bearish', open_time=bar[0], low=ob_low, high=ob_high, status='untested')]
    # Keep later HTF bars below the supply proximal unless the test wants a wick.
    for row in higher['recent_ohlcv']:
        if row[0] <= bar[0]:
            continue
        if not (wick or close_through):
            row[2] = min(row[2], ob_low - .05)
            row[4] = min(row[4], ob_low - .1)
            row[1] = min(row[1], row[4])
            row[3] = min(row[3], row[4])
    if wick or close_through:
        latest = higher['recent_ohlcv'][-1]
        # Wick into the supply box from below.
        latest[2] = max(latest[2], ob_low + .5)
        latest[3] = min(latest[3], price - 1)
        if close_through:
            latest[4] = ob_high + 1  # close through distal → invalid
            latest[2] = max(latest[2], latest[4])
        else:
            latest[4] = min(ob_low - .2, price)  # reject: close still outside distal
            latest[1] = latest[4] + .1
            latest[3] = min(latest[3], latest[4])
    lower = view(ctx, trigger_tf)
    if ltf_choch:
        choch = lower['recent_ohlcv'][-1]
        choch[1:5] = [100., 101., 96., 97.]
        lower['features']['structure_shifts'] = [
            dict(at=choch[0], direction='bearish', level=99., kind='choch')]
    else:
        lower['features']['structure_shifts'] = []
    for name in ('15m', '4h', '5m', '1h'):
        try:
            frame = view(ctx, name)
        except StopIteration:
            continue
        if name == trigger_tf and ltf_choch:
            continue
        if name != structure_tf:
            frame['features'].setdefault('structure_shifts', [])
    ctx['_price'] = price
    return ctx


def test_proximity_watch_arms_within_two_percent_without_auto_entry():
    # OB proximal (low) at 101; price 100 → ~1% away → prox_alert.
    market = _reject_market(ob_low=101., ob_high=104., price=100.)
    cards = _reject(market, 100.)
    assert cards, 'expected proximity reject watch'
    card = cards[0]
    assert card['state'] == 'watch'
    assert card['auto_entry'] is False
    assert card['evidence']['kind'] == HTF_OB_REJECT_KIND
    assert card['evidence']['stage'] == 'prox_alert'
    assert card['evidence']['proximity_armed'] is True
    assert card['evidence']['proximity_pct'] <= PROXIMITY_PCT
    assert card['evidence']['weak_field'] == 'low'
    assert card['side'] == 'short'
    assert card['evidence'].get('entry_path') not in ('confirm_bar', 'ob_retest')
    assert not any(_would_auto_enter(row) for row in cards)
    assert not any(_would_auto_enter(row) for row in
                   build_htf_zone_setups(market, current_price=100., price_step=.01)
                   if row.get('side') == 'short' and row['evidence'].get('kind') == HTF_OB_REJECT_KIND)


def test_proximity_not_armed_when_far_from_ob():
    # OB at 120-124, price 100 → far; still ob_armed watch.
    market = _reject_market(ob_low=120., ob_high=124., price=100.)
    cards = _reject(market, 100.)
    assert cards
    assert cards[0]['evidence']['stage'] == 'ob_armed'
    assert cards[0]['evidence']['proximity_armed'] is False
    assert cards[0]['state'] == 'watch'


def test_close_through_ob_distal_invalidates_watch():
    market = _reject_market(ob_low=101., ob_high=104., price=100., wick=True, close_through=True)
    assert _reject(market, 100.) == []
    # Direct builder agrees.
    assert build_htf_ob_reject_watch(market, current_price=100., price_step=.01) == []


def test_wick_reject_without_close_through_advances_stage():
    market = _reject_market(ob_low=101., ob_high=104., price=100., wick=True, close_through=False)
    card, = _reject(market, 100.)
    assert card['evidence']['htf_wick_ok'] is True
    assert card['evidence']['stage'] in ('htf_wick_ok', 'ltf_choch')
    assert card['state'] == 'watch'


def test_ltf_choch_toward_weak_extreme_is_watch_only():
    market = _reject_market(ob_low=101., ob_high=104., price=100., wick=True, ltf_choch=True)
    card, = _reject(market, 100.)
    assert card['evidence']['stage'] == 'ltf_choch'
    assert card['evidence']['shift']['direction'] == 'bearish'
    assert card['state'] == 'watch'
    assert card.get('entry_path') is None
    assert not _would_auto_enter(card)


def test_both_tf_pairs_emit_when_structure_present():
    # 1h path from fixture; also seed 4h weak+OB for swing path.
    market = _reject_market(structure_tf='1h', trigger_tf='5m', ob_low=101., ob_high=104., price=100.)
    higher4 = view(market, '4h')
    swing = next(row for row in higher4['features']['swings'] if row['kind'] == 'low')
    swing['price'] = 90.
    pivot = next(row for row in higher4['recent_ohlcv'] if row[0] == swing['pivot_at'])
    pivot[3] = 90.
    higher4['features']['sweeps'] = []
    bar = higher4['recent_ohlcv'][1]
    higher4['features']['order_block_candidates'] = [dict(
        direction='bearish', open_time=bar[0], low=101., high=104., status='untested')]
    bar[2] = max(bar[2], 104.)
    bar[3] = min(bar[3], 101.)
    fifteen = view(market, '15m')
    fifteen['features']['structure_shifts'] = []
    cards = _reject(market, 100.)
    frames = {(c['context_timeframe'], c['trigger_timeframe']) for c in cards}
    assert ('1h', '5m') in frames
    assert ('4h', '15m') in frames
    assert all(c['state'] == 'watch' for c in cards)


def test_management_link_when_open_long_faces_short_threat():
    market = _reject_market(ob_low=101., ob_high=104., price=100., wick=True, ltf_choch=True)
    threat, = _reject(market, 100.)
    assert threat['side'] == 'short'
    opposing = threat_cards_against_position([threat], position_side='long')
    assert opposing and opposing[0]['setup_id'] == threat['setup_id']
    # Same-side position is not threatened.
    assert threat_cards_against_position([threat], position_side='short') == []
    plan = management_link_for_open_position(
        position_side='long', entry=100., stop=95., target=110.,
        cards=[threat], current_price=100.5, price_step=.01)
    assert plan['linked'] is True
    assert plan['flip'] is False
    assert plan['side'] == 'long'
    assert plan['threat_side'] == 'short'
    assert plan['tighten_stop'] is True
    assert plan['shorten_target'] is True
    assert plan['stop'] > 95.
    assert plan['target'] < 110.
    assert plan['stop'] < 100.5 < plan['target']
    # Never proposes a reverse entry.
    assert 'flip' in plan and plan['flip'] is False
    assert plan.get('action') == 'defensive_protect'


def test_management_link_does_not_fire_on_ob_armed_only():
    market = _reject_market(ob_low=120., ob_high=124., price=100.)
    threat, = _reject(market, 100.)
    assert threat['evidence']['stage'] == 'ob_armed'
    plan = management_link_for_open_position(
        position_side='long', entry=100., stop=95., target=110., cards=[threat])
    assert plan['linked'] is False


def test_propose_defensive_management_never_widens_or_flips():
    threat = dict(
        setup_id='t1', side='short',
        evidence=dict(kind=HTF_OB_REJECT_KIND, stage='prox_alert', threat_side='short'))
    plan = propose_defensive_management(
        position_side='long', entry=100., stop=96., target=108.,
        threat_card=threat, current_price=101., price_step=.01)
    assert plan['linked'] and not plan['flip']
    assert plan['stop'] >= 96.
    assert plan['target'] <= 108.
    # Wrong geometry / same-side threat → no link.
    assert propose_defensive_management(
        position_side='short', entry=100., stop=104., target=90.,
        threat_card=threat)['linked'] is False


def test_zone_ready_flag_untouched_by_reject_family(monkeypatch):
    from trading.quant import structure_setups as setups

    monkeypatch.setattr(setups, 'ZONE_CARDS_READY', False)
    market = _reject_market(ob_low=101., ob_high=104., price=100.)
    rows = build_htf_zone_setups(market, current_price=100., price_step=.01)
    reject = [r for r in rows if r['evidence'].get('kind') == HTF_OB_REJECT_KIND]
    zone = [r for r in rows if r['evidence'].get('kind', '').startswith('htf_zone_')
            and r['evidence'].get('kind') != 'htf_zone_internal']
    assert reject
    assert all(r['state'] == 'watch' for r in reject)
    # Zone family still respects ZONE_CARDS_READY=False when present.
    assert all(r['state'] != 'ready' or r['evidence'].get('kind') == 'htf_zone_internal'
               for r in rows)
    assert setups.ZONE_CARDS_READY is False
    # Silence unused when no zone cards in this fixture.
    assert zone == [] or all(z['state'] == 'watch' for z in zone)
