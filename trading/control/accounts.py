"""Server-owned registry. Telegram never accepts exchange secrets."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re


@dataclass(frozen=True)
class Account:
    account_id: str
    telegram_user_id: int
    telegram_chat_id: int
    mexc_api_key: str = field(repr=False)
    mexc_api_secret: str = field(repr=False)


def load_accounts(path: str, environ=None) -> list[Account]:
    """Load {accounts:[{id, telegram_user_id, telegram_chat_id, mexc_key_env, mexc_secret_env}]}.

    Each private Telegram identity has exactly one server-provisioned account.
    Store this registry in secrets/accounts.json; actual credentials stay in env.
    """
    env = os.environ if environ is None else environ
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, dict) or not isinstance(payload.get('accounts'), list):
        raise ValueError('invalid account registry')
    accounts, ids, users, chats = [], set(), set(), set()
    for row in payload['accounts']:
        if not isinstance(row, dict) or any(k in row for k in ('mexc_api_key', 'mexc_api_secret')):
            raise ValueError('registry accepts secret environment references only')
        ident = row['id']
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', ident):
            raise ValueError('invalid account id')
        user, chat = int(row['telegram_user_id']), int(row['telegram_chat_id'])
        if user <= 0 or chat != user:
            raise ValueError('only private user chats may control an account')
        if ident in ids or user in users or chat in chats:
            raise ValueError('duplicate account or Telegram identity')
        key_ref, secret_ref = row['mexc_key_env'], row['mexc_secret_env']
        if not all(isinstance(ref, str) and re.fullmatch(r'[A-Z][A-Z0-9_]*', ref) for ref in (key_ref, secret_ref)):
            raise ValueError('invalid secret environment reference')
        key, secret = env.get(key_ref, '').strip(), env.get(secret_ref, '').strip()
        if bool(key) != bool(secret):
            raise ValueError(f'partial exchange credentials for account {ident}')
        ids.add(ident); users.add(user); chats.add(chat)
        accounts.append(Account(ident, user, chat, key, secret))
    if not accounts:
        raise ValueError('registry contains no accounts')
    return accounts
