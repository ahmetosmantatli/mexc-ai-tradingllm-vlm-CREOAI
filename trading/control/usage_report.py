"""Export observed AI usage without any exchange/provider calls."""
import argparse
import csv
import json
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from trading.runtime.usage import TRACE_FIELDS, ATTEMPT_FIELDS


def read_traces(root):
    rows=[]
    for path in sorted(Path(root).rglob('*.sqlite3')):
        relative=path.relative_to(root)
        parts=relative.parts
        if len(parts)<3 or parts[-2] not in ('text','vlm','live'):
            continue
        db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=10)
        try:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            traces={}
            if 'state' in tables:
                found=db.execute("SELECT value FROM state WHERE key='analysis_history'").fetchone()
                for trace in json.loads(found[0]) if found else []:
                    traces[trace['id']]=trace
            if 'ai_usage_events' in tables:
                for ident,payload in db.execute('SELECT id,payload FROM ai_usage_events'):
                    traces[ident]=json.loads(payload)
            for trace in traces.values():
                safe={key:trace[key] for key in TRACE_FIELDS if key in trace}
                safe['attempts']=[{key:a[key] for key in ATTEMPT_FIELDS if key in a} for a in trace.get('attempts',[])]
                rows.append(dict(account=parts[0],session='/'.join(parts[1:-2]),variant=(trace.get('analysis_version','unknown') if parts[-2]=='live' else parts[-2]),**safe))
        finally:
            db.close()
    return sorted(rows,key=lambda r:r.get('started_at',0))


def summarize(rows):
    groups=defaultdict(list)
    for trace in rows:
        for a in trace['attempts']:
            groups[(trace['account'],trace['variant'],a.get('provider','?'),a.get('model','?'),a.get('credential_id','legacy'),a.get('quota_group','legacy'))].append(a)
    result=[]
    for (account,variant,provider,model,credential_id,quota_group),attempts in sorted(groups.items()):
        sent=[a for a in attempts if a.get('generation_requests',0)>0]
        known=[a for a in sent if a.get('input_tokens') is not None and a.get('output_tokens') is not None]
        costs=[a['estimated_cost_usd'] for a in sent if a.get('estimated_cost_usd') is not None]
        result.append(dict(account=account,variant=variant,provider=provider,model=model,credential_id=credential_id,quota_group=quota_group,
            generation_requests=sum(a.get('generation_requests',0) for a in attempts),
            count_requests=sum(a.get('count_requests',0) for a in attempts),
            successful_replies=sum(a.get('status')=='success' for a in sent),
            unsuccessful_requests=sum(a.get('status')!='success' for a in sent),
            cooldown_skips=sum(a.get('status')=='cooldown_skipped' for a in attempts),
            known_usage_requests=len(known),unknown_usage_requests=len(sent)-len(known),
            input_tokens=sum(a['input_tokens'] for a in known),output_tokens=sum(a['output_tokens'] for a in known),
            avg_input_tokens=mean(a['input_tokens'] for a in known) if known else None,
            avg_output_tokens=mean(a['output_tokens'] for a in known) if known else None,
            known_estimated_cost_usd=sum(costs) if costs else None,
            avg_known_estimated_cost_usd=mean(costs) if costs else None,
            pending_usd=sum(a.get('pending_usd') or 0 for a in attempts)))
    return result


def review_summary(rows):
    """Explain paid routing and repeated evidence; repetition is not proof of waste.

    An unchanged market may legitimately be reconsidered after account/plan
    changes. Exact input fingerprints are counted separately from market hashes.
    Neither label is interpreted as a missed profitable trade.
    """
    symbols=defaultdict(list);paid=defaultdict(list)
    decisions=Counter();routing=Counter();fallbacks=Counter()
    for trace in rows:
        attempts=trace.get('attempts',[])
        for attempt in attempts:
            if attempt.get('status')=='quota_skipped':routing[attempt.get('error_category','unspecified')]+=1
        if not any(a.get('generation_requests',0) for a in attempts):continue
        symbols[(trace['account'],trace['variant'],trace.get('symbol','?'),trace.get('setup_family') or trace.get('strategy','?'))].append(trace)
        action=trace.get('model_action') or (trace.get('action') if trace.get('status')=='completed' else None)
        if action:decisions[action]+=1
        paid_attempts=[a for a in attempts if a.get('provider')=='anthropic' and a.get('generation_requests',0)]
        if paid_attempts:
            fallbacks[trace.get('fallback_reason') or 'direct_or_legacy_unknown']+=sum(a['generation_requests'] for a in paid_attempts)
            paid[(trace['account'],trace['variant'],trace.get('setup_family') or trace.get('strategy','?'))].extend(paid_attempts)
    repeated=[]
    for (account,variant,symbol,strategy),traces in symbols.items():
        inputs=Counter(t['input_fingerprint'] for t in traces if t.get('input_fingerprint'))
        markets=Counter(t['market_fingerprint'] for t in traces if t.get('market_fingerprint'))
        repeated.append(dict(account=account,variant=variant,symbol=symbol,strategy=strategy,
            sent_analyses=len(traces),exact_input_repeat_reviews=sum(max(0,n-1) for n in inputs.values()),
            same_market_repeat_reviews=sum(max(0,n-1) for n in markets.values()),
            missing_input_fingerprints=sum(not bool(t.get('input_fingerprint')) for t in traces)))
    spending=[]
    for (account,variant,strategy),attempts in paid.items():
        costs=[a['estimated_cost_usd'] for a in attempts if a.get('estimated_cost_usd') is not None]
        spending.append(dict(account=account,variant=variant,strategy=strategy,
            generation_requests=sum(a['generation_requests'] for a in attempts),
            known_estimated_cost_usd=sum(costs) if costs else None,
            unknown_cost_requests=sum(a.get('estimated_cost_usd') is None for a in attempts)))
    return dict(model_decisions=dict(decisions),routing_skip_reasons=dict(routing),paid_route_reasons=dict(fallbacks),
        paid_by_strategy=spending,review_counts=sorted(repeated,key=lambda r:(-r['sent_analyses'],r['account'],r['symbol'],r['strategy'])),
        note='Same market can be legitimately reviewed after account/plan changes. Exact repeat counts are audit evidence, not proof that a request was unnecessary. Missing historical routing fields remain unknown.')


def export(root, output, since=0):
    rows=[r for r in read_traces(Path(root)) if r.get('started_at',0)>=since]
    groups=summarize(rows)
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True,mode=0o700)
    report=dict(generated_at=datetime.now(timezone.utc).isoformat(),evaluations=len(rows),groups=groups,review_summary=review_summary(rows),
        limitations='Observed records only; older history may be incomplete. Unknown usage is not zero. Groq billing is not inferred. Token cost estimates are not invoices. No future spend forecast.',
        traces=rows)
    # These fixed files are regenerated on demand; server directory must stay private.
    files=[]
    for name in ('ai-usage.json','ai-requests.csv','ai-usage-summary.txt'):
        path=output/name
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        os.fchmod(descriptor,0o600)
        files.append((path,os.fdopen(descriptor,'w',encoding='utf-8')))
    try:
        json.dump(report,files[0][1],ensure_ascii=False,indent=2)
        writer=csv.DictWriter(files[1][1],fieldnames=['account','variant','analysis_id','symbol','strategy','started_at',*ATTEMPT_FIELDS])
        writer.writeheader()
        for r in rows:
            for a in r['attempts']:
                writer.writerow(dict(account=r['account'],variant=r['variant'],analysis_id=r['id'],symbol=r.get('symbol'),strategy=r.get('strategy'),started_at=r.get('started_at'),**a))
        text=files[2][1]
        text.write(f'Kayitli analiz: {len(rows)}\nYalniz gozlenen kullanim; eksik token veya maliyet sifir degildir.\n')
        for g in groups:
            text.write(json.dumps(g,ensure_ascii=False)+'\n')
        text.write('Karar / tekrar / ucretli yonlendirme ozeti:\n')
        details=report['review_summary']
        for key in ('model_decisions','routing_skip_reasons','paid_route_reasons','paid_by_strategy'):
            text.write(key+': '+json.dumps(details[key],ensure_ascii=False)+'\n')
        text.write('En sik incelenen 20 parite/kurulum (ayni piyasa verisi tekrari tek basina gereksiz istek kaniti degildir):\n')
        for row in details['review_counts'][:20]:text.write(json.dumps(row,ensure_ascii=False)+'\n')
    finally:
        for _,handle in files:handle.close()
    return report


def main():
    from trading.config import get_settings
    parser=argparse.ArgumentParser(description='Local read-only AI usage export; no API calls')
    parser.add_argument('--output-dir',default='data/reports')
    parser.add_argument('--since',help='ISO date/time; absent timezone means UTC')
    args=parser.parse_args()
    since=0
    if args.since:
        stamp=datetime.fromisoformat(args.since)
        since=stamp.replace(tzinfo=timezone.utc).timestamp() if stamp.tzinfo is None else stamp.timestamp()
    cfg=get_settings()
    report=export(Path(cfg.runtime_db_path).parent,args.output_dir,since)
    print(f"{report['evaluations']} kayıtlı analiz; raporlar: {args.output_dir}")
    print('ai-usage-summary.txt | ai-requests.csv | ai-usage.json')


if __name__=='__main__':
    main()
