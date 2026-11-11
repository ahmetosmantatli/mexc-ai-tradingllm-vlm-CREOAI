"""Self-contained model context with compact tables and an application-owned card.

Every call contains a current baseline, never just a delta. Table encoding keeps
all supplied feature/closed OHLCV values exactly; audit metadata is deliberately
kept out of the prompt. The short setup card is a bounded summary, not an exact
transcript or a promise that old detail remains in a model's hidden memory.
"""
import hashlib
import json
from math import isfinite

POLICY_VERSION = 'pa-context-v4'
POSITION_FIELDS = ('symbol','side','entry','stop','target','quantity','leverage','risk',
                   'opened_at','initial_stop','current_r','mfe_r','mae_r','unrealized','margin','notional','stop_loss_estimate','stop_risk_pct')


def canonical(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False,default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def table(rows):
    """Represent dictionaries by columns without dropping fields or rounding."""
    if not rows or not all(isinstance(row,dict) for row in rows):
        return rows
    columns=list(dict.fromkeys(key for row in rows for key in row))
    # Missing is represented explicitly; present None has identical model meaning.
    return {'columns':columns,'rows':[[row.get(key) for key in columns] for row in rows]}


def compact_market(market):
    result={key:value for key,value in market.items() if key not in ('fingerprint','timeframes')}
    views=[]
    for view in market.get('timeframes',[]):
        features=view.get('features') or {}
        compact={key:value for key,value in view.items() if key not in ('features','fingerprint')}
        compact['features']={key:table(value) if isinstance(value,list) else value
                             for key,value in features.items() if key not in ('definition_version','caveat')}
        views.append(compact)
    if 'timeframes' in market:
        result['timeframes']=views
    return result


# Open-position management only needs a short wick window and a small structure tail.
MANAGEMENT_OHLCV_BARS = 8
MANAGEMENT_STRUCTURE_ROWS = 3
_MANAGEMENT_STRUCTURE_KEYS = ('swings', 'breaks', 'sweeps', 'fvgs')


def management_market(market):
    """Compact market packet for live position management.

    Entry prompts may carry long OHLCV and observation lists. Management only
    needs recent closed bars for wick references plus a short structure tail
    (trend, last swings, recent breaks). Padded history is dropped.
    """
    result = {key: value for key, value in (market or {}).items()
              if key not in ('fingerprint', 'timeframes')}
    views = []
    for view in market.get('timeframes', []) if isinstance(market, dict) else []:
        if not isinstance(view, dict):
            continue
        features = view.get('features') if isinstance(view.get('features'), dict) else {}
        compact = {key: value for key, value in view.items()
                   if key not in ('features', 'fingerprint', 'recent_ohlcv')}
        rows = [row for row in (view.get('recent_ohlcv') or [])
                if isinstance(row, (list, tuple)) and row]
        compact['recent_ohlcv'] = [list(row) for row in rows[-MANAGEMENT_OHLCV_BARS:]]
        compact['history_bars'] = len(compact['recent_ohlcv'])
        trimmed = {}
        for key, value in features.items():
            if key in ('definition_version', 'caveat'):
                continue
            if key in _MANAGEMENT_STRUCTURE_KEYS and isinstance(value, list):
                trimmed[key] = table(value[-MANAGEMENT_STRUCTURE_ROWS:])
            elif isinstance(value, list):
                trimmed[key] = table(value[-MANAGEMENT_STRUCTURE_ROWS:])
            else:
                trimmed[key] = value
        compact['features'] = trimmed
        views.append(compact)
    if isinstance(market, dict) and 'timeframes' in market:
        result['timeframes'] = views
    return result


def position_cards(positions):
    return [{key:position[key] for key in POSITION_FIELDS if key in position}
            for symbol,position in sorted(positions.items())]


def setup_card(memory):
    """Migrate a prior journal to a small explicit durable working summary."""
    card=dict(memory.get('setup_card') or {})
    history=memory.get('history') or []
    first=history[0] if history else {}
    last=history[-1] if history else {}
    card.setdefault('first_reason',first.get('rationale',''))
    card.setdefault('last_reason',last.get('rationale',''))
    card.setdefault('last_action',last.get('action'))
    card.setdefault('invalidation',last.get('invalidation',''))
    card['watch_note']=memory.get('watch_note',card.get('watch_note',''))
    card['last_outcome']=memory.get('last_outcome',card.get('last_outcome'))
    return card


def change_summary(previous, current, previous_fingerprint=None, fingerprint=None):
    before={v.get('timeframe'):v for v in (previous or {}).get('timeframes',[])}
    changed=[]
    for view in current.get('timeframes',[]):
        name=view.get('timeframe')
        old=before.get(name)
        if old is None:
            changed.append({'timeframe':name,'reason':'first_observation','closed_at':view.get('closed_at')})
        elif old.get('fingerprint')!=view.get('fingerprint'):
            old_features=old.get('features') or {}
            features=view.get('features') or {}
            kinds=[key for key in ('last_swing_high','last_swing_low','breaks','sweeps','fvgs','trend')
                   if old_features.get(key)!=features.get(key)]
            changed.append({'timeframe':name,'from_closed_at':old.get('closed_at'),
                            'closed_at':view.get('closed_at'),'changed_structure':kinds,
                            'new_closed_candles':True})
    reason='first_review' if not previous_fingerprint else 'closed_context_changed' if previous_fingerprint!=fingerprint else 'account_or_setup_changed'
    names=', '.join(str(row['timeframe']) for row in changed)
    summary=('Bu kurulum ilk kez değerlendiriliyor.' if reason=='first_review' else
             f'Kapanmış mum verisi değişti: {names or "güncel piyasa paketi"}. Önceki kurulum kartı korunuyor.' if reason=='closed_context_changed' else
             'Mumlar aynı; hesap riski, açık pozisyon veya kurulum yönü değişti.')
    return {'reason':reason,'summary':summary,'timeframes':changed}


def runner3_model_view(account, market):
    """Prompt copy for Runner 3.

    The model sees setups that already have a closed CHoCH at a block or a flow,
    and the candles those levels name, not the raw history. When no such setup
    exists, a watch can still be shown. The system prompt is not built here.
    """
    import copy

    from trading.runtime.runner3_policy import closed_choch_at_block_or_flow

    account = copy.deepcopy(account) if isinstance(account, dict) else {}
    market = copy.deepcopy(market) if isinstance(market, dict) else {}
    selection = account.get('setup_selection') if isinstance(account.get('setup_selection'), dict) else None
    options = list(selection.get('options') or []) if selection else []

    def _early(row):
        evidence = row.get('evidence') if isinstance(row, dict) and isinstance(row.get('evidence'), dict) else {}
        return row.get('state') == 'ready' and evidence.get('kind') == 'htf_zone_internal'

    early = [row for row in options if _early(row)]
    # The 1h weak-low card is the early entry. A 4h/15m CHoCH is not what this path waits for.
    ready = early or [row for row in options if closed_choch_at_block_or_flow(row)]
    shown = ready if ready else options
    if selection is not None:
        selection['options'] = shown
    wanted = {}

    def mark(timeframe, stamp):
        if not isinstance(timeframe, str) or isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            return
        wanted.setdefault(timeframe, set()).add(int(stamp))

    for row in shown:
        if not isinstance(row, dict):
            continue
        evidence = row.get('evidence') if isinstance(row.get('evidence'), dict) else {}
        shift = evidence.get('shift') if isinstance(evidence.get('shift'), dict) else {}
        zone = evidence.get('zone') if isinstance(evidence.get('zone'), dict) else {}
        mark(row.get('trigger_timeframe'), shift.get('at'))
        mark(zone.get('timeframe'), zone.get('open_time'))
        # Prefer the open-time stamp stored on the card; touch_at is the close clock.
        mark(row.get('trigger_timeframe'), evidence.get('touch_open_time') or evidence.get('touch_at'))
        for ref in (row.get('stop_reference'), row.get('target_reference')):
            if isinstance(ref, dict):
                mark(ref.get('timeframe'), ref.get('open_time'))
        for level in row.get('opposing_target_levels') or []:
            if isinstance(level, dict):
                mark(level.get('timeframe'), level.get('open_time'))
    frames = []
    total = 0
    for view in market.get('timeframes') or []:
        if not isinstance(view, dict):
            continue
        rows = [row for row in (view.get('recent_ohlcv') or []) if isinstance(row, (list, tuple)) and row]
        keep = wanted.get(view.get('timeframe')) or set()
        tail = {row[0] for row in rows[-4:]}
        chosen = []
        seen = set()
        for row in rows:
            stamp = row[0]
            if stamp in seen or (stamp not in keep and stamp not in tail):
                continue
            chosen.append(list(row))
            seen.add(stamp)
        if len(chosen) > 8:
            referenced = [row for row in chosen if row[0] in keep]
            latest = [row for row in chosen if row[0] not in keep][-4:]
            chosen = (referenced + latest)[:8]
        view = dict(view)
        view['recent_ohlcv'] = chosen
        view['history_bars'] = len(chosen)
        total += len(chosen)
        frames.append(view)
    market['timeframes'] = frames
    early_shown = [row for row in shown if isinstance(row, dict) and _early(row)]
    summary = dict(
        kind='algorithm_read' if early_shown and not any(closed_choch_at_block_or_flow(row) for row in shown if isinstance(row, dict)) else 'closed_choch_at_block_or_flow',
        setup_ids=[row.get('setup_id') for row in shown if isinstance(row, dict)],
        candle_count=total,
        prior_hold=isinstance(account.get('prior_hold'), dict))
    if early_shown:
        summary['read'] = early_shown[0].get('algorithm_read')
    market['summary'] = summary
    return account, market


def build_prompt(candidate, market, memory, positions, account, version, changes):
    data=candidate.model_dump(mode='json')
    data.pop('created_at',None)
    features=dict(data.get('features') or {})
    features.pop('dedup_key',None)
    data['features']=features
    card=setup_card(memory)
    prompt=dict(context_policy=POLICY_VERSION,
        policy=dict(self_contained=True,closed_ohlcv_columns=['UTC_seconds','open','high','low','close','volume'],
                    supplied_history='Latest12 closed OHLCV and up to12 observations per structure list per timeframe; full exchange history is not supplied.',
                    working_memory='Setup card summarizes previous decisions; full journal is server-side.',
                    evidence_caveat='Historical FVG formations are not asserted unfilled; patterns/scores are not calibrated probabilities.'),
        version=version,candidate=data,market=compact_market(market),account=account,
        setup_card=card,what_changed=changes,open_positions=position_cards(positions))
    return canonical(prompt)


COMPACT_SYSTEM = (
    'Return JSON only for this MEXC USDT perpetual. HOLD is a valid decision. '
    'Decide from the compact review. You may disagree with the prior clause; it is not an instruction. '
    'The algorithm read lists swings, the weak-low level, the trigger candle, and the candidate block. That read is not a fact you must obey. '
    'Use the lane, direction, swing points from the last candles, higher-timeframe bias, ATR, '
    'distance from price to the block or flow, funding when it is present, the closed CHoCH price, '
    'the stop swing, the next target, any farther with-trend target, and the three gates '
    '(1.25 R, 15% equity stop, free margin). '
    'The prior note is at most one short clause plus the numeric trigger. '
    'Raw candles are omitted and are not a hold reason. '
    'A block or flow trade still needs that closed CHoCH and a supplied target. '
    'The 1h weak swing with its 5m trigger does not need a 15m close. Touch alone is not an entry. '
    'If the decision data is missing or the call would be a guess, return hold. Do not invent a trade.'
)

PRIOR_CLAUSE_LIMIT = 80


def system_with_entry_gate(base, full_system):
    """Keep the Runner 3 cost gate when a short system prompt replaces the full one."""
    from trading.runtime.runner3_policy import ENTRY_GATE_BEGIN, ENTRY_GATE_END
    if not isinstance(full_system, str) or not isinstance(base, str):
        return base
    start = full_system.find(ENTRY_GATE_BEGIN)
    end = full_system.find(ENTRY_GATE_END, start if start >= 0 else 0)
    if start < 0 or end < start:
        return base
    clause = full_system[start:end + len(ENTRY_GATE_END)]
    if clause in base:
        return base
    return base + ' ' + clause


def _positive_price(value):
    number = _finite(value)
    if number is None or number <= 0:
        return None
    return number


def _lane_name(context_tf, trigger_tf):
    if context_tf in ('4h', '1h') and trigger_tf in ('15m', '5m'):
        return f'{context_tf}/{trigger_tf}'
    if isinstance(context_tf, str) and context_tf and isinstance(trigger_tf, str) and trigger_tf:
        return f'{context_tf}/{trigger_tf}'
    if isinstance(context_tf, str) and context_tf:
        return context_tf
    if isinstance(trigger_tf, str) and trigger_tf:
        return trigger_tf
    return None


def _choch_price(evidence, shift):
    if isinstance(shift, dict):
        price = _positive_price(shift.get('level'))
        if price is not None:
            return price
        nested = shift.get('context_last_shift')
        if isinstance(nested, dict):
            price = _positive_price(nested.get('level'))
            if price is not None:
                return price
    nested = evidence.get('context_last_shift') if isinstance(evidence, dict) else None
    if isinstance(nested, dict):
        return _positive_price(nested.get('level'))
    return None


def _candle_rows(rows):
    """Last 30 closed bars. Fewer than 20 are kept when that is all the packet has."""
    parsed = [row for row in rows or [] if isinstance(row, (list, tuple)) and len(row) >= 5]
    return parsed[-30:]


def _swing_points(rows, timeframe):
    """Pivot highs and lows from the recent window. Not open, high, low, close, or volume rows."""
    window = _candle_rows(rows)
    points = []
    radius = 2
    for index in range(radius, len(window) - radius):
        high = _finite(window[index][2])
        low = _finite(window[index][3])
        neighbors = window[index - radius:index] + window[index + 1:index + radius + 1]
        neighbor_high = [_finite(item[2]) for item in neighbors]
        neighbor_low = [_finite(item[3]) for item in neighbors]
        if high is not None and all(value is not None and high > value for value in neighbor_high):
            points.append(dict(timeframe=timeframe, kind='high', price=high))
        if low is not None and all(value is not None and low < value for value in neighbor_low):
            points.append(dict(timeframe=timeframe, kind='low', price=low))
    return points


def _window_atr(rows):
    window = _candle_rows(rows)
    if len(window) < 2:
        return None
    previous = _finite(window[0][4])
    total = 0.
    count = 0
    for row in window[1:]:
        high, low, close = _finite(row[2]), _finite(row[3]), _finite(row[4])
        if previous is None or None in (high, low, close):
            previous = close
            continue
        total += max(high - low, abs(high - previous), abs(low - previous))
        count += 1
        previous = close
    if not count:
        return None
    return total / count


def _frames(market):
    frames = {}
    for view in market.get('timeframes') or []:
        if isinstance(view, dict) and isinstance(view.get('timeframe'), str):
            frames[view['timeframe']] = view.get('recent_ohlcv') or []
    return frames


def _reference_price(data, frames, trigger):
    candidate = data.get('candidate') if isinstance(data.get('candidate'), dict) else {}
    price = _positive_price(candidate.get('entry_price'))
    if price is not None:
        return price
    rows = _candle_rows(frames.get(trigger) or [])
    if rows:
        return _positive_price(rows[-1][4])
    for rows in frames.values():
        window = _candle_rows(rows)
        if window:
            price = _positive_price(window[-1][4])
            if price is not None:
                return price
    return None


def _funding_rate(data, market, account, candidate):
    """Copy a funding rate only when the supplied feed already has one."""
    containers = [market, account, candidate]
    features = candidate.get('features') if isinstance(candidate, dict) else None
    if isinstance(features, dict):
        containers.append(features)
    ticker = market.get('ticker') if isinstance(market, dict) else None
    if isinstance(ticker, dict):
        containers.append(ticker)
    if isinstance(data, dict):
        containers.append(data)
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in ('funding_rate', 'fundingRate', 'funding', 'last_funding_rate'):
            if key not in container:
                continue
            value = container[key]
            if isinstance(value, dict):
                value = value.get('fundingRate', value.get('funding_rate', value.get('rate')))
            number = _finite(value)
            if number is not None:
                return number
    return None


def _short_clause(text):
    """One short clause. The rest of a hold paragraph is not carried forward."""
    if not isinstance(text, str):
        return None
    clause = ' '.join(text.split())
    if not clause:
        return None
    for index, char in enumerate(clause):
        if char in '.!?;':
            clause = clause[:index]
            break
    clause = clause.strip(' ,;:-')
    if len(clause) > PRIOR_CLAUSE_LIMIT:
        clipped = clause[:PRIOR_CLAUSE_LIMIT].rsplit(' ', 1)[0].strip(' ,;:-')
        clause = clipped or clause[:PRIOR_CLAUSE_LIMIT].strip()
    return clause or None


def _decision_setup(option, price):
    """One lane. Candles stay out; the caller supplies swing points and ATR."""
    evidence = option.get('evidence') if isinstance(option.get('evidence'), dict) else {}
    zone = evidence.get('zone') if isinstance(evidence.get('zone'), dict) else {}
    shift = evidence.get('shift') if isinstance(evidence.get('shift'), dict) else {}
    direction = option.get('side') if option.get('side') in ('long', 'short') else None
    kind = zone.get('kind') if zone.get('kind') in ('block', 'flow') else None
    low = _positive_price(zone.get('low'))
    high = _positive_price(zone.get('high'))
    block_price = (low if low is not None else high) if direction == 'short' else (high if high is not None else low)
    prices = []
    levels = option.get('opposing_target_levels')
    for level in levels if isinstance(levels, list) else []:
        if not isinstance(level, dict):
            continue
        level_price = _positive_price(level.get('price'))
        if level_price is not None and level_price not in prices:
            prices.append(level_price)
    if not prices:
        target = _positive_price(option.get('take_profit_price', option.get('target')))
        if target is not None:
            prices.append(target)
    bias = option.get('macro_trend')
    if bias not in ('bullish', 'bearish'):
        bias = zone.get('direction') if zone.get('direction') in ('bullish', 'bearish') else None
    card = {}
    lane = _lane_name(option.get('context_timeframe') or zone.get('timeframe'), option.get('trigger_timeframe'))
    if lane:
        card['lane'] = lane
    if direction:
        card['direction'] = direction
    choch = _choch_price(evidence, shift)
    if choch is not None:
        card['closed_choch_price'] = choch
    stop = _positive_price(option.get('stop_loss_price', option.get('stop')))
    if stop is not None:
        card['stop'] = stop
    if kind or block_price is not None:
        block = {}
        if kind:
            block['kind'] = kind
        if block_price is not None:
            block['price'] = block_price
        card['block_or_flow'] = block
        if price is not None and block_price is not None:
            card['distance_to_block'] = abs(price - block_price)
    if bias:
        card['higher_timeframe_bias'] = bias
    weak = _positive_price(evidence.get('weak_level'))
    if weak is not None:
        card['weak_level'] = weak
    distance = evidence.get('weak_distance_atr')
    if isinstance(distance, (int, float)) and not isinstance(distance, bool) and isfinite(distance) and distance >= 0:
        card['weak_distance_atr'] = distance
    trigger = evidence.get('trigger_candle')
    if isinstance(trigger, dict):
        card['trigger_candle'] = {key: trigger.get(key) for key in ('timeframe', 'open_time', 'open', 'high', 'low', 'close')}
    block = evidence.get('candidate_block')
    if isinstance(block, dict):
        card['candidate_block'] = {key: block.get(key) for key in ('timeframe', 'open_time', 'field', 'price', 'kind') if key in block}
    if isinstance(option.get('algorithm_read'), str):
        card['read'] = option['algorithm_read']
    if option.get('swing_highs'):
        card['highs'] = option['swing_highs']
    if option.get('swing_lows'):
        card['lows'] = option['swing_lows']
    if prices:
        card['next_target'] = prices[0]
        if evidence.get('alignment') != 'against_higher_timeframe' and len(prices) > 1:
            card['farther_with_trend_targets'] = prices[1:]
    return card or None


def compact_decision_packet(user):
    """Smaller user packet for an 8K TPM model. Not a restatement of the prior conclusion.

    Returns canonical JSON, or None when the text is not a decision packet.
    Keeps short fields: swing points from the last 20 to 30 candles, higher-timeframe
    bias, ATR, distance from price to the block or flow, funding when the feed
    already has it, the closed CHoCH price, the stop swing, the next target, any
    farther with-trend target, and the three gates. The prior hold is at most one
    short clause plus the numeric trigger that fired. Raw candles and repeated
    history are dropped. The full packet used for Gemini is never rewritten here.
    """
    if not isinstance(user, str) or not user.startswith('{'):
        return None
    try:
        data = json.loads(user)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    account = data.get('account') if isinstance(data.get('account'), dict) else {}
    market = data.get('market') if isinstance(data.get('market'), dict) else {}
    if data.get('context_policy') not in (POLICY_VERSION, 'pa-context-v4-compact') and not account and not market:
        return None
    from trading.runtime.runner3_policy import CONTINUOUS_STOP_RISK_CAP, MIN_NET_TARGET_R
    candidate = data.get('candidate') if isinstance(data.get('candidate'), dict) else {}
    frames = _frames(market)
    options = (account.get('setup_selection') or {}).get('options') if isinstance(account.get('setup_selection'), dict) else []
    parsed = []
    for option in options if isinstance(options, list) else []:
        if not isinstance(option, dict):
            continue
        trigger = option.get('trigger_timeframe')
        if not isinstance(trigger, str):
            zone = (option.get('evidence') or {}).get('zone') if isinstance(option.get('evidence'), dict) else {}
            trigger = zone.get('timeframe') if isinstance(zone, dict) else None
        trigger = trigger if isinstance(trigger, str) else None
        price = _reference_price(data, frames, trigger)
        card = _decision_setup(option, price)
        if card:
            parsed.append((option.get('state'), trigger, card))
    ready_states = [(trigger, card) for state, trigger, card in parsed if state == 'ready']
    chosen = ready_states or [(trigger, card) for _, trigger, card in parsed]
    setups = [card for _, card in chosen]
    triggers = []
    for trigger, _card in chosen:
        if trigger and trigger not in triggers:
            triggers.append(trigger)
    if not triggers:
        for name in ('15m', '5m', '1h', '4h'):
            if name in frames:
                triggers.append(name)
                break
    swings = []
    atr = None
    for timeframe in triggers:
        swings.extend(_swing_points(frames.get(timeframe), timeframe))
        if atr is None:
            atr = _window_atr(frames.get(timeframe))
    capital = account.get('capital_allocation') if isinstance(account.get('capital_allocation'), dict) else {}
    operating = account.get('operating_context') if isinstance(account.get('operating_context'), dict) else {}
    free_margin = capital.get('available_usdt', operating.get('available_usdt', account.get('available')))
    symbol = candidate.get('symbol') if isinstance(candidate.get('symbol'), str) else market.get('symbol')
    packet = dict(
        context_policy='pa-context-v4-compact',
        symbol=symbol if isinstance(symbol, str) else None,
        swings=swings,
        atr=atr,
        setups=setups,
        gates=dict(cost_adjusted_r=MIN_NET_TARGET_R, equity_stop=f'{CONTINUOUS_STOP_RISK_CAP:.0%}', free_margin=free_margin))
    funding = _funding_rate(data, market, account, candidate)
    if funding is not None:
        packet['funding'] = funding
    prior = account.get('prior_hold')
    if isinstance(prior, dict):
        clause = _short_clause(prior.get('rationale'))
        if clause:
            packet['prior_clause'] = clause
        trigger = _positive_price(prior.get('printed_trigger'))
        if trigger is not None:
            packet['trigger'] = trigger
    return canonical(packet)


def _finite(value):
    from math import isfinite
    if isinstance(value,bool):
        return None
    try:
        number=float(value)
        return number if isfinite(number) else None
    except (TypeError,ValueError,OverflowError):
        return None


def _timestamp(value,now):
    value=_finite(value)
    if value is None or value<=0:
        return None
    if value>=100_000_000_000:
        value/=1000
    return value if value<=now else None


def account_research_context(store,equity,settings=None,now=None):
    """Describe measured PAPER operation and an optional research objective.

    This never reads env seed values as a wallet, converts TRY, changes risk, or
    claims a target is attainable. Unknown legacy fields stay explicitly unknown.
    The deadline anchor is persisted in the same per-session ledger database.
    """
    import copy
    from datetime import datetime
    from time import time
    from zoneinfo import ZoneInfo
    from trading.runtime.reports import daily_trade_report,_net
    now=time() if now is None else float(now)
    with store.lock:
        state=copy.deepcopy(store.get('paper'))
        if not isinstance(state,dict):
            return dict(mode='PAPER',ledger_available=False,equity_snapshot_usdt=_finite(equity),
                objective={'enabled':False,'status':'paper_ledger_not_initialized'})
        currency=state.get('currency')
        if currency!='USDT':
            return dict(mode='PAPER',ledger_available=True,currency=currency,
                objective={'enabled':False,'status':'unsupported_or_unknown_ledger_currency'},
                data_note='No currency conversion or legacy seed reconstruction was performed.')
        current=_finite(equity)
        initial=_finite(state.get('initial'))
        positions=state.get('positions') or {}
        positions=positions if isinstance(positions,dict) else {}
        margins=[_finite(p.get('margin')) for p in positions.values() if isinstance(p,dict)]
        unrealized=[_finite(p.get('unrealized')) for p in positions.values() if isinstance(p,dict)]
        margin=sum(margins) if len(margins)==len(positions) and all(v is not None for v in margins) else None
        open_pnl=sum(unrealized) if len(unrealized)==len(positions) and all(v is not None for v in unrealized) else None
        peak=_finite(state.get('peak'))
        day_start=_finite(state.get('day_start'))
        day=datetime.fromtimestamp(now,ZoneInfo('Europe/Istanbul')).date().isoformat()
        report=daily_trade_report(state,day=day,max_rows=5)
        closed=[]
        seen=set()
        for index,row in enumerate(state.get('closed_positions') or []):
            if not isinstance(row,dict):
                continue
            identity=(row.get('symbol'),row.get('opened_at'),row.get('closed_at'))
            if identity[1] is None or identity[2] is None:
                identity=('legacy_index',index)
            if identity in seen:
                continue
            seen.add(identity)
            net,complete=_net(row)
            closed.append(dict(symbol=row.get('symbol'),side=row.get('side'),strategy=row.get('strategy'),
                reason=row.get('close_reason') or row.get('reason') or 'UNKNOWN',
                leverage=_finite(row.get('leverage')),closed_at=_timestamp(row.get('closed_at'),now),
                net_pnl_usdt=net,net_complete=complete))
        counts={reason:sum(row['reason']==reason for row in closed) for reason in ('TP','SL','MANUAL','LIQ')}
        counts['UNKNOWN']=sum(row['reason'] not in counts for row in closed)
        closed.sort(key=lambda row:row.get('closed_at') or 0,reverse=True)
        today_start_matches=state.get('day')==day
        cash=_finite(state.get('cash'))
        result=dict(schema='paper-account-v1',mode='PAPER',ledger_available=True,currency='USDT',
            balance_source='Persisted per-session paper ledger; not the real MEXC wallet and not a TRY conversion.',
            initial_usdt=initial,equity_snapshot_usdt=current,cash_usdt=cash,
            ledger_equity_usdt=cash+open_pnl if cash is not None and open_pnl is not None else None,
            open_margin_usdt=margin,available_usdt=max(0,current-margin) if current is not None and margin is not None else None,
            open_unrealized_usdt=open_pnl,realized_net_total_usdt=_finite(state.get('realized')),
            fees_total_usdt=_finite(state.get('fees')),funding_total_usdt=_finite(state.get('funding')),
            current_return_pct=(current/initial-1)*100 if current is not None and initial is not None and initial>0 else None,
            day=day,day_start_usdt=day_start if today_start_matches else None,
            daily_loss_pct=max(0,1-current/day_start) if current is not None and day_start is not None and day_start>0 and today_start_matches else None,
            drawdown_pct=max(0,1-current/peak) if current is not None and peak is not None and peak>0 else None,
            daily_equity_change_usdt=current-day_start if current is not None and day_start is not None and today_start_matches else None,
            daily_closed_realized_net_usdt=report['net_pnl'],daily_pnl_complete=report['pnl_complete'] and not report['unassigned_legacy_records'],
            daily_closed_trade_fees_usdt=report['closed_trade_entry_fees']+report['closed_trade_exit_fees'],
            daily_closed_trade_funding_usdt=report['closed_trade_funding'],
            open_position_count=len(positions),closed_position_count=len(closed),close_reason_counts=counts,
            recent_closed=closed[:5],objective={'enabled':False,'status':'not_configured'})
        if not getattr(settings,'research_goal_enabled',False):
            return result
        multiple=_finite(getattr(settings,'research_goal_multiple',14))
        days=_finite(getattr(settings,'research_goal_days',7))
        stretch=_finite(getattr(settings,'research_goal_stretch_multiple',200))
        if initial is None or initial<=0 or current is None or multiple is None or multiple<=1 or days is None or days<=0:
            result['objective']={'enabled':False,'status':'invalid_or_missing_research_basis'}
            return result
        anchor=store.get('research_objective_start')
        if not isinstance(anchor,dict) or _timestamp(anchor.get('started_at'),now) is None:
            candidates=[]
            created=_timestamp(state.get('created_at'),now)
            if created is not None:
                candidates.append((created,'paper_created_at'))
            for event in state.get('events') or []:
                if isinstance(event,dict):
                    stamp=_timestamp(event.get('event_at'),now)
                    if stamp is not None:
                        candidates.append((stamp,'earliest_recorded_paper_event'))
            for row in [*(state.get('closed_positions') or []),*positions.values()]:
                if isinstance(row,dict):
                    stamp=_timestamp(row.get('opened_at'),now)
                    if stamp is not None:
                        candidates.append((stamp,'earliest_recorded_position_open'))
            started,source=min(candidates) if candidates else (now,'first_goal_context_observation; original_session_start_unknown')
            anchor=dict(started_at=started,source=source,initial_usdt=initial)
            store.put('research_objective_start',anchor)
        reference=_finite(anchor.get('initial_usdt'))
        if reference is None or reference<=0:
            result['objective']={'enabled':False,'status':'invalid_persisted_research_basis'}
            return result
        started=anchor['started_at']
        deadline=started+days*86400
        target=reference*multiple
        result['objective']=dict(enabled=True,kind='paper_research_only',
            policy='Reporting objective only: never force entries, increase risk after losses, or override any risk limit to chase the target.',
            unit_note='USDT target is initial paper seed times configured multiple. No TRY/USDT conversion or guaranteed outcome.',
            reference_initial_usdt=reference,target_multiple=multiple,target_usdt=target,
            remaining_usdt=max(0,target-current),remaining_multiple=target/current if current>0 else None,
            progress_fraction=(current-reference)/(target-reference),
            started_at=started,start_source=anchor['source'],deadline_at=deadline,
            remaining_seconds=max(0,deadline-now),deadline_passed=now>=deadline,target_reached=current>=target)
        if stretch is not None and stretch>1:
            result['objective'].update(stretch_multiple=stretch,stretch_target_usdt=reference*stretch)
        return result


def economic_review_key(account,positions):
    """Project review triggers onto account events, not every quote movement.

    The model still receives the complete fresh snapshot. Mark-to-market equity,
    floating PnL/R/excursions and countdowns do not independently justify another
    model call on identical closed bars. Realized cash/costs, positions, stops,
    risk settings and objective policy changes do.
    """
    stable={key:value for key,value in account.items() if key not in ('equity','operating_context')}
    allocation=stable.get('capital_allocation')
    if isinstance(allocation, dict):
        # Floating equity and pnl must not schedule another review by themselves.
        stable['capital_allocation']={key:value for key,value in allocation.items()
                                      if key not in ('equity_usdt','available_usdt')}
        stable['capital_allocation']['open_positions']=[
            {key:value for key,value in row.items() if key!='unrealized_pnl'}
            for row in allocation.get('open_positions') or [] if isinstance(row, dict)]
    selection=stable.get('setup_selection')
    if isinstance(selection,dict):
        stable['setup_selection']={**selection,'options':[{key:row.get(key) for key in ('setup_id','family','side','state')} for row in selection.get('options',[])]}
    operating=account.get('operating_context') or {}
    if operating.get('mode')=='LIVE':
        ignored_live={'observed_at','generation','started_at','equity','current_equity','equity_snapshot_usdt',
            'available','available_usdt','position_margin','unrealized','open_unrealized_usdt','managed_capital',
            'current_return_pct','daily_loss_pct','drawdown_pct','daily_equity_change_usdt'}
        stable['operating_context']={key:value for key,value in operating.items() if key not in ignored_live}
        for name in ('risk_limits','leverage'):
            limits=stable['operating_context'].get(name)
            if isinstance(limits,dict):
                stable['operating_context'][name]={symbol:[{key:value for key,value in row.items() if key!='max_contracts'}
                    for row in rows] for symbol,rows in limits.items()}
        if not positions:
            stable['flat_equity']=account.get('equity')
        objective=stable['operating_context'].get('objective')
        if isinstance(objective,dict):
            stable['operating_context']['objective']={key:value for key,value in objective.items() if key not in
                ('remaining_usdt','remaining_multiple','progress_fraction','remaining_seconds','target_reached')}
    elif not operating.get('ledger_available'):
        # A caller without a persisted paper basis may be supplying actual new
        # capital. Do not silently suppress those externally observed changes.
        stable['equity']=account.get('equity')
    else:
        stable['operating_context']={key:value for key,value in operating.items() if key not in (
            'equity_snapshot_usdt','ledger_equity_usdt','available_usdt','open_unrealized_usdt',
            'current_return_pct','daily_loss_pct','drawdown_pct','daily_equity_change_usdt','objective')}
        objective=operating.get('objective') or {}
        stable['operating_context']['objective']={key:value for key,value in objective.items() if key not in (
            'remaining_usdt','remaining_multiple','progress_fraction','remaining_seconds','target_reached')}
    ignored=('unrealized','current_r','mfe_r','mae_r')
    position_basis=[{key:value for key,value in row.items() if key not in ignored} for row in position_cards(positions)]
    return dict(account=stable,positions=position_basis)


def canonical_watch_card(card):
    """Bounded cross-strategy plan, including revision for explicit supersession."""
    keys=('symbol','revision','status','side','plan','created_at','updated_at','triggered_at','reason','last_action','last_rationale','structure_event')
    result={key:card[key] for key in keys if key in card}
    candidate=card.get('candidate') or {}
    if candidate:
        result['candidate']={key:candidate[key] for key in ('symbol','side','strategy','entry_price','stop_loss_price','take_profit_price') if key in candidate}
    return result


def watch_review_key(card):
    """Changes in active plan/lifecycle trigger a review; HOLD revision alone does not."""
    result={key:card[key] for key in ('symbol','status','side','plan','created_at','reason','structure_event') if key in card}
    # Triggered lifecycle revisions are meaningful evidence. A HOLD may increment
    # the coordinator revision but must not manufacture a new model call.
    if card.get('status')=='triggered':
        result['trigger_at']=card.get('triggered_at')
    return result
