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
import re
from time import time

TIMEFRAMES = {'4h': ('Hour4', 14400), '15m': ('Min15', 900), '5m': ('Min5', 300)}
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

    def to_context(self):
        return dict(timeframe=self.label, closed_at=self.closed_at, history_bars=len(self.candles),
                    fingerprint=self.fingerprint, features=self.features,
                    recent_ohlcv=[c.row() for c in self.candles[-12:]])


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

    def to_context(self):
        return dict(symbol=self.symbol, exchange=self.exchange, instrument=self.instrument,
                    observed_at=self.observed_at, fingerprint=self.fingerprint,
                    feature_version=FEATURE_VERSION,
                    timeframes=[v.to_context() for v in self.views])


def build_bundle(client, symbol, *, now=None, limit=120):
    """Fetch only MEXC contract klines; reject invalid/missing/stale timeframes.

    Caller must verify contract rules settleCoin=USDT and active status before
    acting; naming alone does not prove a tradable instrument.
    """
    if not re.fullmatch(r'[A-Z0-9]+_USDT', symbol):
        raise ValueError('expected MEXC USDT perpetual symbol, e.g. BTC_USDT')
    if not 30 <= limit <= 240:
        raise ValueError('chart history limit must be 30..240')
    observed = time() if now is None else float(now)
    views = []
    for label, (interval, seconds) in TIMEFRAMES.items():
        payload = client.get_klines(symbol, interval=interval, limit=limit+1)
        if payload.get('symbol') != symbol or payload.get('interval') != interval:
            raise ValueError('market response symbol/interval mismatch')
        candles = closed_candles(payload.get('candles', []), seconds, observed)[-limit:]
        views.append(TimeframeView(label, seconds, candles, structure_features(candles)))
    return AnalysisBundle(symbol, observed, tuple(views))
