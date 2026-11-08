"""Read-only MEXC protection inspection; no POST, no AI, no raw responses."""
import argparse
import json
from pathlib import Path
import re
import sqlite3

from trading.config import get_settings
from trading.control.live_preflight import resolve_account
from trading.exchange.mexc_private import MexcPrivateClient


NUMERIC_FIELDS=('positionId','id','orderId','lossTrend','profitTrend','stopLossPrice',
    'takeProfitPrice','state','positionType','vol','isFinished','takeProfitVol',
    'stopLossVol','volType','takeProfitReverse','stopLossReverse','takeProfitType',
    'stopLossType','errorCode','stopLossOrderPrice','orderType','type','side','dealVol')


def safe_protection(row):
    result={key:value for key in NUMERIC_FIELDS if (value:=row.get(key)) is not None
        and type(value) in (int,float,str) and re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?',str(value))}
    volume_type=row.get('profitLossVolType')
    if volume_type in ('SAME','SEPARATE'):result['profitLossVolType']=volume_type
    result['missing_fields']=[key for key in ('lossTrend','profitTrend','stopLossType',
        'takeProfitType','stopLossReverse','takeProfitReverse','profitLossVolType','isFinished') if key not in row]
    return result


def inspect(client,symbol,position_ids):
    groups=[]
    for finished in (0,1):
        matches=[]
        complete=False
        for page in range(1,11):
            rows=client.protection_orders(symbol,page_num=page,page_size=100,finished=finished)
            for row in rows:
                if row.get('symbol')==symbol and str(row.get('positionId')) in position_ids:
                    matches.append(safe_protection(row))
            if len(rows)<100:
                complete=True
                break
        groups.append(dict(finished=finished,complete=complete,records=matches))
    return groups


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account',required=True)
    parser.add_argument('--symbol',required=True)
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',args.account) or not re.fullmatch(r'[A-Z0-9]{1,30}_USDT',args.symbol):
        parser.error('invalid account or symbol')
    cfg=get_settings()
    path=Path(cfg.runtime_db_path).parent/args.account/'live'/'runtime.sqlite3'
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        intents=[json.loads(value) for value, in db.execute("SELECT value FROM state WHERE key LIKE 'live:intent:%'")]
    finally:db.close()
    intents=[r for r in intents if r.get('proposal',{}).get('symbol')==args.symbol and r.get('position_id')]
    intents=sorted(intents,key=lambda r:r.get('staged_at',0),reverse=True)[:3]
    if not intents:
        print(json.dumps(dict(status='no_recorded_position_id',writes_enabled=False)))
        return
    account=resolve_account(cfg,args.account)
    client=MexcPrivateClient(account.mexc_api_key,account.mexc_api_secret,writes_enabled=False)
    try:
        result=inspect(client,args.symbol,{str(r['position_id']) for r in intents})
        entries=[]
        for intent in intents:
            order=client.order_by_external_id(args.symbol,intent['external_id'])
            entries.append(dict(
                recorded_exchange_type=intent.get('entry_exchange_type'),
                submitted_type=intent.get('entry_submitted_type'),
                protection_observation=intent.get('protection_observation'),
                exchange_order={k:v for k,v in safe_protection(order).items()
                    if k in ('orderId','positionId','orderType','type','side','state','vol','dealVol')},
                exchange_order_id_matches=str(order.get('orderId'))==str(intent.get('exchange_order_id'))))
        print(json.dumps(dict(account=args.account,symbol=args.symbol,writes_enabled=False,
            protections=result,entries=entries),ensure_ascii=False,indent=2))
    except Exception as exc:
        from trading.runtime.execution_diagnostics import safe_execution_error
        print(json.dumps(dict(status='read_failed',details=safe_execution_error(exc),writes_enabled=False)))
    finally:client.close()


if __name__=='__main__':main()
