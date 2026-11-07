"""Experimental paper-only closed-candle structure trailing.

No LLM calls, order requests, timers that force exits, partial takes or promises
of ICT edge. Policy thresholds are research parameters, not optimized results.
Every proposed change needs a fresh executable-quote/identity check in ledger.
"""
from math import isfinite
from time import time

from trading.analysis.market import closed_candles

POLICY_VERSION = 'paper-structure-trail-v1'
SWING_RADIUS = 2
ATR_WINDOW = 14
ATR_BUFFER_MULTIPLE = .25
MIN_CONFIRMED_ADVANCE_R = 1.0


def _basis(position):
    entry = float(position['entry'])
    initial_stop = float(position['initial_stop'])
    if not all(isfinite(v) and v > 0 for v in (entry, initial_stop)):
        raise ValueError('invalid original risk basis')
    long = position['side'] == 'long'
    if position['side'] not in ('long', 'short') or (initial_stop >= entry if long else initial_stop <= entry):
        raise ValueError('invalid original stop direction')
    distance = abs(entry-initial_stop)
    explicit = position.get('initial_risk_price')
    if explicit is not None and (not isfinite(float(explicit)) or abs(float(explicit)-distance) > max(1e-10, distance*1e-8)):
        raise ValueError('original risk basis mismatch')
    return entry, distance, long


def measure_excursions(position, bid, ask):
    """Return observed gross R metrics without mutation or candle hindsight.

    R is *price distance* from entry to immutable initial_stop, not leverage ROI,
    margin percent or dollars at risk. Fees/funding remain separate PnL fields.
    Observed MFE/MAE retain only prices actually sampled by the caller.
    """
    entry, distance, long = _basis(position)
    bid, ask = float(bid), float(ask)
    if not all(isfinite(v) and v > 0 for v in (bid, ask)) or ask < bid:
        raise ValueError('invalid executable quote')
    price = bid if long else ask
    current_r = (price-entry)/distance*(1 if long else -1)
    previous_mfe = float(position.get('mfe_r', 0))
    previous_mae = float(position.get('mae_r', 0))
    if not all(isfinite(v) and v >= 0 for v in (previous_mfe, previous_mae)):
        raise ValueError('invalid excursion history')
    mfe = max(previous_mfe, current_r, 0.)
    mae = max(previous_mae, -current_r, 0.)
    return dict(current_r=current_r, mfe_r=mfe, mae_r=mae,
                max_favorable_price=entry+(1 if long else -1)*mfe*distance,
                max_adverse_price=entry-(1 if long else -1)*mae*distance,
                r_basis='gross_price_distance_to_initial_stop')


def propose_management(position, candles, timeframe_seconds=900, now=None):
    """Propose tighter stop after >=1R closed advance and confirmed structure.

    Two strict post-entry low pivots form a higher low for longs; the symmetric
    lower-high sequence is required for shorts. Each pivot requires two closed
    candles on its right. Candidate is latest pivot minus/plus .25 x ATR14.
    Initial 1R, ATR buffer and pivot radius are explicit experimental constants.
    Caller persists initial_stop before any modification and revalidates price,
    opened_at, stop monotonicity and evidence freshness immediately on apply.
    """
    now = time() if now is None else float(now)
    base = dict(action='hold', policy_version=POLICY_VERSION, research_only=True)
    try:
        entry, distance, long = _basis(position)
        stop = float(position['stop'])
        opened_at = float(position['opened_at'])
        if not isfinite(stop) or stop <= 0 or not isfinite(opened_at) or opened_at < 0:
            raise ValueError('invalid position')
        tf = float(timeframe_seconds)
        if not isfinite(tf) or tf <= 0 or tf != int(tf):
            raise ValueError('invalid timeframe')
        expected_tf = position.get('entry_timeframe_seconds', int(tf))
        if int(expected_tf) != int(tf):
            raise ValueError('entry timeframe mismatch')
        closed = closed_candles(candles, int(tf), now, min_count=ATR_WINDOW+1)
    except (ValueError, TypeError, KeyError, OverflowError):
        return {**base, 'reason':'invalid_or_incomplete_management_data'}
    opened_seconds = opened_at/1000
    if opened_seconds > now:
        return {**base, 'reason':'position_open_time_in_future'}
    latest = closed[-1]
    advance = (latest.close-entry)/distance*(1 if long else -1)
    if advance < MIN_CONFIRMED_ADVANCE_R:
        return {**base, 'reason':'await_confirmed_r_advance'}
    pivots = []
    radius = SWING_RADIUS
    for i in range(radius, len(closed)-radius):
        pivot = closed[i]
        # Bars which started before entry can contain pre-entry extrema.
        if pivot.timestamp < opened_seconds:
            continue
        neighbors = closed[i-radius:i]+closed[i+1:i+radius+1]
        strict = all(pivot.low < bar.low for bar in neighbors) if long else all(pivot.high > bar.high for bar in neighbors)
        if strict:
            pivots.append((pivot, closed[i+radius].timestamp+int(tf)))
    if len(pivots) < 2:
        return {**base, 'reason':'await_post_entry_structure'}
    previous, _ = pivots[-2]
    pivot, evidence_at = pivots[-1]
    structural_progress = pivot.low > previous.low if long else pivot.high < previous.high
    if not structural_progress:
        return {**base, 'reason':'no_higher_low_or_lower_high'}
    previous_evidence = position.get('last_management_evidence_at', 0)
    try:
        if not isfinite(float(previous_evidence)):
            raise ValueError()
        if evidence_at <= float(previous_evidence):
            return {**base, 'reason':'structure_already_processed'}
    except (TypeError, ValueError):
        return {**base, 'reason':'invalid_previous_evidence'}
    true_ranges = []
    for before, bar in zip(closed[:-1], closed[1:]):
        true_ranges.append(max(bar.high-bar.low, abs(bar.high-before.close), abs(bar.low-before.close)))
    atr = sum(true_ranges[-ATR_WINDOW:])/ATR_WINDOW
    new_stop = pivot.low-atr*ATR_BUFFER_MULTIPLE if long else pivot.high+atr*ATR_BUFFER_MULTIPLE
    # Every stop proposal remains on the valid side of the latest close. The
    # executable current bid/ask may differ and must be checked by the caller.
    if not isfinite(new_stop) or new_stop <= 0 or not (stop < new_stop < latest.close if long else latest.close < new_stop < stop):
        return {**base, 'reason':'candidate_does_not_tighten_valid_stop'}
    return dict(action='tighten_stop', new_stop=new_stop,
                reason='confirmed_higher_low_atr_buffer' if long else 'confirmed_lower_high_atr_buffer',
                evidence_at=evidence_at, evidence_candle_at=pivot.timestamp,
                last_closed_at=latest.timestamp+int(tf), position_opened_at=int(opened_at),
                initial_risk_price=distance, confirmed_advance_r=advance, atr=atr,
                timeframe_seconds=int(tf), policy_version=POLICY_VERSION,
                research_only=True)
