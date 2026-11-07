"""Self-contained model context with compact tables and an application-owned card.

Every call contains a current baseline, never just a delta. Table encoding keeps
all supplied feature/closed OHLCV values exactly; audit metadata is deliberately
kept out of the prompt. The short setup card is a bounded summary, not an exact
transcript or a promise that old detail remains in a model's hidden memory.
"""
import hashlib
import json

POLICY_VERSION = 'pa-context-v2'
POSITION_FIELDS = ('symbol','side','entry','stop','target','quantity','leverage','risk',
                   'opened_at','initial_stop','current_r','mfe_r','mae_r','unrealized')


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
