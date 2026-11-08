"""Read-only retrospective audit. No exchange calls, no LLM, no order writes."""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
from time import time
from zoneinfo import ZoneInfo


def audit(path,*,since=0,now=None):
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
        return dict(evaluations=len(traces),model_decisions=dict(decisions),analysis_outcomes=dict(outcomes),
            model_trade_proposals=len(proposals),execution_rejections=dict(rejections),unresolved_proposal_records=len(unresolved),
            intent_records=len(intents),entry_attempt_records=sum(bool(r.get('entry_write_attempted')) for r in intents),
            entry_fill_records=sum(bool(r.get('entry_fill_price') and r.get('position_id')) for r in intents),
            intent_states=dict(Counter(r.get('status','unknown') for r in intents)),watch_states=dict(watches),
            latest_trade_proposals=detail,candidate_error_types=dict(Counter(r.get('code',r.get('type','unknown')) for r in errors)),
            snapshot_age_seconds=max(0,now-account.get('observed_at',0)) if account.get('observed_at') else None,
            start_grant_present=bool(grant),start_grant_revoked=grant.get('revoked'),start_grant_expired=grant.get('expires_at',0)<=now,
            halt=bool(state('live:halt')),last_entry_reason=state('live_entry_status',{}).get('reason'),
            limitations='Persisted records only, not a fresh exchange check. Entry attempt record is not proof of a POST/fill. Old unfinalized traces cannot establish a rejection reason.')
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
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',args.account) or not 0<args.hours<=720:parser.error('invalid account/hours')
    path=Path(get_settings().runtime_db_path).parent/args.account/'live'/'runtime.sqlite3'
    if not path.exists():parser.error('live runtime database not found')
    print(format_report(audit(path,since=time()-args.hours*3600)))


if __name__=='__main__':main()
