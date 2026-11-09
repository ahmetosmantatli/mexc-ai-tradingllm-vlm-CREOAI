"""Versioned PA definitions, not a claim that discretionary ICT predicts returns.

Confirmed swing: strict extremum over two candles on both sides; available only
on close of right-hand confirmation candle. BOS: first close crossing the most
recent previously-confirmed swing. Sweep: wick crosses prior confirmed swing and
same candle closes back inside it. FVG: three-candle nonoverlap high/low gap.
All features are retrospective evidence computed exclusively from closed candles.
"""
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from math import isfinite
from statistics import median
import re
from time import time

import httpx

TIMEFRAMES = {'4h': ('Hour4', 14400), '15m': ('Min15', 900), '5m': ('Min5', 300)}
HOURLY_TIMEFRAME = ('Min60', 3600)
FEATURE_VERSION = 'pa-closed-v1'


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def row(self):
        return [self.timestamp, self.open, self.high, self.low, self.close, self.volume]


def closed_candles(rows, interval_seconds, now=None, *, min_count=30):
    """Strictly validate a contiguous closed history. Timestamp is UTC seconds.

    Incomplete/future candles are excluded before OHLC validation, so a malformed
    current candle never contributes evidence. Duplicate/out-of-order closed bars
    are rejected rather than silently sorted. Missing history fails closed.
    """
    now = time() if now is None else float(now)
    if not isfinite(now) or interval_seconds <= 0:
        raise ValueError('invalid candle clock/interval')
    candles = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            raise ValueError('incomplete OHLCV row')
        stamp = float(row[0])
        if not isfinite(stamp) or stamp != int(stamp) or stamp < 0 or stamp > 100_000_000_000:
            raise ValueError('expected UTC candle timestamp in seconds')
        if stamp + interval_seconds > now:
            continue
        values = [float(v) for v in row[1:6]]
        if not all(isfinite(v) for v in values):
            raise ValueError('non-finite OHLCV')
        op, hi, lo, cl, vol = values
        if min(op, hi, lo, cl) <= 0 or vol < 0 or hi < max(op, cl) or lo > min(op, cl) or hi < lo:
            raise ValueError('invalid OHLCV prices/volume')
        if candles and int(stamp)-candles[-1].timestamp != interval_seconds:
            raise ValueError('duplicate, unordered, or missing closed candles')
        candles.append(Candle(int(stamp), *values))
    if len(candles) < min_count:
        raise ValueError('insufficient closed candle history')
    # API may omit currently forming bar; latest completed bar must still be recent.
    if now-(candles[-1].timestamp+interval_seconds) >= interval_seconds*2:
        raise ValueError('stale closed candle history')
    return tuple(candles)


def structure_features(candles, *, swing_radius=2):
    """Return timestamped, explicitly confirmed structure. No hidden future bars."""
    if swing_radius < 1:
        raise ValueError('swing radius must be positive')
    interval = candles[1].timestamp-candles[0].timestamp if len(candles)>1 else 0
    swings, breaks, sweeps, gaps = [], [], [], []
    last_high = last_low = None
    broken_high = broken_low = None
    for i, candle in enumerate(candles):
        # Existing confirmed levels only: a newly confirmed level at this close
        # does not retroactively change the level used for this bar's signal.
        if last_high is not None:
            level = last_high['price']
            if candle.close > level and broken_high != last_high['pivot_at']:
                breaks.append(dict(direction='bullish', at=candle.timestamp, level=level, swing_at=last_high['pivot_at']))
                broken_high = last_high['pivot_at']
            elif candle.high > level and candle.close <= level:
                sweeps.append(dict(direction='high_sweep', at=candle.timestamp, level=level))
        if last_low is not None:
            level = last_low['price']
            if candle.close < level and broken_low != last_low['pivot_at']:
                breaks.append(dict(direction='bearish', at=candle.timestamp, level=level, swing_at=last_low['pivot_at']))
                broken_low = last_low['pivot_at']
            elif candle.low < level and candle.close >= level:
                sweeps.append(dict(direction='low_sweep', at=candle.timestamp, level=level))
        if i >= 2:
            before = candles[i-2]
            if candle.low > before.high:
                gaps.append(dict(direction='bullish', at=candle.timestamp, lower=before.high, upper=candle.low))
            elif candle.high < before.low:
                gaps.append(dict(direction='bearish', at=candle.timestamp, lower=candle.high, upper=before.low))
        pivot_index = i-swing_radius
        if pivot_index >= swing_radius:
            pivot = candles[pivot_index]
            neighbors = [candles[j] for j in range(pivot_index-swing_radius, i+1) if j != pivot_index]
            if all(pivot.high > c.high for c in neighbors):
                last_high = dict(kind='high', price=pivot.high, pivot_at=pivot.timestamp, confirmed_at=candle.timestamp+interval)
                swings.append(last_high)
            if all(pivot.low < c.low for c in neighbors):
                last_low = dict(kind='low', price=pivot.low, pivot_at=pivot.timestamp, confirmed_at=candle.timestamp+interval)
                swings.append(last_low)
    return dict(definition_version=FEATURE_VERSION, swing_radius=swing_radius,
                last_swing_high=last_high, last_swing_low=last_low,
                swings=swings[-12:], breaks=breaks[-12:], sweeps=sweeps[-12:], fvgs=gaps[-12:],
                trend='bullish' if breaks and breaks[-1]['direction']=='bullish' else 'bearish' if breaks else 'undetermined',
                caveat='FVGs are historical formations, not asserted unfilled zones; patterns are not calibrated trade probabilities.')


def ict_features(candles, features):
    """Operational ICT labels from closed candles, without trade-flow inference."""
    by_time={c.timestamp:i for i,c in enumerate(candles)}
    previous=None
    shifts=[]
    blocks=[]
    for br in features['breaks']:
        direction=br['direction']
        index=by_time[br['at']]
        candle=candles[index]
        bullish=direction=='bullish'
        choch=previous is not None and previous!=direction
        previous=direction
        bodies=[abs(bar.close-bar.open) for bar in candles[max(0,index-10):index]]
        displacement=(len(bodies)>=5 and abs(candle.close-candle.open)>1.5*median(bodies)
                      and (candle.close>candle.open if bullish else candle.close<candle.open))
        if choch:
            recent={bar.timestamp for bar in candles[max(0,index-3):index]}
            swept=any(sweep['at'] in recent and sweep['direction']==('low_sweep' if bullish else 'high_sweep')
                      for sweep in features['sweeps'])
            shifts.append(dict(br,kind='mss' if swept and displacement else 'choch'))
        if not displacement:
            continue
        block=next((bar for bar in reversed(candles[max(0,index-3):index])
                    if (bar.close<bar.open if bullish else bar.close>bar.open)),None)
        if block is None:
            continue
        later=candles[index+1:]
        invalid=any(bar.close<block.low if bullish else bar.close>block.high for bar in later)
        touched=any(bar.low<=block.high and bar.high>=block.low for bar in later)
        blocks.append(dict(direction=direction,open_time=block.timestamp,confirmed_by=br['at'],
            low=block.low,high=block.high,status='invalidated' if invalid else 'retested' if touched else 'untested'))
    return dict(structure_shifts=shifts[-4:],order_block_candidates=blocks[-4:])


@dataclass(frozen=True)
class TimeframeView:
    label: str
    interval_seconds: int
    candles: tuple[Candle, ...]
    features: dict

    @property
    def closed_at(self):
        return self.candles[-1].timestamp+self.interval_seconds

    @property
    def fingerprint(self):
        material = json.dumps([c.row() for c in self.candles], separators=(',', ':'), allow_nan=False)
        return sha256(material.encode()).hexdigest()

    def to_context(self, *, ict=False, htf_history=False):
        features={**self.features, **ict_features(self.candles,self.features)} if ict else self.features
        context_bars = 48 if htf_history and self.label in ('4h', '1h') else 12
        return dict(timeframe=self.label, closed_at=self.closed_at, history_bars=len(self.candles),
                    fingerprint=self.fingerprint, features=features,
                    recent_ohlcv=[c.row() for c in self.candles[-context_bars:]])


@dataclass(frozen=True)
class AnalysisBundle:
    symbol: str
    observed_at: float
    views: tuple[TimeframeView, ...]
    exchange: str = 'MEXC'
    instrument: str = 'USDT perpetual'

    @property
    def fingerprint(self):
        return sha256((self.symbol+':'+':'.join(v.fingerprint for v in self.views)).encode()).hexdigest()

    def to_context(self, *, ict=False, htf_history=False):
        return dict(symbol=self.symbol, exchange=self.exchange, instrument=self.instrument,
                    observed_at=self.observed_at, fingerprint=self.fingerprint,
                    feature_version=FEATURE_VERSION,
                    timeframes=[v.to_context(ict=ict, htf_history=htf_history) for v in self.views])


def trade_flow_context(client, symbol):
    """Reported trade-side imbalance over a bounded recent sample."""
    fetch=getattr(client,'get_recent_trades',None)
    if not callable(fetch):
        return None
    try:
        rows=fetch(symbol)
        now=time()*1000
        volume={1:0.,2:0.}
        timestamps=[]
        for row in rows:
            side,stamp,quantity=row['T'],row['t'],float(row['v'])
            if (type(side) is not int or side not in volume or type(stamp) is not int
                    or not isfinite(quantity) or quantity<=0 or stamp>now+5000):
                raise ValueError('invalid trade sample')
            if stamp>=now-60000 and row.get('M')!=1:
                volume[side]+=quantity
                timestamps.append(stamp)
        if not timestamps:
            return None
        total=volume[1]+volume[2]
        if not isfinite(total):
            return None
        return dict(source='MEXC recent trades',sample_is_complete=False,trade_count=len(timestamps),
            first_at_ms=min(timestamps),last_at_ms=max(timestamps),
            buy_quantity=volume[1],sell_quantity=volume[2],imbalance=(volume[1]-volume[2])/total)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, httpx.HTTPError):
        return None


def benchmark_context(client, market, *, include_structure=False):
    """Compare returns across identical closed intervals, using shared public caches."""
    result={}
    fetch=getattr(client,'get_klines',None)
    if not callable(fetch):
        return result
    observed=market.get('observed_at')
    for symbol in ('BTC_USDT','ETH_USDT'):
        if symbol==market.get('symbol'):
            continue
        frames={}
        for view in market.get('timeframes',[]):
            tf=view['timeframe']
            resolution = HOURLY_TIMEFRAME if tf == '1h' else TIMEFRAMES.get(tf)
            if resolution is None:
                continue
            interval, seconds = resolution
            rows=view.get('recent_ohlcv',[])
            if len(rows)<7 or observed is None:
                continue
            try:
                payload=fetch(symbol,interval=interval,limit=61)
                if payload.get('symbol')!=symbol or payload.get('interval')!=interval:
                    raise ValueError('benchmark identity mismatch')
                end=rows[-1][0]
                candles=closed_candles(payload['candles'],seconds,min(observed,end+seconds))
                if candles[-1].timestamp!=end or candles[-7].timestamp!=rows[-7][0]:
                    raise ValueError('benchmark bars not aligned')
                benchmark=candles[-1].close/candles[-7].close-1
                relative=rows[-1][4]/rows[-7][4]-1-benchmark
                features = structure_features(candles)
                comparison = dict(closed_at=end+seconds, trend=features['trend'],
                                  return_6_bars=benchmark, relative_return_6_bars=relative)
                if include_structure:
                    ict = ict_features(candles, features)
                    comparison.update(last_break=features['breaks'][-1] if features['breaks'] else None,
                                      structure_shifts=ict['structure_shifts'][-2:],
                                      order_block_candidates=[block for block in ict['order_block_candidates']
                                                              if block['status'] != 'invalidated'][-2:],
                                      historical_fvgs=features['fvgs'][-2:])
                frames[tf] = comparison
            except (ValueError, KeyError, TypeError, OSError, RuntimeError, httpx.HTTPError):
                frames[tf]=None
        result[symbol]=frames
    return result


def entry_refinement_context(client, market):
    """Optional closed 1m timing context after a higher-level setup is ready."""
    fetch = getattr(client, 'get_klines', None)
    symbol = market.get('symbol')
    observed = market.get('observed_at')
    if (not callable(fetch) or not isinstance(symbol, str)
            or not re.fullmatch(r'[A-Z0-9]+_USDT', symbol)
            or type(observed) not in (int, float) or not isfinite(observed) or observed <= 0):
        return None
    seconds = 60
    try:
        payload = fetch(symbol, interval='Min1', limit=41)
        if payload.get('symbol') != symbol or payload.get('interval') != 'Min1':
            return None
        candles = closed_candles(payload['candles'], seconds, observed)
        closed_at = candles[-1].timestamp + seconds
        if closed_at != int(observed // seconds) * seconds:
            return None
        features = structure_features(candles)
        ict = ict_features(candles, features)
        return dict(timeframe='1m', closed_at=closed_at, trend=features['trend'],
                    last_break=features['breaks'][-1] if features['breaks'] else None,
                    structure_shifts=ict['structure_shifts'][-2:],
                    recent_ohlcv=[candle.row() for candle in candles[-6:]])
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, httpx.HTTPError):
        return None


def build_bundle(client, symbol, *, now=None, limit=120, include_hourly=False):
    """Fetch MEXC contract klines with strict validation of each closed series.

    Caller must verify contract rules settleCoin=USDT and active status before
    acting; naming alone does not prove a tradable instrument. A missing optional
    1h series disables the hourly setup lane while leaving 4h context available.
    """
    if not re.fullmatch(r'[A-Z0-9]+_USDT', symbol):
        raise ValueError('expected MEXC USDT perpetual symbol, e.g. BTC_USDT')
    if not 30 <= limit <= 240:
        raise ValueError('chart history limit must be 30..240')
    observed = time() if now is None else float(now)
    views = []
    frames = {**TIMEFRAMES, '1h': HOURLY_TIMEFRAME} if include_hourly else TIMEFRAMES
    for label, (interval, seconds) in frames.items():
        try:
            payload = client.get_klines(symbol, interval=interval, limit=limit+1)
            if payload.get('symbol') != symbol or payload.get('interval') != interval:
                raise ValueError('market response symbol/interval mismatch')
            candles = closed_candles(payload.get('candles', []), seconds, observed)[-limit:]
        except (ValueError, KeyError, TypeError, OSError, RuntimeError, httpx.HTTPError):
            if label != '1h':
                raise
            continue
        views.append(TimeframeView(label, seconds, candles, structure_features(candles)))
    return AnalysisBundle(symbol, observed, tuple(views))
