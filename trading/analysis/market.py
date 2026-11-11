"""Versioned PA definitions, not a claim that discretionary ICT predicts returns.

Confirmed swing: extremum over two candles on both sides; available only on
close of the right-hand confirmation candle. Equal highs/lows count: the
leftmost bar of a plateau wins the tie (strict vs left neighbors, inclusive vs
right). BOS: first close crossing the most recent previously-confirmed swing.
Sweep: wick crosses a prior confirmed swing or an equal-high/low pool outer
wick and the same candle closes back inside. Equal-H/L pools cluster consecutive
same-side swings within a tenth of ATR; a pool sweep takes every member.
FVG: three-candle nonoverlap high/low gap. All features are retrospective
evidence computed exclusively from closed candles.
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
FEATURE_VERSION = 'pa-closed-v2'
POOL_ATR_FRACTION = .1


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


def _true_range_atr(candles):
    """Average true range over the closed series (one bar needs a prior close)."""
    if len(candles) < 2:
        return 0.
    total = 0.
    for index in range(1, len(candles)):
        bar, prior = candles[index], candles[index - 1]
        total += max(bar.high - bar.low, abs(bar.high - prior.close), abs(bar.low - prior.close))
    return total / (len(candles) - 1)


def _is_swing_high(pivot, left, right):
    """Leftmost plateau wins: strict vs left neighbors, inclusive vs right."""
    return all(pivot.high > bar.high for bar in left) and all(pivot.high >= bar.high for bar in right)


def _is_swing_low(pivot, left, right):
    """Leftmost plateau wins: strict vs left neighbors, inclusive vs right."""
    return all(pivot.low < bar.low for bar in left) and all(pivot.low <= bar.low for bar in right)


def _swing_neighbors(candles, pivot_index, radius, confirm_index):
    left = [candles[j] for j in range(pivot_index - radius, pivot_index)]
    right = [candles[j] for j in range(pivot_index + 1, confirm_index + 1)]
    return left, right


def _member(swing):
    return dict(price=swing['price'], pivot_at=swing['pivot_at'], confirmed_at=swing['confirmed_at'])


def _update_liquidity_pools(pools, swing, *, atr):
    """Attach ``swing`` to the open same-side pool when within a tenth of ATR.

    Consecutive same-side swings form one pool. The level is the outer wick.
    A pool confirms when its second member confirms. One-member seeds stay
    internal until a second equal swing arrives.
    """
    if atr <= 0:
        return
    side = 'eqh' if swing['kind'] == 'high' else 'eql'
    tolerance = POOL_ATR_FRACTION * atr
    open_pool = next((pool for pool in reversed(pools)
                      if pool['kind'] == side and pool.get('swept_at') is None), None)
    if open_pool is not None and abs(open_pool['members'][-1]['price'] - swing['price']) <= tolerance:
        if any(row['pivot_at'] == swing['pivot_at'] for row in open_pool['members']):
            return
        open_pool['members'].append(_member(swing))
        prices = [row['price'] for row in open_pool['members']]
        open_pool['level'] = max(prices) if side == 'eqh' else min(prices)
        if len(open_pool['members']) >= 2 and open_pool.get('confirmed_at') is None:
            open_pool['confirmed_at'] = swing['confirmed_at']
        return
    pools.append(dict(kind=side, level=swing['price'], members=[_member(swing)],
                      confirmed_at=None, swept_at=None))


def _finalize_liquidity_pools(pools):
    """Drop one-member seeds; keep confirmed equal-H/L pools only."""
    return [dict(pool) for pool in pools if len(pool.get('members') or []) >= 2 and pool.get('confirmed_at')]


def structure_features(candles, *, swing_radius=2):
    """Return timestamped, explicitly confirmed structure. No hidden future bars."""
    if swing_radius < 1:
        raise ValueError('swing radius must be positive')
    interval = candles[1].timestamp-candles[0].timestamp if len(candles)>1 else 0
    swings, breaks, sweeps, gaps, pools = [], [], [], [], []
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
        for pool in pools:
            if pool.get('confirmed_at') is None or pool.get('swept_at') is not None:
                continue
            # confirmed_at is the close time of the bar that confirmed the
            # second member; this bar may sweep it on later closes only.
            if pool['confirmed_at'] > candle.timestamp:
                continue
            level = pool['level']
            took = ((pool['kind'] == 'eqh' and candle.high > level and candle.close <= level)
                    or (pool['kind'] == 'eql' and candle.low < level and candle.close >= level))
            if not took:
                continue
            pool['swept_at'] = candle.timestamp
            direction = 'high_sweep' if pool['kind'] == 'eqh' else 'low_sweep'
            members = [dict(row) for row in pool['members']]
            existing = next((event for event in reversed(sweeps)
                             if event['at'] == candle.timestamp and event['direction'] == direction
                             and event['level'] == level), None)
            if existing is not None:
                existing.update(pool=True, kind=pool['kind'], members=members)
            else:
                sweeps.append(dict(direction=direction, at=candle.timestamp, level=level,
                                   pool=True, kind=pool['kind'], members=members))
        if i >= 2:
            before = candles[i-2]
            if candle.low > before.high:
                gaps.append(dict(direction='bullish', at=candle.timestamp, lower=before.high, upper=candle.low))
            elif candle.high < before.low:
                gaps.append(dict(direction='bearish', at=candle.timestamp, lower=candle.high, upper=before.low))
        pivot_index = i-swing_radius
        if pivot_index >= swing_radius:
            pivot = candles[pivot_index]
            left, right = _swing_neighbors(candles, pivot_index, swing_radius, i)
            # ATR only from bars closed so far — no look-ahead into later candles.
            atr = _true_range_atr(candles[:i + 1])
            if _is_swing_high(pivot, left, right):
                last_high = dict(kind='high', price=pivot.high, pivot_at=pivot.timestamp, confirmed_at=candle.timestamp+interval)
                swings.append(last_high)
                _update_liquidity_pools(pools, last_high, atr=atr)
            if _is_swing_low(pivot, left, right):
                last_low = dict(kind='low', price=pivot.low, pivot_at=pivot.timestamp, confirmed_at=candle.timestamp+interval)
                swings.append(last_low)
                _update_liquidity_pools(pools, last_low, atr=atr)
    return dict(definition_version=FEATURE_VERSION, swing_radius=swing_radius,
                last_swing_high=last_high, last_swing_low=last_low,
                swings=swings[-12:], breaks=breaks[-12:], sweeps=sweeps[-12:], fvgs=gaps[-12:],
                liquidity_pools=_finalize_liquidity_pools(pools)[-8:],
                trend='bullish' if breaks and breaks[-1]['direction']=='bullish' else 'bearish' if breaks else 'undetermined',
                caveat='FVGs are historical formations, not asserted unfilled zones; patterns are not calibrated trade probabilities.')


def _pivots(candles, radius=2):
    """Confirmed pivots as (confirmed_index, kind, price, pivot_index).

    Equal highs/lows use the same leftmost-plateau tie rule as structure_features.
    """
    found=[]
    for i in range(2*radius,len(candles)):
        p=i-radius
        left, right = _swing_neighbors(candles, p, radius, i)
        if _is_swing_high(candles[p], left, right):
            found.append((i,'high',candles[p].high,p))
        if _is_swing_low(candles[p], left, right):
            found.append((i,'low',candles[p].low,p))
    return found


def _alternating(pivots):
    """Swing structure: consecutive same-side pivots collapse to the more extreme one."""
    kept=[]
    for _confirmed,kind,price,index in sorted(pivots,key=lambda row:(row[3],row[0])):
        if kept and kept[-1][0]==kind:
            if (price>kept[-1][1]) if kind=='high' else (price<kept[-1][1]):
                kept[-1]=(kind,price,index)
            continue
        kept.append((kind,price,index))
    return kept


def _swing_level(pivots, index, bullish):
    """Swing-structure level a close at ``index`` must clear for a swing CHoCH.

    Bullish: the last two swing lows made a lower low, and the level is the
    swing high before that lower low. Bearish mirrors it. None when the swing
    structure does not oppose the break, so the break is internal.
    """
    swings=_alternating([row for row in pivots if row[0]<index])
    same='low' if bullish else 'high'
    sides=[n for n,row in enumerate(swings) if row[0]==same]
    if len(sides)<2:
        return None
    last,prior=swings[sides[-1]],swings[sides[-2]]
    if not ((last[1]<prior[1]) if bullish else (last[1]>prior[1])):
        return None
    between=[row for row in swings[sides[-2]+1:sides[-1]] if row[0]!=same]
    return between[-1][1] if between else None


def ict_features(candles, features):
    """Operational ICT labels from closed candles, without trade-flow inference.

    Every CHoCH/MSS carries ``structure``. It is ``swing`` when the closing
    candle clears the opposing swing-structure level (alternating pivots,
    LL then a close above the lower high, or the mirror), and ``internal``
    otherwise.
    """
    by_time={c.timestamp:i for i,c in enumerate(candles)}
    pivots=_pivots(candles,features.get('swing_radius',2))
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
            level=_swing_level(pivots,index,bullish)
            swing=level is not None and (candle.close>level if bullish else candle.close<level)
            shifts.append(dict(br,kind='mss' if swept and displacement else 'choch',
                               structure='swing' if swing else 'internal',
                               **({'swing_level':level} if level is not None else {})))
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
