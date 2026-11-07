"""Read-only server readiness check. Never sends a private POST or AI request."""
import argparse
from dataclasses import replace
import json
import os
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
    from trading.exchange.account import FuturesAccount
    assets=client.account_assets()
    account=FuturesAccount.parse(dict(success=True,code=0,data=assets))
    positions=client.open_positions()
    mode=client.position_mode()
    leverage=client.leverage(symbol)
    risk=client.risk_limits(symbol)
    orders=[]
    complete=False
    for page in range(1,101):
        rows=client.current_orders(page_num=page,page_size=100)
        if not isinstance(rows,list):
            raise ValueError('unknown_open_orders')
        orders.extend(rows)
        if len(rows)<100:
            complete=True
            break
    if not complete:
        raise ValueError('open_orders_pagination_incomplete')
    return dict(account_currency='USDT',equity=account.equity,available=account.available,
        position_margin=account.position_margin,unrealized=account.unrealized,
        symbol=symbol,position_mode=mode,open_positions=len(positions),open_orders=len(orders),
        account_leverage_data_present=bool(leverage),account_risk_data_present=bool(risk),
        writes_enabled=False,automatic_live_worker_available=False,
        note='Read-only connectivity check, not execution approval or a guarantee of trading permission.')


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
        print(json.dumps(dict(status='preflight_failed',error_type=type(exc).__name__,
            code=getattr(exc,'code',None),http_status=getattr(exc,'status_code',None),
            writes_enabled=False),ensure_ascii=False))
        return 2
    finally:
        if client is not None:
            client.close()


if __name__=='__main__':
    raise SystemExit(main())
