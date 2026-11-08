"""Offline matched replay export; optional public candles, never LLM/private writes."""
import argparse
import json
from pathlib import Path
import sqlite3
from time import time
from trading.runtime.edge_research import compare


def export(root,output,*,bars=None,fetch_public=False,horizon_bars=48):
    rows=[]
    for path in Path(root).rglob('*.sqlite3'):
        db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='edge_reviews'").fetchone():
                account=path.relative_to(root).parts[0]
                traces={}
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='ai_usage_events'").fetchone():
                    traces={ident:json.loads(payload) for ident,payload in db.execute('SELECT id,payload FROM ai_usage_events')}
                for payload, in db.execute('SELECT payload FROM edge_reviews'):
                    row=json.loads(payload);row['account']=account
                    row['execution_status']=traces.get(row['id'],{}).get('execution_status',row.get('execution_status'))
                    row['opportunity']=account+':'+row['opportunity'];rows.append(row)
        finally:db.close()
    if fetch_public:
        from trading.exchange.mexc_public import MexcPublicClient
        client=MexcPublicClient()
        try:
            bars={}
            for symbol in sorted({row['symbol'] for row in rows}):
                try:bars[symbol]=client.get_klines(symbol,interval='Min5',limit=1200)['candles']
                except Exception:bars[symbol]=[]
        finally:client.close()
    report=compare(rows,bars or {},now=time(),horizon_bars=horizon_bars)
    report['recorded_reviews']=len(rows)
    report['horizon_5m_bars']=horizon_bars
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    (output/'edge-comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    lines=[f"Kayıtlı değerlendirme: {len(rows)}",f"Tekil fırsat: {report['unique_opportunities']}",
        f"Eşleşen karşılaştırma: {report['matched']}",
        f"Aynı mumda SL/TP sırası belirsiz karşılaştırma: {report['ambiguous_matched']}",
        f"Belirsiz mumlar hariç ortalama fark (R): {report['mean_decisive_delta_r']}",
        "R sonuçlarında tahmini işlem maliyetleri var; funding yok.",
        "İşlem açılmayan adayların referans sonuçları: "+json.dumps(report['withheld_reference_outcomes'],ensure_ascii=False),
        "Pozitif referans sonucu, gerçekleştirilebilir kaçan fırsat veya yanlış BEKLE kararı kanıtı değildir."]
    for group in report['groups']:
        lines.append(json.dumps(group,ensure_ascii=False))
    lines+=['Canlı kâr raporu veya kârlılık kanıtı değildir. AI giderleri ayrı usage_report içindedir.',report['limitations']]
    (output/'edge-comparison.txt').write_text('\n'.join(lines)+'\n')
    return report


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',default='data');parser.add_argument('--output-dir',default='data/reports')
    parser.add_argument('--candles-json',help='symbol -> UTC 5m OHLCV rows; no network when used')
    parser.add_argument('--fetch-public',action='store_true')
    parser.add_argument('--horizon-bars',type=int,default=48)
    args=parser.parse_args()
    if not 1<=args.horizon_bars<=288:parser.error('horizon must be 1..288 5m bars')
    if args.fetch_public and args.candles_json:parser.error('choose local candles or public fetch')
    report=export(args.root,args.output_dir,bars=json.loads(Path(args.candles_json).read_text()) if args.candles_json else None,
        fetch_public=args.fetch_public,horizon_bars=args.horizon_bars)
    print(f"{report['recorded_reviews']} kayıt, {report['matched']} eşleşen karşılaştırma; rapor: {args.output_dir}")


if __name__=='__main__':main()
