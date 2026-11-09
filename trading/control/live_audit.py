"""Read-only retrospective audit. No exchange calls, no LLM, no order writes."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from math import isfinite
from pathlib import Path
import re
import sqlite3
from time import time
from zoneinfo import ZoneInfo


def _number(value):
    return type(value) in (int,float) and isfinite(value)


def performance(traces,intents):
    """Compare observed cohorts without treating missing or unresolved data as losses."""
    groups=defaultdict(lambda:([],[]))
    for trace in traces:
        key=(trace.get('runner_id') or 'baseline-v1',trace.get('strategy_cohort') or 'legacy_unversioned',
             trace.get('analysis_version') or 'unknown')
        groups[key][0].append(trace)
    for intent in intents:
        metadata=intent.get('proposal',{}).get('metadata') or {}
        key=(metadata.get('runner_id') or 'baseline-v1',metadata.get('strategy_cohort') or 'legacy_unversioned',
             metadata.get('analysis_version') or 'unknown')
        groups[key][1].append(intent)

    def mean(values):
        return sum(values)/len(values) if values else None

    def fills(rows):
        attempted=[r for r in rows if r.get('entry_write_attempted')]
        filled=[r for r in attempted if r.get('entry_fill_price') and r.get('position_id')]
        rejected=[r for r in attempted if r.get('status')=='entry_rejected' and r not in filled]
        resolved=len(filled)+len(rejected)
        closed=[r for r in filled if r.get('status')=='closed']
        known=[r for r in closed if r.get('realized_complete') and _number(r.get('net_pnl'))]
        pnl=[r['net_pnl'] for r in known]
        risk_pairs=[(r['net_pnl'],(r.get('entry_metrics') or {}).get('initial_stop_risk_usdt')) for r in known]
        returns=[net/risk for net,risk in risk_pairs if _number(risk) and risk>0]
        slip=[]
        for r in filled:
            quote=r.get('entry_executable_quote')
            price=r.get('entry_fill_price')
            side=r['proposal'].get('side')
            if _number(quote) and quote>0 and _number(price) and side in ('long','short'):
                slip.append((price/quote-1)*10000*(1 if side=='long' else -1))
        return dict(intent_records=len(rows),entry_attempts=len(attempted),confirmed_fills=len(filled),
            rejected_or_unfilled_attempts=len(rejected),unresolved_attempts=len(attempted)-resolved,
            fill_rate_resolved_attempts=len(filled)/resolved if resolved else None,
            closed=len(closed),closed_with_net=len(known),wins=sum(v>0 for v in pnl),losses=sum(v<0 for v in pnl),
            flat=sum(v==0 for v in pnl),win_rate=sum(v>0 for v in pnl)/len(pnl) if pnl else None,
            net_pnl_usdt=sum(pnl) if pnl else None,net_r_samples=len(returns),mean_net_r=mean(returns),
            entry_slippage_samples=len(slip),mean_adverse_entry_slippage_bps=mean(slip),
            emergency_closes=sum(bool(r.get('emergency_close_attempted')) for r in closed))

    result=[]
    for (runner,cohort,variant),(reviews,rows) in sorted(groups.items()):
        horizons=defaultdict(list)
        grades=defaultdict(list)
        segments=defaultdict(list)
        for row in rows:
            metadata=row.get('proposal',{}).get('metadata') or {}
            horizons[(metadata.get('trade_horizon') or {}).get('style') or 'unknown'].append(row)
            grade=(metadata.get('runner3_allocation') or {}).get('grade')
            if grade:
                grades[grade].append(row)
                allocation=metadata['runner3_allocation']
                segment=(metadata.get('setup_family'), allocation.get('horizon'), allocation.get('macro_trend'), grade)
                if all(segment):
                    segments[segment].append(row)
        attempts=[a for t in reviews for a in t.get('attempts',[]) if a.get('generation_requests',0)]
        provider_calls=sum(a['generation_requests'] for a in attempts)
        tokens=[(a.get('input_tokens'),a.get('output_tokens')) for a in attempts]
        known_tokens=[i+o for i,o in tokens if _number(i) and _number(o)]
        costs=[a['estimated_cost_usd'] for a in attempts if _number(a.get('estimated_cost_usd'))]
        generation_reviews=sum(any(a.get('generation_requests',0) for a in t.get('attempts',[])) for t in reviews)
        result.append(dict(runner=runner,cohort=cohort,variant=variant,recorded_reviews=len(reviews),
            reviews_with_generation=generation_reviews,generation_requests=provider_calls,
            model_trade_proposals=sum(t.get('model_action',t.get('action'))=='trade' for t in reviews),
            generation_requests_by_model=dict(Counter(
                f"{a.get('provider','unknown')}/{a.get('model','unknown')}" for a in attempts)),
            token_samples=len(known_tokens),mean_tokens=mean(known_tokens),
            cost_samples=len(costs),known_estimated_ai_cost_usd=sum(costs) if costs else None,
            mean_generation_latency_ms=mean([a['latency_ms'] for a in attempts if _number(a.get('latency_ms'))]),
            **fills(rows),by_opening_horizon={h:fills(batch) for h,batch in sorted(horizons.items())},
            by_evidence_grade={grade:fills(batch) for grade,batch in sorted(grades.items())},
            by_evidence_segment=[dict(setup_family=family,horizon=horizon,macro_trend=trend,grade=grade,**fills(batch))
                                 for (family,horizon,trend,grade),batch in sorted(segments.items())]))
    return result


def audit(path,*,since=0,now=None,include_performance=False):
    now=time() if now is None else now
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        db.execute('BEGIN')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        traces={}
        if 'ai_usage_events' in tables:
            traces={ident:json.loads(value) for ident,value in db.execute('SELECT id,payload FROM ai_usage_events')}
        def state(key,default=None):
            row=db.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
            return json.loads(row[0]) if row else default
        for row in state('analysis_history',[]):traces.setdefault(row['id'],row)
        latest=state('analysis_trace',{})
        if latest.get('id'):traces[latest['id']]=latest
        traces=[t for t in traces.values() if t.get('started_at',0)>=since and t.get('strategy')!='position_management']
        decisions=Counter(t.get('model_action',t.get('action','unknown')) for t in traces if t.get('model_action') or t.get('status')=='completed')
        outcomes=Counter(t.get('status','unknown') for t in traces)
        proposals=[t for t in traces if t.get('model_action',t.get('action'))=='trade']
        rejections=Counter((t.get('execution_reason') or t.get('status','unknown')) for t in proposals if t.get('execution_status') in ('rejected','not_opened'))
        unresolved=[t for t in proposals if t.get('execution_status') not in ('opened','rejected','not_opened')]
        account=next((json.loads(value) for value, in db.execute("SELECT value FROM state WHERE key LIKE 'account:%:live_snapshot'")),{})
        binding=account.get('binding_id')
        intents=[json.loads(value) for value, in db.execute("SELECT value FROM state WHERE key LIKE 'live:intent:%'")]
        intents=[row for row in intents if row.get('staged_at',0)>=since and (not binding or row.get('binding_id')==binding)]
        errors=[json.loads(value) for value, in db.execute("SELECT value FROM state WHERE key LIKE 'live_candidate_error:%'")]
        errors=[row for row in errors if row.get('at',0)>=since]
        detail=[]
        for t in sorted(proposals,key=lambda t:t.get('started_at',0),reverse=True)[:10]:
            detail.append({key:t.get(key) for key in ('id','symbol','started_at','execution_status','execution_reason','execution_details')})
        watches=Counter(json.loads(value).get('status','unknown') for value, in db.execute("SELECT value FROM state WHERE key LIKE 'watch:%'"))
        grant=next((json.loads(value) for value, in db.execute("SELECT value FROM state WHERE key LIKE 'live:automatic_grant:%'")),{})
        report=dict(evaluations=len(traces),model_decisions=dict(decisions),analysis_outcomes=dict(outcomes),
            model_trade_proposals=len(proposals),execution_rejections=dict(rejections),unresolved_proposal_records=len(unresolved),
            intent_records=len(intents),entry_attempt_records=sum(bool(r.get('entry_write_attempted')) for r in intents),
            entry_fill_records=sum(bool(r.get('entry_fill_price') and r.get('position_id')) for r in intents),
            intent_states=dict(Counter(r.get('status','unknown') for r in intents)),watch_states=dict(watches),
            latest_trade_proposals=detail,candidate_error_types=dict(Counter(r.get('code',r.get('type','unknown')) for r in errors)),
            snapshot_age_seconds=max(0,now-account.get('observed_at',0)) if account.get('observed_at') else None,
            start_grant_present=bool(grant),start_grant_revoked=grant.get('revoked'),start_grant_expired=grant.get('expires_at',0)<=now,
            halt=bool(state('live:halt')),last_entry_reason=state('live_entry_status',{}).get('reason'),
            limitations='Persisted records only, not a fresh exchange check. Entry attempt record is not proof of a POST/fill. Old unfinalized traces cannot establish a rejection reason.')
        if include_performance:
            report['performance']=performance(traces,intents)
        return report
    finally:db.close()


def format_report(report):
    text='LIVE işlem açmama denetimi · salt okunur\n'
    for label,key in [('Kayıtlı giriş analizi','evaluations'),('Doğrulanmış model kararları','model_decisions'),
            ('Analiz sonuçları','analysis_outcomes'),('Modelin işlem önerisi','model_trade_proposals'),
            ('Kaydedilmiş yürütme redleri','execution_rejections'),('Sonucu tamamlanmamış öneri kaydı','unresolved_proposal_records'),
            ('Emir denemesi kaydı (dolum değildir)','entry_attempt_records'),('Doğrulanmış giriş dolumu kaydı','entry_fill_records'),
            ('Emir yaşam döngüsü','intent_states'),('Takip durumları','watch_states'),('Son aday hata türleri','candidate_error_types'),
            ('Başlat izni süresi dolmuş','start_grant_expired'),('Başlat izni iptal edilmiş','start_grant_revoked'),('Canlı engel kaydı','halt')]:
        text+=f'{label}: {report[key]}\n'
    for row in report['latest_trade_proposals']:
        stamp=datetime.fromtimestamp(row['started_at'],ZoneInfo('Europe/Istanbul')).strftime('%d.%m %H:%M:%S')
        text+=f"{stamp} | {row['symbol']} | {row['execution_status']} | {row['execution_reason']} | {row.get('execution_details')}\n"
    if report['model_trade_proposals']==0:text+='Bu aralıkta kayıtlı işlem önerisi yok; emir yürütme yolunun çalışıp çalışmadığı bu gözlemle kanıtlanamaz.\n'
    text+='Yalnız kaydedilmiş veriler incelendi; borsaya yeni sorgu gönderilmedi. Emir denemesi kaydı, emrin iletildiğini veya dolduğunu kanıtlamaz. Eski sonuçlanmamış önerilerin ret nedeni bu kayıtlardan çıkarılamaz.\n'
    return text


def main():
    from trading.config import get_settings
    parser=argparse.ArgumentParser()
    parser.add_argument('--account',required=True);parser.add_argument('--hours',type=float,default=12)
    parser.add_argument('--performance',action='store_true',help='JSON with observed live performance cohorts')
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',args.account) or not 0<args.hours<=720:parser.error('invalid account/hours')
    path=Path(get_settings().runtime_db_path).parent/args.account/'live'/'runtime.sqlite3'
    if not path.exists():parser.error('live runtime database not found')
    report=audit(path,since=time()-args.hours*3600,include_performance=args.performance)
    if args.performance:
        report['performance_note']=('Giriş analizi ve emir kaydı başlangıç zamanına göre gruplandı. '
            'Eksik sonuç ve maliyet sıfır sayılmaz. Eski grup farklı sürümler içerebilir. '
            'Gözlemsel karşılaştırma, eşleştirilmiş A/B testi değildir.')
        print(json.dumps(report,ensure_ascii=False,indent=2))
    else:
        print(format_report(report))


if __name__=='__main__':main()
