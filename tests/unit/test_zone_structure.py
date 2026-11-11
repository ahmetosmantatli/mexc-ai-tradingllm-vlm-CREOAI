"""Zone CHoCH/MSS cards: swing structure only, watch by default. Early cards still trade."""
import pytest

from trading.analysis.market import Candle, ict_features
from trading.quant.structure_setups import build_htf_zone_setups
from trading.runtime import runner3_policy
from .test_structure_setups import NOW, htf_zone_context, view
from .test_zone_entry import _internal, weak_long_market, weak_short_market

# (high, low): a high at 115, a low at 97, a lower high at 109, a lower low at 92.
DOWNTREND = [(110, 105), (112, 106), (115, 108), (111, 104), (108, 100), (106, 97), (107, 99),
             (109, 101), (105, 98), (103, 94), (101, 92), (103, 95), (104, 96)]


def _bars(rows, close, *, short=False):
    def flip(price):
        return 200 - price if short else price

    bars = []
    for i, (high, low) in enumerate(rows + [(close + 1, 100)]):
        high, low = (flip(low), flip(high)) if short else (high, low)
        last = i == len(rows)
        mid = flip(close) if last else (high + low) / 2
        bars.append(Candle(300 * (i + 1), mid, high, low, mid, 1))
    return bars


def _shift(bars, *, short=False):
    evidence = dict(breaks=[dict(at=bars[9].timestamp, direction='bullish' if short else 'bearish'),
                            dict(at=bars[-1].timestamp, direction='bearish' if short else 'bullish')],
                    sweeps=[])
    shift, = ict_features(bars, evidence)['structure_shifts']
    return shift


@pytest.mark.parametrize('short', [False, True])
def test_a_choch_inside_the_lower_high_is_internal(short):
    shift = _shift(_bars(DOWNTREND, 105, short=short), short=short)
    assert shift['kind'] == 'choch'
    assert shift['structure'] == 'internal'
    assert shift['swing_level'] == (91 if short else 109)


@pytest.mark.parametrize('short', [False, True])
def test_a_close_through_the_lower_high_after_a_lower_low_is_a_swing_choch(short):
    shift = _shift(_bars(DOWNTREND, 110, short=short), short=short)
    assert shift['structure'] == 'swing'
    assert shift['swing_level'] == (91 if short else 109)


def test_without_a_lower_low_the_break_is_internal():
    rows = list(DOWNTREND)
    rows[10] = (101, 98)
    rows[9] = (103, 99)
    shift = _shift(_bars(rows, 110))
    assert shift['structure'] == 'internal'
    assert 'swing_level' not in shift


def test_the_label_uses_only_pivots_confirmed_before_the_break():
    bars = _bars(DOWNTREND, 105)
    alone = _shift(bars)
    later = bars + [Candle(300 * (len(bars) + i + 1), 100, 101, low, 100, 1)
                    for i, low in enumerate((90, 85, 95, 96, 97))]
    evidence = dict(breaks=[dict(at=bars[9].timestamp, direction='bearish'),
                            dict(at=bars[-1].timestamp, direction='bullish')], sweeps=[])
    shift = next(row for row in ict_features(later, evidence)['structure_shifts'] if row['at'] == bars[-1].timestamp)
    assert shift == alone


@pytest.mark.parametrize('context_tf,trigger_tf', [('4h', '15m'), ('1h', '5m')])
def test_a_confirmed_zone_card_is_watch_only_by_default(context_tf, trigger_tf):
    ctx = htf_zone_context(context_tf, trigger_tf)
    card = next(row for row in build_htf_zone_setups(ctx, current_price=101., price_step=.01)
                if row['context_timeframe'] == context_tf)
    assert card['state'] == 'watch'
    assert card['evidence']['stage'] == 'confirmed'
    assert card['evidence']['watch_only'] is True
    assert not runner3_policy.closed_entry_structure(card)
    with pytest.raises(ValueError, match='runner3_requires_verified_setup'):
        runner3_policy.validate_evidence(card, object(), now=NOW)
    ready = next(row for row in build_htf_zone_setups(ctx, current_price=101., price_step=.01, zone_ready=True)
                 if row['context_timeframe'] == context_tf)
    assert ready['state'] == 'ready'
    assert 'watch_only' not in ready['evidence']


def test_the_zone_path_ignores_an_internal_choch():
    ctx = htf_zone_context('4h', '15m')
    view(ctx, '15m')['features']['structure_shifts'][0]['structure'] = 'internal'
    card, = build_htf_zone_setups(ctx, current_price=101., price_step=.01, zone_ready=True)
    assert card['state'] == 'watch'
    assert card['evidence']['stage'] == 'awaiting_confirmation'


def test_the_early_card_still_trades_on_an_internal_5m_choch():
    market = weak_long_market()
    for shift in view(market, '5m')['features']['structure_shifts']:
        shift['structure'] = 'internal'
    card, = _internal(market, 96.)
    assert card['state'] == 'ready'
    assert card['evidence']['kind'] == 'htf_zone_internal'


def test_the_judging_rubric_is_short_and_uses_our_definitions():
    text = runner3_policy.entry_gate_instruction(fee=.0006, slippage=.0005).split('\n')
    rubric = text[text.index('Price-action playbook, before you answer:') + 1:
                  text.index('Free margin must cover the order. You choose the balance share.')]
    assert len(rubric) <= 12
    joined = ' '.join(rubric)
    assert 'needs no 4h or 1h CHoCH' in joined
    assert 'against 1.25, not 1.5' in joined
    assert 'may reject that read' in joined


def test_the_early_card_needs_price_within_one_hourly_atr_of_the_weak_level():
    from trading.quant.structure_setups import _atr, _views

    market = weak_long_market()
    atr = _atr(_views(market)['1h']['rows'])
    assert (100. - 90.) / atr > 1 > (96. - 90.) / atr
    assert _internal(market, 100.) == []
    card, = _internal(market, 96.)
    assert card['evidence']['weak_distance_atr'] < 1

    short = weak_short_market()
    atr = _atr(_views(short)['1h']['rows'])
    assert (110. - 99.) / atr > 1 > (110. - 103.) / atr
    assert [row for row in _internal(short, 99.) if row['side'] == 'short'] == []
    card, = [row for row in _internal(short, 103.) if row['side'] == 'short']
    assert card['evidence']['weak_distance_atr'] < 1


def _origin_view(direction='bullish'):
    """1h rows: a swing at 2, the extreme at 4, a displacement break at 6."""
    rows = [[3600 * (i + 1), 100., 101., 99., 100., 1.] for i in range(10)]
    rows[2][2] = 104.
    rows[3][1:5] = [100., 100.5, 97., 98.]
    rows[4][1:5] = [98., 98.5, 95., 97.5]
    rows[5][1:5] = [97., 98., 96.5, 97.]
    rows[6][1:5] = [97., 106., 97., 105.]
    if direction == 'bearish':
        rows = [[at, 200 - op, 200 - lo, 200 - hi, 200 - cl, v] for at, op, hi, lo, cl, v in rows]
    brk = dict(direction=direction, at=rows[6][0], level=104. if direction == 'bullish' else 96., swing_at=rows[2][0])
    block = dict(direction=direction, open_time=rows[5][0], confirmed_by=rows[6][0],
                 low=rows[5][3], high=rows[5][2], status='untested')
    return dict(rows=rows, features=dict(breaks=[brk], order_block_candidates=[block])), block


@pytest.mark.parametrize('direction', ['bullish', 'bearish'])
def test_the_block_is_the_extreme_candle_between_the_swing_and_the_break(direction):
    from trading.quant.structure_setups import _confirmed_zone, _origin_block

    higher, block = _origin_view(direction)
    origin = _origin_block(block, higher)
    extreme = higher['rows'][4]
    assert (origin['open_time'], origin['low'], origin['high']) == (extreme[0], extreme[3], extreme[2])
    assert origin['status'] == 'untested'
    zone = _confirmed_zone(origin, higher, '1h')
    assert (zone['low'], zone['high'], zone['open_time']) == (extreme[3], extreme[2], extreme[0])


def test_a_close_through_the_origin_invalidates_it_and_a_missing_swing_keeps_the_block():
    from trading.quant.structure_setups import _origin_block

    higher, block = _origin_view()
    higher['rows'][8][1:5] = [100., 100., 94., 94.5]
    assert _origin_block(block, higher)['status'] == 'invalidated'
    higher['features']['breaks'][0]['swing_at'] = 1
    assert _origin_block(block, higher) is None
