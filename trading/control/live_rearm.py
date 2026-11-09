"""Acknowledge a reviewed emergency halt locally; never starts trading or sends an order."""
import argparse
import copy
import json
from pathlib import Path
import re
from time import time

from trading.config import get_settings
from trading.control.live_preflight import resolve_account
from trading.exchange.mexc_private import MexcPrivateClient
from trading.runtime.store import RuntimeStore


def _verify_protected_positions(store, control_store, client, account_id, pending):
    from trading.brokers.live.execution import ApprovedLiveExecutor, LiveExecutionGates

    binding=control_store.get(f'account:{account_id}:live_binding')
    if not binding:
        raise ValueError('protected_binding_missing')
    active=[r for r in pending if r.get('entry_write_attempted') and r.get('status')=='protected']
    if any(r.get('binding_id')!=binding or not r.get('position_id') or r.get('management_pending')
           or r.get('close_write_attempted') or r.get('emergency_close_attempted') for r in active):
        raise ValueError('protected_position_not_ready')
    positions=client.open_positions()
    if not isinstance(positions,list):
        raise ValueError('exchange_positions_unreadable')
    ids=[str(p.get('positionId')) for p in positions]
    expected=[str(r['position_id']) for r in active]
    if len(set(ids))!=len(ids) or len(set(expected))!=len(expected) or sorted(ids)!=sorted(expected):
        raise ValueError('exchange_positions_not_owned')
    verifier=ApprovedLiveExecutor(LiveExecutionGates(),store,client,None,None,
                                  account_id=account_id,binding_id=binding)
    for row in active:
        position=verifier._position(row,dict(positionId=row['position_id']))
        if position is None or verifier._protected(copy.deepcopy(row),position) is not True:
            raise ValueError('protected_position_not_verified')
    orders=client.current_orders(page_num=1,page_size=100)
    if not isinstance(orders,list) or orders:
        raise ValueError('exchange_orders_present')
    protection_ids=[]
    for page in range(1,11):
        protections=client.protection_orders(page_num=page,page_size=100)
        if not isinstance(protections,list) or any(str(p.get('positionId')) not in expected for p in protections):
            raise ValueError('exchange_protection_orders_present')
        protection_ids.extend(str(p.get('positionId')) for p in protections)
        if len(protections)<100:
            break
    else:
        raise ValueError('exchange_protections_incomplete')
    if sorted(protection_ids)!=sorted(expected):
        raise ValueError('protected_position_not_verified')
    return active


def acknowledge(store,control_store,client,account_id,*,allow_protected_positions=False,expected_intent_id=None):
    def paused():
        control=control_store.get(f'account:{account_id}:control',{})
        return control.get('stopped') is True and control.get('enabled') is False
    if not paused():raise ValueError('telegram_stop_required')
    halt=store.get('live:halt',{})
    if expected_intent_id is not None and halt.get('intent_id')!=expected_intent_id:
        raise ValueError('halt_changed')
    if not halt:return dict(status='no_halt',trading_started=False,writes_enabled=False)
    if halt.get('reason')!='emergency_close_requires_review' or halt.get('requires_review') is not True:
        raise ValueError('different_halt_requires_investigation')
    row=store.get('live:intent:'+str(halt.get('intent_id')), {})
    if row.get('status')!='closed' or row.get('realized_complete') is not True or row.get('close_reason')!='EMERGENCY':
        raise ValueError('emergency_close_not_final')
    pending=[json.loads(value) for value, in store.db.execute("SELECT value FROM state WHERE key LIKE 'live:intent:%'")]
    terminal=('closed','entry_rejected','protected') if allow_protected_positions else ('closed','entry_rejected')
    if any(r.get('entry_write_attempted') and (r.get('status') not in terminal
            or r.get('status')=='closed' and r.get('realized_complete') is not True) for r in pending):
        raise ValueError('unresolved_intent')
    protected=[]
    if allow_protected_positions:
        protected=_verify_protected_positions(store,control_store,client,account_id,pending)
    else:
        if client.open_positions():raise ValueError('exchange_positions_present')
        if client.current_orders():raise ValueError('exchange_orders_present')
        if client.protection_orders():raise ValueError('exchange_protection_orders_present')
    if not paused():raise ValueError('control_changed')
    with store.lock,store.db:
        store.db.execute('BEGIN IMMEDIATE')
        if store.get('live:halt',{})!=halt:raise ValueError('halt_changed')
        for record in protected:
            if store.get('live:intent:'+record['intent_id'])!=record:
                raise ValueError('protected_position_changed')
        store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',
            ('live:reviewed_emergency:'+halt['intent_id'],json.dumps(dict(at=time(),reason=halt['reason']))))
        store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',('live:halt','{}'))
    return dict(status='reviewed_halt_acknowledged',verified_protected_positions=len(protected),
                trading_started=False,writes_enabled=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account',required=True)
    parser.add_argument('--acknowledge-reviewed-emergency',action='store_true',required=True)
    parser.add_argument('--allow-protected-positions',action='store_true',
                        help='Allow existing owned positions only after read-only native protection verification')
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',args.account):parser.error('invalid account')
    cfg=get_settings();account=resolve_account(cfg,args.account)
    root=Path(cfg.runtime_db_path).parent
    if not (root/args.account/'live'/'runtime.sqlite3').exists():parser.error('live database missing')
    store=RuntimeStore(str(root/args.account/'live'/'runtime.sqlite3'))
    control=RuntimeStore(str(root/'control.sqlite3'))
    client=MexcPrivateClient(account.mexc_api_key,account.mexc_api_secret,writes_enabled=False)
    try:
        print(json.dumps(acknowledge(store,control,client,args.account,
                                    allow_protected_positions=args.allow_protected_positions)))
    except Exception as exc:
        from trading.runtime.execution_diagnostics import safe_execution_error
        allowed={'telegram_stop_required','different_halt_requires_investigation','emergency_close_not_final',
            'unresolved_intent','exchange_positions_present','exchange_orders_present',
            'exchange_protection_orders_present','control_changed','halt_changed',
            'protected_binding_missing','protected_position_not_ready','exchange_positions_unreadable',
            'exchange_positions_not_owned','protected_position_not_verified',
            'exchange_protections_incomplete','protected_position_changed'}
        print(json.dumps(dict(status='not_cleared',reason=str(exc) if str(exc) in allowed else 'read_or_local_error',
            details=safe_execution_error(exc),trading_started=False,writes_enabled=False)))
    finally:
        client.close();store.db.close();control.db.close()


if __name__=='__main__':main()
