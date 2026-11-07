import json
from pathlib import Path
import pytest
from dotenv import dotenv_values
from trading.control.live_config import configure


def setup(tmp_path):
    env=tmp_path/'.env';env.write_text('BROKER_MODE=paper\nGROQ_API_KEY=private-value\nBROKER_MODE=paper\n')
    registry=tmp_path/'accounts.json'
    registry.write_text(json.dumps({'accounts':[dict(id='a',telegram_user_id=7,telegram_chat_id=7,mexc_key_env='A_KEY',mexc_secret_env='A_SECRET')]}))
    return env,registry


def test_live_config_preview_never_changes_env(tmp_path):
    env,registry=setup(tmp_path);before=env.read_bytes()
    result=configure(env,registry,'a')
    assert not result['applied'] and env.read_bytes()==before
    assert 'private-value' not in str(result)


def test_apply_has_private_backup_preserves_credentials_and_sets_only_one_account(tmp_path):
    env,registry=setup(tmp_path)
    result=configure(env,registry,'a',apply=True,ack_real_orders=True)
    values=dotenv_values(env)
    assert values['BROKER_MODE']=='live' and values['LIVE_TRADING_ENABLED']=='true'
    assert json.loads(values['LIVE_ALLOWED_ACCOUNTS'])==['a']
    assert values['GROQ_API_KEY']=='private-value' and 'private-value' not in str(result)
    assert env.stat().st_mode&0o777==0o600
    assert (tmp_path/result['backup']).stat().st_mode&0o777==0o600


def test_unknown_account_or_missing_ack_never_modifies_env(tmp_path):
    env,registry=setup(tmp_path);before=env.read_bytes()
    with pytest.raises(ValueError):configure(env,registry,'wrong',apply=True,ack_real_orders=True)
    with pytest.raises(ValueError):configure(env,registry,'a',apply=True)
    assert env.read_bytes()==before


def test_compose_worker_does_not_override_explicit_env_gates_but_api_stays_paper():
    from pathlib import Path
    text=Path('infrastructure/docker-compose.yml').read_text()
    api=text.split('  api:',1)[1].split('  worker:',1)[0]
    worker=text.split('  worker:',1)[1].split('  postgres:',1)[0]
    assert 'BROKER_MODE: paper' in api
    assert 'LIVE_TRADING_ENABLED:' not in worker
    assert 'BROKER_MODE:' not in worker
    assert 'env_file:' in worker and '../.env' in worker
