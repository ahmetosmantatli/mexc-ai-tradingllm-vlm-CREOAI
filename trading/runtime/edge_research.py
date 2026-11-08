"""Immutable opportunity records and matched initial-plan OHLCV replay.

Research only. No broker calls. This isolates selection/initial-price planning,
not AI trailing, funding, liquidation or achievable portfolio profitability.
"""
import hashlib
import json
import math


def record_review(store,trace,candidate,planned,settings):
    data=candidate.model_dump(mode='json')
    opportunity=hashlib.sha256(json.dumps([data['symbol'],data['strategy'],data['side'],
        data['features'].get('source_open_at'),trace.get('market_fingerprint'),settings.analysis_version],sort_keys=True).encode()).hexdigest()
    row=dict(id=trace['id'],opportunity=opportunity,at=trace['completed_at'],symbol=candidate.symbol,
        action=trace['action'],variant=settings.analysis_version,strategy=candidate.strategy.value,
        baseline=data,model_plan=planned.model_dump(mode='json') if trace['action']=='trade' else None,
        fee_rate=settings.paper_fee_rate,slippage_bps=settings.paper_slippage_bps,
        observed_spread_bps=data['features'].get('observed_spread_bps'),funding_known=False,
        execution_status=trace.get('execution_status'),is_shadow=True)
    with store.lock,store.db:
        store.db.execute('CREATE TABLE IF NOT EXISTS edge_reviews (id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        store.db.execute('INSERT OR IGNORE INTO edge_reviews VALUES (?,?)',(row['id'],json.dumps(row,allow_nan=False)))


def replay(plan,bars,*,decision_at,horizon_bars,fee_rate,slippage_bps,spread_bps,now):
    """Matched next-full-5m-bar entry, stop-first collision, explicit gap costs.

    Quotes at decision time cannot reconstruct intrabar fills. Both variants
    therefore use the same next-bar open convention, never a hidden instant fill.
    """
    if spread_bps is None:return dict(status='missing_spread')
    if not all(isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) and x>=0
               for x in (fee_rate,slippage_bps,spread_bps)):return dict(status='invalid_costs')
    expected=(int(decision_at)//300+1)*300
    if any(not isinstance(r,(list,tuple)) or len(r)<6 or not isinstance(r[0],(int,float)) or not math.isfinite(r[0]) for r in bars):
        return dict(status='invalid_or_missing_bars')
    rows=[r for r in bars if r[0]>=expected and r[0]+300<=now]
    if not rows or rows[0][0]!=expected:return dict(status='missing_entry_bar')
    rows=rows[:horizon_bars]
    for i,row in enumerate(rows):
        if (len(row)<6 or row[0]!=expected+i*300 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in row[:6])
                or min(row[1:5])<=0 or row[2]<max(row[1],row[4]) or row[3]>min(row[1],row[4])):
            return dict(status='invalid_or_missing_bars')
    long=plan['side']=='long';stop=plan['stop_loss_price'];target=plan['take_profit_price']
    raw_entry=rows[0][1]
    if abs(raw_entry/plan['entry_price']-1)>.005:return dict(status='entry_drift')
    if not (stop<raw_entry<target if long else target<raw_entry<stop):return dict(status='invalid_entry_levels')
    cost=slippage_bps/10000+spread_bps/20000
    entry=raw_entry*(1+cost if long else 1-cost)
    initial_risk=abs(raw_entry-stop)
    chosen=None;ambiguous=False
    for row in rows:
        op,high,low=row[1:4]
        hit_stop=low<=stop if long else high>=stop
        hit_target=high>=target if long else low<=target
        if hit_stop:
            chosen=min(op,stop) if long else max(op,stop)
            reason='SL';ambiguous=hit_target;break
        if hit_target:
            chosen=target;reason='TP';break
    if chosen is None:
        if len(rows)<horizon_bars:return dict(status='pending',observed_bars=len(rows))
        chosen=rows[-1][4];reason='HORIZON'
    exit_price=chosen*(1-cost if long else 1+cost)
    pnl=(exit_price-entry if long else entry-exit_price)-fee_rate*(entry+exit_price)
    return dict(status='completed',r_after_estimated_costs_ex_funding=pnl/initial_risk,
        exit_reason=reason,ambiguous_intrabar=ambiguous,funding_included=False,
        entry_convention='next_complete_5m_open',cost_model='configured fees/slippage; decision spread held constant')


def compare(rows,bars_by_symbol,*,now,horizon_bars=48):
    results=[];seen=set()
    for row in sorted(rows,key=lambda r:(r['at'],r['id'])):
        if row['opportunity'] in seen:continue
        seen.add(row['opportunity'])
        if row['strategy']=='watch_review':
            results.append(dict(id=row['id'],status='watch_review_no_quant_baseline'));continue
        kwargs=dict(decision_at=row['at'],horizon_bars=horizon_bars,fee_rate=row['fee_rate'],
            slippage_bps=row['slippage_bps'],spread_bps=row['observed_spread_bps'],now=now)
        baseline=replay(row['baseline'],bars_by_symbol.get(row['symbol'],[]),**kwargs)
        model=replay(row['model_plan'],bars_by_symbol.get(row['symbol'],[]),**kwargs) if row['action']=='trade' else dict(status='no_entry',r_after_estimated_costs_ex_funding=0)
        complete=baseline['status']=='completed' and model['status'] in ('completed','no_entry')
        results.append(dict(id=row['id'],symbol=row['symbol'],action=row['action'],execution_status=row.get('execution_status'),status='matched' if complete else 'incomplete',
            quant=baseline,model=model,delta_r=(model['r_after_estimated_costs_ex_funding']-baseline['r_after_estimated_costs_ex_funding']) if complete else None))
    matched=[r for r in results if r['status']=='matched']
    return dict(unique_opportunities=len(seen),matched=len(matched),
        mean_delta_r=sum(r['delta_r'] for r in matched)/len(matched) if matched else None,results=results,
        limitations='Shadow initial-plan comparison only; not actual fills, trailing management, funding, liquidation, AI dollar costs or portfolio returns. Overlapping opportunities are not independent. Missing data is not zero. No edge claim.')
