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
