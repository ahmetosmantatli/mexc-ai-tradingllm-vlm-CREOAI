"""Export observed AI usage without any exchange/provider calls."""
import argparse
import csv
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from trading.runtime.usage import TRACE_FIELDS, ATTEMPT_FIELDS


def read_traces(root):
    rows=[]
    for path in sorted(Path(root).rglob('*.sqlite3')):
        relative=path.relative_to(root)
        parts=relative.parts
        if len(parts)<3 or parts[-2] not in ('text','vlm'):
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
                rows.append(dict(account=parts[0],session='/'.join(parts[1:-2]),variant=parts[-2],**safe))
        finally:
            db.close()
    return sorted(rows,key=lambda r:r.get('started_at',0))


def summarize(rows):
    groups=defaultdict(list)
    for trace in rows:
        for a in trace['attempts']:
            groups[(trace['account'],trace['variant'],a.get('provider','?'),a.get('model','?'))].append(a)
    result=[]
    for (account,variant,provider,model),attempts in sorted(groups.items()):
        sent=[a for a in attempts if a.get('generation_requests',0)>0]
        known=[a for a in sent if a.get('input_tokens') is not None and a.get('output_tokens') is not None]
        costs=[a['estimated_cost_usd'] for a in sent if a.get('estimated_cost_usd') is not None]
        result.append(dict(account=account,variant=variant,provider=provider,model=model,
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


def export(root, output, since=0):
    rows=[r for r in read_traces(Path(root)) if r.get('started_at',0)>=since]
    groups=summarize(rows)
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True,mode=0o700)
    report=dict(generated_at=datetime.now(timezone.utc).isoformat(),evaluations=len(rows),groups=groups,
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
