"""Self-contained model context with compact tables and an application-owned card.

Every call contains a current baseline, never just a delta. Table encoding keeps
all supplied feature/closed OHLCV values exactly; audit metadata is deliberately
kept out of the prompt. The short setup card is a bounded summary, not an exact
transcript or a promise that old detail remains in a model's hidden memory.
"""
import hashlib
import json

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
    'A trade needs the supplied zone, a closed touch, and a later closed CHoCH or MSS. Touch alone is not an entry. '
    'Use the text decision why, the BTC and ETH summary, equity, free margin, open positions, and the confirming timeframe closed candles. '
    'If those are missing or the call would be a guess, return hold. Do not invent a trade.'
)


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


def _summary_frame(frame):
    if not isinstance(frame, dict):
        return frame
    summary = {key: frame.get(key) for key in ('closed_at', 'trend', 'return_6_bars', 'relative_return_6_bars', 'last_break')}
    shifts = frame.get('structure_shifts') or []
    if shifts:
        summary['last_shift'] = shifts[-1]
    return summary


def compact_decision_packet(user):
    """Smaller user packet for an 8K TPM model. Required decision fields stay intact.

    Returns canonical JSON, or None when the text is not a decision packet.
    Callers must not delete zone, touch, CHoCH/MSS, why, BTC/ETH, equity,
    free margin, open positions, or the confirming candles to force a fit.
    The full packet used for Gemini is never rewritten here.
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
    options = (account.get('setup_selection') or {}).get('options') if isinstance(account.get('setup_selection'), dict) else []
    kept = []
    confirm = set()
    for option in options if isinstance(options, list) else []:
        if not isinstance(option, dict):
            continue
        evidence = option.get('evidence') if isinstance(option.get('evidence'), dict) else {}
        shift = evidence.get('shift')
        if shift is None:
            shift = dict(kind=evidence.get('kind'), confirmation_at=evidence.get('confirmation_at'),
                         context_last_shift=evidence.get('context_last_shift'))
        trigger = option.get('trigger_timeframe')
        if trigger:
            confirm.add(trigger)
        kept.append(dict(setup_id=option.get('setup_id'), state=option.get('state'), side=option.get('side'),
                         horizon=option.get('horizon'), context_timeframe=option.get('context_timeframe'),
                         trigger_timeframe=trigger, zone=evidence.get('zone'), touch_at=evidence.get('touch_at'),
                         closed_choch_mss=shift, stop=option.get('stop_loss_price', option.get('stop')),
                         target=option.get('take_profit_price', option.get('target')),
                         reason=option.get('caveat') or evidence.get('kind')))
    if not confirm:
        confirm = {'15m', '5m'}
    candles = []
    for view in market.get('timeframes') or []:
        if isinstance(view, dict) and view.get('timeframe') in confirm:
            candles.append(dict(timeframe=view.get('timeframe'), closed_at=view.get('closed_at'),
                                recent_ohlcv=view.get('recent_ohlcv')))
    raw_bench = market.get('benchmarks') if isinstance(market.get('benchmarks'), dict) else {}
    benchmarks = {}
    for symbol in ('BTC_USDT', 'ETH_USDT'):
        frames = raw_bench.get(symbol)
        if isinstance(frames, dict):
            benchmarks[symbol] = {name: _summary_frame(frame) for name, frame in frames.items()}
        else:
            benchmarks[symbol] = frames
    text = account.get('text_model_decision') if isinstance(account.get('text_model_decision'), dict) else {}
    card = data.get('setup_card') if isinstance(data.get('setup_card'), dict) else {}
    changed = data.get('what_changed') if isinstance(data.get('what_changed'), dict) else {}
    why = text.get('rationale') or card.get('last_reason') or changed.get('summary')
    capital = account.get('capital_allocation') if isinstance(account.get('capital_allocation'), dict) else {}
    operating = account.get('operating_context') if isinstance(account.get('operating_context'), dict) else {}
    equity = capital.get('equity_usdt', account.get('equity', operating.get('equity_snapshot_usdt')))
    free_margin = capital.get('available_usdt', operating.get('available_usdt', account.get('available')))
    positions = data.get('open_positions')
    if positions is None:
        positions = capital.get('open_positions', operating.get('open_positions'))
    packet = dict(context_policy='pa-context-v4-compact',
                  symbol=(data.get('candidate') or {}).get('symbol') if isinstance(data.get('candidate'), dict) else market.get('symbol'),
                  zone=[row.get('zone') for row in kept], touch=[row.get('touch_at') for row in kept],
                  closed_choch_mss=[row.get('closed_choch_mss') for row in kept], why=why,
                  text_decision_why=text.get('rationale'), benchmarks=benchmarks, equity=equity,
                  free_margin=free_margin, open_positions=positions if positions is not None else [],
                  confirming_candles=candles, setups=kept)
    gates = account.get('runner3_entry_gate')
    if isinstance(gates, str) and gates.strip():
        packet['entry_gates'] = gates
    if not isinstance(packet['symbol'], str):
        packet['symbol'] = market.get('symbol')
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
