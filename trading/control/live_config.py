"""Prepare an explicit LIVE deployment configuration; never starts a process/API.

Only the selected existing registry account is enabled; provider credentials and
other env values are preserved. Applying this is separate from Telegram Start.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import tempfile

from dotenv import dotenv_values,set_key
from trading.control.accounts import load_accounts


def configure(env_file,registry,account_id,*,apply=False,ack_real_orders=False):
    path=Path(env_file)
    if not path.is_file():raise ValueError('env_file_missing')
    environment={**dotenv_values(path),**os.environ}
    accounts=load_accounts(registry,environment)
    if account_id not in {account.account_id for account in accounts}:raise ValueError('unknown_registry_account')
    values={'BROKER_MODE':'live','LIVE_TRADING_ENABLED':'true','LIVE_AUTOMATIC_ENABLED':'true',
            'LIVE_ALLOWED_ACCOUNTS':json.dumps([account_id],separators=(',',':')),
            'LIVE_READINESS_ACK':'I_UNDERSTAND_REAL_ORDERS'}
    if not apply:return dict(applied=False,account=account_id,settings=values)
    if ack_real_orders is not True:raise ValueError('explicit_real_orders_acknowledgement_required')
    backup=path.with_name(path.name+'.backup-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    # Do not briefly create a world-readable secret backup under a permissive umask.
    descriptor=os.open(backup,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(descriptor,'wb') as handle:handle.write(path.read_bytes())
    descriptor,temp=tempfile.mkstemp(prefix=path.name+'.live-',dir=path.parent)
    os.close(descriptor)
    try:
        shutil.copyfile(path,temp)
        os.chmod(temp,0o600)
        for key,value in values.items():set_key(temp,key,value,quote_mode='never')
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)
    return dict(applied=True,account=account_id,backup=backup.name,
        note='No worker restarted and no API request sent. Live first boot requires a fresh authenticated Telegram Start.')


def main(argv=None):
    parser=argparse.ArgumentParser(description='Preview/apply LIVE env gates for one existing account; no exchange requests')
    parser.add_argument('--account',required=True)
    parser.add_argument('--env-file',default='.env')
    parser.add_argument('--registry',default='secrets/accounts.json')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--ack-real-orders',action='store_true')
    args=parser.parse_args(argv)
    try:
        result=configure(args.env_file,args.registry,args.account,apply=args.apply,ack_real_orders=args.ack_real_orders)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except Exception as exc:
        # Never emit env contents, credentials or raw filesystem exception text.
        print(json.dumps(dict(status='configuration_refused',type=type(exc).__name__)))
        return 2


if __name__=='__main__':raise SystemExit(main())
