"""Authenticated encrypted exchange credentials stored only on the server.

The master key must be supplied separately through ACCOUNT_VAULT_KEY. Telegram
identity authorization and credential validation against MEXC belong to callers.
Deleting a record revokes application access; it does not securely erase SQLite
WAL files, backups, Telegram history, or an exchange API key itself.
"""
from dataclasses import dataclass, field
import json
import os
import re

from cryptography.fernet import Fernet


class VaultError(RuntimeError):
    """Sanitized vault error: never includes submitted credentials or ciphertext."""


@dataclass(frozen=True)
class SecretCredentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)


class Vault:
    def __init__(self, store, key=None):
        self.store = store
        raw = key if key is not None else os.environ.get('ACCOUNT_VAULT_KEY', '')
        try:
            if isinstance(raw, str):
                raw = raw.encode('ascii')
            self._cipher = Fernet(raw)
        except Exception:
            raise VaultError('A valid ACCOUNT_VAULT_KEY is required') from None

    @staticmethod
    def _storage_key(account_id):
        if not isinstance(account_id, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,40}', account_id) is None:
            raise VaultError('Invalid account identifier')
        return f'account:{account_id}:credentials'

    @staticmethod
    def _validate(api_key, api_secret):
        # Do not claim format or permission validity from opaque key strings.
        if not all(isinstance(value, str) and 8 <= len(value) <= 512 for value in (api_key, api_secret)):
            raise VaultError('Credentials must contain between 8 and 512 characters each')

    def put(self, account_id, api_key, api_secret):
        storage_key = self._storage_key(account_id)
        self._validate(api_key, api_secret)
        payload = json.dumps(dict(version=1, account_id=account_id, api_key=api_key, api_secret=api_secret), separators=(',', ':')).encode('utf-8')
        token = self._cipher.encrypt(payload).decode('ascii')
        self.store.put(storage_key, {'version': 1, 'token': token})

    def get(self, account_id):
        record = self.store.get(self._storage_key(account_id))
        if record is None:
            return None
        try:
            if not isinstance(record, dict) or record.get('version') != 1:
                raise ValueError()
            payload = json.loads(self._cipher.decrypt(record['token'].encode('ascii')))
            if payload.get('version') != 1 or payload.get('account_id') != account_id:
                raise ValueError()
            self._validate(payload['api_key'], payload['api_secret'])
            return SecretCredentials(payload['api_key'], payload['api_secret'])
        except Exception:
            raise VaultError('Stored credentials could not be authenticated') from None

    def delete(self, account_id):
        storage_key = self._storage_key(account_id)
        with self.store.lock, self.store.db:
            cursor = self.store.db.execute('DELETE FROM state WHERE key=?', (storage_key,))
            return cursor.rowcount > 0
