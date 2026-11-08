"""Acknowledge a reviewed emergency halt locally; never starts trading or sends an order."""
import argparse
import json
from pathlib import Path
import re
from time import time

from trading.config import get_settings
from trading.control.live_preflight import resolve_account
from trading.exchange.mexc_private import MexcPrivateClient
from trading.runtime.store import RuntimeStore


def acknowledge(store,control_store,client,account_id):
    def paused():
        control=control_store.get(f'account:{account_id}:control',{})
        return control.get('stopped') is True and control.get('enabled') is False
    if not paused():raise ValueError('telegram_stop_required')
    halt=store.get('live:halt',{})
    if not halt:return dict(status='no_halt',trading_started=False,writes_enabled=False)
    if halt.get('reason')!='emergency_close_requires_review' or halt.get('requires_review') is not True:
        raise ValueError('different_halt_requires_investigation')
    row=store.get('live:intent:'+str(halt.get('intent_id')), {})
    if row.get('status')!='closed' or row.get('realized_complete') is not True or row.get('close_reason')!='EMERGENCY':
        raise ValueError('emergency_close_not_final')
    pending=[json.loads(value) for value, in store.db.execute("SELECT value FROM state WHERE key LIKE 'live:intent:%'")]
    if any(r.get('entry_write_attempted') and (r.get('status') not in ('closed','entry_rejected')
            or r.get('status')=='closed' and r.get('realized_complete') is not True) for r in pending):
        raise ValueError('unresolved_intent')
    # Refuse if ANY exchange exposure/order remains, including other symbols.
    if client.open_positions():raise ValueError('exchange_positions_present')
    if client.current_orders():raise ValueError('exchange_orders_present')
    if client.protection_orders():raise ValueError('exchange_protection_orders_present')
    if not paused():raise ValueError('control_changed')
    with store.lock,store.db:
        store.db.execute('BEGIN IMMEDIATE')
        if store.get('live:halt',{})!=halt:raise ValueError('halt_changed')
        store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',
            ('live:reviewed_emergency:'+halt['intent_id'],json.dumps(dict(at=time(),reason=halt['reason']))))
        store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',('live:halt','{}'))
    return dict(status='reviewed_halt_acknowledged',trading_started=False,writes_enabled=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account',required=True)
    parser.add_argument('--acknowledge-reviewed-emergency',action='store_true',required=True)
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',args.account):parser.error('invalid account')
    cfg=get_settings();account=resolve_account(cfg,args.account)
    root=Path(cfg.runtime_db_path).parent
    if not (root/args.account/'live'/'runtime.sqlite3').exists():parser.error('live database missing')
    store=RuntimeStore(str(root/args.account/'live'/'runtime.sqlite3'))
    control=RuntimeStore(str(root/'control.sqlite3'))
    client=MexcPrivateClient(account.mexc_api_key,account.mexc_api_secret,writes_enabled=False)
    try:
        print(json.dumps(acknowledge(store,control,client,args.account)))
    except Exception as exc:
        from trading.runtime.execution_diagnostics import safe_execution_error
        allowed={'telegram_stop_required','different_halt_requires_investigation','emergency_close_not_final',
            'unresolved_intent','exchange_positions_present','exchange_orders_present',
            'exchange_protection_orders_present','control_changed','halt_changed'}
        print(json.dumps(dict(status='not_cleared',reason=str(exc) if str(exc) in allowed else 'read_or_local_error',
            details=safe_execution_error(exc),trading_started=False,writes_enabled=False)))
    finally:
        client.close();store.db.close();control.db.close()


if __name__=='__main__':main()
