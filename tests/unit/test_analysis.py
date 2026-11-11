from pathlib import Path
import pytest

from trading.analysis import Candle, build_bundle, closed_candles, structure_features, render_bundle
from trading.analysis.market import TIMEFRAMES


def rows(count=40, seconds=300, end=1_800_000_000):
    return [[end-(count-i)*seconds, 100+i, 102+i, 99+i, 101+i, i+1] for i in range(count)]


def test_forming_candle_excluded_and_closed_history_validated():
    data=rows()
    data.append([1_800_000_000, None, None, None, None, None])
    bars=closed_candles(data,300,1_800_000_010)
    assert len(bars)==40
    assert bars[-1].timestamp==1_799_999_700
    with pytest.raises(ValueError,match='missing'):
        closed_candles(data[:10]+data[11:],300,1_800_000_010)


@pytest.mark.parametrize('index,value',[(1,float('nan')),(2,1),(3,999),(4,0),(5,-1)])
def test_bad_ohlcv_rejected(index,value):
    data=rows()
    data[-1][index]=value
    with pytest.raises(ValueError):
        closed_candles(data,300,1_800_000_000)


def test_stale_or_duplicate_history_refused():
    data=rows()
    with pytest.raises(ValueError,match='stale'):
        closed_candles(data,300,1_800_000_900)
    with pytest.raises(ValueError,match='duplicate'):
        closed_candles(data+data[-1:],300,1_800_000_000)


def test_swing_only_exists_after_right_side_confirmation():
    bars=[Candle(i*300, 10, high, 9, 10, 1) for i,high in enumerate([11,12,15,12,11,16])]
    before=structure_features(bars[:4])
    assert before['last_swing_high'] is None
    confirmed=structure_features(bars[:5])['last_swing_high']
    assert confirmed['pivot_at']==600 and confirmed['confirmed_at']==1500
    # A future new high does not invalidate the historically confirmed pivot.
    assert structure_features(bars)['last_swing_high']==confirmed


def test_break_and_sweep_use_prior_confirmed_level():
    bars=[Candle(i*300, 10, high, 9, 10, 1) for i,high in enumerate([11,12,15,12,11])]
    swept=bars+[Candle(1500,14,16,13,14,1)]
    assert structure_features(swept)['sweeps'][-1]['direction']=='high_sweep'
    broken=swept+[Candle(1800,14,17,13,16,1)]
    feat=structure_features(broken)
    assert feat['breaks'][-1]['direction']=='bullish'
    assert feat['breaks'][-1]['at']==1800


def test_fvg_is_explicitly_historical():
    bars=[Candle(0,10,11,9,10,1),Candle(300,12,14,11,13,1),Candle(600,15,16,14,15,1)]
    f=structure_features(bars)
    assert f['fvgs']==[dict(direction='bullish',at=600,lower=11,upper=14)]
    assert 'not asserted unfilled' in f['caveat']


class Client:
    def get_klines(self,symbol,interval,limit):
        seconds=next(v[1] for v in TIMEFRAMES.values() if v[0]==interval)
        return dict(symbol=symbol,interval=interval,candles=rows(seconds=seconds))


def test_bundle_has_exact_three_contract_timeframes_and_stable_hash():
    a=build_bundle(Client(),'BTC_USDT',now=1_800_000_010)
    b=build_bundle(Client(),'BTC_USDT',now=1_800_000_020)
    assert [v.label for v in a.views]==['4h','15m','5m']
    assert a.fingerprint==b.fingerprint
    assert a.to_context()['instrument']=='USDT perpetual'
    assert all(len(v.to_context()['recent_ohlcv'])==12 for v in a.views)
    with pytest.raises(ValueError):
        build_bundle(Client(),'../BTC_USDT',now=1_800_000_010)


def test_chart_artifacts_are_content_addressed_pngs(tmp_path):
    pytest.importorskip('matplotlib')
    bundle=build_bundle(Client(),'BTC_USDT',now=1_800_000_010)
    artifacts=render_bundle(bundle,tmp_path)
    assert len(artifacts)==3
    for a in artifacts:
        assert Path(a.path).read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
        assert len(a.sha256)==64 and Path(a.path).is_absolute()
    assert render_bundle(bundle,tmp_path)==artifacts


def test_runner3_hourly_bundle_uses_closed_min60_and_degrades_by_lane(tmp_path):
    now = 1_800_000_010

    class HourlyClient(Client):
        def get_klines(self, symbol, interval, limit):
            if interval == 'Min60':
                data = rows(count=60, seconds=3600)
                data.append([1_800_000_000, None, None, None, None, None])
                return dict(symbol=symbol, interval=interval, candles=data)
            seconds = next(v[1] for v in TIMEFRAMES.values() if v[0] == interval)
            return dict(symbol=symbol, interval=interval, candles=rows(count=60, seconds=seconds))

    class MissingHourlyClient(HourlyClient):
        def get_klines(self, symbol, interval, limit):
            if interval == 'Min60':
                raise ValueError('hourly unavailable')
            return super().get_klines(symbol, interval, limit)

    bundle = build_bundle(HourlyClient(), 'TEST_USDT', now=now, include_hourly=True)
    context = bundle.to_context(ict=True, htf_history=True)
    frames = {view['timeframe']: view for view in context['timeframes']}
    assert {frame: len(view['recent_ohlcv']) for frame, view in frames.items()} == {
        '4h': 48, '1h': 48, '15m': 48, '5m': 48,
    }
    assert frames['1h']['closed_at'] == 1_800_000_000
    assert frames['1h']['recent_ohlcv'][-1][0] == 1_799_996_400
    assert 'structure_shifts' in frames['1h']['features']
    degraded = build_bundle(MissingHourlyClient(), 'TEST_USDT', now=now, include_hourly=True)
    assert {view.label for view in degraded.views} == {'4h', '15m', '5m'}

    pytest.importorskip('matplotlib')
    assert {chart.timeframe for chart in render_bundle(bundle, tmp_path)} == set(frames)


def test_hourly_benchmark_keeps_closed_relative_returns_and_structure():
    from trading.analysis.market import benchmark_context

    step = 3600
    end = 1_800_000_000
    raw = [[end - (45 - i) * step, 100, 102, 99, 100, 1] for i in range(45)]
    # Establish pivots followed by opposing closes with displacement.
    raw[30][2] = 110
    raw[31][3] = 90
    raw[33][1:5] = [101, 104, 99, 100]
    raw[34][1:5] = [100, 116, 98, 115]
    raw[39][1:5] = [101, 103, 101, 102]
    raw[40][1:5] = [100, 101, 82, 84]
    # A currently forming hourly candle is deliberately invalid and excluded.
    raw.append([end, None, None, None, None, None])

    class Benchmarks:
        def get_klines(self, symbol, interval, limit):
            assert interval == 'Min60'
            return dict(symbol=symbol, interval=interval,
                        candles=raw if symbol == 'BTC_USDT' else raw[:-2])

    frame = dict(timeframe='1h', recent_ohlcv=rows(count=12, seconds=step, end=end))
    market = dict(symbol='TEST_USDT', observed_at=end + 10, timeframes=[frame])
    benchmarks = benchmark_context(Benchmarks(), market, include_structure=True)
    btc = benchmarks['BTC_USDT']['1h']
    assert btc['closed_at'] == end
    assert btc['last_break']['direction'] == 'bearish'
    assert btc['structure_shifts'][-1]['kind'] == 'choch'
    assert btc['order_block_candidates'][-1]['direction'] == 'bearish'
    assert btc['relative_return_6_bars'] == pytest.approx(112 / 106 - 1)
    assert benchmarks['ETH_USDT']['1h'] is None


def test_one_minute_refinement_requires_the_latest_closed_aligned_candle():
    from trading.analysis.market import entry_refinement_context

    end = 1_800_000_000
    data = rows(seconds=60, end=end)

    class MinuteClient:
        def get_klines(self, symbol, interval, limit):
            assert (symbol, interval, limit) == ('TEST_USDT', 'Min1', 41)
            forming = [end, None, None, None, None, None]
            return dict(symbol=symbol, interval=interval, candles=[*data, forming])

    market = dict(symbol='TEST_USDT', observed_at=end + 1)
    context = entry_refinement_context(MinuteClient(), market)
    assert context['timeframe'] == '1m'
    assert context['closed_at'] == end
    assert context['recent_ohlcv'] == data[-6:]
    assert context['structure_shifts'] == []
    data.pop()
    assert entry_refinement_context(MinuteClient(), market) is None
