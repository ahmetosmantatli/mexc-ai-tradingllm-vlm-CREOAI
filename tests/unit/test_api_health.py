import time
from fastapi.testclient import TestClient
import apps.api.main as api_main
from trading.config import Settings
from trading.runtime.store import RuntimeStore
import pytest


@pytest.fixture
def client(tmp_path,monkeypatch):
    settings=Settings(_env_file=None,runtime_db_path=str(tmp_path/'runtime.sqlite3'))
    monkeypatch.setattr(api_main,'get_settings',lambda:settings)
    return TestClient(api_main.app),settings


def test_health_and_settings_no_secrets(client):
    cli,_=client
    assert cli.get('/health').json()['live_trading_enabled'] is False
    settings=cli.get('/settings').json()
    assert 'mexc_api_secret' not in settings and 'telegram_bot_token' not in settings


def test_equity_does_not_invent_seed_when_worker_absent(client):
    cli,_=client
    assert cli.get('/equity').status_code==503
    for endpoint in ('deposit','floor','reset'):
        assert cli.post('/equity/'+endpoint,json={'amount_try':300}).status_code==409


def test_equity_separates_real_and_simulated_and_marks_stale(client):
    cli,cfg=client
    RuntimeStore(cfg.runtime_db_path).put('status',dict(observed_at=time.time()-100,reason='HOLD_NO_QUALIFIED_SIGNAL',real_account=dict(equity=20),paper=dict(equity=18)))
    response=cli.get('/equity').json()
    assert response['real_account']['equity']==20
    assert response['paper']['equity']==18 and response['stale']
    assert cli.get('/ready').status_code==503
