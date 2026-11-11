"""After LTF CHoCH: confirm_bar preferred, else OB retest; naked CHoCH stays WATCH."""
from copy import deepcopy

import pytest

from trading.evaluation.context import _decision_setup
from trading.quant.structure_setups import ZONE_CARDS_READY
from .test_zone_entry import _bullish_block, _internal, _ltf_confirm_after_choch, view, weak_long_market, weak_short_market


@pytest.fixture(autouse=True)
def _wide_entry_cap(monkeypatch):
    monkeypatch.setattr('trading.quant.structure_setups.WEAK_ENTRY_MAX_ATR', 2.)


def _naked_choch_market():
    """Same early gates, but the last LTF bar is the CHoCH itself (no continuation)."""
    market = weak_long_market()
    lower = view(market, '5m')
    latest = lower['recent_ohlcv'][-1]
    latest[1:5] = [99., 102., 98., 101.]
    lower['features']['structure_shifts'] = [
        dict(at=latest[0], direction='bullish', level=100., kind='choch')]
    lower['features']['order_block_candidates'] = []
    return market


def test_zone_cards_ready_stays_false():
    assert ZONE_CARDS_READY is False


def test_naked_choch_without_confirm_or_ob_is_watch_not_ready():
    market = _naked_choch_market()
    card, = _internal(market)
    assert card['state'] == 'watch'
    assert card['evidence']['stage'] == 'awaiting_entry'
    assert 'entry_path' not in card['evidence']
    assert 'awaiting confirm bar' in card['algorithm_read'].lower()


def test_confirm_bar_after_choch_is_ready_with_entry_path():
    market = weak_long_market()
    card, = _internal(market)
    assert card['state'] == 'ready'
    assert card['evidence']['entry_path'] == 'confirm_bar'
    assert card['evidence']['confirm_bar_open_time'] == view(market, '5m')['recent_ohlcv'][-1][0]
    compact = _decision_setup(card, 100.)
    assert compact['entry_path'] == 'confirm_bar'


def test_ob_retest_without_confirm_bar_is_ready_and_stops_beyond_ob_distal():
    market = _naked_choch_market()
    lower = view(market, '5m')
    # Demand OB formed before the CHoCH; last bar is the CHoCH break (no confirm body after).
    choch = lower['recent_ohlcv'][-1]
    demand = lower['recent_ohlcv'][-4]
    demand[1:5] = [97., 98., 95., 96.]  # bearish candle that becomes the bullish-impulse OB source in fixtures
    _bullish_block(lower, low=95., high=98.)
    # Force the OB open_time onto the pre-CHoCH demand bar and mark a later pullback touch
    # by reshaping the bar just before CHoCH into a retest that still leaves CHoCH last.
    lower['features']['order_block_candidates'] = [dict(
        direction='bullish', open_time=demand[0], low=95., high=98., status='untested')]
    # Insert a closed pullback into the OB after an earlier CHoCH: move CHoCH to -3, touch on -1.
    earlier = lower['recent_ohlcv'][-3]
    earlier[1:5] = [99., 102., 98., 101.]
    lower['features']['structure_shifts'] = [
        dict(at=earlier[0], direction='bullish', level=100., kind='choch')]
    mid = lower['recent_ohlcv'][-2]
    mid[1:5] = [100.5, 101., 99., 100.]  # close < open → no confirm_bar; still holds CHoCH level
    touch = lower['recent_ohlcv'][-1]
    # Wick into demand OB; close stays above CHoCH level so the shift is not undone; bearish body.
    touch[1:5] = [101., 101.5, 96.5, 100.2]
    card, = _internal(market, price=97.5)
    assert card['state'] == 'ready'
    assert card['evidence']['entry_path'] == 'ob_retest'
    assert card['evidence']['ltf_ob']['low'] == 95.
    assert card['stop_loss_price'] < 95.
    assert card['stop_reference']['field'] == 'low'
    compact = _decision_setup(card, 97.5)
    assert compact['entry_path'] == 'ob_retest'


def test_short_mirror_naked_choch_is_watch_and_confirm_is_ready():
    naked = weak_short_market()
    lower = view(naked, '5m')
    latest = lower['recent_ohlcv'][-1]
    latest[1:5] = [100., 101., 96., 97.]
    lower['features']['structure_shifts'] = [
        dict(at=latest[0], direction='bearish', level=99., kind='choch')]
    lower['features']['order_block_candidates'] = []
    watch, = [row for row in _internal(naked, 99.) if row['side'] == 'short']
    assert watch['state'] == 'watch'
    assert 'entry_path' not in watch['evidence']

    ready_m = weak_short_market()
    ready, = [row for row in _internal(ready_m, 99.) if row['side'] == 'short']
    assert ready['state'] == 'ready'
    assert ready['evidence']['entry_path'] == 'confirm_bar'


def test_confirm_bar_is_preferred_over_ob_when_both_exist():
    market = weak_long_market()
    lower = view(market, '5m')
    demand = lower['recent_ohlcv'][-5]
    _bullish_block(lower, low=94., high=97.)
    lower['features']['order_block_candidates'] = [dict(
        direction='bullish', open_time=demand[0], low=94., high=97., status='retested')]
    # Ensure a post-CHoCH bar also overlapped the OB so both paths are available.
    touch = deepcopy(lower['recent_ohlcv'][-1])
    touch[3] = 96.
    lower['recent_ohlcv'][-1][3] = min(lower['recent_ohlcv'][-1][3], 96.)
    card, = _internal(market)
    assert card['evidence']['entry_path'] == 'confirm_bar'
