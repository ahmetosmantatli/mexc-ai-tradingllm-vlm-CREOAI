"""Read-only server readiness check. Never sends a private POST or AI request."""
import argparse
from dataclasses import replace
import json
import os
import re
from pathlib import Path

from dotenv import dotenv_values

from trading.config import Settings
from trading.control.accounts import load_accounts
from trading.control.vault import Vault
from trading.exchange.mexc_private import MexcPrivateClient
from trading.runtime.store import RuntimeStore


def resolve_account(cfg, account_id, env_file='.env'):
    environment={**dotenv_values(env_file),**os.environ}
    account=next((a for a in load_accounts(cfg.accounts_file,environment) if a.account_id==account_id),None)
    if account is None:
        raise ValueError('unknown_account')
    store=RuntimeStore(str(Path(cfg.runtime_db_path).parent/'control.sqlite3'))
    try:
        if store.get(f'account:{account_id}:disconnected',False):
            raise ValueError('account_disconnected')
        saved=Vault(store,cfg.account_vault_key).get(account_id) if cfg.account_vault_key else None
        if saved:
            account=replace(account,mexc_api_key=saved.api_key,mexc_api_secret=saved.api_secret)
        if not account.mexc_api_key or not account.mexc_api_secret:
            raise ValueError('account_credentials_missing')
        return account
    finally:
        store.db.close()


def inspect_account(client, symbol):
    """Minimal safe output; unknown fields remain unknown rather than guessed."""
    from trading.runtime.live_account import LiveAccountReconciler
    store=RuntimeStore(':memory:')
    try:
        snapshot=LiveAccountReconciler('preflight',store,binding_id='read-only-inspection').refresh(client,symbols=[symbol])
        balance=snapshot['balance']
        caps=[]
        for risk in snapshot['risk_limits'][symbol]:
            setting=next(row for row in snapshot['leverage'][symbol] if row['side']==risk['side'])
            caps.append(dict(side='long' if risk['side']==1 else 'short',configured_leverage=setting['leverage'],
                account_max_leverage=min(risk['max_leverage'],setting['max_leverage']),
                max_contracts=min(risk['max_contracts'],setting['max_contracts']),
                isolated=setting['open_type']==1,maintenance_rate=max(risk['maintenance_rate'],setting['maintenance_rate'])))
        return dict(account_currency='USDT',equity=balance['equity'],available=balance['available'],
            position_margin=balance['position_margin'],unrealized=balance['unrealized'],symbol=symbol,
            position_mode=snapshot['position_mode'],open_positions=len(snapshot['positions']),open_orders=len(snapshot['orders']),
            account_leverage_data_present=True,account_risk_data_present=True,account_leverage=caps,
            writes_enabled=False,automatic_live_worker_available=True,execution_enabled_by_this_check=False,
            note='Read-only connectivity check, not execution approval or a guarantee of trading permission.')
    finally:
        store.db.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description='MEXC live readiness: read-only, no orders, no AI calls')
    parser.add_argument('--account',help='Existing server account ID, not Telegram token')
    parser.add_argument('--list-accounts',action='store_true',help='List local account IDs without any API calls')
    parser.add_argument('--symbol',default='BTC_USDT',help='MEXC symbol, e.g. BTC_USDT')
    parser.add_argument('--env-file',default='.env')
    args=parser.parse_args(argv)
    client=None
    try:
        cfg=Settings(_env_file=args.env_file)
        if args.list_accounts:
            environment={**dotenv_values(args.env_file),**os.environ}
            print(json.dumps([a.account_id for a in load_accounts(cfg.accounts_file,environment)]))
            return 0
        if not args.account:
            raise ValueError('account_required')
        account=resolve_account(cfg,args.account,args.env_file)
        client=MexcPrivateClient(account.mexc_api_key,account.mexc_api_secret,writes_enabled=False)
        report=inspect_account(client,args.symbol)
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return 0
    except Exception as exc:
        # Neither raw transport exceptions nor provider response bodies contain
        # material that should be echoed to a shared terminal/chat transcript.
        from trading.runtime.live_account import LiveAccountError
        reason=str(exc) if isinstance(exc,LiveAccountError) and re.fullmatch('[A-Z0-9_]{1,128}',str(exc)) else None
        print(json.dumps(dict(status='preflight_failed',error_type=type(exc).__name__,reason=reason,
            code=getattr(exc,'code',None),http_status=getattr(exc,'status_code',None),
            writes_enabled=False),ensure_ascii=False))
        return 2
    finally:
        if client is not None:
            client.close()


if __name__=='__main__':
    raise SystemExit(main())
