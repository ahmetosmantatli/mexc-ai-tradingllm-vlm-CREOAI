import json
import pytest
from cryptography.fernet import Fernet
from trading.control.vault import Vault, VaultError
from trading.runtime.store import RuntimeStore


def test_vault_encrypts_at_rest_and_restarts(tmp_path):
    path = str(tmp_path/'vault.sqlite3')
    key = Fernet.generate_key()
    store = RuntimeStore(path)
    vault = Vault(store, key)
    vault.put('ahmet', 'mexc-key-private-123', 'mexc-secret-private-456')
    raw = json.dumps(store.get('account:ahmet:credentials'))
    assert 'mexc-key-private-123' not in raw and 'mexc-secret-private-456' not in raw
    restored = Vault(RuntimeStore(path), key).get('ahmet')
    assert restored.api_key == 'mexc-key-private-123'
    assert restored.api_secret == 'mexc-secret-private-456'
    assert restored.api_key not in repr(restored)
    assert restored.api_secret not in repr(restored)
    assert vault.get('friend') is None


def test_vault_delete_is_scoped_and_idempotent(tmp_path):
    vault = Vault(RuntimeStore(str(tmp_path/'s')), Fernet.generate_key())
    vault.put('a', 'key-aaaaaaaa', 'secret-aaaaaaaa')
    vault.put('b', 'key-bbbbbbbb', 'secret-bbbbbbbb')
    assert vault.delete('a') is True
    assert vault.delete('a') is False
    assert vault.get('a') is None
    assert vault.get('b').api_key == 'key-bbbbbbbb'


def test_wrong_master_key_and_tampering_fail_without_secret_echo(tmp_path):
    store = RuntimeStore(str(tmp_path/'s'))
    vault = Vault(store, Fernet.generate_key())
    vault.put('a', 'key-aaaaaaaa', 'secret-aaaaaaaa')
    with pytest.raises(VaultError, match='authenticated') as error:
        Vault(store, Fernet.generate_key()).get('a')
    assert 'secret-aaaaaaaa' not in str(error.value)
    record = store.get('account:a:credentials')
    record['token'] = record['token'][:-3] + 'xxx'
    store.put('account:a:credentials', record)
    with pytest.raises(VaultError, match='authenticated'):
        vault.get('a')


def test_ciphertext_cannot_be_moved_to_different_account(tmp_path):
    store = RuntimeStore(str(tmp_path/'s'))
    vault = Vault(store, Fernet.generate_key())
    vault.put('a', 'key-aaaaaaaa', 'secret-aaaaaaaa')
    store.put('account:b:credentials', store.get('account:a:credentials'))
    with pytest.raises(VaultError, match='authenticated'):
        vault.get('b')


def test_missing_master_key_and_bad_inputs_fail_closed(tmp_path, monkeypatch):
    store = RuntimeStore(str(tmp_path/'s'))
    monkeypatch.delenv('ACCOUNT_VAULT_KEY', raising=False)
    with pytest.raises(VaultError, match='ACCOUNT_VAULT_KEY'):
        Vault(store)
    vault = Vault(store, Fernet.generate_key())
    for ident in ('../a', 'a:b', '', 123):
        with pytest.raises(VaultError, match='identifier'):
            vault.put(ident, 'key-aaaaaaaa', 'secret-aaaaaaaa')
    with pytest.raises(VaultError, match='between'):
        vault.put('a', 'short', 'secret-aaaaaaaa')
    assert vault.get('a') is None


def test_master_key_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('ACCOUNT_VAULT_KEY', Fernet.generate_key().decode())
    vault = Vault(RuntimeStore(str(tmp_path/'s')))
    vault.put('a', 'key-aaaaaaaa', 'secret-aaaaaaaa')
    assert vault.get('a').api_secret == 'secret-aaaaaaaa'
