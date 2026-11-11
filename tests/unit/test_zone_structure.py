"""Zone CHoCH/MSS cards: swing structure only, watch by default. Early cards still trade."""
import pytest

from trading.analysis.market import Candle, ict_features
from trading.quant.structure_setups import build_htf_zone_setups
from trading.runtime import runner3_policy
from .test_structure_setups import NOW, htf_zone_context, view
from .test_zone_entry import _internal, weak_long_market

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
    card, = _internal(market)
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
