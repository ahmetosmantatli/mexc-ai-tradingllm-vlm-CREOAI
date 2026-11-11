"""4h weak swing, 15m CHoCH. Same gates as the 1h/5m early card; zone cards stay off."""
from copy import deepcopy

import pytest

from trading.quant.structure_setups import ZONE_CARDS_READY, _atr, build_htf_zone_setups
from .test_structure_setups import htf_zone_context, view
from .test_zone_entry import _bearish_block, _bullish_block, _internal


@pytest.fixture(autouse=True)
def _wide_entry_cap(monkeypatch):
    """These fixtures price the entry ~1.2-1.4 structure ATRs from the weak level."""
    monkeypatch.setattr('trading.quant.structure_setups.WEAK_ENTRY_MAX_ATR', 2.)


def weak_long_4h_market(*, target_high=120., target_low=112., block=True):
    """4h weak low, bullish candle there, 15m CHoCH + confirm bar. No sweep and no higher CHoCH."""
    from .test_zone_entry import _ltf_confirm_after_choch

    ctx = htf_zone_context('4h', '15m')
    higher = view(ctx, '4h')
    swing = next(row for row in higher['features']['swings'] if row['kind'] == 'low')
    pivot = next(row for row in higher['recent_ohlcv'] if row[0] == swing['pivot_at'])
    pivot[1] = swing['price'] + 1
    pivot[4] = swing['price'] + 4
    pivot[2] = max(pivot[2], pivot[4])
    higher['features']['sweeps'] = []
    higher['features']['structure_shifts'] = []
    higher['features']['breaks'] = []
    if block:
        _bearish_block(higher, low=target_low, high=target_high)
    for name in ('5m',):
        frame = view(ctx, name)
        frame['features']['structure_shifts'] = []
        frame['features']['sweeps'] = []
    # The 15m reaction reaches near the 4h weak low at 90 (within half a 4h ATR).
    lower = view(ctx, '15m')
    low_swing = lower['features']['last_swing_low']
    origin = next(row for row in lower['recent_ohlcv'] if row[0] == low_swing['pivot_at'])
    origin[3] = 92.
    low_swing['price'] = 92.
    for row in lower['features']['swings']:
        if row.get('kind') == 'low':
            row['price'] = 92.
    _ltf_confirm_after_choch(lower, long=True, level=100.)
    return ctx


def weak_short_4h_market(*, reaction=108.):
    """4h weak high at 110, bearish candle there, 15m bearish CHoCH + confirm bar after a 15m high at ``reaction``."""
    from .test_zone_entry import _ltf_confirm_after_choch

    market = weak_long_4h_market()
    higher = view(market, '4h')
    swing = next(row for row in higher['features']['swings'] if row['kind'] == 'high')
    pivot = next(row for row in higher['recent_ohlcv'] if row[0] == swing['pivot_at'])
    pivot[1] = swing['price'] - 1
    pivot[4] = swing['price'] - 4
    pivot[2] = swing['price']
    pivot[3] = min(pivot[3], pivot[4])
    lower = view(market, '15m')
    _ltf_confirm_after_choch(lower, long=False, level=99.)
    high_swing = lower['features']['last_swing_high']
    origin = next(row for row in lower['recent_ohlcv'] if row[0] == high_swing['pivot_at'])
    origin[2] = reaction
    high_swing['price'] = reaction
    for row in lower['features']['swings']:
        if row.get('kind') == 'high':
            row['price'] = reaction
    _bullish_block(higher, low=80., high=86.)
    return market


def _shorts(market, price=99.):
    return [row for row in _internal(market, price) if row['side'] == 'short']


def _set_15m_low(market, price):
    lower = view(market, '15m')
    swing = lower['features']['last_swing_low']
    origin = next(row for row in lower['recent_ohlcv'] if row[0] == swing['pivot_at'])
    origin[3] = price
    for row in lower['features']['swings']:
        if row.get('kind') == 'low':
            row['price'] = price
    return origin


def _add_4h_swing(market, index, kind, price):
    higher = view(market, '4h')
    row = higher['recent_ohlcv'][index]
    row[3 if kind == 'low' else 2] = price
    swing = dict(kind=kind, price=price, pivot_at=row[0], confirmed_at=higher['recent_ohlcv'][index + 2][0] + 14400)
    higher['features']['swings'].insert(0, swing)
    return swing


def _bearish_gap(market, index, *, lower, upper):
    higher = view(market, '4h')
    row = higher['recent_ohlcv'][index]
    row[2] = upper
    higher['features']['fvgs'] = [dict(direction='bearish', at=row[0], lower=lower, upper=upper)]


def test_zone_cards_ready_stays_false():
    assert ZONE_CARDS_READY is False


def test_weak_low_uses_the_15m_trigger_and_does_not_wait_for_a_sweep():
    market = weak_long_4h_market()
    card, = _internal(market)
    assert card['context_timeframe'] == '4h'
    assert card['trigger_timeframe'] == '15m'
    assert card['horizon'] == 'swing'
    assert card['state'] == 'ready'
    assert card['side'] == 'long'
    assert card['evidence']['weak_field'] == 'low'
    assert card['evidence']['entry_path'] == 'confirm_bar'
    assert card['evidence']['shift']['kind'] in ('choch', 'mss')
    assert view(market, '4h')['features']['structure_shifts'] == []
    assert view(market, '4h')['features']['sweeps'] == []
    swing = next(row for row in card['swing_lows'] if row['timeframe'] == '15m')
    assert card['stop_reference']['timeframe'] == '15m'
    assert card['stop_reference']['field'] == 'low'
    assert card['stop_loss_price'] < swing['price']
    assert card['target_reference']['timeframe'] == '4h'
    assert card['take_profit_price'] == pytest.approx(120.)
    assert card['evidence']['candidate_block']['timeframe'] == '4h'
    assert card['evidence']['trigger_candle']['timeframe'] == '15m'
    read = card['algorithm_read'].lower()
    assert 'not swept' in read
    assert '4h low' in read
    assert '15m reached' in read
    assert 'entry_path confirm_bar' in read
    assert 'valid' not in read


def test_weak_high_is_the_short_with_the_15m_trigger():
    market = weak_short_4h_market()
    card, = _shorts(market)
    assert card['trigger_timeframe'] == '15m'
    assert card['context_timeframe'] == '4h'
    assert card['evidence']['weak_field'] == 'high'
    assert card['stop_reference']['field'] == 'high'
    assert card['stop_loss_price'] > 108.
    assert card['target_reference']['timeframe'] == '4h'
    assert 'confirmed' not in card['algorithm_read'].lower()


def test_a_sweep_is_not_required_and_taken_liquidity_is_not_a_weak_low():
    untouched = weak_long_4h_market()
    assert _internal(untouched)
    taken = weak_long_4h_market()
    higher = view(taken, '4h')
    swing = next(row for row in higher['features']['swings'] if row['kind'] == 'low')
    higher['features']['sweeps'] = [dict(direction='low_sweep', at=higher['recent_ohlcv'][-2][0], level=swing['price'])]
    assert _internal(taken) == []


def test_the_15m_reaction_must_reach_near_the_weak_level():
    near = weak_long_4h_market()
    card, = _internal(near)
    hourly = view(near, '4h')['recent_ohlcv']
    assert card['evidence']['reaction_extreme'] == 92.
    assert card['evidence']['proximity_band_atr'] == .5
    assert card['evidence']['weak_distance_atr'] == pytest.approx(10. / _atr(hourly), abs=1e-3)
    assert '15m reached 92' in card['algorithm_read']
    far = weak_long_4h_market()
    _set_15m_low(far, 94.)
    assert _internal(far) == []
    assert _shorts(weak_short_4h_market())
    assert _shorts(weak_short_4h_market(reaction=103.)) == []


def test_the_nearest_weak_level_is_tried_first():
    market = weak_long_4h_market()
    _add_4h_swing(market, 1, 'low', 89.5)
    card, = _internal(market)
    assert card['evidence']['weak_level'] == 90.
    assert card['invalidation_price'] == 90.
    short = weak_short_4h_market()
    _add_4h_swing(short, 0, 'high', 110.5)
    card, = _shorts(short)
    assert card['evidence']['weak_level'] == 110.


def test_the_stop_is_beyond_the_15m_extreme_before_the_choch():
    market = weak_long_4h_market()
    lower = view(market, '15m')
    origin = next(row for row in lower['recent_ohlcv'] if row[3] == 92.)
    minor = lower['recent_ohlcv'][-3]
    lower['features']['swings'].append(dict(kind='low', price=minor[3], pivot_at=minor[0], confirmed_at=lower['closed_at']))
    card, = _internal(market)
    assert card['stop_reference'] == dict(timeframe='15m', open_time=origin[0], field='low')
    assert card['stop_loss_price'] < 92.
    short = weak_short_4h_market()
    top = next(row for row in view(short, '15m')['recent_ohlcv'] if row[2] == 108.)
    card, = _shorts(short)
    assert card['stop_reference'] == dict(timeframe='15m', open_time=top[0], field='high')
    assert card['stop_loss_price'] > 108.


def test_without_a_4h_block_the_target_is_an_unfilled_flow_then_an_untaken_swing():
    market = weak_long_4h_market(block=False)
    _bearish_gap(market, 6, lower=106., upper=108.)
    card, = _internal(market)
    assert card['take_profit_price'] == pytest.approx(108.)
    assert card['opposing_target_levels'][0]['kind'] == 'flow'
    assert card['evidence']['candidate_block']['kind'] == 'flow'
    assert 'next 4h flow 108' in card['algorithm_read']
    filled = weak_long_4h_market(block=False)
    _bearish_gap(filled, 6, lower=106., upper=108.)
    view(filled, '4h')['recent_ohlcv'][9][2] = 108.5
    card, = _internal(filled)
    assert card['take_profit_price'] == pytest.approx(110.)
    assert card['opposing_target_levels'] == [dict(timeframe='4h', open_time=card['target_reference']['open_time'],
                                                   field='high', price=110., kind='swing')]
    bare, = _internal(weak_long_4h_market(block=False))
    assert bare['opposing_target_levels'][0]['kind'] == 'swing'


def test_entry_beyond_one_structure_atr_is_rejected(monkeypatch):
    monkeypatch.setattr('trading.quant.structure_setups.WEAK_ENTRY_MAX_ATR', 1.)
    market = weak_long_4h_market()
    atr = _atr(view(market, '4h')['recent_ohlcv'])
    # Price is 100; weak low is 90 → distance 10. Cap at 1 ATR rejects when ATR < 10.
    if atr < 10.:
        assert _internal(market) == []
    monkeypatch.setattr('trading.quant.structure_setups.WEAK_ENTRY_MAX_ATR', 2.)
    assert _internal(market)


def test_compact_card_shows_distance_and_target_kind():
    from trading.evaluation.context import _decision_setup
    card, = _internal(weak_long_4h_market())
    compact = _decision_setup(card, 100.)
    assert compact['weak_distance_atr'] == card['evidence']['weak_distance_atr']
    assert compact['candidate_block']['kind'] == 'block'
    assert compact['candidate_block']['timeframe'] == '4h'


def test_zone_cards_from_the_same_market_stay_watch():
    market = weak_long_4h_market()
    rows = build_htf_zone_setups(market, current_price=100., price_step=.01)
    early = [row for row in rows if row['evidence'].get('kind') == 'htf_zone_internal']
    zones = [row for row in rows if row['evidence'].get('kind') in ('htf_zone_choch', 'htf_zone_mss', 'htf_zone_watch')]
    assert early and early[0]['state'] == 'ready'
    assert all(row['state'] == 'watch' for row in zones)
    assert ZONE_CARDS_READY is False


def test_4h_path_does_not_use_a_5m_trigger():
    market = weak_long_4h_market()
    # Even with a bullish 5m CHoCH present, the 4h early card stays on 15m.
    five = view(market, '5m')
    latest = five['recent_ohlcv'][-1]
    latest[1:5] = [99., 102., 98., 101.]
    five['features']['structure_shifts'] = [dict(at=latest[0], direction='bullish', level=100., kind='choch')]
    card, = _internal(market)
    assert card['trigger_timeframe'] == '15m'
    assert card['evidence']['trigger_candle']['timeframe'] == '15m'
