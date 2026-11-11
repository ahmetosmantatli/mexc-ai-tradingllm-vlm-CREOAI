"""Equal high/low swings, liquidity pools, and pool-level sweeps."""
from copy import deepcopy

import pytest

from trading.analysis.market import Candle, structure_features
from trading.quant.structure_setups import _liquidity_taken


def _bars(rows):
    return [Candle(index * 300, *row, 1) for index, row in enumerate(rows)]


def _eqh_series(*, sweep_close=14.):
    """Two equal highs at 15, then a wick through the pool that closes back."""
    return _bars([
        (10.5, 11, 10, 10.5),
        (10.5, 12, 10, 10.5),
        (10.5, 15, 10, 10.5),
        (10.5, 12, 10, 10.5),
        (10.5, 11, 10, 10.5),
        (10.5, 12, 10, 10.5),
        (10.5, 15, 10, 10.5),
        (10.5, 12, 10, 10.5),
        (10.5, 11, 10, 10.5),
        (14, 16, 10, sweep_close),
    ])


def _eql_series():
    """Double bottom near 5 / 5.02, then a pool sweep of the outer wick."""
    return _bars([
        (10.5, 11, 9.5, 10.5),
        (11, 12, 10, 11),
        (11, 12, 5, 5.5),
        (5.5, 8, 5.2, 7),
        (7, 9, 6.5, 8),
        (8, 10, 7, 9),
        (9, 10, 5.02, 5.5),
        (5.5, 8, 5.1, 7),
        (7, 9, 6.5, 8),
        (8, 9, 4.8, 7),
    ])


def test_equal_high_is_a_confirmed_swing():
    bars = _eqh_series()
    before = structure_features(bars[:4])
    assert before['last_swing_high'] is None
    confirmed = structure_features(bars[:5])['last_swing_high']
    assert confirmed == dict(kind='high', price=15, pivot_at=600, confirmed_at=1500)
    highs = [row for row in structure_features(bars)['swings'] if row['kind'] == 'high']
    assert [row['pivot_at'] for row in highs] == [600, 1800]
    assert all(row['price'] == 15 for row in highs)


def test_plateau_keeps_only_the_leftmost_equal_high():
    bars = _bars([
        (10, 11, 9, 10),
        (10, 12, 9, 10),
        (10, 15, 9, 10),
        (10, 15, 9, 10),
        (10, 14, 9, 10),
        (10, 13, 9, 10),
        (10, 12, 9, 10),
    ])
    highs = [row for row in structure_features(bars)['swings'] if row['kind'] == 'high']
    assert highs == [dict(kind='high', price=15, pivot_at=600, confirmed_at=1500)]


@pytest.mark.parametrize('builder,kind,level', [(_eqh_series, 'eqh', 15), (_eql_series, 'eql', 5)])
def test_equal_swings_form_a_liquidity_pool_at_the_outer_wick(builder, kind, level):
    features = structure_features(builder())
    pool, = [row for row in features['liquidity_pools'] if row['kind'] == kind]
    assert pool['level'] == level
    assert len(pool['members']) == 2
    assert pool['confirmed_at'] is not None
    assert pool['swept_at'] is not None


def test_pool_sweep_takes_every_member_not_only_the_latest_swing():
    features = structure_features(_eql_series())
    pool, = features['liquidity_pools']
    assert pool['kind'] == 'eql' and pool['swept_at'] == 2700
    event = next(row for row in features['sweeps'] if row.get('pool'))
    assert event['direction'] == 'low_sweep'
    assert event['level'] == 5
    assert {row['price'] for row in event['members']} == {5, 5.02}


def test_inner_last_break_still_records_outer_pool_sweep():
    """Close above the inner last high but back under the outer pool wick."""
    bars = _bars([
        (10.5, 11, 10, 10.5),
        (10.5, 12, 10, 10.5),
        (10.5, 15.0, 10, 10.5),
        (10.5, 12, 10, 10.5),
        (10.5, 11, 10, 10.5),
        (10.5, 12, 10, 10.5),
        (10.5, 14.95, 10, 10.5),
        (10.5, 12, 10, 10.5),
        (10.5, 11, 10, 10.5),
        (14.97, 15.1, 10, 14.97),
    ])
    features = structure_features(bars)
    assert features['breaks'][-1]['level'] == 14.95
    pool, = features['liquidity_pools']
    assert pool['level'] == 15.0 and pool['swept_at'] == 2700
    assert any(row.get('pool') and row['level'] == 15.0 for row in features['sweeps'])


def test_liquidity_taken_reads_pool_sweep_for_the_older_member():
    features = structure_features(_eql_series())
    older = next(row for row in features['swings'] if row['kind'] == 'low' and row['price'] == 5)
    newer = next(row for row in features['swings'] if row['kind'] == 'low' and row['price'] == 5.02)
    view = dict(rows=[[c.timestamp, c.open, c.high, c.low, c.close, c.volume] for c in _eql_series()],
                features=features, closed_at=3000)
    assert _liquidity_taken(view, dict(price=older['price'], open_time=older['pivot_at']), 'low')
    assert _liquidity_taken(view, dict(price=newer['price'], open_time=newer['pivot_at']), 'low')
    untouched = deepcopy(view)
    untouched['features'] = structure_features(_eql_series()[:-1])
    assert not _liquidity_taken(untouched, dict(price=older['price'], open_time=older['pivot_at']), 'low')


def _extend(bars, rows):
    start = bars[-1].timestamp + 300
    return list(bars) + [Candle(start + index * 300, *row, 1) for index, row in enumerate(rows)]


def test_structure_features_are_prefix_invariant():
    bars = _extend(_eql_series(), [
        (7, 9, 6.5, 8),
        (8, 10, 7, 9),
        (9, 11, 8, 10),
        (10, 12, 9, 11),
    ])
    full = structure_features(bars)
    for end in range(5, len(bars) + 1):
        truncated = structure_features(bars[:end])
        cutoff = bars[end - 1].timestamp
        assert truncated == structure_features(bars[:end])
        for event in truncated['sweeps'] + truncated['breaks']:
            assert event['at'] <= cutoff
        for pool in truncated['liquidity_pools']:
            assert pool['confirmed_at'] <= cutoff + 300
            if pool.get('swept_at') is not None:
                assert pool['swept_at'] <= cutoff
        known = {row['pivot_at'] for row in truncated['swings']}
        assert [row for row in full['swings'] if row['pivot_at'] in known] == truncated['swings']


def test_future_bars_do_not_change_prior_pool_state():
    bars = _eql_series()
    at_sweep = structure_features(bars)
    later = structure_features(_extend(bars, [(7, 8, 6, 7), (7, 9, 6.5, 8), (8, 10, 7, 9)]))
    assert at_sweep['liquidity_pools'][0]['members'] == later['liquidity_pools'][0]['members']
    assert at_sweep['liquidity_pools'][0]['swept_at'] == later['liquidity_pools'][0]['swept_at']
    assert [row for row in at_sweep['sweeps'] if row.get('pool')] == [row for row in later['sweeps'] if row.get('pool')]
