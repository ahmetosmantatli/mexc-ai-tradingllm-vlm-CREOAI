"""Local server provisioning. Never paste exchange secrets into Telegram."""
import argparse
import json
import os
from pathlib import Path
from dotenv import load_dotenv
import httpx


def main():
    parser=argparse.ArgumentParser(description='Display Telegram identities or provision a server account reference')
    parser.add_argument('--identities',action='store_true')
    parser.add_argument('--account')
    parser.add_argument('--telegram-id',type=int)
    parser.add_argument('--key-env')
    parser.add_argument('--secret-env')
    parser.add_argument('--registry',default='secrets/accounts.json')
    args=parser.parse_args()
    load_dotenv()
    if args.identities:
        token=os.environ.get('TELEGRAM_BOT_TOKEN','')
        if not token:
            raise SystemExit('TELEGRAM_BOT_TOKEN missing')
        try:
            response=httpx.post(f'https://api.telegram.org/bot{token}/getUpdates',json={'timeout':0},timeout=10)
            response.raise_for_status()
            data=response.json()
            if not data.get('ok'):
                raise ValueError()
            identities=set()
            for update in data['result']:
                message=update.get('message',{})
                user=message.get('from',{})
                chat=message.get('chat',{})
                if chat.get('type')=='private' and user.get('id')==chat.get('id'):
                    identities.add(user['id'])
            print('Private Telegram IDs:',sorted(identities))
        except Exception:
            raise SystemExit('Telegram lookup failed; verify token/webhook/network locally')
        return
    if not all((args.account,args.telegram_id,args.key_env,args.secret_env)):
        parser.error('provide --account --telegram-id --key-env --secret-env')
    path=Path(args.registry)
    payload=json.loads(path.read_text()) if path.exists() else {'accounts':[]}
    if any(row['id']==args.account for row in payload['accounts']):
        raise SystemExit('account exists; edit server registry explicitly')
    payload['accounts'].append(dict(id=args.account,telegram_user_id=args.telegram_id,telegram_chat_id=args.telegram_id,mexc_key_env=args.key_env,mexc_secret_env=args.secret_env))
    from trading.control.accounts import load_accounts
    import tempfile
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,delete=False) as temp:
        json.dump(payload,temp,indent=2)
        staging=Path(temp.name)
    try:
        load_accounts(str(staging))
        staging.chmod(0o600)
        staging.replace(path)
    finally:
        staging.unlink(missing_ok=True)
    print('Account registered; Telegram entries remain paused until /run.')


if __name__=='__main__':
    main()
