from copy import deepcopy
import pytest

from trading.quant.structure_setups import build_structure_setups


NOW = 1440000


def context():
    views = []
    for tf, seconds in [('4h',14400),('15m',900),('5m',300)]:
        rows = [[NOW-seconds*(12-i),98.,99.,96.,98.,1.] for i in range(12)]
        high = 100. if tf=='5m' else 110.
        low = 94. if tf=='5m' else 90.
        rows[2][2] = high;rows[5][3] = low
        rows[-1][1:5] = [100.,102.,98.,101.]
        swings = [dict(kind='high',price=high,pivot_at=rows[2][0],confirmed_at=rows[4][0]+seconds),
                  dict(kind='low',price=low,pivot_at=rows[5][0],confirmed_at=rows[7][0]+seconds)]
        features = dict(trend='bullish',swings=swings,last_swing_high=swings[0],last_swing_low=swings[1],
                        breaks=[],sweeps=[],fvgs=[])
        views.append(dict(timeframe=tf,recent_ohlcv=rows,closed_at=NOW,features=features))
    return dict(symbol='TEST_USDT',observed_at=NOW,timeframes=views)


def view(ctx,tf):return next(v for v in ctx['timeframes'] if v['timeframe']==tf)


def mirror(ctx):
    ctx=deepcopy(ctx)
    def price(x):return 200-x
    for v in ctx['timeframes']:
        for row in v['recent_ohlcv']:
            row[1:5]=[price(row[1]),price(row[3]),price(row[2]),price(row[4])]
        f=v['features'];f['trend']='bearish' if f['trend']=='bullish' else 'bullish'
        for s in f['swings']:
            s['kind']='low' if s['kind']=='high' else 'high';s['price']=price(s['price'])
        f['last_swing_high'],f['last_swing_low']=f['last_swing_low'],f['last_swing_high']
        for b in f['breaks']:
            b['direction']='bearish' if b['direction']=='bullish' else 'bullish';b['level']=price(b['level'])
        for s in f['sweeps']:
            s['direction']='high_sweep' if s['direction']=='low_sweep' else 'low_sweep';s['level']=price(s['level'])
    return ctx


@pytest.mark.parametrize('short',[False,True])
def test_fresh_continuation_is_bidirectional_and_grounded(short):
    ctx=context();micro=view(ctx,'5m')
    micro['features']['breaks']=[dict(direction='bullish',at=micro['recent_ohlcv'][-1][0],level=100.)]
    if short:ctx=mirror(ctx)
    setups=build_structure_setups(ctx,current_price=99. if short else 101.,price_step=.01)
    plan=next(p for p in setups if p['family']=='trend_continuation')
    assert plan['side']==('short' if short else 'long') and plan['state']=='ready'
    assert plan['requires_full_context'] is False
    assert plan['stop_reference']['timeframe']=='5m'
    assert plan['target_reference']['timeframe'] in ('4h','15m')
    # Stable identity excludes transient current tick when the same closed setup is unchanged.
    assert plan['setup_id']==next(p for p in build_structure_setups(ctx,current_price=99. if short else 101.,price_step=.01)
                                  if p['family']=='trend_continuation')['setup_id']


@pytest.mark.parametrize('short',[False,True])
def test_pullback_can_oppose_micro_trend_without_universal_alignment(short):
    ctx=context();micro=view(ctx,'5m');fifteen=view(ctx,'15m')
    micro['features']['trend']='bearish'
    fifteen['features']['breaks']=[dict(direction='bullish',at=fifteen['recent_ohlcv'][-3][0],level=100.)]
    if short:ctx=mirror(ctx)
    setups=build_structure_setups(ctx,current_price=99. if short else 101.)
    plan=next(p for p in setups if p['family']=='pullback')
    assert plan['side']==('short' if short else 'long') and plan['state']=='ready'
    assert plan['evidence']['kind']=='prior_15m_bos_retest_reclaim'
    assert plan['stop_reference']['open_time']==micro['recent_ohlcv'][-1][0]


@pytest.mark.parametrize('long',[False,True])
def test_countertrend_requires_actual_closed_sweep_reclaim(long):
    ctx=context();micro=view(ctx,'5m');last=micro['recent_ohlcv'][-1]
    last[1:5]=[99.,102.,97.,99.]
    micro['features']['sweeps']=[dict(direction='high_sweep',at=last[0],level=100.)]
    if long:ctx=mirror(ctx)
    plans=build_structure_setups(ctx,current_price=101. if long else 99.)
    plan=next(p for p in plans if p['family']=='countertrend_sweep_reclaim')
    assert plan['side']==('long' if long else 'short')
    assert plan['management_posture']=='reduced_risk_countertrend'
    # Wick without reclaim must not count as a countertrend setup.
    changed=deepcopy(ctx);bar=view(changed,'5m')['recent_ohlcv'][-1]
    bar[4]=97. if long else 103.;bar[2]=max(bar[2],bar[4]);bar[3]=min(bar[3],bar[4])
    assert not any(p['family']=='countertrend_sweep_reclaim' for p in build_structure_setups(changed,current_price=bar[4]))


def test_partial_15m_seed_is_explicitly_not_full_macro_context():
    ctx=context();ctx['timeframes']=[view(ctx,'15m')];v=ctx['timeframes'][0]
    # Last confirmed high is above current price; watch seed has a future trigger,
    # with a larger, older confirmed high retained as the structural target.
    v['recent_ohlcv'][8][2]=105.
    v['features']['swings'].append(dict(kind='high',price=105.,pivot_at=v['recent_ohlcv'][8][0],confirmed_at=NOW-900))
    plans=build_structure_setups(ctx,current_price=101.)
    p=next(p for p in plans if p['family']=='trend_continuation')
    assert p['state']=='watch' and p['trigger']==105.
    assert p['macro_trend']=='unknown' and p['requires_full_context'] is True
    assert p['trigger_timeframe']=='15m'
    assert not any(p['family']=='countertrend_sweep_reclaim' for p in plans)


def test_future_or_open_bars_are_not_pattern_evidence():
    ctx=context();v=view(ctx,'5m');v['recent_ohlcv'][-1][0]=NOW
    ctx['timeframes']=[v]
    assert build_structure_setups(ctx,current_price=101.)==[]


def test_future_confirmed_swing_cannot_supply_a_stop():
    ctx=context();micro=view(ctx,'5m')
    micro['features']['last_swing_low']['confirmed_at']=NOW+300
    micro['features']['breaks']=[dict(direction='bullish',at=micro['recent_ohlcv'][-1][0],level=100.)]
    assert not any(p['family']=='trend_continuation' for p in build_structure_setups(ctx,current_price=101.))


def test_no_fabricated_target_when_all_known_targets_are_behind_price():
    ctx=context();micro=view(ctx,'5m')
    micro['features']['breaks']=[dict(direction='bullish',at=micro['recent_ohlcv'][-1][0],level=100.)]
    assert build_structure_setups(ctx,current_price=200.)==[]


def test_missing_15m_is_not_labeled_full_context():
    ctx=context();micro=view(ctx,'5m')
    micro['features']['breaks']=[dict(direction='bullish',at=micro['recent_ohlcv'][-1][0],level=100.)]
    ctx['timeframes']=[v for v in ctx['timeframes'] if v['timeframe']!='15m']
    plans=build_structure_setups(ctx,current_price=101.)
    assert plans and all(p['requires_full_context'] for p in plans)


@pytest.mark.parametrize('timeframe,horizon',[('5m','scalp'),('15m','intraday'),('4h','swing')])
def test_runner2_horizon_has_own_trigger_and_references(timeframe,horizon):
    ctx=context()
    selected=view(ctx,timeframe)
    # Use only this source. The other horizons cannot create its seed.
    selected['recent_ohlcv'][2][2]=112.
    selected['features']['swings'][0]['price']=112.
    selected['features']['breaks']=[dict(direction='bullish',at=selected['recent_ohlcv'][-1][0],level=100.)]
    ctx['timeframes']=[selected]
    plan=next(p for p in build_structure_setups(ctx,current_price=101.,trigger_timeframe=timeframe) if p['state']=='ready')
    assert plan['horizon']==horizon
    assert plan['stop_reference']['timeframe']==plan['target_reference']['timeframe']==timeframe


def test_ict_shift_and_order_block_are_tied_to_closed_evidence():
    from trading.analysis.market import Candle, ict_features

    bars=[Candle(300*(i+1),100,102,98,101,1) for i in range(8)]
    bars[5]=Candle(1800,101,102,97,100,1)
    bars[6]=Candle(2100,100,109,99,108,1)
    bars[7]=Candle(2400,104,105,95,96,1)
    evidence=dict(breaks=[dict(at=1500,direction='bearish'),dict(at=2100,direction='bullish')],
                  sweeps=[dict(at=1800,direction='low_sweep')])
    result=ict_features(bars,evidence)
    assert result['structure_shifts'][0]['kind']=='mss'
    assert result['order_block_candidates'][0]['open_time']==1800
    assert result['order_block_candidates'][0]['status']=='invalidated'
    assert ict_features(bars,dict(evidence,sweeps=[]))['structure_shifts'][0]['kind']=='choch'


def test_trade_flow_rejects_stale_or_unknown_direction(monkeypatch):
    from types import SimpleNamespace
    from trading.analysis.market import trade_flow_context

    monkeypatch.setattr('trading.analysis.market.time',lambda:100.)
    trades=[dict(T=1,t=99000,v=8),dict(T=2,t=98000,v=2),
            dict(T=1,t=39000,v=100),dict(T=2,t=99500,v=100,M=1)]
    client=SimpleNamespace(get_recent_trades=lambda symbol:trades)
    sample=trade_flow_context(client,'TEST_USDT')
    assert sample['trade_count']==2 and sample['imbalance']==.6 and sample['sample_is_complete'] is False
    trades.append(dict(T=3,t=99900,v=10))
    assert trade_flow_context(client,'TEST_USDT') is None


def test_benchmark_only_uses_aligned_closed_bars():
    from types import SimpleNamespace
    from trading.analysis.market import benchmark_context

    ctx=context()
    fifteen=view(ctx,'15m')
    ctx['timeframes']=[fifteen]
    rows=[[NOW-900*(35-i),98.,102.,96.,98.,1.] for i in range(35)]

    def klines(symbol,interval,limit):
        data=rows if symbol=='BTC_USDT' else rows[:-1]
        return dict(symbol=symbol,interval=interval,candles=data)

    results=benchmark_context(SimpleNamespace(get_klines=klines),ctx)
    assert results['BTC_USDT']['15m']['closed_at']==NOW
    assert results['BTC_USDT']['15m']['relative_return_6_bars']==pytest.approx(101/98-1)
    assert results['ETH_USDT']['15m'] is None


def htf_zone_context(context_tf, trigger_tf):
    ctx = context()
    if context_tf == '1h':
        hourly = deepcopy(view(ctx, '4h'))
        hourly['timeframe'] = '1h'
        old_times = [row[0] for row in hourly['recent_ohlcv']]
        new_times = [NOW - 3600 * (12 - i) for i in range(12)]
        timestamps = dict(zip(old_times, new_times))
        for row in hourly['recent_ohlcv']:
            row[0] = timestamps[row[0]]
        for swing in hourly['features']['swings']:
            swing['pivot_at'] = timestamps[swing['pivot_at']]
            swing['confirmed_at'] = swing['pivot_at'] + 3 * 3600
        ctx['timeframes'].append(hourly)

    higher = view(ctx, context_tf)
    origin, confirmation = higher['recent_ohlcv'][-4:-2]
    origin[1:5] = [96., 97., 94., 95.]
    confirmation[1:5] = [95., 102., 95., 101.]
    higher['features']['order_block_candidates'] = [dict(
        direction='bullish', open_time=origin[0], confirmed_by=confirmation[0],
        low=94., high=97., status='retested')]
    lower = view(ctx, trigger_tf)
    for row in lower['recent_ohlcv']:
        row[1:5] = [99., 100., 98., 99.]
    low_swing = lower['features']['last_swing_low']
    low_swing['price'] = 98.
    high_swing = lower['features']['last_swing_high']
    high_swing['price'] = 100.
    lower['recent_ohlcv'][-3][3] = 96.
    latest = lower['recent_ohlcv'][-1]
    latest[1:5] = [99., 102., 98., 101.]
    lower['features']['structure_shifts'] = [dict(at=latest[0], direction='bullish', level=100., kind='mss')]
    return ctx


@pytest.mark.parametrize('context_tf,trigger_tf,horizon', [('4h','15m','swing'), ('1h','5m','intraday')])
@pytest.mark.parametrize('short', [False, True])
def test_htf_zone_touch_then_ltf_shift_keeps_different_entry_stop_and_target_timeframes(
        context_tf, trigger_tf, horizon, short):
    from trading.quant.structure_setups import build_htf_zone_setups

    ctx = htf_zone_context(context_tf, trigger_tf)
    if short:
        ctx = mirror(ctx)
        block = view(ctx, context_tf)['features']['order_block_candidates'][0]
        block.update(direction='bearish', low=103., high=106.)
        shift = view(ctx, trigger_tf)['features']['structure_shifts'][0]
        shift.update(direction='bearish')
    setup, = build_htf_zone_setups(ctx, current_price=99. if short else 101., price_step=.01)
    assert setup['state'] == 'ready'
    assert (setup['context_timeframe'], setup['trigger_timeframe'], setup['horizon']) == (context_tf, trigger_tf, horizon)
    assert setup['side'] == ('short' if short else 'long')
    assert setup['stop_reference']['timeframe'] == trigger_tf
    assert setup['target_reference']['timeframe'] == context_tf
    assert setup['opposing_target_levels']
    assert setup['target_reference'] == {
        key: setup['opposing_target_levels'][0][key] for key in ('timeframe', 'open_time', 'field')}
    assert all(level['timeframe'] == context_tf for level in setup['opposing_target_levels'])
    assert all(level['field'] == ('low' if short else 'high') for level in setup['opposing_target_levels'])
    entry = setup['entry_reference']
    assert all((level['price'] < entry if short else level['price'] > entry) for level in setup['opposing_target_levels'])
    assert setup['evidence']['zone']['confirmed_at'] < setup['evidence']['touch_at'] < setup['evidence']['confirmation_at']
    assert setup['evidence']['kind'] == 'htf_zone_mss'
    ctx['benchmarks'] = {'BTC_USDT': {'trend': 'bullish' if short else 'bearish'}}
    assert build_htf_zone_setups(ctx, current_price=99. if short else 101., price_step=.01)[0]['setup_id'] == setup['setup_id']


def test_farther_opposing_high_is_supplied_after_the_nearest():
    from trading.quant.structure_setups import build_htf_zone_setups

    ctx = htf_zone_context('4h', '15m')
    higher = view(ctx, '4h')
    higher['recent_ohlcv'][1][2] = 120.
    higher['features']['swings'].append(dict(
        kind='high', price=120., pivot_at=higher['recent_ohlcv'][1][0],
        confirmed_at=higher['recent_ohlcv'][4][0]))
    setup, = build_htf_zone_setups(ctx, current_price=101., price_step=.01)
    assert [level['price'] for level in setup['opposing_target_levels']] == [110., 120.]
    assert setup['take_profit_price'] == pytest.approx(110.)
    assert setup['target_reference']['open_time'] == setup['opposing_target_levels'][0]['open_time']


@pytest.mark.parametrize('context_tf,trigger_tf', [('4h', '15m'), ('1h', '5m')])
@pytest.mark.parametrize('short', [False, True])
def test_confirmed_stop_is_just_beyond_the_choch_candle(context_tf, trigger_tf, short):
    from trading.quant.structure_setups import build_htf_zone_setups

    ctx = htf_zone_context(context_tf, trigger_tf)
    if short:
        ctx = mirror(ctx)
        block = view(ctx, context_tf)['features']['order_block_candidates'][0]
        block.update(direction='bearish', low=103., high=106.)
        shift = view(ctx, trigger_tf)['features']['structure_shifts'][0]
        shift.update(direction='bearish')
    lower = view(ctx, trigger_tf)
    choch = lower['recent_ohlcv'][-1]
    other = lower['recent_ohlcv'][-2]
    if short:
        other[2] = choch[2] + 3
    else:
        other[3] = choch[3] - 3
    setup, = build_htf_zone_setups(ctx, current_price=99. if short else 101., price_step=.01)
    wick = choch[2] if short else choch[3]
    assert setup['stop_reference'] == dict(
        timeframe=trigger_tf, open_time=choch[0], field='high' if short else 'low')
    assert setup['stop_reference']['open_time'] == setup['evidence']['shift']['at']
    assert setup['target_reference']['timeframe'] == context_tf
    assert setup['target_reference']['field'] == ('low' if short else 'high')
    if short:
        assert wick < setup['stop_loss_price'] < other[2]
        assert setup['take_profit_price'] < 99 < setup['stop_loss_price']
    else:
        assert other[3] < setup['stop_loss_price'] < wick
        assert setup['stop_loss_price'] < 101 < setup['take_profit_price']


def test_htf_zone_requires_a_separate_touch_and_closed_confirmation_and_exposes_watch_stages():
    from trading.quant.structure_setups import build_htf_zone_setups

    ctx = htf_zone_context('4h', '15m')
    lower = view(ctx, '15m')
    lower['recent_ohlcv'][-3][3] = 98.
    lower['recent_ohlcv'][-1][3] = 96.
    assert not any(row['state'] == 'ready' for row in build_htf_zone_setups(ctx, current_price=101.))
    lower['recent_ohlcv'][-1][3] = 98.
    untouched, = build_htf_zone_setups(ctx, current_price=101.)
    assert untouched['state'] == 'watch'
    assert untouched['evidence']['stage'] == 'awaiting_touch'
    assert untouched['trigger_condition'] == 'price_below'
    assert untouched['trigger'] == 97.

    lower['recent_ohlcv'][-3][3] = 96.
    lower['recent_ohlcv'][-1][4] = 99.
    waiting, = build_htf_zone_setups(ctx, current_price=99.)
    assert waiting['state'] == 'watch'
    assert waiting['evidence']['stage'] == 'awaiting_confirmation'
    assert waiting['trigger_condition'] == 'closed_candle_cross_above'
    assert waiting['trigger'] == 100.

    block = view(ctx, '4h')['features']['order_block_candidates'][0]
    block['status'] = 'invalidated'
    assert build_htf_zone_setups(ctx, current_price=99.) == []


def _counter_context(context_tf, trigger_tf):
    """Higher timeframe stays long. The trigger CHoCH is short, at a bearish block."""
    ctx = htf_zone_context(context_tf, trigger_tf)
    higher = view(ctx, context_tf)
    higher['features']['trend'] = 'bullish'
    origin, confirmation = higher['recent_ohlcv'][-4], higher['recent_ohlcv'][-3]
    origin[1:5] = [105., 106., 103., 105.]
    confirmation[1:5] = [104., 104., 100., 101.]
    higher['features']['order_block_candidates'] = [dict(
        direction='bearish', open_time=origin[0], confirmed_by=confirmation[0],
        low=103., high=106., status='retested')]
    swing = higher['features']['last_swing_low']
    swing['price'] = 95.
    for row in higher['features']['swings']:
        if row.get('kind') == 'low':
            row['price'] = 95.
    pivot = next(row for row in higher['recent_ohlcv'] if row[0] == swing['pivot_at'])
    pivot[3] = 95.
    lower = view(ctx, trigger_tf)
    block = lower['recent_ohlcv'][-6]
    block[1:5] = [90., 92., 88., 89.]
    lower['features']['order_block_candidates'] = [dict(
        direction='bullish', open_time=block[0], low=88., high=92., status='untested')]
    touch = lower['recent_ohlcv'][-3]
    touch[1:5] = [102., 104., 101., 103.]
    latest = lower['recent_ohlcv'][-1]
    latest[1:5] = [100., 101., 96., 97.]
    lower['features']['structure_shifts'] = [dict(
        at=latest[0], direction='bearish', level=99., kind='choch')]
    return ctx


@pytest.mark.parametrize('context_tf,trigger_tf', [('4h', '15m'), ('1h', '5m')])
def test_counter_htf_target_is_the_next_block(context_tf, trigger_tf):
    from trading.quant.structure_setups import build_htf_zone_setups

    ctx = _counter_context(context_tf, trigger_tf)
    setup = next(row for row in build_htf_zone_setups(ctx, current_price=99., price_step=.01)
                 if row['context_timeframe'] == context_tf and row['side'] == 'short' and row['state'] == 'ready')
    assert setup['evidence']['alignment'] == 'against_higher_timeframe'
    assert setup['evidence']['shift']['kind'] == 'choch'
    assert setup['take_profit_price'] == pytest.approx(88.)
    assert setup['target_reference']['timeframe'] == trigger_tf
    assert setup['opposing_target_levels'] == [dict(setup['target_reference'], price=88.)]
    assert all(level['timeframe'] == trigger_tf for level in setup['opposing_target_levels'])
    assert all(level['price'] != 95. for level in setup['opposing_target_levels'])


@pytest.mark.parametrize('context_tf,trigger_tf', [('4h', '15m'), ('1h', '5m')])
def test_with_htf_target_can_be_farther(context_tf, trigger_tf):
    from trading.quant.structure_setups import build_htf_zone_setups

    ctx = htf_zone_context(context_tf, trigger_tf)
    higher = view(ctx, context_tf)
    higher['recent_ohlcv'][1][2] = 120.
    higher['features']['swings'].append(dict(
        kind='high', price=120., pivot_at=higher['recent_ohlcv'][1][0],
        confirmed_at=higher['recent_ohlcv'][4][0]))
    setup = next(row for row in build_htf_zone_setups(ctx, current_price=101., price_step=.01)
                 if row['context_timeframe'] == context_tf and row['state'] == 'ready')
    assert setup['evidence']['alignment'] == 'with_higher_timeframe'
    assert setup['side'] == 'long'
    prices = [level['price'] for level in setup['opposing_target_levels']]
    assert prices[0] < prices[-1]
    assert 120. in prices
    assert setup['take_profit_price'] == pytest.approx(prices[0])
    assert all(level['timeframe'] == context_tf for level in setup['opposing_target_levels'])


@pytest.mark.parametrize('context_tf,trigger_tf', [('4h', '15m'), ('1h', '5m')])
@pytest.mark.parametrize('short', [False, True])
def test_stop_is_the_choch_swing(context_tf, trigger_tf, short):
    from trading.quant.structure_setups import build_htf_zone_setups

    ctx = htf_zone_context(context_tf, trigger_tf)
    if short:
        ctx = mirror(ctx)
        block = view(ctx, context_tf)['features']['order_block_candidates'][0]
        block.update(direction='bearish', low=103., high=106.)
        shift = view(ctx, trigger_tf)['features']['structure_shifts'][0]
        shift.update(direction='bearish')
    choch = view(ctx, trigger_tf)['recent_ohlcv'][-1]
    setup, = [row for row in build_htf_zone_setups(ctx, current_price=99. if short else 101., price_step=.01)
              if row['context_timeframe'] == context_tf and row['state'] == 'ready']
    assert setup['stop_reference'] == dict(
        timeframe=trigger_tf, open_time=choch[0], field='high' if short else 'low')
    assert setup['stop_reference']['open_time'] == setup['evidence']['shift']['at']
    if short:
        assert setup['stop_loss_price'] > choch[2]
    else:
        assert setup['stop_loss_price'] < choch[3]


@pytest.mark.parametrize('context_tf,trigger_tf,horizon', [('4h', '15m', 'swing'), ('1h', '5m', 'intraday')])
def test_flow_confirms_the_same_way_as_a_block(context_tf, trigger_tf, horizon):
    from trading.quant.structure_setups import build_htf_zone_setups

    ctx = htf_zone_context(context_tf, trigger_tf)
    higher = view(ctx, context_tf)
    higher['features']['order_block_candidates'] = []
    origin = higher['recent_ohlcv'][-5]
    higher['features']['fvgs'] = [dict(direction='bullish', at=origin[0], lower=93., upper=97.)]
    setup = next(row for row in build_htf_zone_setups(ctx, current_price=101., price_step=.01)
                 if row['context_timeframe'] == context_tf and row['state'] == 'ready')
    assert setup['evidence']['zone']['kind'] == 'flow'
    assert (setup['context_timeframe'], setup['trigger_timeframe'], setup['horizon']) == (context_tf, trigger_tf, horizon)
    assert setup['side'] == 'long'
    assert setup['stop_reference']['timeframe'] == trigger_tf
    assert setup['stop_reference']['field'] == 'low'


@pytest.mark.parametrize('context_tf,trigger_tf', [('4h', '15m'), ('1h', '5m')])
def test_a_filled_flow_is_not_a_zone(context_tf, trigger_tf):
    from trading.quant.structure_setups import build_htf_zone_setups

    ctx = htf_zone_context(context_tf, trigger_tf)
    higher = view(ctx, context_tf)
    higher['features']['order_block_candidates'] = []
    origin = higher['recent_ohlcv'][-5]
    higher['features']['fvgs'] = [dict(direction='bullish', at=origin[0], lower=93., upper=97.)]
    higher['recent_ohlcv'][-3][3] = 92.5
    assert not [row for row in build_htf_zone_setups(ctx, current_price=101., price_step=.01)
                if row['context_timeframe'] == context_tf and row['evidence'].get('zone', {}).get('kind') == 'flow']


def test_flow_fill_reads_only_later_wicks_through_the_far_edge():
    from trading.quant.structure_setups import _flow_filled

    rows = [[0, 10., 11., 9., 10., 1.], [1, 10., 12., 10., 11., 1.], [2, 11., 13., 10.5, 12., 1.]]
    view_ = dict(rows=rows)
    assert not _flow_filled(dict(direction='bullish', at=1, lower=10.4, upper=11.), view_)
    assert _flow_filled(dict(direction='bullish', at=1, lower=10.5, upper=11.), view_)
    assert not _flow_filled(dict(direction='bullish', at=2, lower=10.5, upper=11.), view_)
    assert not _flow_filled(dict(direction='bearish', at=1, lower=12., upper=13.5), view_)
    assert _flow_filled(dict(direction='bearish', at=1, lower=12., upper=13.), view_)
    assert not _flow_filled(dict(direction='bearish', at=0, lower=12., upper=13.), dict(rows=rows[:2]))


def test_a_filled_flow_is_not_a_target():
    from trading.quant.structure_setups import _opposing_locations

    rows = [[0, 10., 11., 9., 10., 1.], [1, 10., 15., 10., 11., 1.], [2, 11., 12., 10.5, 11.5, 1.]]
    gap = dict(direction='bearish', at=1, lower=13., upper=15.)
    open_gap = dict(rows=rows, features=dict(fvgs=[gap]))
    assert [ref['price'] for ref in _opposing_locations(open_gap, '1h', long=True, entry=11.5)] == [15.]
    filled = dict(rows=rows + [[3, 11.5, 15.2, 11., 12., 1.]], features=dict(fvgs=[gap]))
    assert _opposing_locations(filled, '1h', long=True, entry=11.5) == []


def test_htf_zone_rejects_open_confirmation_and_distal_close_invalidation():
    from trading.quant.structure_setups import build_htf_zone_setups

    ctx = htf_zone_context('4h', '15m')
    ctx['observed_at'] = NOW - 1
    assert build_htf_zone_setups(ctx, current_price=101.) == []
    ctx['observed_at'] = NOW
    row = view(ctx, '15m')['recent_ohlcv'][-2]
    row[3:5] = [92., 93.]
    assert build_htf_zone_setups(ctx, current_price=101.) == []
